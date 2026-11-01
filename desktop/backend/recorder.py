"""Audio recording of one clip: stereo Opus in an Ogg file, left = me, right = everyone else.

The file follows the WALL CLOCK: blocks are placed at the time they arrived, so pauses and
"listening off" periods become silence and the recording lines up with the transcript and
the screen video.  Several devices on the same side are summed.
"""
import threading
import time

import numpy as np
import soundfile as sf

SR = 16000


class AudioRecorder:
    def __init__(self, path):
        self.path = path
        self.t0 = time.time()
        self._lock = threading.Lock()
        self._pending = np.zeros((2, 0), dtype="float32")
        self._written = 0
        self._stop = threading.Event()
        self._file = sf.SoundFile(str(path), "w", samplerate=SR, channels=2, format="OGG", subtype="OPUS")
        self._thread = threading.Thread(target=self._writer, daemon=True, name="audio-writer")
        self._thread.start()

    def feed(self, role, label, block):
        """Called from the capture threads for every 0.25 s block."""
        ch = 0 if role == "you" else 1
        n = len(block)
        with self._lock:
            end = int((time.time() - self.t0) * SR)
            start = max(self._written, end - n)
            rel = start - self._written
            need = rel + n
            if need > self._pending.shape[1]:
                grow = np.zeros((2, need - self._pending.shape[1]), dtype="float32")
                self._pending = np.concatenate([self._pending, grow], axis=1)
            seg = self._pending[ch, rel:rel + n]
            self._pending[ch, rel:rel + n] = np.clip(seg + block[:len(seg)], -1.0, 1.0)

    def _flush(self, upto):
        with self._lock:
            count = upto - self._written
            if count <= 0:
                return
            if self._pending.shape[1] < count:
                pad = np.zeros((2, count - self._pending.shape[1]), dtype="float32")
                self._pending = np.concatenate([self._pending, pad], axis=1)
            chunk, self._pending = self._pending[:, :count], self._pending[:, count:]
            self._written = upto
        self._file.write(np.ascontiguousarray(chunk.T))

    def _writer(self):
        while not self._stop.wait(0.5):
            try:
                self._flush(int((time.time() - self.t0) * SR) - SR // 2)
            except Exception:
                pass

    def stop(self):
        self._stop.set()
        self._thread.join(5)
        try:
            self._flush(int((time.time() - self.t0) * SR))
        finally:
            self._file.close()
        return time.time() - self.t0
