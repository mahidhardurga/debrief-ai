"""Audio capture and local transcription.

Two kinds of sources are captured with `soundcard` (WASAPI):
  * loopback of EVERY output device  -> label "other" (the remote people; this works whichever
    speaker device Teams/Zoom happens to use)
  * EVERY microphone                 -> label "you"
One thread per source, 0.25 s blocks, 16 kHz mono float32.

Transcription pipeline (all offline, faster-whisper):
  draft thread  : base.en, italic partial line within about a second
  final workers : small.en, parallel, writes the real line
  refine thread : when idle, re-transcribes with the accurate model and silently replaces the text
                  (only if the accurate model decodes fast enough on this machine)

IMPORTANT: this module is imported in the main thread before Flask starts its threads, and every
capture thread initialises COM itself, otherwise soundcard fails with error 0x800401f0.
"""
import ctypes
import heapq
import queue
import re
import threading
import time
from collections import deque

import numpy as np

import config

try:  # soundcard talks to WASAPI through COM; importing it here, in the main thread, initialises COM once
    import soundcard as sc
except Exception as exc:  # pragma: no cover - import problems are reported at start()
    sc = None
    _SC_ERROR = str(exc)
else:
    _SC_ERROR = ""

SR = 16000
BLOCK = SR // 4          # 0.25 s
HALLUCINATIONS = {"thank you.", "thanks for watching.", "you", "bye.", "thank you for watching.", ".", ""}
HEADSET_WORDS = ("headset", "headphone", "earbud", "airpod", "hands-free", "hands free", "buds")


def _co_init():
    """Each capture thread needs its own COM apartment."""
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
    except Exception:
        pass


def _words(text):
    return re.findall(r"[a-z0-9']+", text.lower())


class ModelStore:
    """Loads the Whisper models once, in a background thread, and caches them."""

    def __init__(self):
        self._models = {}
        self._lock = threading.Lock()
        self.error = ""
        self.ready = threading.Event()
        self.accurate_ok = False
        self.status = "idle"

    def _load(self, name):
        with self._lock:
            if name not in self._models:
                from faster_whisper import WhisperModel
                config.WHISPER_DIR.mkdir(parents=True, exist_ok=True)
                kw = dict(device="cpu", compute_type="int8", download_root=str(config.WHISPER_DIR))     # models stay inside the project
                try:
                    self._models[name] = WhisperModel(name, local_files_only=True, **kw)
                except Exception:
                    if not config.ALLOW_MODEL_DOWNLOAD:
                        raise
                    self._models[name] = WhisperModel(name, local_files_only=False, **kw)           # first run: download into desktop/models
            return self._models[name]

    def preload(self):
        if self.status != "idle":
            return  # already loading or loaded
        self.status = "loading speech models"
        threading.Thread(target=self._preload, daemon=True, name="model-preload").start()

    def _preload(self):
        try:
            self.status = "loading speech models"
            self.get("final")
            self.get("draft")
            self.ready.set()
            self.status = "ready"
            try:
                self._benchmark_accurate()
            except Exception as exc:
                self.accurate_ok = False
                self.status = f"ready (accurate model off: {str(exc)[:80]})"
        except Exception as exc:
            self.error = (f"Speech model not available ({exc}). Run scripts/download-models.bat once "
                          f"(needs internet), then it runs offline.")
            self.status = "error"
            self.ready.set()

    def get(self, which):
        name = {"draft": config.DRAFT_WHISPER_MODEL, "final": config.WHISPER_MODEL,
                "accurate": config.FINAL_WHISPER_MODEL}[which]
        return self._load(name)

    def _benchmark_accurate(self):
        """Use the accurate model only if it decodes 5 s of audio faster than ACCURATE_MAX_DECODE."""
        model = self.get("accurate")
        rng = np.random.default_rng(1)
        audio = (rng.standard_normal(SR * 5) * 0.01).astype("float32")
        t0 = time.time()
        list(model.transcribe(audio, language="en", beam_size=1)[0])
        took = time.time() - t0
        self.accurate_ok = took <= config.ACCURATE_MAX_DECODE
        self.status = "ready" + (" (accurate refine on)" if self.accurate_ok else " (accurate refine off: too slow)")

    def transcribe(self, which, audio, prompt=""):
        model = self.get(which)
        segs, _ = model.transcribe(
            audio, language="en", beam_size=1 if which == "draft" else 3,
            initial_prompt=prompt or None, condition_on_previous_text=False,
            vad_filter=False, no_speech_threshold=0.6)
        return " ".join(s.text.strip() for s in segs).strip()


MODELS = ModelStore()


class Gate:
    """Adaptive speech gate: tracks the noise floor and says whether a block contains speech."""

    def __init__(self):
        self.floor = 0.002

    def is_speech(self, rms):
        if rms < self.floor * 2.5:
            self.floor = 0.97 * self.floor + 0.03 * rms
        else:
            self.floor = min(self.floor * 1.0008, 0.02)
        return rms > max(self.floor * 3.0, 0.006)


class Source(threading.Thread):
    """Captures one device and segments it into utterances."""

    def __init__(self, cap, dev, role):
        super().__init__(daemon=True, name=f"cap-{role}-{dev.name[:20]}")
        self.cap, self.dev, self.role = cap, dev, role
        self.gate = Gate()
        self.level = 0.0
        self.buf, self.start_t, self.silent, self.draft_sent = [], 0.0, 0, 0.0

    def run(self):
        _co_init()
        try:
            with self.dev.recorder(samplerate=SR, channels=1, blocksize=BLOCK) as rec:
                while not self.cap.stop_evt.is_set():
                    data = rec.record(numframes=BLOCK)
                    block = np.asarray(data, dtype="float32").reshape(-1)
                    self.handle_block(block)
        except Exception as exc:
            self.cap.source_error(self, exc)
        finally:
            self.flush()

    def handle_block(self, block):
        rms = float(np.sqrt(np.mean(block ** 2))) if len(block) else 0.0
        self.level = rms
        if self.cap.paused:
            self.flush()
            return
        if not self.cap.listening:  # "Listening off" means silence, also in a running recording
            self.flush()
            return
        self.cap.on_audio(self.role, self.dev.name, block)
        speech = self.gate.is_speech(rms)
        now = time.time()
        if speech or self.buf:
            if not self.buf:
                self.start_t, self.silent, self.draft_sent = now, 0, now
            self.buf.append(block)
            self.silent = 0 if speech else self.silent + 1
            if now - self.draft_sent >= 1.0 and speech:
                self.draft_sent = now
                self.cap.submit_draft(self.role, self, np.concatenate(self.buf))
            if self.silent >= 3 or len(self.buf) * 0.25 >= 25:
                self.flush()

    def flush(self):
        if not self.buf:
            return
        audio = np.concatenate(self.buf)
        start, self.buf = self.start_t, []
        dur = len(audio) / SR
        if dur - 0.75 >= 0.4:  # drop clicks; trailing silence does not count
            self.cap.submit_final(self.role, self, audio, start)
        self.cap.clear_partial(self)


class Capture:
    def __init__(self, profile, add_utterance, on_partial, on_audio, t0=None):
        """add_utterance(label, text, ts) -> line id | None;  on_partial(key, label, text|None);
        on_audio(role, label, block);  t0 = wall-clock start of the session."""
        self.profile = profile
        self.add_utterance, self.on_partial, self.on_audio = add_utterance, on_partial, on_audio
        self.t0 = t0 or time.time()
        self.stop_evt = threading.Event()
        self.paused = False
        self.listening = True
        self.sources, self.threads = [], []
        self.final_q, self.draft_q, self.refine_q = queue.Queue(), queue.Queue(maxsize=4), queue.Queue()
        self.recent_other = deque(maxlen=40)   # (time, words)
        self.pending_mic = []                  # heap of (release_time, n, label, text, ts, refine_audio)
        self._n = 0
        self.headset = False
        self.errors = []
        self.busy = 0

    # ---- start / stop -------------------------------------------------------
    def start(self):
        """Start capturing. Returns "" on success or a message when running without a transcriber."""
        if sc is None:
            return f"Audio capture unavailable: {_SC_ERROR}"
        MODELS.preload()
        try:
            speakers = sc.all_speakers()
            mics = [m for m in sc.all_microphones(include_loopback=True)]
        except Exception as exc:
            return f"Could not list audio devices: {exc}"
        want = config.SPEAKER_DEVICE.lower()
        seen = set()
        for dev in mics:
            if dev.id in seen:
                continue
            seen.add(dev.id)
            if getattr(dev, "isloopback", False):
                if want and want not in dev.name.lower():
                    continue
                self.sources.append(Source(self, dev, "other"))
            else:
                self.sources.append(Source(self, dev, "you"))
        try:
            default = sc.default_speaker().name.lower()
            self.headset = any(w in default for w in HEADSET_WORDS)
        except Exception:
            self.headset = False
        if not self.sources:
            return "No audio devices found."
        for s in self.sources:
            s.start()
        for name, fn in (("draft", self._draft_loop), ("refine", self._refine_loop), ("tick", self._tick_loop)):
            t = threading.Thread(target=fn, daemon=True, name=f"cap-{name}")
            t.start()
            self.threads.append(t)
        for i in range(2):
            t = threading.Thread(target=self._final_loop, daemon=True, name=f"cap-final-{i}")
            t.start()
            self.threads.append(t)
        return MODELS.error

    def stop(self):
        """Stop capture; flush open utterances so the last words are transcribed."""
        for s in self.sources:
            s.flush()
        deadline = time.time() + 20
        while (self.final_q.qsize() or self.busy) and time.time() < deadline:
            time.sleep(0.1)
        self.stop_evt.set()
        self._release_pending(force=True)

    def source_error(self, src, exc):
        self.errors.append(f"{src.dev.name}: {exc}")

    @property
    def levels(self):
        you = max([s.level for s in self.sources if s.role == "you"] or [0.0])
        other = max([s.level for s in self.sources if s.role == "other"] or [0.0])
        return {"you": round(you, 4), "other": round(other, 4)}

    def source_names(self):
        return [{"role": s.role, "name": s.dev.name} for s in self.sources]

    # ---- queues -------------------------------------------------------------
    def submit_draft(self, role, src, audio):
        try:
            self.draft_q.put_nowait((role, src, audio))
        except queue.Full:
            pass

    def clear_partial(self, src):
        self.on_partial(id(src), src.role, None)

    def submit_final(self, role, src, audio, start_wall):
        self.final_q.put((role, audio, start_wall))

    def _draft_loop(self):
        while not self.stop_evt.is_set():
            try:
                role, src, audio = self.draft_q.get(timeout=0.3)
            except queue.Empty:
                continue
            if not MODELS.ready.is_set() or MODELS.error:
                continue
            try:
                text = MODELS.transcribe("draft", audio[-SR * 12:], self.profile.get("vocabulary", ""))
                if text and text.lower() not in HALLUCINATIONS and src.buf:
                    self.on_partial(id(src), role, text)
            except Exception:
                pass

    def _final_loop(self):
        while not self.stop_evt.is_set():
            try:
                role, audio, start_wall = self.final_q.get(timeout=0.3)
            except queue.Empty:
                continue
            self.busy += 1
            try:
                MODELS.ready.wait(120)
                if MODELS.error:
                    continue
                text = MODELS.transcribe("final", audio, self.profile.get("vocabulary", ""))
                if not text or text.lower() in HALLUCINATIONS:
                    continue
                ts = max(0.0, start_wall - self.t0)
                self._emit(role, text, ts, audio)
            except Exception as exc:
                self.errors.append(f"transcribe: {exc}")
            finally:
                self.busy -= 1

    # ---- echo suppression ---------------------------------------------------
    def _emit(self, role, text, ts, audio):
        now = time.time()
        if role == "other":
            self.recent_other.append((now, set(_words(text))))
            self._publish("other", text, ts, audio)
            return
        if self.headset or not self.recent_other:
            self._publish("you", text, ts, audio)
            return
        # speaker bleed into the mic: hold briefly and compare with what "other" said
        self._n += 1
        heapq.heappush(self.pending_mic, (now + 1.2, self._n, text, ts, audio))

    def _is_echo(self, text):
        mine = set(_words(text))
        if len(mine) < 3:
            return False
        now = time.time()
        for t, words in self.recent_other:
            if now - t < 15 and words and len(mine & words) / len(mine) >= 0.7:
                return True
        return False

    def _release_pending(self, force=False):
        now = time.time()
        while self.pending_mic and (force or self.pending_mic[0][0] <= now):
            _, _, text, ts, audio = heapq.heappop(self.pending_mic)
            if not self._is_echo(text):
                self._publish("you", text, ts, audio)

    def _tick_loop(self):
        while not self.stop_evt.is_set():
            self._release_pending()
            time.sleep(0.2)

    def _publish(self, role, text, ts, audio):
        line_id = self.add_utterance(role, text, ts, speaker=self.speaker_of(role, audio))
        if line_id is not None and MODELS.accurate_ok and len(audio) / SR >= 1.5:
            self.refine_q.put((line_id, role, audio))

    # ---- refine -------------------------------------------------------------
    def _refine_loop(self):
        """When nothing else is waiting, replace the fast text with the accurate model's text."""
        while not self.stop_evt.is_set():
            try:
                line_id, role, audio = self.refine_q.get(timeout=0.5)
            except queue.Empty:
                continue
            while (self.final_q.qsize() or self.busy) and not self.stop_evt.is_set():
                time.sleep(0.3)
            try:
                text = MODELS.transcribe("accurate", audio, self.profile.get("vocabulary", ""))
                if text and text.lower() not in HALLUCINATIONS:
                    self.refine(line_id, text)
            except Exception:
                pass

    refine = staticmethod(lambda line_id, text: None)  # replaced by the session
    speaker_of = staticmethod(lambda role, audio: None)  # replaced by the session: who spoke (the other side's voices)


class DemoCapture:
    """MOCK_ANALYZER demo: replays a sample transcript as if it were being heard live."""

    def __init__(self, profile, add_utterance, on_partial, on_audio, t0=None, path=None):
        self.profile, self.add_utterance, self.on_partial = profile, add_utterance, on_partial
        self.paused = False
        self.listening = True
        self.stop_evt = threading.Event()
        self.t0 = t0 or time.time()
        self.path = path
        self.errors = []
        self.headset = True
        self.sources = []
        self.refine = lambda *_: None

    levels = property(lambda self: {"you": 0.02, "other": 0.02})

    def source_names(self):
        return [{"role": "other", "name": "Demo (sample transcript)"}]

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="demo-feed").start()
        return ""

    def stop(self):
        self.stop_evt.set()

    def _run(self):
        try:
            rows = [r for r in self.path.read_text(encoding="utf-8").splitlines() if ":" in r]
        except Exception:
            return
        for row in rows:
            if self.stop_evt.wait(2.0):
                return
            while self.paused or not self.listening:
                if self.stop_evt.wait(0.3):
                    return
            who, text = row.split(":", 1)
            you = who.strip().lower() in ("interviewer", "you", "me")
            self.add_utterance("you" if you else "other", text.strip(), time.time() - self.t0)
