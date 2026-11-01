"""Server-side speech recognition (faster-whisper) and the speech segmenter.

Browsers stream 16 kHz mono PCM16 over a WebSocket. A per-source `Segmenter` cuts the stream into utterances
with an adaptive noise-floor gate; finished utterances are transcribed by a small thread pool and appended as
transcript lines. Models load lazily on first use and run offline unless ASR_ALLOW_DOWNLOAD=1.
"""
import logging
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from ..config import get_settings

log = logging.getLogger("debrief.asr")
SR = 16000
BLOCK = SR // 4                      # 0.25 s
HALLUCINATIONS = {"thank you.", "thanks for watching.", "you", "bye.", "thank you for watching.", ".", ""}

_model = None
_model_lock = threading.Lock()
_pool: ThreadPoolExecutor | None = None
_override: Callable[[np.ndarray], str] | None = None
model_error = ""
_refine_model = None
_refine_override: Callable[[np.ndarray], str] | None = None
_refine_pool: ThreadPoolExecutor | None = None
_refine_backlog = 0
_refine_lock = threading.Lock()
status = "idle"                      # idle | loading | ready | error  (what the UI shows while the model downloads/loads)


def set_transcriber(fn: Callable[[np.ndarray], str] | None) -> None:
    """For tests: replace Whisper with a fake."""
    global _override
    _override = fn


def set_refiner(fn: Callable[[np.ndarray], str] | None) -> None:
    """For tests: replace the accurate model with a fake."""
    global _refine_override
    _refine_override = fn


def refine_enabled() -> bool:
    return bool(get_settings().asr_refine_model) or _refine_override is not None


def refine_text(audio: np.ndarray, prompt: str = "") -> str:
    global _refine_model
    if _refine_override:
        return _refine_override(audio)
    with _model_lock:
        if _refine_model is None:
            from faster_whisper import WhisperModel
            s = get_settings()
            _refine_model = WhisperModel(s.asr_refine_model, device=s.asr_device, compute_type=s.asr_compute_type,
                                         local_files_only=not s.asr_allow_download)
    segs, _ = _refine_model.transcribe(audio, language="en", beam_size=5, initial_prompt=prompt or None,
                                       condition_on_previous_text=False, vad_filter=False, no_speech_threshold=0.6)
    return " ".join(x.text.strip() for x in segs).strip()


def submit_refine(audio: np.ndarray, prompt: str, apply: Callable[[str], None]) -> bool:
    """Re-transcribe one finished utterance with the accurate model in the background; `apply(text)` gets the better text.
    Skipped (False) when too many lines are already waiting, so the live transcript never falls behind."""
    global _refine_backlog, _refine_pool
    with _refine_lock:
        if _refine_backlog >= get_settings().asr_refine_backlog:
            return False
        _refine_backlog += 1
        if _refine_pool is None:
            _refine_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr-refine")

    def work():
        global _refine_backlog
        try:
            text = refine_text(audio, prompt)
            if text and text.lower() not in HALLUCINATIONS:
                apply(text)
        except Exception:
            log.exception("accurate pass failed")
        finally:
            with _refine_lock:
                _refine_backlog -= 1
    _refine_pool.submit(work)
    return True


def _load():
    global _model, model_error, status
    with _model_lock:
        if _model is None:
            status = "loading"
            s = get_settings()
            if not s.asr_allow_download:
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
            from faster_whisper import WhisperModel
            try:
                _model = WhisperModel(s.asr_model, device=s.asr_device, compute_type=s.asr_compute_type,
                                      local_files_only=not s.asr_allow_download)
                model_error, status = "", "ready"
            except Exception as exc:
                model_error = (f"Speech model {s.asr_model!r} is not available ({exc}). Set ASR_ALLOW_DOWNLOAD=true once "
                               f"to download it, then it runs offline.")
                status = "error"
                raise
    return _model


def start_preload() -> None:
    """Load (and, if allowed, download) the model in the background when the server starts, so the first words are not lost."""
    global status
    s = get_settings()
    if _override or not s.asr_enabled:
        status = "ready" if _override else "idle"
        return

    def run():
        try:
            _load()
        except Exception:
            log.exception("speech model failed to load")
    threading.Thread(target=run, daemon=True, name="asr-preload").start()
    status = "loading"


def transcribe(audio: np.ndarray, prompt: str = "", fast: bool = False) -> str:
    """fast=True is used for the italic live drafts (greedy decoding); finals use beam search."""
    if _override:
        return _override(audio)
    segs, _ = _load().transcribe(audio, language="en", beam_size=1 if fast else 3, initial_prompt=prompt or None,
                                 condition_on_previous_text=False, vad_filter=False, no_speech_threshold=0.6)
    return " ".join(x.text.strip() for x in segs).strip()


def pool() -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=max(1, get_settings().asr_workers), thread_name_prefix="asr")
    return _pool


class Segmenter:
    """Adaptive speech gate + utterance segmentation for one audio source."""

    PARTIAL_EVERY = 1.6                  # seconds between live drafts while someone is still speaking

    def __init__(self, on_utterance: Callable[[np.ndarray, float], None], on_partial: Callable[[np.ndarray], None] | None = None):
        self.on_utterance = on_utterance
        self.on_partial = on_partial
        self.last_partial = 0.0
        self.floor = 0.002
        self.acc = np.zeros(0, dtype=np.float32)
        self.buf: list[np.ndarray] = []
        self.silent = 0
        self.start_t = 0.0
        self.level = 0.0

    def push(self, samples: np.ndarray, now: float) -> None:
        self.acc = np.concatenate([self.acc, samples])
        while len(self.acc) >= BLOCK:
            block, self.acc = self.acc[:BLOCK], self.acc[BLOCK:]
            self._block(block, now)

    def _block(self, b: np.ndarray, now: float) -> None:
        rms = float(np.sqrt(np.mean(b ** 2)))
        self.level = rms
        if rms < self.floor * 2.5:
            self.floor = 0.97 * self.floor + 0.03 * rms
        else:
            self.floor = min(self.floor * 1.0008, 0.02)
        speech = rms > max(self.floor * 3.0, 0.006)
        if speech or self.buf:
            if not self.buf:
                self.start_t, self.silent = now, 0
            self.buf.append(b)
            if self.on_partial and speech and len(self.buf) >= 4 and now - self.last_partial >= self.PARTIAL_EVERY:
                self.last_partial = now
                self.on_partial(np.concatenate(self.buf[-48:]))      # the last 12 s
            self.silent = 0 if speech else self.silent + 1
            if self.silent >= 3 or len(self.buf) >= 100:       # 0.75 s of silence, or 25 s max
                self.flush()

    def reset(self) -> None:
        """Forget the audio heard so far (the session is paused or listening is off)."""
        self.buf, self.acc, self.silent = [], np.zeros(0, dtype=np.float32), 0

    def flush(self) -> None:
        if not self.buf:
            return
        audio, start = np.concatenate(self.buf), self.start_t
        self.buf = []
        if len(audio) / SR - 0.75 >= 0.4:                         # drop clicks
            self.on_utterance(audio, start)


def pcm16_to_float(data: bytes) -> np.ndarray:
    return np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
