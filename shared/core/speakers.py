"""Tell the people on a call apart.

Every finished utterance gets a voice fingerprint (a speaker embedding from a small ONNX model, run with sherpa-onnx on
the CPU) and is matched against the voices heard so far in the same session: close enough means the same person,
otherwise a new person ("Speaker 2", ...). Matching is online, so it works live, with no audio kept.

Use `SpeakerBook` once per audio channel and session; `assign(audio)` returns the speaker number (1, 2, ...).
Short clips are unreliable, so they never create a new person on their own.
"""
import threading
import urllib.request
from pathlib import Path

import numpy as np

SR = 16000
MODEL_FILE = "wespeaker_en_voxceleb_resnet34_LM.onnx"          # 26 MB, English voices
MODEL_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/" + MODEL_FILE

_embed = None            # fn(np.ndarray float32 mono 16 kHz) -> unit vector; tests replace it with set_embedder()
_lock = threading.Lock()
_extractor = None
error = ""


def set_embedder(fn) -> None:
    """For tests: replace the neural model with a fake."""
    global _embed
    _embed = fn


def ensure_model(path: str | Path, allow_download: bool) -> Path:
    path = Path(path)
    if path.exists():
        return path
    if not allow_download:
        raise FileNotFoundError(f"Speaker model not found at {path}. Set ASR_ALLOW_DOWNLOAD=true once to download it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    urllib.request.urlretrieve(MODEL_URL, tmp)
    tmp.replace(path)
    return path


def load(model_path: str | Path, allow_download: bool = False) -> None:
    """Load the neural model once (idempotent). Raises with a readable message when it cannot be used."""
    global _extractor, _embed, error
    with _lock:
        if _embed is not None or _extractor is not None:
            return
        try:
            import sherpa_onnx
            p = ensure_model(model_path, allow_download)
            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(p), num_threads=1, provider="cpu")
            _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
            error = ""
        except Exception as exc:
            error = f"Speaker recognition is unavailable ({exc})"
            raise RuntimeError(error) from exc


def embed(audio: np.ndarray) -> np.ndarray:
    if _embed is not None:
        return _embed(audio)
    with _lock:
        s = _extractor.create_stream()
        s.accept_waveform(sample_rate=SR, waveform=audio.astype(np.float32))
        s.input_finished()
        e = np.array(_extractor.compute(s), dtype=np.float32)
    return e / (np.linalg.norm(e) + 1e-9)


class SpeakerBook:
    """The voices heard so far on one channel of one session."""

    def __init__(self, threshold: float = 0.62, max_speakers: int = 8, min_new_seconds: float = 1.2, min_seconds: float = 0.7):
        self.threshold, self.max_speakers = threshold, max_speakers
        self.min_new, self.min_seconds = min_new_seconds, min_seconds
        self.centroids: list[np.ndarray] = []
        self.counts: list[int] = []
        self.last = 0
        self._lock = threading.Lock()

    def assign(self, audio: np.ndarray) -> int:
        seconds = len(audio) / SR
        if seconds < self.min_seconds and self.last:
            return self.last                                   # too short to judge: it is most likely the same person
        e = embed(audio)
        with self._lock:
            if not self.centroids:
                return self._new(e)
            sims = [float(e @ c) for c in self.centroids]
            best = int(np.argmax(sims))
            if sims[best] >= self.threshold:
                self._update(best, e, seconds)
                self.last = best + 1
            elif seconds >= self.min_new and len(self.centroids) < self.max_speakers:
                return self._new(e)
            else:                                              # unsure and short, or too many people: the closest voice
                self.last = best + 1
            return self.last

    def _new(self, e: np.ndarray) -> int:
        self.centroids.append(e)
        self.counts.append(1)
        self.last = len(self.centroids)
        return self.last

    def _update(self, i: int, e: np.ndarray, seconds: float) -> None:
        if seconds < 1.0:
            return                                             # short clips do not move a voice's centre
        n = min(self.counts[i], 20)                            # a cap keeps the centre able to follow a changing voice
        c = self.centroids[i] * n + e
        self.centroids[i] = c / (np.linalg.norm(c) + 1e-9)
        self.counts[i] += 1
