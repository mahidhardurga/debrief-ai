"""Keeps the AI tokens the user pasted (a Claude token from `claude setup-token`, a GitHub token for Copilot) in
desktop/secrets.json. On Windows each value is encrypted with the Windows login of this user (DPAPI), so the file is of
no use on another PC or account; elsewhere it is stored as is (the file is git-ignored). Never sent to the browser."""
import base64
import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path

FILE = Path(__file__).resolve().parents[1] / "secrets.json"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _protect(raw: bytes, encrypt: bool) -> bytes:
    inp, keep = _blob(raw)
    out = _Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if encrypt else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("Windows could not protect the token")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)
        del keep


def _enc(token: str) -> str:
    if os.name == "nt":
        return "dpapi:" + base64.b64encode(_protect(token.encode(), True)).decode()
    return "plain:" + token


def _dec(value: str):
    try:
        if value.startswith("dpapi:") and os.name == "nt":
            return _protect(base64.b64decode(value[6:]), False).decode()
        if value.startswith("plain:"):
            return value[6:]
    except (OSError, ValueError):
        pass
    return None


def _all() -> dict:
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(provider: str, token: str) -> None:
    data = _all()
    data[provider] = _enc(token)
    FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load(provider: str):
    v = _all().get(provider)
    return _dec(v) if v else None


def clear(provider: str) -> None:
    data = _all()
    if data.pop(provider, None) is not None:
        FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
