"""Real audio capture on an isolated server (port 5058, no mock). Speech models may be absent:
the app must keep running, say so, and still finish the session without crashing."""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
S = ROOT / ".scratch_real"
BASE = "http://127.0.0.1:5058"


def call(path, data=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(data).encode() if data is not None else None, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


shutil.rmtree(S, ignore_errors=True)
S.mkdir()
env = dict(os.environ, PORT="5058", SESSIONS_DIR=str(S / "s"), RECORDINGS_DIR=str(S / "r"), MOCK_ANALYZER="0")
env.pop("MOCK_SCREEN", None)
p = subprocess.Popen([sys.executable, str(ROOT / "backend" / "server.py")], cwd=ROOT, env=env)
try:
    for _ in range(40):
        try:
            call("/api/config")
            break
        except Exception:
            time.sleep(0.5)
    sid = call("/api/start", {"profile": "meeting", "subject": "Real capture"})["sid"]
    time.sleep(6)
    st = call(f"/api/state?sid={sid}&first=1")
    cap = st["live"]["capture"]
    print("sources:", cap["sources"])
    print("capture error:", cap["error"], "| models:", cap["models"], "| errors:", cap["errors"])
    rr = call("/api/record", {"sid": sid, "action": "start", "audio": True, "video": False, "consent": True})
    print("record start:", rr)
    time.sleep(3)
    call("/api/record", {"sid": sid, "action": "stop"})
    time.sleep(2)
    d = call(f"/api/state?sid={sid}&rev=-1")["detail"]
    print("clip:", [(c["state"], c["size_text"]) for c in d["clips"]])
    call("/api/end", {"sid": sid})
    for _ in range(30):
        d = call(f"/api/state?sid={sid}&rev=-1")["detail"]
        if d["state"] == "review":
            break
        time.sleep(1)
    print("end state:", d["state"], "| summary:", (d["final"] or {}).get("summary"), "| error:", d["error"])
finally:
    p.terminate()
