"""One listening session on the server: state machine, live loops, persistence, recordings, exhibits, chat.

This is the multi-user, browser-driven counterpart of desktop/backend/session.py and keeps the same behaviour,
state machine (live -> stopping -> finalizing -> review -> decided) and on-disk format. What differs:
  * audio arrives from the browser over a WebSocket (routers/audio.py) instead of from local devices,
  * screenshots, screen watching and recordings are captured by the browser and uploaded,
  * every model call runs with the owner's own token for the model they picked (analyzer.auth_scope),
  * there is no portal form-filling window; the form answers are copied or used with the browser helper.
A disk error must never stop the live transcript, so every write is guarded.
"""
import copy
import json
import os
import re
import shutil
import threading
import time
from collections import deque
from datetime import datetime

import analyzer
import reports
import speakers

import config  # the shim in server/backend/config.py (server settings under the names the shared analyzer expects)

from ..asr import engine as asr
from ..config import get_settings
from . import SHARED

SAMPLE_FOR = {"interview": "sample_transcript.txt"}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def human_dur(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _words(text):
    return re.findall(r"[a-z0-9']+", (text or "").lower())


class Session:
    def __init__(self, store, profile, subject="", extra="", context=None, sid=None, role="", speakers=False):
        self.store = store
        self.profile = copy.deepcopy(profile)      # the rubric is per session (built for the role, grows during the call)
        self.role = (role or "").strip()
        self.rubric_building = False
        self.rubric_note = ""
        self.speakers_on = bool(speakers)            # tell the people on the other side of the call apart (Speaker 1, 2, ...)
        self.speaker_names = {}                      # {"2": "Priya"}: names the user gave (string keys: they go through JSON)
        self.speaker_book = None                     # voices heard so far, see shared/core/speakers.py
        self.subject, self.extra = subject.strip(), extra
        self.context = list(context or [])
        self.state = "live"
        self.rev = 1
        self.lock = threading.RLock()
        now = datetime.now()
        self.date, self.time = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
        self.started = now_iso()
        self.started_wall = time.time()
        self.closed = ""
        self.consent = None
        self.dismissed = False
        self.sid = sid or store.new_sid(self.date, reports.slug(self.subject or self.profile["id"]))
        self.dir = store.sessions_dir / self.sid
        self.lines, self.vcount = [], 0
        self.notes = ""
        self.exhibits, self.chat, self.events, self.clips = [], [], [], []
        self.analysis, self.final, self.drafts, self.form_data = None, None, {}, None
        self.decision = ""
        self.result_date = ""
        self.feedback = {"state": ""}
        self.error = ""               # finalization error (shows Retry)
        self.analysis_error = ""
        self.active = 0.0             # seconds of non-paused time
        self.seg_start = time.time()
        self.paused, self.listening = False, True
        self.watch = {"on": False, "interval": 30, "kept": 0, "was_on": False, "status": ""}
        self.source = None            # label of the screen/window the browser is sharing {"label": ...}
        self.partials = {}
        self.force = False
        self.last_analyze, self.lines_at_last = 0.0, 0
        self.analyzing = False
        self.chat_busy = False
        self.form_busy = False
        self.regenerating = False
        self.stop_evt = threading.Event()
        # browser audio (filled by routers/audio.py)
        self.audio_clients = 0
        self.levels = {"you": 0.0, "other": 0.0}
        self.sources = []             # [{"role": "you"|"other", "name": ...}]
        self.recent_other = deque(maxlen=12)
        self.chunk_seq = {}           # (clip n, kind) -> next expected piece number of a recording upload

    # ------------------------------------------------------------ identity / paths
    @property
    def name(self):
        return self.subject or "Untitled"

    @property
    def rec_dir(self):
        return self.store.rec_root / self.sid

    def elapsed(self):
        if self.state in ("live",) and not self.paused:
            return self.active + (time.time() - self.seg_start)
        return self.active

    def llm(self):
        """Run analyzer calls with the model this user picked (Claude or Copilot) and their own token for it."""
        provider, token = self.store.auth()
        return analyzer.auth_scope(token, provider)

    def event(self, text):
        with self.lock:
            self.events.append({"ts": datetime.now().strftime("%H:%M:%S"), "text": text})
        self.touch()

    def touch(self, save=True):
        with self.lock:
            self.rev += 1
        if save:
            self.save()

    # ------------------------------------------------------------ transcript
    def add_line(self, label, text, ts, marker=False, speaker=None):
        with self.lock:
            self.vcount += 1
            line = {"id": len(self.lines), "label": label, "text": text, "ts": round(ts, 1),
                    "marker": marker, "v": self.vcount, "checked": marker, "speaker": speaker}
            self.lines.append(line)
            rec = {k: line[k] for k in ("id", "label", "text", "ts", "marker")}
            if speaker is not None:
                rec["speaker"] = speaker
        self._append_jsonl(rec)
        return line["id"]

    def _append_jsonl(self, rec):
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.dir / "transcript.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass  # never stop the live transcript because of a disk error

    def override_line(self, line_id, text, kind):
        with self.lock:
            if not (0 <= line_id < len(self.lines)) or self.lines[line_id]["text"] == text:
                return
            self.vcount += 1
            ln = self.lines[line_id]
            ln["text"], ln["v"], ln[kind] = text, self.vcount, True
        self._append_jsonl({"override": line_id, "text": text, "kind": kind})

    def is_echo(self, text):
        """Speaker sound leaking into the microphone: the same words were just said by the other side."""
        mine = set(_words(text))
        if len(mine) < 3:
            return False
        now = time.time()
        return any(now - t < 15 and words and len(mine & words) / len(mine) >= 0.7 for t, words in self.recent_other)

    def on_utterance(self, label, text, ts, speaker=None):
        if self.state != "live":
            return None
        if label == "other":
            self.recent_other.append((time.time(), set(_words(text))))
        elif self.sources and any(s["role"] == "other" for s in self.sources) and self.is_echo(text):
            return None
        return self.add_line(label, text, ts, speaker=speaker)

    def on_partial(self, key, label, text):
        with self.lock:
            if text is None:
                self.partials.pop(key, None)
            else:
                self.partials[key] = {"label": label, "text": text}

    def marker(self, text):
        self.add_line("", text, time.time() - self.started_wall, marker=True)

    # ------------------------------------------------------------ start
    def start(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        self.event("Session started")
        if self.role and self.profile.get("rubric"):
            self.rubric_building = True
            threading.Thread(target=self._build_rubric, daemon=True, name="session-rubric").start()
        if config.MOCK_ANALYZER:                                   # demo: replay a sample call like the desktop demo mode
            self.sources = [{"role": "other", "name": "Demo (sample transcript)"}]
            self.audio_clients = 1
            threading.Thread(target=self._demo_feed, daemon=True, name="demo-feed").start()
        threading.Thread(target=self._loop, daemon=True, name="session-loop").start()
        self.touch()

    def _demo_feed(self):
        path = SHARED / "samples" / SAMPLE_FOR.get(self.profile["id"], "sample_meeting.txt")
        try:
            rows = [r for r in path.read_text(encoding="utf-8").splitlines() if ":" in r]
        except OSError:
            return
        for row in rows:
            if self.stop_evt.wait(get_settings().demo_delay):
                return
            while self.paused or not self.listening:
                if self.stop_evt.wait(0.3):
                    return
            who, text = row.split(":", 1)
            you = who.strip().lower() in ("interviewer", "you", "me")
            self.on_utterance("you" if you else "other", text.strip(), time.time() - self.started_wall)

    def _build_rubric(self):
        """Topic list for the role the interviewer is hiring for (falls back to the profile's default list)."""
        try:
            with self.llm():
                res = analyzer.generate_rubric(self.profile, self.role, self.extra, "\n".join(self.context))
            with self.lock:
                self.profile["rubric"]["role"] = res["role"]
                self.profile["rubric"]["areas"] = res["areas"]
                self.profile["vocabulary"] = ", ".join([res["role"]] + [x["name"] for x in res["areas"]])
            self.event(f"Topic list built for {res['role']} ({len(res['areas'])} topics)")
        except Exception as exc:
            self.rubric_note = f"Could not build a topic list for this role ({exc}); using the default list."
            self.event(self.rubric_note)
        finally:
            self.rubric_building = False
            self.touch()

    # ------------------------------------------------------------ live loops
    def _speaker_of(self, role, audio):
        """Speaker number for an utterance from the other side of the call (None: not tracked)."""
        if role != "other" or not self.speakers_on:
            return None
        try:
            if self.speaker_book is None:
                cfg = get_settings()
                speakers.load(cfg.speaker_model or (cfg.data_dir / "models" / speakers.MODEL_FILE), cfg.asr_allow_download)
                self.speaker_book = speakers.SpeakerBook(cfg.speaker_threshold)
            return self.speaker_book.assign(audio)
        except Exception as exc:                                  # never lose the words because the speaker model is missing
            self.speakers_on = False
            self.event(f"Speakers are not told apart: {exc}")
            return None

    def speaker_info(self):
        """Who spoke on the other side: {"names": {...}, "multi": True when two or more different people have spoken}."""
        with self.lock:
            ids = {ln.get("speaker") for ln in self.lines if ln.get("label") == "other" and ln.get("speaker") is not None}
            return {"names": dict(self.speaker_names), "multi": len(ids) >= 2}

    def set_speaker_name(self, speaker_id, name):
        name = (name or "").strip()[:40]
        with self.lock:
            if name:
                self.speaker_names[str(int(speaker_id))] = name
            else:
                self.speaker_names.pop(str(int(speaker_id)), None)
        self.touch()

    def ctx(self, previous=True):
        with self.lock:
            return {"profile": self.profile, "subject": self.subject, "extra": self.extra, "speakers": self.speaker_info(),
                    "lines": [dict(ln) for ln in self.lines], "notes": self.notes, "context": list(self.context),
                    "exhibits": [dict(e) for e in self.exhibits],
                    "previous": self.analysis if previous else None}

    def _loop(self):
        last_correct = time.time()
        while not self.stop_evt.wait(1.0):
            if self.state != "live" or self.paused or self.rubric_building:
                continue
            self._check_disk()
            n_lines = len([ln for ln in self.lines if not ln.get("marker")])
            new = n_lines - self.lines_at_last
            gap = time.time() - self.last_analyze
            due = (gap >= config.ANALYZE_INTERVAL and new > 0) or (new >= config.ANALYZE_NEW_LINES and gap >= config.ANALYZE_MIN_GAP)
            if (self.force or due) and n_lines and not self.analyzing:
                self.force = False
                self.run_live_analysis()
            if time.time() - last_correct > 25 and not config.MOCK_ANALYZER and get_settings().corrections:
                last_correct = time.time()
                self.run_corrections()

    def run_live_analysis(self):
        self.analyzing = True
        try:
            ctx = self.ctx()
            n = len([ln for ln in ctx["lines"] if not ln.get("marker")])
            with self.llm():
                res = analyzer.live_analysis(ctx)
            with self.lock:
                self.analysis, self.analysis_error = res, ""
                self.lines_at_last, self.last_analyze = n, time.time()
        except analyzer.AnalyzerError as exc:
            self.analysis_error = str(exc)
            self.last_analyze = time.time()
        except Exception as exc:
            self.analysis_error = f"Analysis failed: {exc}"
            self.last_analyze = time.time()
        finally:
            self.analyzing = False
            self.touch()

    def run_corrections(self):
        with self.lock:
            batch = [ln for ln in self.lines if not ln["checked"] and not ln["marker"]][-15:]
        if not batch:
            return
        context = "; ".join([self.subject] + self.context)[:1500]
        try:
            with self.llm():
                fixes = analyzer.correct_lines(self.profile, [{"id": ln["id"], "label": ln["label"], "text": ln["text"]} for ln in batch], context)
        except Exception:
            return
        for fx in fixes:
            if isinstance(fx.get("id"), int):
                self.override_line(fx["id"], fx.get("text", ""), "fixed")
        with self.lock:
            for ln in batch:
                ln["checked"] = True

    def _check_disk(self):
        if not any(c["state"] in ("recording", "paused") for c in self.clips):
            return
        try:
            self.rec_root_ok()
        except OSError:
            return
        free = shutil.disk_usage(self.store.rec_root).free / 1048576
        if free < config.MIN_FREE_MB:
            self.event(f"Recording stopped automatically: only {free:.0f} MB free disk left on the server")
            self.record_stop()

    def rec_root_ok(self):
        self.store.rec_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ pause / listening
    def set_paused(self, on):
        with self.lock:
            if self.state != "live" or on == self.paused:
                return
            self.paused = on
            if on:
                self.active += time.time() - self.seg_start
                self.watch["was_on"], self.watch["on"] = self.watch["on"], False
            else:
                self.seg_start = time.time()
                self.watch["on"] = self.watch["was_on"]
        self.marker("Session paused" if on else "Session resumed")
        self._clip_modes()
        self.event("Session paused" if on else "Session resumed")

    def set_listening(self, on):
        with self.lock:
            if self.state != "live" or on == self.listening:
                return
            self.listening = on
        self.marker("Listening resumed" if on else "Listening paused")
        self.event("Listening on" if on else "Listening off")

    # ------------------------------------------------------------ end / finalize
    def end(self):
        with self.lock:
            if self.state != "live":
                return
            if self.paused:
                self.paused = False
            else:
                self.active += time.time() - self.seg_start
            self.state = "stopping"
            self.watch["on"] = False
        self.touch()
        threading.Thread(target=self._stop_and_finalize, daemon=True, name="finalize").start()

    def _stop_and_finalize(self):
        time.sleep(1.0)  # let the last lines arrive
        deadline = time.time() + 120               # a recording the browser is still uploading
        while any(c["state"] == "saving" for c in self.clips) and time.time() < deadline:
            time.sleep(0.5)
        if any(c["state"] in ("recording", "paused") for c in self.clips):
            self.record_stop()
        self.stop_evt.set()
        self.closed = now_iso()
        self.event("Session ended")
        self._finalize()

    def _finalize(self):
        with self.lock:
            self.state, self.error = "finalizing", ""
        self.touch()
        deadline = time.time() + 90
        while (self.rubric_building or any(e["status"] == "analyzing" for e in self.exhibits)) and time.time() < deadline:
            time.sleep(0.5)
        try:
            n = len([ln for ln in self.lines if not ln.get("marker")])
            if n == 0 and not self.exhibits:
                out = self._empty_final()
            else:
                ctx = self.ctx(previous=False)
                with self.llm():
                    out = analyzer.final_analysis(ctx)
            self._set_final(out)
        except analyzer.AnalyzerError as exc:
            self.error = str(exc)
        except Exception as exc:
            self.error = f"Final analysis failed: {exc}"
        with self.lock:
            self.state = "review"
        self.touch()

    def _empty_final(self):
        schema = analyzer.final_schema(self.profile)
        out = {}
        for k, v in schema["properties"].items():
            t = v.get("type")
            out[k] = [] if t == "array" else ("" if t == "string" else None)
        out["summary"] = "No conversation was captured in this session."
        return out

    def _set_final(self, out):
        with self.lock:
            self.final = out
            self.form_data = out.get("form_answers")
            self.drafts = {d["id"]: out.get(d["id"], "") for d in self.profile.get("drafts", [])}
        self.write_outputs()

    def regenerate(self):
        """Rewrite the summary/analysis (before saving) including later screenshots and context."""
        with self.lock:
            if self.state != "review" or self.regenerating:
                return False
            self.regenerating = True
        threading.Thread(target=self._regen, daemon=True, name="regen").start()
        return True

    def _regen(self):
        with self.lock:
            self.state = "finalizing"
        self.touch(save=False)
        self._finalize()
        self.regenerating = False

    # ------------------------------------------------------------ decisions and files
    def decide(self, decision_id):
        dec = next((d for d in self.profile.get("decisions", []) if d["id"] == decision_id), None)
        if not dec or not self.final:
            return False
        with self.lock:
            if not self.result_date:
                self.result_date = self.date
            self.decision = decision_id
            self.state = "decided"
            form = self.profile.get("form")
            if self.form_data and form:
                cat = form["category_for"].get(decision_id)
                if cat:
                    self.form_data.setdefault("overall", {})["category"] = cat
            if form and self.feedback["state"] == "":
                self.feedback = {"state": "pending"}
        self.event(f"Decision: {dec['short']}")
        self.write_outputs()
        self.touch()
        return True

    def finish(self):
        """Profiles without decisions: save all drafts and mark the session as saved."""
        if not self.final or self.profile.get("decisions"):
            return False
        with self.lock:
            self.decision, self.state = "saved", "decided"
            self.result_date = self.result_date or self.date
        self.write_outputs()
        self.touch()
        return True

    def _draft_files(self):
        keep = {}
        decs = {d["id"]: d for d in self.profile.get("decisions", [])}
        chosen = decs.get(self.decision, {}).get("draft")
        for d in self.profile.get("drafts", []):
            if d.get("always") or d["id"] == chosen or (not decs and self.state == "decided"):
                keep[d["id"]] = d
        return keep

    def write_outputs(self):
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            if not self.final:
                return
            _write(self.dir / "evaluation.json", reports.to_json(self.final))
            keep = self._draft_files() if self.state == "decided" else {
                d["id"]: d for d in self.profile.get("drafts", []) if d.get("always")}
            for d in self.profile.get("drafts", []):
                path = self.dir / d["filename"]
                if d["id"] in keep:
                    text = self.drafts.get(d["id"], "")
                    if d["id"] == "topic_feedback" and not text:
                        text = reports.topics_md(self.final.get("topics"))
                    _write(path, text)
                elif path.exists():
                    path.unlink()
            if self.form_data:
                _write(self.dir / "evaluation-form.md", reports.form_md(self.form_data, self.profile.get("form")))
            _write(self.dir / "report.md", reports.report_md(
                self.meta(), self.profile, self.final, self.exhibits, self.decision, self.clips_public()))
            if self.state == "decided":
                _write(self.dir / "result.json", reports.to_json({
                    "decision": self.decision, "date": self.result_date, "subject": self.subject,
                    "score": self.final.get("weighted_score"), "profile": self.profile["id"]}))
            elif (self.dir / "result.json").exists():
                (self.dir / "result.json").unlink()
            self.store.update_day_summary(self.date)
        except OSError as exc:
            self.event(f"Could not write files: {exc}")

    def set_feedback(self, state):
        if state not in ("pending", "filled", "submitted") or self.state != "decided" or not self.profile.get("form"):
            return False
        self.feedback = {"state": state, "updated": now_iso()}
        self.touch()
        self.store.update_day_summary(self.date)
        return True

    # ------------------------------------------------------------ persistence
    def meta(self):
        return {"id": self.sid, "profile": self.profile["id"], "subject": self.subject, "extra": self.extra,
                "started": self.started, "closed": self.closed, "consent": self.consent,
                "date": self.date, "time": self.time, "active": round(self.active, 1),
                "dismissed": self.dismissed, "decision": self.decision, "result_date": self.result_date,
                "state": self.state, "error": self.error, "watch_interval": self.watch["interval"],
                "role": self.role, "rubric": self.profile.get("rubric"), "rubric_note": self.rubric_note,
                "speakers_on": self.speakers_on, "speaker_names": self.speaker_names}

    def save(self):
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            _write(self.dir / "session.json", reports.to_json(self.meta()))
            _write(self.dir / "notes.txt", self.notes)
            _write(self.dir / "context.json", reports.to_json(self.context))
            _write(self.dir / "chat.json", reports.to_json(self.chat))
            _write(self.dir / "events.json", reports.to_json(self.events))
            _write(self.dir / "clips.json", reports.to_json(self.clips))
            _write(self.dir / "feedback.json", reports.to_json(self.feedback))
            _write(self.dir / "exhibits.json", reports.to_json(self.exhibits))
            if self.analysis:
                _write(self.dir / "analysis.json", reports.to_json(self.analysis))
            if self.drafts:
                _write(self.dir / "drafts.json", reports.to_json(self.drafts))
            rows = []
            spk = self.speaker_info()
            for ln in self.lines:
                if ln["marker"]:
                    rows.append(f"--- {ln['text']} ---")
                else:
                    who = self.profile["you_label"] if ln["label"] == "you" else self.profile["other_label"]
                    if ln["label"] == "other" and spk["multi"] and ln.get("speaker") is not None:
                        who += f" ({spk['names'].get(str(ln['speaker'])) or 'Speaker ' + str(ln['speaker'])})"
                    rows.append(f"[{analyzer.fmt_ts(ln['ts'])}] {who}: {ln['text']}")
            _write(self.dir / "transcript.txt", "\n".join(rows))
        except OSError:
            pass

    @classmethod
    def load(cls, store, path, profiles):
        meta = json.loads((path / "session.json").read_text(encoding="utf-8"))
        profile = profiles.get(meta.get("profile"))
        if not profile:
            return None

        def rj(name, default):
            try:
                return json.loads((path / name).read_text(encoding="utf-8"))
            except Exception:
                return default

        s = cls(store, profile, meta.get("subject", ""), meta.get("extra", ""), rj("context.json", []), sid=meta["id"])
        s.dir = path
        s.role, s.rubric_note = meta.get("role", ""), meta.get("rubric_note", "")
        s.speakers_on, s.speaker_names = meta.get("speakers_on", False), meta.get("speaker_names", {})
        if meta.get("rubric") and s.profile.get("rubric"):
            s.profile["rubric"] = meta["rubric"]
        s.date, s.time = meta.get("date", ""), meta.get("time", "")
        s.started, s.closed = meta.get("started", ""), meta.get("closed", "")
        s.consent, s.dismissed = meta.get("consent"), meta.get("dismissed", False)
        s.active, s.decision, s.result_date = meta.get("active", 0), meta.get("decision", ""), meta.get("result_date", "")
        s.watch["interval"] = meta.get("watch_interval", 30)
        s.notes = (path / "notes.txt").read_text(encoding="utf-8") if (path / "notes.txt").exists() else ""
        s.chat, s.events, s.clips = rj("chat.json", []), rj("events.json", []), rj("clips.json", [])
        s.feedback = rj("feedback.json", {"state": ""})
        s.exhibits = rj("exhibits.json", [])
        s.analysis = rj("analysis.json", None)
        s.drafts = rj("drafts.json", {})
        s.final = rj("evaluation.json", None)
        if s.final and "ief" in s.final and "form_answers" not in s.final:        # sessions saved by earlier versions
            s.final["form_answers"] = s.final.pop("ief")
        old_file = path / ("ief" + "-form.md")
        if old_file.exists() and not (path / "evaluation-form.md").exists():
            try:
                old_file.rename(path / "evaluation-form.md")
            except OSError:
                pass
        s.form_data = (s.final or {}).get("form_answers")
        try:                                                    # transcript with overrides
            for raw in (path / "transcript.jsonl").read_text(encoding="utf-8").splitlines():
                rec = json.loads(raw)
                if "override" in rec:
                    if 0 <= rec["override"] < len(s.lines):
                        s.lines[rec["override"]]["text"] = rec["text"]
                else:
                    s.vcount += 1
                    s.lines.append({"speaker": None, **rec, "v": s.vcount, "checked": True})
        except Exception:
            pass
        for c in s.clips:                                       # interrupted work becomes something retryable
            if c["state"] in ("recording", "paused", "saving"):
                c["state"] = "saved" if any((s.rec_dir / c[k]).exists() for k in ("audio_file", "video_file") if c.get(k)) else "error"
                c["error"] = c.get("error") or ("" if c["state"] == "saved" else "The server restarted before the upload finished.")
        for e in s.exhibits:
            if e["status"] == "analyzing":
                e["status"], e["error"] = "error", "Interrupted. Press Retry."
        s.state = "decided" if (path / "result.json").exists() and s.final else "review"
        s.stop_evt.set()
        if s.state == "review" and not s.final:
            s.error = s.error or "No summary yet. Press Retry."
        s.paused = False
        return s

    # ------------------------------------------------------------ notes / context
    def set_notes(self, text):
        self.notes = text
        self.touch()

    def add_context(self, text):
        text = text.strip()
        if text and text not in self.context:
            self.context.append(text)
            self.touch()

    def remove_context(self, idx):
        if 0 <= idx < len(self.context):
            self.context.pop(idx)
            self.touch()

    # ------------------------------------------------------------ exhibits
    def add_exhibit(self, png, question="", note="", auto=False):
        with self.lock:
            n = len(self.exhibits) + 1
            ex = {"n": n, "status": "analyzing", "question": question, "note": note, "auto": auto,
                  "ts": round(time.time() - self.started_wall, 1), "file": f"exhibits/{n:02d}.png",
                  "analysis": {}, "error": ""}
            self.exhibits.append(ex)
            if auto:
                self.watch["kept"] += 1
        try:
            (self.dir / "exhibits").mkdir(parents=True, exist_ok=True)
            (self.dir / ex["file"]).write_bytes(png)
        except OSError:
            pass
        self.touch()
        threading.Thread(target=self._analyze_exhibit, args=(ex,), daemon=True, name="exhibit").start()
        return ex

    def _analyze_exhibit(self, ex):
        try:
            png = (self.dir / ex["file"]).read_bytes()
            with self.llm():
                res = analyzer.exhibit_analysis(self.ctx(previous=False), png, ex["question"], ex["note"])
            with self.lock:
                if ex["auto"] and res.get("relevant") is False and get_settings().watch_drop_irrelevant:
                    self.exhibits.remove(ex)  # only when WATCH_DROP_IRRELEVANT is on: by default everything that was watched stays
                    self.watch["kept"] = max(0, self.watch["kept"] - 1)
                    self._renumber_exhibits()
                else:
                    ex["analysis"], ex["status"], ex["error"] = res, "done", ""
        except Exception as exc:
            with self.lock:
                ex["status"], ex["error"] = "error", str(exc)
        self.touch()

    def _renumber_exhibits(self):
        """Keep numbering contiguous after a drop; the files are renamed to match."""
        for i, e in enumerate(self.exhibits, 1):
            new = f"exhibits/{i:02d}.png"
            if e["file"] != new:
                try:
                    os.replace(self.dir / e["file"], self.dir / new)
                except OSError:
                    pass
                e["file"] = new
            e["n"] = i
        extra = self.dir / "exhibits" / f"{len(self.exhibits) + 1:02d}.png"
        extra.unlink(missing_ok=True)

    def exhibit_retry(self, n):
        ex = next((e for e in self.exhibits if e["n"] == n), None)
        if ex and ex["status"] == "error":
            ex["status"], ex["error"] = "analyzing", ""
            self.touch()
            threading.Thread(target=self._analyze_exhibit, args=(ex,), daemon=True).start()
            return True
        return False

    def exhibit_delete(self, n):
        with self.lock:
            ex = next((e for e in self.exhibits if e["n"] == n), None)
            if not ex:
                return False
            self.exhibits.remove(ex)
            (self.dir / ex["file"]).unlink(missing_ok=True)
            self._renumber_exhibits()
        self.touch()
        return True

    # ------------------------------------------------------------ screen watching (the browser grabs the frames)
    def set_watch(self, on, source=None):
        with self.lock:
            if source is not None:
                self.source = source
            self.watch["on"] = bool(on) and self.state == "live" and not self.paused
            self.watch["status"] = ""
        self.event("Screen watching on" if on else "Screen watching off")

    # ------------------------------------------------------------ recording (clips; the browser records and uploads)
    def _clip(self, n):
        return next((c for c in self.clips if c["n"] == n), None)

    def record_start(self, audio, video, consent=False):
        """Returns {"ok":True,"n":..} | {"need_consent":True} | {"error":msg}."""
        if self.state != "live":
            return {"error": "Recording only works inside a live session."}
        if any(c["state"] in ("recording", "paused", "saving") for c in self.clips):
            return {"error": "Already recording."}
        if not (audio or video):
            return {"error": "Choose Audio and/or Screen video in Options first."}
        if config.REQUIRE_CONSENT and not self.consent and not consent:
            return {"need_consent": True}
        if consent and not self.consent:
            self.consent = {"confirmed": True, "time": now_iso()}
        try:
            self.rec_dir.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(self.rec_dir).free / 1048576
        except OSError as exc:
            return {"error": f"Cannot use the recordings folder on the server: {exc}"}
        if free < config.MIN_FREE_MB:
            return {"error": f"Only {free:.0f} MB free on the server disk (limit {config.MIN_FREE_MB} MB)."}
        with self.lock:
            n = max([c["n"] for c in self.clips] + [0]) + 1
            base = f"{self.sid}_rec{n:02d}"
            clip = {"n": n, "name": f"Recording {n}", "state": "recording", "audio": bool(audio), "video": bool(video),
                    "started": now_iso(), "start_ts": round(time.time() - self.started_wall, 1), "duration": 0,
                    "size": 0, "audio_file": f"{base}.audio.webm" if audio else "", "video_file": f"{base}.video.webm" if video else "",
                    "source": (self.source or {}).get("label", "Shared screen") if video else "", "error": "", "got": []}
            self.clips.append(clip)
        self._clip_modes()
        self.event(f"Recording {n} started ({'audio' if audio else ''}{' + ' if audio and video else ''}{'video' if video else ''})")
        return {"ok": True, "n": n}

    def _clip_modes(self):
        """Pause/resume the active clip together with the session (the browser pauses its recorders)."""
        for c in self.clips:
            if c["state"] in ("recording", "paused"):
                c["state"] = "paused" if self.paused else "recording"
        self.touch(save=False)

    def record_stop(self):
        """The browser stops recording and then uploads the file(s); the clip is 'saving' until they arrive."""
        clip = next((c for c in self.clips if c["state"] in ("recording", "paused")), None)
        if not clip:
            return False
        clip["state"] = "saving"
        clip["duration"] = round(time.time() - self.started_wall - clip["start_ts"], 1)
        self.touch(save=False)
        threading.Thread(target=self._upload_watchdog, args=(clip,), daemon=True, name="clip-watchdog").start()
        return True

    def _upload_watchdog(self, clip):
        for _ in range(240):                                    # up to 2 minutes for the browser to send its files
            if clip["state"] != "saving":
                return
            time.sleep(0.5)
        if clip["state"] == "saving":
            clip["error"] = "The recording was not uploaded in time."
            self._finish_clip(clip)

    def clip_upload_path(self, n, kind):
        c = self._clip(n)
        if not c or c["state"] not in ("saving", "recording", "paused") or kind not in ("audio", "video"):
            return None
        name = c["audio_file"] if kind == "audio" else c["video_file"]
        if not name:
            return None
        self.rec_dir.mkdir(parents=True, exist_ok=True)
        return self.rec_dir / (name + ".part")

    def clip_uploaded(self, n, kind, duration=None):
        """Called after an upload finished: promote the .part file; when every file arrived the clip is saved."""
        c = self._clip(n)
        name = c and (c["audio_file"] if kind == "audio" else c["video_file"])
        part = self.rec_dir / (name + ".part") if name else None
        if not c or not part or not part.exists():
            return False
        part.replace(self.rec_dir / name)
        with self.lock:
            c["got"] = sorted(set(c.get("got", [])) | {kind})
            if duration:
                c["duration"] = round(float(duration), 1)
            want = {k for k in ("audio", "video") if c[k]}
        if want <= set(c["got"]):
            self._finish_clip(c)
        return True

    def _finish_clip(self, clip):
        for k in ("audio_file", "video_file"):                  # the browser vanished before its last piece: keep what arrived
            part = self.rec_dir / (clip[k] + ".part") if clip[k] else None
            if part and part.exists() and not (self.rec_dir / clip[k]).exists():
                part.replace(self.rec_dir / clip[k])
        clip["state"] = "saved" if any((self.rec_dir / clip[k]).exists() for k in ("audio_file", "video_file") if clip[k]) else "error"
        if clip["state"] == "error" and not clip["error"]:
            clip["error"] = "No recording data was received."
        clip["ended"] = now_iso()
        clip["size"] = sum((self.rec_dir / clip[k]).stat().st_size for k in ("audio_file", "video_file")
                           if clip[k] and (self.rec_dir / clip[k]).exists())
        self.event(f"Recording {clip['n']} saved ({human_size(clip['size'])})")
        self.touch()

    def clips_public(self):
        out = []
        for c in self.clips:
            d = {k: v for k, v in c.items() if k != "got"}
            d["duration_text"] = human_dur(c.get("duration", 0))
            d["size_text"] = human_size(c.get("size", 0))
            d["path"] = ""
            d["audio_url"] = f"/api/clip/{self.sid}/{c['n']}/audio" if c["audio"] and c["state"] == "saved" and c["audio_file"] else ""
            d["video_url"] = f"/api/clip/{self.sid}/{c['n']}/video" if c["video"] and c["state"] == "saved" and c["video_file"] else ""
            if c["state"] in ("recording", "paused"):
                d["duration_text"] = human_dur(time.time() - self.started_wall - c["start_ts"])
            out.append(d)
        return out

    def clip_file(self, n, kind):
        c = self._clip(n)
        if not c or c["state"] != "saved":
            return None
        name = c["audio_file"] if kind == "audio" else c["video_file"]
        path = self.rec_dir / name if name else None
        return path if path and path.exists() else None

    def delete_clip(self, n=None):
        targets = [c for c in self.clips if (n is None or c["n"] == n) and c["state"] not in ("recording", "paused", "saving")]
        for c in targets:
            for key in ("audio_file", "video_file"):
                if c[key]:
                    (self.rec_dir / c[key]).unlink(missing_ok=True)
            self.clips.remove(c)
        self.event("Recordings deleted")
        return len(targets)

    # ------------------------------------------------------------ chat
    def form_state_text(self):
        return reports.form_md(self.form_data, self.profile.get("form")) if self.form_data else ""

    def chat_add(self, role, text, **extra):
        with self.lock:
            msg = {"id": len(self.chat) + 1, "role": role, "text": text, "ts": datetime.now().strftime("%H:%M"),
                   "unread": role == "assistant", **extra}
            self.chat.append(msg)
        self.touch()
        return msg

    def chat_context(self, text):
        self.chat_add("user", f"Add context: {text}")
        self.add_context(text)
        self.chat_add("assistant", "Saved. I will use it in every later analysis.", unread=False)

    def chat_ask(self, text):
        self.chat_add("user", text)
        self.chat_busy = True
        threading.Thread(target=self._chat_job, args=(text,), daemon=True, name="chat").start()

    def _chat_job(self, text):
        hist = [{"role": m["role"], "text": m["text"]} for m in self.chat[:-1] if m.get("kind") is None][-12:]
        try:
            ctx = self.ctx()
            ctx["previous"] = self.final or self.analysis
            with self.llm():
                res = analyzer.chat_answer(ctx, hist, text, self.form_state_text())
        except Exception as exc:
            self.chat_busy = False
            self.chat_add("assistant", f"Sorry, that failed: {exc}", error=True)
            return
        self.chat_busy = False
        if res.get("remember", "").strip():
            self.add_context(res["remember"])
        self.chat_add("assistant", res.get("reply", ""))
        self._chat_action(res)

    def _chat_action(self, res):
        action = res.get("action", "none")
        if action in ("fill_form", "update_answer") and self.profile.get("form"):
            self.chat_add("assistant", "On the web edition the portal is filled with the browser helper: open the evaluation form "
                          "below, copy the answers (or all fields) and use the Fill helper on the portal page. Nothing is submitted for you.")
        elif action == "stop_fill":
            self.chat_add("assistant", "There is no fill running on the web edition.")
        elif action == "list_pending":
            items = [{"sid": s.sid, "name": s.name} for s in self.store.sessions.values()
                     if s.state == "decided" and s.profile.get("form") and s.feedback.get("state") != "submitted"]
            self.chat_add("assistant", "Interviews still needing feedback:" if items else "Nothing is waiting for feedback.",
                          kind="pending_list", items=items)

    def chat_dismiss(self, msg_id=None):
        with self.lock:
            for m in self.chat:
                if msg_id is None:
                    m["unread"] = False
                elif m["id"] == msg_id:
                    m["status"], m["unread"] = "dismissed", False
        self.touch()

    # ------------------------------------------------------------ evaluation form
    def write_form_answers(self):
        """For interviews written up before the form existed: write only the form answers."""
        if not self.profile.get("form") or not self.final:
            return False
        self.form_busy = True
        self.touch(save=False)
        threading.Thread(target=self._form_job, daemon=True, name="form-answers").start()
        return True

    def _form_job(self):
        try:
            with self.llm():
                form_data = analyzer.form_answers(self.ctx(previous=False), self.final)
            with self.lock:
                self.form_data = form_data
                self.final["form_answers"] = form_data
                cat = self.profile["form"]["category_for"].get(self.decision)
                if cat:
                    form_data.setdefault("overall", {})["category"] = cat
            self.write_outputs()
        except Exception as exc:
            self.event(f"Form answers failed: {exc}")
        self.form_busy = False
        self.touch()

    # ------------------------------------------------------------ public views
    def list_item(self):
        rec = ""
        if any(c["state"] == "recording" for c in self.clips):
            rec = "rec"
        elif any(c["state"] == "paused" for c in self.clips):
            rec = "paused"
        return {"id": self.sid, "name": self.name, "type": self.profile["id"], "type_name": self.profile["name"],
                "state": self.state, "date": self.date, "time": self.time, "rec": rec, "paused": self.paused,
                "listening": self.listening, "dec": self.decision,
                "fb": self.feedback.get("state", "") if self.profile.get("form") and self.state == "decided" else "",
                "err": bool(self.error), "needs_summary": self.state == "review" and not self.final,
                "dismissed": self.dismissed, "has_form": bool(self.profile.get("form"))}

    def exhibits_public(self):
        out = []
        for e in self.exhibits:
            d = dict(e)
            d["url"] = f"/api/exhibit_img/{self.sid}/{e['n']}"
            out.append(d)
        return out

    def live_block(self, since, first):
        with self.lock:
            lines = [ln for ln in self.lines if ln["v"] > since]
            parts = list(self.partials.values())
        live = self.state == "live"
        err = asr.model_error if asr.status == "error" else ""
        cap_info = {"levels": dict(self.levels) if live else {"you": 0, "other": 0},
                    "sources": list(self.sources), "error": err, "errors": [],
                    "models": "demo" if config.MOCK_ANALYZER else ("loading" if asr.status == "loading" else asr.status),
                    "connected": self.audio_clients > 0}
        active = next((c for c in self.clips if c["state"] in ("recording", "paused")), None)
        rec_dur = round(time.time() - self.started_wall - active["start_ts"], 1) if active else 0
        return {"rec_dur": rec_dur, "lines": lines, "partials": parts, "max_v": self.vcount, "elapsed": round(self.elapsed(), 1),
                "state": self.state, "paused": self.paused, "listening": self.listening, "capture": cap_info,
                "watch": dict(self.watch), "source": self.source, "analyzing": self.analyzing,
                "analysis_error": self.analysis_error, "chat_busy": self.chat_busy, "form_busy": self.form_busy,
                "fill": None, "rec_active": active is not None, "rec_audio_only_paused": (not self.listening) and active is not None}

    def detail_block(self):
        form = self.profile.get("form")
        return {"id": self.sid, "name": self.name, "subject": self.subject, "extra": self.extra, "profile": self.profile["id"],
                "state": self.state, "date": self.date, "time": self.time, "started": self.started,
                "analysis": self.analysis, "final": self.final, "drafts": self.drafts, "decision": self.decision,
                "error": self.error, "context": self.context, "exhibits": self.exhibits_public(),
                "clips": self.clips_public(), "chat": self.chat, "feedback": self.feedback,
                "form_data": self.form_data, "form_fields": reports.form_fields(self.form_data, form), "form_text": reports.form_md(self.form_data, form),
                "consent": self.consent, "files": sorted(p.name for p in self.dir.iterdir()) if self.dir.exists() else [],
                "draft_files": {d["id"]: d["filename"] for d in self.profile.get("drafts", [])},
                "score": self._score_info(), "rubric": self.rubric_public(),
                "speakers": {"on": self.speakers_on, "names": dict(self.speaker_names)}}

    def rubric_public(self):
        r = self.profile.get("rubric")
        if not r:
            return None
        with self.lock:
            areas = [{k: a.get(k) for k in ("id", "name", "weight", "hint", "dynamic", "off_role")} for a in r["areas"]]
        return {"role": r.get("role", ""), "areas": areas, "building": self.rubric_building, "note": self.rubric_note,
                "custom": bool(self.role)}

    def _score_info(self):
        if not self.profile.get("rubric") or not self.final:
            return None
        score, n, share = analyzer.weighted_score(self.profile, self.final.get("areas"))
        return {"score": score, "scored": n, "total": len(self.profile["rubric"]["areas"]), "share": round(share, 2)}

    def zip_bytes(self, with_recordings=False):
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in self.dir.rglob("*"):
                if p.is_file():
                    z.write(p, f"{self.sid}/{p.relative_to(self.dir).as_posix()}")
            if with_recordings and self.rec_dir.exists():
                for p in self.rec_dir.iterdir():
                    if p.is_file() and not p.name.endswith(".part"):
                        z.write(p, f"{self.sid}/recordings/{p.name}")
        buf.seek(0)
        return buf

    def discard(self, delete_files):
        with self.lock:
            if self.state == "live":
                self.stop_evt.set()
                if any(c["state"] in ("recording", "paused") for c in self.clips):
                    self.record_stop()
            self.dismissed = True
        if delete_files:
            shutil.rmtree(self.dir, ignore_errors=True)
            shutil.rmtree(self.rec_dir, ignore_errors=True)
            self.store.sessions.pop(self.sid, None)
        else:
            self.touch()

