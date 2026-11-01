"""Dynamic topic list test (isolated server on port 5060, mock analyzer).

Usage:  desktop\\.venv\\Scripts\\python desktop\\tests\\dynamic_rubric_test.py
Checks: the topic list is built from the role at start, a new topic found during the call is added to the
session's rubric and scored, the list survives a restart, and the default list is used when no role is given.
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
SCRATCH = ROOT / ".scratch_rubric"
PORT = 5060
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


def detail(sid):
    return call(f"/api/state?sid={sid}&rev=-1").get("detail") or {}


def serve(extra_env=None):
    env = dict(os.environ, PORT=str(PORT), SESSIONS_DIR=str(SCRATCH / "sessions"), RECORDINGS_DIR=str(SCRATCH / "rec"),
               MOCK_ANALYZER="1", MOCK_SCREEN="1", ANALYZE_INTERVAL="3", ANALYZE_MIN_GAP="2",
               FORM_PROFILE_DIR=str(SCRATCH / "formprofile"), HF_HUB_OFFLINE="1", **(extra_env or {}))
    proc = subprocess.Popen([sys.executable, str(ROOT / "backend" / "server.py")], cwd=ROOT, env=env)
    wait(lambda: call("/api/config").get("mock"), 30, "server")
    return proc


def main():
    shutil.rmtree(SCRATCH, ignore_errors=True)
    SCRATCH.mkdir()
    proc = serve()
    try:
        sid = call("/api/start", {"profile": "interview", "subject": "Dee", "role": "Golang backend engineer", "extra": "Mid (3-6 yrs)"})["sid"]
        d = wait(lambda: (lambda x: x if x.get("rubric") and not x["rubric"]["building"] else None)(detail(sid)), 20, "rubric build")
        names = [a["name"] for a in d["rubric"]["areas"]]
        assert d["rubric"]["role"] == "Golang backend engineer" and "Golang" in names and "Java" not in " ".join(names), names
        print("built topics:", names)
        # the demo call plays lines; after >= 6 lines the mock analyzer reports an unrelated new topic
        d = wait(lambda: (lambda x: x if any(a.get("dynamic") for a in x.get("rubric", {}).get("areas", [])) else None)(detail(sid)), 90, "dynamic topic")
        extra = [a for a in d["rubric"]["areas"] if a.get("dynamic")]
        assert extra[0]["name"] == "Kubernetes" and extra[0]["off_role"], extra
        assert any(a["id"] == "kubernetes" and a["score"] == 4 for a in d["analysis"]["areas"]), d["analysis"]["areas"]
        call("/api/end", {"sid": sid})
        d = wait(lambda: (lambda x: x if x.get("final") else None)(detail(sid)), 60, "final")
        assert any(a["id"] == "kubernetes" for a in d["final"]["areas"]), "scored in final"
        assert d["score"]["total"] == len(d["rubric"]["areas"]) and d["score"]["score"], d["score"]
        print("final score:", d["score"])
        # no role -> the profile's default list, nothing is built
        call("/api/decision", {"sid": sid, "decision": "advance"})
        sid2 = call("/api/start", {"profile": "interview", "subject": "Default"})["sid"]
        d2 = wait(lambda: detail(sid2) or None, 10, "second session")
        assert len(d2["rubric"]["areas"]) == 15 and not d2["rubric"]["custom"], d2["rubric"]
        call("/api/end", {"sid": sid2})
        wait(lambda: detail(sid2).get("final"), 60, "final 2")
    finally:
        pass
    # restart: the saved session keeps its own rubric, including the added topic
    proc.terminate()
    proc.wait(10)
    proc = serve()
    try:
        d = detail(sid)
        assert d["rubric"]["role"] == "Golang backend engineer" and any(a.get("dynamic") for a in d["rubric"]["areas"]), d["rubric"]
        print("rubric survives restart")
    finally:
        proc.terminate()
        proc.wait(10)
        shutil.rmtree(SCRATCH, ignore_errors=True)
    print("DYNAMIC RUBRIC OK")


if __name__ == "__main__":
    main()
