"""Window list and screen/window capture for Windows, using ctypes and mss.

* Every open application window is listed (even one behind other windows, even minimised ones).
* Windows are captured with PrintWindow(PW_RENDERFULLCONTENT) so what is behind them does not matter.
* Minimised windows are restored without stealing focus (SW_SHOWNOACTIVATE) when picked or captured.
* Per-monitor DPI awareness is set at import so pixel sizes are real.
* MOCK_SCREEN=1 returns synthetic frames so the screen features can be tested without a display.
"""
import ctypes
import os
import struct
import time
import zlib
from ctypes import wintypes

import numpy as np

MOCK = os.environ.get("MOCK_SCREEN", "0") == "1"
OWN_TITLES = ("Debrief AI", "Call Copilot")
PW_RENDERFULLCONTENT = 2

try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
dwm = ctypes.windll.dwmapi
kernel32 = ctypes.windll.kernel32

FRIENDLY = {
    "ms-teams": "Microsoft Teams", "teams": "Microsoft Teams", "zoom": "Zoom", "chrome": "Google Chrome",
    "msedge": "Microsoft Edge", "firefox": "Firefox", "slack": "Slack", "code": "Visual Studio Code",
    "idea64": "IntelliJ IDEA", "eclipse": "Eclipse", "notepad": "Notepad", "explorer": "File Explorer",
    "winword": "Word", "excel": "Excel", "powerpnt": "PowerPoint", "outlook": "Outlook", "onenote": "OneNote",
    "windowsterminal": "Windows Terminal", "cmd": "Command Prompt", "powershell": "PowerShell",
    "webex": "Webex", "ciscocollabhost": "Webex", "discord": "Discord", "brave": "Brave",
}


# --------------------------------------------------------------------------- PNG
def encode_png(rgb):
    """Encode an (h, w, 3) uint8 array as PNG without needing Pillow."""
    rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def resize(arr, width):
    """Nearest-neighbour resize to a given width (keeps aspect). Works on (h, w, c) arrays."""
    h, w = arr.shape[:2]
    if w <= width:
        return arr
    nh = max(2, int(h * width / w))
    ys = (np.arange(nh) * h / nh).astype(int)
    xs = (np.arange(width) * w / width).astype(int)
    return arr[ys][:, xs]


def bgra_to_rgb(arr):
    return np.ascontiguousarray(arr[:, :, 2::-1])


# --------------------------------------------------------------------------- window list
def _exe_name(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = wintypes.DWORD(520)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.splitext(os.path.basename(buf.value))[0].lower()
    finally:
        kernel32.CloseHandle(h)
    return ""


def _cloaked(hwnd):
    val = wintypes.DWORD()
    dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(val), ctypes.sizeof(val))  # DWMWA_CLOAKED
    return bool(val.value)


def list_windows(limit=60):
    """Open top-level windows: [{hwnd, title, app, minimized}], biggest/most relevant first."""
    if MOCK:
        return [{"hwnd": 1001, "title": "Mock editor - Main.java", "app": "Visual Studio Code", "minimized": False},
                {"hwnd": 1002, "title": "Mock meeting", "app": "Microsoft Teams", "minimized": True}]
    out = []
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        try:
            if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):  # owned windows
                return True
            ex = user32.GetWindowLongW(hwnd, -20)
            if ex & 0x80:  # WS_EX_TOOLWINDOW
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n == 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value.strip()
            if not title:
                return True
            if _cloaked(hwnd):
                return True
            if any(t in title for t in OWN_TITLES):
                return True
            exe = _exe_name(hwnd)
            if exe in ("textinputhost", "searchhost", "shellexperiencehost", "startmenuexperiencehost"):
                return True
            out.append({"hwnd": int(hwnd), "title": title, "app": FRIENDLY.get(exe, exe.title() or "App"),
                        "minimized": bool(user32.IsIconic(hwnd))})
        except Exception:
            pass
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return out[:limit]


def list_screens():
    if MOCK:
        return [{"index": 1, "name": "Screen 1 (mock)", "width": 1280, "height": 720, "primary": True}]
    import mss
    with mss.mss() as sct:
        mons = sct.monitors[1:]
        return [{"index": i + 1, "name": f"Screen {i + 1}" + (" (primary)" if i == 0 else ""),
                 "width": m["width"], "height": m["height"], "primary": i == 0} for i, m in enumerate(mons)]


# --------------------------------------------------------------------------- capture
def restore_window(hwnd):
    """Un-minimise without activating (no focus stealing)."""
    if MOCK:
        return True
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
        time.sleep(0.4)
        return True
    return False


class _BMIH(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def _window_size(hwnd):
    r = wintypes.RECT()
    if dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:  # EXTENDED_FRAME_BOUNDS
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.right - r.left, r.bottom - r.top


def grab_window(hwnd):
    """Return a BGRA array of the window, or None if it no longer exists."""
    if MOCK:
        return fake_frame()
    if not user32.IsWindow(hwnd):
        return None
    restore_window(hwnd)
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return None
    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mdc, bmp)
    try:
        user32.PrintWindow(hwnd, mdc, PW_RENDERFULLCONTENT)
        bmi = _BMIH(ctypes.sizeof(_BMIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4).copy()
    finally:
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, hdc)
    # crop away the invisible resize border: use the DWM frame bounds
    fw, fh = _window_size(hwnd)
    if 0 < fw <= w and 0 < fh <= h:
        dx, dy = (w - fw) // 2, 0
        arr = arr[dy:dy + fh, dx:dx + fw]
    return arr


def grab_screen(index=1):
    if MOCK:
        return fake_frame()
    import mss
    with mss.mss() as sct:
        mons = sct.monitors
        mon = mons[index] if 0 < index < len(mons) else mons[1]
        return np.asarray(sct.grab(mon))  # BGRA


def grab(source):
    """source: {"kind": "screen", "index": n} | {"kind": "window", "hwnd": n} | None (primary screen)."""
    if not source or source.get("kind") == "screen":
        return grab_screen((source or {}).get("index", 1))
    return grab_window(int(source["hwnd"]))


def source_alive(source):
    if MOCK or not source or source.get("kind") == "screen":
        return True
    return bool(user32.IsWindow(int(source["hwnd"])))


def fake_frame():
    """Synthetic frame that changes in 6-second steps (tests the stable/changed logic)."""
    step = int(time.time() // 6)
    rng = np.random.default_rng(step)
    arr = np.full((360, 640, 4), 40, dtype=np.uint8)
    arr[:, :, 3] = 255
    for i in range(6):
        y = 30 + i * 50
        arr[y:y + 20, 40:40 + int(rng.integers(100, 500)), :3] = 220
    return arr


def thumb_png(source, width=320):
    arr = grab(source)
    if arr is None:
        return None
    return encode_png(resize(bgra_to_rgb(arr), width))


def frame_png(source, width=1600):
    arr = grab(source)
    if arr is None:
        return None
    return encode_png(resize(bgra_to_rgb(arr), width))
