"""End-to-end test of the in-app form filler against the mock portal (ports 5058 app, 5059 portal).

Needs a display and the WebView2 runtime. Starts both servers itself, stops them by PID, and
verifies the filler fills the fields but never presses Submit/Save, never touches the photo question,
and never follows the Withdraw link.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / ".scratch_form"
BASE = "http://127.0.0.1:5058"
PORTAL = "http://127.0.0.1:5059"


def call(path, data=None, base=BASE):
    req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}") | {"_status": e.code}


def wait(cond, timeout=60, what=""):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = cond()
        if v:
            return v
        time.sleep(0.7)
    raise AssertionError("timeout: " + what)


def main():
    shutil.rmtree(SCRATCH, ignore_errors=True)
    SCRATCH.mkdir()
    env = dict(os.environ, PORT="5058", SESSIONS_DIR=str(SCRATCH / "s"), RECORDINGS_DIR=str(SCRATCH / "r"),
               MOCK_ANALYZER="1", MOCK_SCREEN="1", FORM_PROFILE_DIR=str(SCRATCH / "fp"), FILL_TIMEOUT="120")
    portal = subprocess.Popen([sys.executable, str(ROOT / "tests" / "mock_portal.py")], cwd=ROOT)
    app = subprocess.Popen([sys.executable, str(ROOT / "backend" / "server.py")], cwd=ROOT, env=env)
    try:
        wait(lambda: call("/api/config").get("mock"), 30, "app")
        wait(lambda: call("/hits", base=PORTAL).get("hits") is not None, 15, "portal")
        sid = call("/api/start", {"profile": "interview", "subject": "Ravi Kumar"})["sid"]
        wait(lambda: len(call(f"/api/state?sid={sid}")["live"]["lines"]) >= 3, 30, "lines")
        call("/api/end", {"sid": sid})
        wait(lambda: call(f"/api/state?sid={sid}&rev=-1")["detail"]["state"] == "review", 60, "review")
        call("/api/decision", {"sid": sid, "decision": "advance"})
        r = call("/api/fill_form", {"sid": sid, "url": PORTAL + "/list"})
        assert r.get("ok"), r
        def fill():
            st = call(f"/api/state?sid={sid}")["live"]["fill"]
            return st if st and st["state"] in ("done", "error") else None
        st = wait(fill, 120, "fill done")
        chat = call(f"/api/state?sid={sid}&rev=-1")["detail"]["chat"]
        print("fill state:", st["state"], "| last chat:", [m["text"][:90] for m in chat if m["role"] == "assistant"][-1:])
        assert st["state"] == "done", st
        data = call("/hits", base=PORTAL)
        print("portal:", data)
        assert "submit" not in data["hits"] and "save" not in data["hits"] and "withdraw" not in data["hits"], data["hits"]
        v = data["values"]
        assert "photo" not in v, "photo question must not be touched"
        assert v.get("analytical"), "radio not set"
        assert v.get("logic"), "dropdown revealed after the radio was not filled"
        assert v.get("probe") == "Mock answer", "textarea not filled"
        assert v.get("rating"), "hidden <select> behind the custom widget not filled"
        # control channel: change one answer after the fill
        call("/api/chat", {"sid": sid, "text": "x", "mode": "context"})
        print("FORM FILL OK")
    finally:
        for p in (app, portal):
            p.terminate()
        # the form window is a child of the app; give it a moment to close
        time.sleep(1)


if __name__ == "__main__":
    main()
