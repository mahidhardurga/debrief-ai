"""Backend smoke test against an ISOLATED server (port 5058, .scratch data, mock analyzer).

Usage:  .venv\\Scripts\\python tests\\backend_smoke.py
It starts its own server process, drives the API, checks results and stops the server by PID.
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
SCRATCH = ROOT / ".scratch"
PORT = 5058
BASE = f"http://127.0.0.1:{PORT}"


def call(path, data=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(data).encode() if data is not None else None,
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
        time.sleep(0.5)
    raise AssertionError(f"timeout waiting for {what}")


def main():
    shutil.rmtree(SCRATCH, ignore_errors=True)
    SCRATCH.mkdir()
    env = dict(os.environ, PORT=str(PORT), SESSIONS_DIR=str(SCRATCH / "sessions"), RECORDINGS_DIR=str(SCRATCH / "rec"),
               MOCK_ANALYZER="1", MOCK_SCREEN="1", ANALYZE_INTERVAL="3", ANALYZE_MIN_GAP="2",
               FORM_PROFILE_DIR=str(SCRATCH / "formprofile"), HF_HUB_OFFLINE="1")
    proc = subprocess.Popen([sys.executable, str(ROOT / "backend" / "server.py")], cwd=ROOT, env=env)
    try:
        wait(lambda: call("/api/config").get("mock"), 30, "server")
        profs = call("/api/profiles")
        assert {p["id"] for p in profs} == {"interview", "meeting"}, profs
        r = call("/api/start", {"profile": "interview", "subject": "Test Candidate", "extra": "Mid (3-6 yrs)"})
        sid = r["sid"]
        print("started", sid)
        r2 = call("/api/start", {"profile": "meeting"})
        assert r2["_status"] == 409, r2
        st = wait(lambda: (lambda s: s if len(s["live"]["lines"]) >= 4 else None)(call(f"/api/state?sid={sid}&first=1")), 40, "lines")
        print("lines:", len(st["live"]["lines"]))
        wait(lambda: call(f"/api/state?sid={sid}")["detail"]["analysis"] if "detail" in call(f"/api/state?sid={sid}&rev=-1") else None, 40, "analysis")
        call("/api/notes", {"sid": sid, "text": "he seems nervous"})
        call("/api/chat", {"sid": sid, "text": "he has 5 years experience", "mode": "context"})
        call("/api/chat", {"sid": sid, "text": "How is the candidate doing?", "mode": "ask"})
        # exhibit via mock screen
        cap = call("/api/capture_screen", {"sid": sid, "source": {"kind": "screen", "index": 1, "label": "Screen 1"}})
        assert cap.get("token"), cap
        call("/api/exhibit", {"sid": sid, "token": cap["token"], "question": "Reverse a string", "note": "used a loop"})
        # recording (mock screen frames + silence audio)
        rr = call("/api/record", {"sid": sid, "action": "start", "audio": True, "video": True})
        assert rr.get("need_consent"), rr
        rr = call("/api/record", {"sid": sid, "action": "start", "audio": True, "video": True, "consent": True})
        assert rr.get("ok"), rr
        time.sleep(4)
        call("/api/pause", {"sid": sid, "on": True})
        time.sleep(2)
        call("/api/pause", {"sid": sid, "on": False})
        call("/api/record", {"sid": sid, "action": "stop"})
        d = wait(lambda: (lambda s: s["detail"] if s["detail"]["clips"] and s["detail"]["clips"][0]["state"] == "saved" else None)(call(f"/api/state?sid={sid}&rev=-1")), 90, "clip saved")
        print("clip:", d["clips"][0]["size_text"], d["clips"][0]["duration_text"])
        # end -> review
        call("/api/end", {"sid": sid})
        d = wait(lambda: (lambda s: s["detail"] if s["detail"]["state"] == "review" else None)(call(f"/api/state?sid={sid}&rev=-1")), 90, "review")
        assert d["final"] and d["final"]["summary"], d
        assert d["form_data"], "interview should produce the evaluation form answers"
        print("score", d["score"])
        call("/api/decision", {"sid": sid, "decision": "advance"})
        d = call(f"/api/state?sid={sid}&rev=-1")["detail"]
        assert d["state"] == "decided" and d["feedback"]["state"] == "pending", d["feedback"]
        files = set(d["files"])
        print(sorted(files))
        assert {"report.md", "result.json", "evaluation.json", "topic-wise-feedback.md", "next-round-handoff.md", "evaluation-form.md"} <= files, files
        assert "rejection-feedback.md" not in files
        call("/api/decision", {"sid": sid, "decision": "reject"})
        files = set(call(f"/api/state?sid={sid}&rev=-1")["detail"]["files"])
        assert "rejection-feedback.md" in files and "next-round-handoff.md" not in files, files
        # clip serving: range request
        req = urllib.request.Request(f"{BASE}/api/clip/{sid}/1/video", headers={"Range": "bytes=0-99"})
        with urllib.request.urlopen(req) as r:
            assert r.status == 206 and len(r.read()) == 100
        with urllib.request.urlopen(f"{BASE}/api/export/{sid}") as r:
            assert r.read(2) == b"PK"
        call("/api/feedback", {"sid": sid, "state": "submitted"})
        assert call(f"/api/state?sid={sid}&rev=-1")["detail"]["feedback"]["state"] == "submitted"
        # recordings delete
        call("/api/recording_delete", {"sid": sid, "all": True})
        assert not call(f"/api/state?sid={sid}&rev=-1")["detail"]["clips"]
        # meeting profile
        r = call("/api/start", {"profile": "meeting", "subject": "Sync"})
        sid2 = r["sid"]
        wait(lambda: len(call(f"/api/state?sid={sid2}")["live"]["lines"]) >= 3, 40, "meeting lines")
        call("/api/end", {"sid": sid2})
        wait(lambda: call(f"/api/state?sid={sid2}&rev=-1")["detail"]["state"] == "review", 60, "meeting review")
        call("/api/finish", {"sid": sid2})
        d2 = call(f"/api/state?sid={sid2}&rev=-1")["detail"]
        assert d2["state"] == "decided" and "recap-email.md" in d2["files"], d2["files"]
        today = list((SCRATCH / "sessions").glob("day-summary-*.csv"))
        assert today, "day summary missing"
        print("BACKEND SMOKE OK")
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
