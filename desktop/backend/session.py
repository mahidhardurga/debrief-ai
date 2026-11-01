"""One listening session: state machine, live loops, persistence, recordings, exhibits, chat, form fill.

State machine:  live -> stopping -> finalizing -> review -> decided
Everything is saved under SESSIONS_DIR/<yyyy-mm-dd>_<nn>_<slug>/ and the sidebar is rebuilt from
disk on restart.  A disk error must never stop the live transcript, so every write is guarded.
"""
import base64
import copy
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "shared" / "core"))   # analyzer.py, reports.py
import analyzer
import capture
import config
import formfill
import reports
import speakers
import windows_capture as wc
from recorder import AudioRecorder
from video_recorder import VideoRecorder

LOCK = threading.RLock()
SESSIONS = {}        # sid -> Session
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


def _small_signature(arr):
    """64x36 grayscale of a frame, used to tell whether the screen changed."""
    g = arr[:, :, :3].mean(axis=2)
    ys = (np.arange(36) * g.shape[0] / 36).astype(int)
    xs = (np.arange(64) * g.shape[1] / 64).astype(int)
    return g[ys][:, xs]


class Session:
    def __init__(self, profile, subject="", extra="", context=None, sid=None, role="", speakers=False):
        self.profile = copy.deepcopy(profile)      # the rubric is per session (built for the role, grows during the call)
        self.role = (role or "").strip()
        self.rubric_building = False
        self.rubric_note = ""
        self.speakers_on = bool(speakers)            # tell the people on the other side apart (Speaker 1, 2, ...)
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
        self.sid = sid or self._new_sid()
        self.dir = config.SESSIONS_DIR / self.sid
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
        self.source = None            # picked share source {"kind", "hwnd"/"index", "label"}
        self.pending_shots = {}
        self.partials = {}
        self.capture = None
        self.capture_error = ""
        self.force = False
        self.last_analyze, self.lines_at_last = 0.0, 0
        self.analyzing = False
        self.chat_busy = False
        self.form_busy = False
        self.regenerating = False
        self.fill = None
        self.fill_info = {}
        self.recs = {}
        self.stop_evt = threading.Event()
        self.threads = []

    # ------------------------------------------------------------ identity / paths
    def _new_sid(self):
        with LOCK:
            config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            n = 1 + len([p for p in config.SESSIONS_DIR.glob(f"{self.date}_*") if p.is_dir()])
            return f"{self.date}_{n:02d}_{reports.slug(self.subject or self.profile['id'])}"

    @property
    def name(self):
        return self.subject or "Untitled"

    @property
    def rec_dir(self):
        return config.RECORDINGS_DIR / self.sid

    def elapsed(self):
        if self.state in ("live",) and not self.paused:
            return self.active + (time.time() - self.seg_start)
        return self.active

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

    def on_utterance(self, label, text, ts, speaker=None):
        if self.state != "live":
            return None
        return self.add_line(label, text, ts, speaker=speaker)

    def _speaker_of(self, role, audio):
        """Speaker number for an utterance from the other side of the call (None: not tracked)."""
        if role != "other" or not self.speakers_on:
            return None
        try:
            if self.speaker_book is None:
                speakers.load(config.SPEAKER_MODEL, config.ALLOW_MODEL_DOWNLOAD)
                self.speaker_book = speakers.SpeakerBook(config.SPEAKER_THRESHOLD)
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
        sample = config.SAMPLES_DIR / SAMPLE_FOR.get(self.profile["id"], "sample_meeting.txt")
        if config.MOCK_ANALYZER:
            self.capture = capture.DemoCapture(self.profile, self.on_utterance, self.on_partial, self.on_audio,
                                               t0=self.started_wall, path=sample)
        else:
            self.capture = capture.Capture(self.profile, self.on_utterance, self.on_partial, self.on_audio,
                                           t0=self.started_wall)
            self.capture.refine = lambda lid, text: self.override_line(lid, text, "refined")
            self.capture.speaker_of = self._speaker_of
        try:
            self.capture_error = self.capture.start()
        except Exception as exc:
            self.capture_error = f"Could not start audio capture: {exc}"
        if self.capture_error:
            self.event(f"Running without a transcriber: {self.capture_error}")
        for name, fn in (("loop", self._loop), ("watch", self._watch_loop)):
            t = threading.Thread(target=fn, daemon=True, name=f"session-{name}")
            t.start()
            self.threads.append(t)
        self.touch()

    def _build_rubric(self):
        """Topic list for the role the interviewer is hiring for (falls back to the profile's default list)."""
        try:
            res = analyzer.generate_rubric(self.profile, self.role, self.extra, "\n".join(self.context))
            with self.lock:
                self.profile["rubric"]["role"] = res["role"]
                self.profile["rubric"]["areas"] = res["areas"]
                # speech-recognition hints for this role instead of the Java defaults
                self.profile["vocabulary"] = ", ".join([res["role"]] + [x["name"] for x in res["areas"]])
            self.event(f"Topic list built for {res['role']} ({len(res['areas'])} topics)")
        except Exception as exc:
            self.rubric_note = f"Could not build a topic list for this role ({exc}); using the default list."
            self.event(self.rubric_note)
        finally:
            self.rubric_building = False
            self.touch()

    # ------------------------------------------------------------ live loops
    def ctx(self, previous=True):
        with self.lock:
            return {"profile": self.profile, "subject": self.subject, "extra": self.extra, "speakers": self.speaker_info(),
                    "lines": [dict(l) for l in self.lines], "notes": self.notes, "context": list(self.context),
                    "exhibits": [dict(e) for e in self.exhibits],
                    "previous": self.analysis if previous else None}

    def _loop(self):
        last_correct = time.time()
        while not self.stop_evt.wait(1.0):
            if self.state != "live" or self.paused or self.rubric_building:
                continue
            self._check_disk()
            n_lines = len([l for l in self.lines if not l.get("marker")])
            new = n_lines - self.lines_at_last
            gap = time.time() - self.last_analyze
            due = (gap >= config.ANALYZE_INTERVAL and new > 0) or (new >= config.ANALYZE_NEW_LINES and gap >= config.ANALYZE_MIN_GAP)
            if (self.force or due) and n_lines and not self.analyzing:
                self.force = False
                self.run_live_analysis()
            if time.time() - last_correct > 25 and not config.MOCK_ANALYZER:
                last_correct = time.time()
                self.run_corrections()

    def run_live_analysis(self):
        self.analyzing = True
        try:
            ctx = self.ctx()
            n = len([l for l in ctx["lines"] if not l.get("marker")])
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
            batch = [l for l in self.lines if not l["checked"] and not l["marker"]][-15:]
        if not batch:
            return
        context = "; ".join([self.subject] + self.context)[:1500]
        try:
            fixes = analyzer.correct_lines(self.profile, [{"id": l["id"], "label": l["label"], "text": l["text"]} for l in batch], context)
        except Exception:
            return
        for fx in fixes:
            if isinstance(fx.get("id"), int):
                self.override_line(fx["id"], fx.get("text", ""), "fixed")
        with self.lock:
            for l in batch:
                l["checked"] = True

    def _check_disk(self):
        if not self.clips or not any(c["state"] in ("recording", "paused") for c in self.clips):
            return
        try:
            free = shutil.disk_usage(config.RECORDINGS_DIR).free / 1048576
        except OSError:
            return
        if free < config.MIN_FREE_MB:
            self.event(f"Recording stopped automatically: only {free:.0f} MB free disk left")
            self.record_stop()

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
            if self.capture:
                self.capture.paused = on
        self.marker("Session paused" if on else "Session resumed")
        self._clip_modes()
        self.event("Session paused" if on else "Session resumed")

    def set_listening(self, on):
        with self.lock:
            if self.state != "live" or on == self.listening:
                return
            self.listening = on
            if self.capture:
                self.capture.listening = on
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
        try:
            if self.capture:
                self.capture.stop()
        except Exception:
            pass
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
            n = len([l for l in self.lines if not l.get("marker")])
            if n == 0 and not self.exhibits:
                out = self._empty_final()
            else:
                ctx = self.ctx(previous=False)
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
        """Which drafts are written given the current decision."""
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
            meta = self.meta()
            _write(self.dir / "report.md", reports.report_md(
                meta, self.profile, self.final, self.exhibits, self.decision, self.clips_public()))
            if self.state == "decided":
                score = self.final.get("weighted_score")
                _write(self.dir / "result.json", reports.to_json({
                    "decision": self.decision, "date": self.result_date, "subject": self.subject,
                    "score": score, "profile": self.profile["id"]}))
            elif (self.dir / "result.json").exists():
                (self.dir / "result.json").unlink()
            self.update_day_summary()
        except OSError as exc:
            self.event(f"Could not write files: {exc}")

    def update_day_summary(self):
        rows = []
        for s in list(SESSIONS.values()):
            rows.append({"date": s.date, "time": s.time, "name": s.name, "type": s.profile["name"],
                         "decision": s.decision, "score": (s.final or {}).get("weighted_score") or "",
                         "feedback": s.feedback.get("state", "")})
        csv_text, md = reports.day_summary(rows, self.date)
        _write(config.SESSIONS_DIR / f"day-summary-{self.date}.csv", csv_text)
        _write(config.SESSIONS_DIR / f"day-summary-{self.date}.md", md)

    def set_feedback(self, state):
        if state not in ("pending", "filled", "submitted") or self.state != "decided" or not self.profile.get("form"):
            return False
        self.feedback = {"state": state, "updated": now_iso()}
        self.touch()
        self.update_day_summary()
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
            for l in self.lines:
                if l["marker"]:
                    rows.append(f"--- {l['text']} ---")
                else:
                    who = self.profile["you_label"] if l["label"] == "you" else self.profile["other_label"]
                    if l["label"] == "other" and spk["multi"] and l.get("speaker") is not None:
                        who += f" ({spk['names'].get(str(l['speaker'])) or 'Speaker ' + str(l['speaker'])})"
                    rows.append(f"[{analyzer.fmt_ts(l['ts'])}] {who}: {l['text']}")
            _write(self.dir / "transcript.txt", "\n".join(rows))
        except OSError:
            pass

    @classmethod
    def load(cls, path, profiles):
        meta = json.loads((path / "session.json").read_text(encoding="utf-8"))
        profile = profiles.get(meta.get("profile"))
        if not profile:
            return None

        def rj(name, default):
            try:
                return json.loads((path / name).read_text(encoding="utf-8"))
            except Exception:
                return default

        s = cls(profile, meta.get("subject", ""), meta.get("extra", ""), rj("context.json", []), sid=meta["id"])
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
        # transcript with overrides
        try:
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
        # interrupted work becomes something retryable
        for c in s.clips:
            if c["state"] in ("recording", "paused", "saving"):
                c["state"] = "saved"
                part = s.rec_dir / c["video_file"].replace(".mp4", ".part.mp4") if c.get("video_file") else None
                if part and part.exists() and not (s.rec_dir / c["video_file"]).exists():
                    try:  # a crash leaves a fragmented MP4 that still plays: keep it
                        part.replace(s.rec_dir / c["video_file"])
                    except OSError:
                        pass
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
            res = analyzer.exhibit_analysis(self.ctx(previous=False), png, ex["question"], ex["note"])
            with self.lock:
                if ex["auto"] and res.get("relevant") is False and config.WATCH_DROP_IRRELEVANT:
                    self.exhibits.remove(ex)  # only when WATCH_DROP_IRRELEVANT is on: by default everything that was watched stays
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
        if extra.exists():
            try:
                extra.unlink()
            except OSError:
                pass

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
            try:
                (self.dir / ex["file"]).unlink()
            except OSError:
                pass
            self._renumber_exhibits()
        self.touch()
        return True

    # ------------------------------------------------------------ screen watching
    def set_watch(self, on, source=None):
        with self.lock:
            if source is not None:
                self.source = source
            self.watch["on"] = bool(on) and self.state == "live" and not self.paused
            self.watch["status"] = ""
        self.event("Screen watching on" if on else "Screen watching off")

    def _watch_loop(self):
        prev = last_kept = None
        change_since = stable_since = None
        next_t = 0.0
        while not self.stop_evt.wait(0.5):
            if not self.watch["on"] or self.state != "live":
                prev = last_kept = None
                change_since = stable_since = None
                continue
            if time.time() < next_t:
                continue
            next_t = time.time() + self.watch["interval"]
            src = self.source
            try:
                if not wc.source_alive(src):
                    self.watch["status"] = "pick the window again"
                    continue
                arr = wc.grab(src)
                if arr is None:
                    continue
                sig = _small_signature(arr)
            except Exception as exc:
                self.watch["status"] = f"capture failed: {exc}"
                continue
            now = time.time()
            changed = prev is None or float(np.mean(np.abs(sig - prev) > 12)) > config.WATCH_CHANGE
            prev = sig
            if changed:
                change_since = change_since or now
                stable_since = None
            else:
                stable_since = stable_since or now
                change_since = None
            stable = (not changed) and (self.watch["interval"] >= config.WATCH_STABLE or now - stable_since >= config.WATCH_STABLE)
            forced = change_since is not None and now - change_since >= config.WATCH_FORCE
            differs = last_kept is None or float(np.mean(np.abs(sig - last_kept) > 12)) > config.WATCH_CHANGE
            if config.WATCH_MODE == "interval":          # every interval: keep the frame when something new is on screen
                stable, forced = True, False
            if (stable or forced) and differs:
                last_kept = sig
                change_since = now if forced else change_since
                png = wc.encode_png(wc.resize(wc.bgra_to_rgb(arr), 1600))
                self.watch["kept"] += 1
                self.add_exhibit(png, "(screen watch)", "", auto=True)
                self.watch["status"] = ""
            else:
                self.watch["status"] = "nothing new on screen" if config.WATCH_MODE == "interval" else "waiting for the screen to settle"

    # ------------------------------------------------------------ recording (clips)
    def on_audio(self, role, label, block):
        for rec in list(self.recs.values()):
            a = rec.get("audio")
            if a:
                a.feed(role, label, block)

    def _clip(self, n):
        return next((c for c in self.clips if c["n"] == n), None)

    def record_start(self, audio, video, consent=False):
        """Returns {"ok":True} | {"need_consent":True} | {"error":msg}."""
        if self.state != "live":
            return {"error": "Recording only works inside a live session."}
        if any(c["state"] in ("recording", "paused") for c in self.clips):
            return {"error": "Already recording."}
        if not (audio or video):
            return {"error": "Choose Audio and/or Screen video in Options first."}
        if config.REQUIRE_CONSENT and not self.consent:
            if not consent:
                return {"need_consent": True}
        if consent and not self.consent:
            self.consent = {"confirmed": True, "time": now_iso()}
        try:
            self.rec_dir.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(self.rec_dir).free / 1048576
        except OSError as exc:
            return {"error": f"Cannot use the recordings folder: {exc}"}
        if free < config.MIN_FREE_MB:
            return {"error": f"Only {free:.0f} MB free on the recordings disk (limit {config.MIN_FREE_MB} MB)."}
        with self.lock:
            n = max([c["n"] for c in self.clips] + [0]) + 1
            base = f"{self.sid}_rec{n:02d}"
            clip = {"n": n, "name": f"Recording {n}", "state": "recording", "audio": bool(audio), "video": bool(video),
                    "started": now_iso(), "start_ts": round(time.time() - self.started_wall, 1), "duration": 0,
                    "size": 0, "audio_file": f"{base}.ogg" if audio else "", "video_file": f"{base}.mp4" if video else "",
                    "source": (self.source or {}).get("label", "Screen 1") if video else "", "error": ""}
            rec = {}
            try:
                if audio:
                    rec["audio"] = AudioRecorder(self.rec_dir / clip["audio_file"])
                if video:
                    rec["video"] = VideoRecorder(self.rec_dir / clip["video_file"], lambda: self.source,
                                                 config.VIDEO_FPS, config.VIDEO_MAX_WIDTH, config.VIDEO_CRF)
            except Exception as exc:
                for r in rec.values():
                    try:
                        r.stop()
                    except Exception:
                        pass
                return {"error": f"Could not start recording: {exc}"}
            self.clips.append(clip)
            self.recs[n] = rec
        self._clip_modes()
        self.event(f"Recording {n} started ({'audio' if audio else ''}{' + ' if audio and video else ''}{'video' if video else ''})")
        return {"ok": True}

    def _clip_modes(self):
        """Pause/resume the active clip together with the session."""
        for c in self.clips:
            if c["state"] in ("recording", "paused"):
                c["state"] = "paused" if self.paused else "recording"
                v = self.recs.get(c["n"], {}).get("video")
                if v:
                    v.set_mode("paused" if self.paused else "record")
        self.touch(save=False)

    def record_stop(self):
        clip = next((c for c in self.clips if c["state"] in ("recording", "paused")), None)
        if not clip:
            return False
        clip["state"] = "saving"
        self.touch(save=False)
        threading.Thread(target=self._finish_clip, args=(clip,), daemon=True, name="clip-save").start()
        return True

    def _finish_clip(self, clip):
        rec = self.recs.pop(clip["n"], {})
        dur = 0
        try:
            a = rec.get("audio")
            if a:
                dur = a.stop()
            v = rec.get("video")
            if v:
                dur = v.stop(self.rec_dir / clip["audio_file"] if a else None)
                if v.error:
                    clip["error"] = v.error
            clip["state"] = "saved"
        except Exception as exc:
            clip["state"], clip["error"] = "error", str(exc)
        clip["duration"] = round(dur, 1)
        clip["ended"] = now_iso()
        clip["size"] = sum((self.rec_dir / clip[k]).stat().st_size for k in ("audio_file", "video_file")
                           if clip[k] and (self.rec_dir / clip[k]).exists())
        self.event(f"Recording {clip['n']} saved ({human_size(clip['size'])})")
        self.touch()

    def clips_public(self):
        out = []
        for c in self.clips:
            d = dict(c)
            d["duration_text"] = human_dur(c.get("duration", 0))
            d["size_text"] = human_size(c.get("size", 0))
            d["path"] = str(self.rec_dir)
            d["audio_url"] = f"/api/clip/{self.sid}/{c['n']}/audio" if c["audio"] and c["state"] == "saved" else ""
            d["video_url"] = f"/api/clip/{self.sid}/{c['n']}/video" if c["video"] and c["state"] == "saved" else ""
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
        """Delete one clip or all of them; Windows keeps files open for a moment, so retry."""
        targets = [c for c in self.clips if (n is None or c["n"] == n) and c["state"] not in ("recording", "paused", "saving")]
        for c in targets:
            for key in ("audio_file", "video_file"):
                if not c[key]:
                    continue
                path = self.rec_dir / c[key]
                for _ in range(10):
                    try:
                        path.unlink(missing_ok=True)
                        break
                    except OSError:
                        time.sleep(0.3)
            self.clips.remove(c)
        self.event("Recordings deleted")
        return len(targets)

    # ------------------------------------------------------------ chat
    def form_state_text(self):
        parts = []
        if self.form_data:
            parts.append(reports.form_md(self.form_data, self.profile.get("form")))
        if self.fill and self.fill.status:
            parts.append(f"Form fill state: {self.fill.status.get('state')}")
        return "\n".join(parts)

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
        if action == "fill_form" and self.profile.get("form"):
            self.chat_add("assistant", "Open the evaluation form in the in-app browser and fill it in? "
                          "I will not submit anything.", kind="permission", action="fill_form", status="pending")
        elif action == "stop_fill":
            if self.fill and self.fill.running():
                self.fill.cancel()
                self.chat_add("assistant", "Stopped the form filler.")
            else:
                self.chat_add("assistant", "No fill is running.")
        elif action == "update_answer":
            seq = self.fill.update_answer(res.get("field", ""), res.get("value", "")) if self.fill else None
            if seq:
                self.chat_add("assistant", f"Sent the change for {res.get('field')} to the open form window; waiting for its confirmation.")
            else:
                self.chat_add("assistant", "The form window is not open, so I cannot change it. Open the form first.")
        elif action == "list_pending":
            items = [{"sid": s.sid, "name": s.name} for s in SESSIONS.values()
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

    def start_fill(self, url=None):
        """Open the in-app browser and fill the portal form. Never submits."""
        if not self.profile.get("form"):
            return {"error": "This profile has no form."}
        if self.fill and self.fill.running():
            return {"error": "A fill is already running."}
        if not self.form_data:
            return {"error": "Write the form answers first."}
        url = (url or config.FORM_URL).strip()
        if not re.match(r"^https?://", url):
            return {"error": "Enter a full web address starting with https://"}
        job = {"url": url, "name": self.subject, "answers": reports.form_md(self.form_data, self.profile["form"]),
               "form": self.profile["form"], "mode": config.FORM_MODE}
        self._fill_steps_seen, self._fill_acks_seen = 0, 0
        self._activity_id = self.chat_add("assistant", "", kind="activity", title=f"Filling {self.subject}'s evaluation form",
                                          steps=0, lines=[], state="running", unread=False)["id"]
        self.fill = formfill.Fill(self.dir / "fill", job, on_change=self._fill_changed)
        self.fill.start()
        self.event("Form fill started")
        return {"ok": True}

    def _fill_changed(self, st):
        with self.lock:
            msg = next((m for m in self.chat if m["id"] == self._activity_id), None)
            if msg:
                msg["lines"] = st.get("log", [])[-200:]
                msg["steps"] = len(st.get("log", []))
                msg["state"] = {"done": "done", "error": "error"}.get(st.get("state"), "running")
            acks = st.get("acks", [])
            new_acks = acks[self._fill_acks_seen:]
            self._fill_acks_seen = len(acks)
        for a in new_acks:
            self.chat_add("assistant", a.get("text", ""))
        if st.get("state") == "done" and msg and not msg.get("reported"):
            msg["reported"] = True
            self.chat_add("assistant", st.get("report", ""), unread=True)
            if self.feedback.get("state") in ("pending", ""):
                self.set_feedback("filled")
            self.event("Form filled; waiting for you to review and submit")
        elif st.get("state") == "error" and msg and not msg.get("reported"):
            msg["reported"] = True
            self.chat_add("assistant", f"The form fill stopped: {st.get('error', 'unknown error')}", error=True)
        self.touch(save=False)

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
            lines = [l for l in self.lines if l["v"] > since]
            parts = list(self.partials.values())
        cap = self.capture
        cap_info = {"levels": cap.levels if cap and self.state == "live" else {"you": 0, "other": 0},
                    "sources": cap.source_names() if cap else [],
                    "error": self.capture_error or ("" if config.MOCK_ANALYZER else capture.MODELS.error),
                    "errors": (cap.errors[-3:] if cap else []),
                    "models": capture.MODELS.status if not config.MOCK_ANALYZER else "demo"}
        fill = None
        if self.fill:
            fill = {"state": self.fill.status.get("state"), "window_open": self.fill.window_open(),
                    "log": self.fill.status.get("log", [])[-5:], "steps": len(self.fill.status.get("log", []))}
        active = next((c for c in self.clips if c["state"] in ("recording", "paused")), None)
        any_rec = active is not None
        rec_dur = round(time.time() - self.started_wall - active["start_ts"], 1) if active else 0
        return {"rec_dur": rec_dur,"lines": lines, "partials": parts, "max_v": self.vcount, "elapsed": round(self.elapsed(), 1),
                "state": self.state, "paused": self.paused, "listening": self.listening, "capture": cap_info,
                "watch": dict(self.watch), "source": self.source, "analyzing": self.analyzing,
                "analysis_error": self.analysis_error, "chat_busy": self.chat_busy, "form_busy": self.form_busy,
                "fill": fill, "rec_active": any_rec,
                "rec_audio_only_paused": (not self.listening) and any_rec}

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
                    z.write(p, f"{self.sid}/recordings/{p.name}")
        buf.seek(0)
        return buf

    def discard(self, delete_files):
        with self.lock:
            if self.state == "live":
                self.stop_evt.set()
                try:
                    self.capture.stop()
                except Exception:
                    pass
                if any(c["state"] in ("recording", "paused") for c in self.clips):
                    self.record_stop()
            self.dismissed = True
        if delete_files:
            shutil.rmtree(self.dir, ignore_errors=True)
            SESSIONS.pop(self.sid, None)
        else:
            self.touch()


# ---------------------------------------------------------------------------- store helpers
def load_all(profiles):
    """Rebuild the sidebar from disk (called at startup)."""
    config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    for p in sorted(config.SESSIONS_DIR.iterdir()):
        if p.is_dir() and (p / "session.json").exists():
            try:
                s = Session.load(p, profiles)
                if s:
                    SESSIONS[s.sid] = s
            except Exception as exc:
                print(f"[load] skipping {p.name}: {exc}")


def cleanup_recordings():
    """Delete whole recordings older than RECORDING_RETENTION_DAYS (0 keeps everything)."""
    days = config.RECORDING_RETENTION_DAYS
    if days <= 0 or not config.RECORDINGS_DIR.exists():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for s in SESSIONS.values():
        for c in list(s.clips):
            files = [s.rec_dir / c[k] for k in ("audio_file", "video_file") if c.get(k)]
            existing = [f for f in files if f.exists()]
            if existing and all(f.stat().st_mtime < cutoff for f in existing):
                for f in existing:
                    try:
                        f.unlink()
                    except OSError:
                        pass
                s.clips.remove(c)
                removed += 1
        if removed:
            s.save()
    return removed


def day_summary_csv(date):
    """The day summary as CSV text, for the download link."""
    rows = [{"date": s.date, "time": s.time, "name": s.name, "type": s.profile["name"], "decision": s.decision,
             "score": (s.final or {}).get("weighted_score") or "", "feedback": s.feedback.get("state", "")} for s in list(SESSIONS.values())]
    return reports.day_summary(rows, date)[0]


def live_session():
    return next((s for s in SESSIONS.values() if s.state == "live"), None)
