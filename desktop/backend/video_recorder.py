"""Screen video recording of one clip (H.264 via the ffmpeg bundled with imageio-ffmpeg).

While recording the video is a *fragmented* MP4 so a crash still leaves a playable file; when the
clip stops it is muxed with the audio into a normal MP4 (faststart).  Pauses and "screen off" are
shown with placeholder frames so the timeline stays aligned with the audio.  The placeholder
texts contain no colons because ffmpeg's drawtext filter breaks on them.
"""
import subprocess
import threading
import time

import numpy as np

import windows_capture as wc

PAUSED_TEXT = "Session paused - screen not recorded"
OFF_TEXT = "Screen recording is switched off"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def ffmpeg_exe():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _placeholder(text, w, h):
    """Render a text frame to raw BGRA bytes with ffmpeg's drawtext."""
    font = "C\\:/Windows/Fonts/segoeui.ttf"
    vf = (f"drawtext=fontfile='{font}':text='{text}':fontcolor=white:fontsize={max(14, h // 18)}"
          f":x=(w-text_w)/2:y=(h-text_h)/2")
    cmd = [ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i", f"color=c=0x262624:s={w}x{h}", "-vf", vf,
           "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "bgra", "-"]
    try:
        data = subprocess.run(cmd, capture_output=True, timeout=30, creationflags=NO_WINDOW).stdout
        if len(data) == w * h * 4:
            return data
    except Exception:
        pass
    return bytes([38, 38, 36, 255]) * (w * h)  # plain dark frame if drawtext is unavailable


def _fit(arr, w, h):
    """Resize any BGRA frame to exactly w x h (nearest neighbour) so ffmpeg sees a constant size."""
    ah, aw = arr.shape[:2]
    if (ah, aw) == (h, w):
        return arr
    ys = (np.arange(h) * ah / h).astype(int)
    xs = (np.arange(w) * aw / w).astype(int)
    return arr[ys][:, xs]


class VideoRecorder:
    def __init__(self, path, get_source, fps=5, max_width=1280, crf=30):
        self.path = path                      # final .mp4
        self.tmp = path.with_suffix(".part.mp4")
        self.get_source, self.fps, self.crf = get_source, max(1, fps), crf
        self.mode = "record"                  # record | paused | off
        self.error = ""
        self._stop = threading.Event()
        first = None
        try:
            first = wc.grab(get_source())
        except Exception:
            pass
        if first is None:
            first = np.zeros((720, 1280, 4), dtype=np.uint8)
        h, w = first.shape[:2]
        if w > max_width:
            h, w = int(h * max_width / w), max_width
        self.w, self.h = w - w % 2, h - h % 2
        self._ph = {"paused": _placeholder(PAUSED_TEXT, self.w, self.h),
                    "off": _placeholder(OFF_TEXT, self.w, self.h)}
        self._last = first
        cmd = [ffmpeg_exe(), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{self.w}x{self.h}",
               "-framerate", str(self.fps), "-i", "-", "-an", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", str(self.crf), "-pix_fmt", "yuv420p",
               "-movflags", "frag_keyframe+empty_moov+default_base_moof", str(self.tmp)]
        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        self.t0 = time.time()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="video-rec")
        self._thread.start()

    def _frame_bytes(self):
        if self.mode in self._ph:
            return self._ph[self.mode]
        try:
            arr = wc.grab(self.get_source())
            if arr is not None:
                self._last = arr
        except Exception:
            pass
        return np.ascontiguousarray(_fit(self._last, self.w, self.h)).tobytes()

    def _loop(self):
        """One frame per 1/fps seconds on the wall clock (drops/duplicates to stay in sync)."""
        n = 0
        while not self._stop.is_set():
            due = self.t0 + n / self.fps
            wait = due - time.time()
            if wait > 0:
                time.sleep(wait)
            if time.time() - due > 2:  # fell far behind: skip ahead rather than drift
                n = int((time.time() - self.t0) * self.fps)
            try:
                self._proc.stdin.write(self._frame_bytes())
            except Exception as exc:
                self.error = f"ffmpeg stopped: {exc}"
                break
            n += 1

    def set_mode(self, mode):
        self.mode = mode

    def stop(self, audio_path=None):
        """Finish the video and mux it with the audio. Returns the duration in seconds."""
        self._stop.set()
        self._thread.join(10)
        try:
            self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(30)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        duration = time.time() - self.t0
        cmd = [ffmpeg_exe(), "-y", "-v", "error", "-i", str(self.tmp)]
        if audio_path is not None and audio_path.exists():
            cmd += ["-i", str(audio_path), "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "64k"]
        else:
            cmd += ["-c", "copy"]
        cmd += ["-movflags", "+faststart", str(self.path)]
        res = subprocess.run(cmd, capture_output=True, timeout=300, creationflags=NO_WINDOW)
        if res.returncode == 0 and self.path.exists():
            try:
                self.tmp.unlink()
            except OSError:
                pass
        else:  # keep the fragmented file as the result rather than losing the video
            self.error = res.stderr.decode("utf-8", "replace")[:200]
            try:
                self.tmp.replace(self.path)
            except OSError:
                pass
        return duration
