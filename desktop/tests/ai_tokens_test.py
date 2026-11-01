"""Pasted AI tokens, desktop edition: stored encrypted, used instead of the CLI's own login, tested before they are saved.

Usage:  desktop\\.venv\\Scripts\\python desktop\\tests\\ai_tokens_test.py
Uses fake `claude` / `copilot` programs, so it needs no login and no network.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="dbf-tok-"))
os.environ.update(SESSIONS_DIR=str(SCRATCH / "sessions"), RECORDINGS_DIR=str(SCRATCH / "rec"), MOCK_ANALYZER="0", HF_HUB_OFFLINE="1")
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT.parent / "shared" / "core"))

import analyzer  # noqa: E402
import config  # noqa: E402
import secrets_store  # noqa: E402

secrets_store.FILE = SCRATCH / "secrets.json"
config.SETTINGS_FILE = SCRATCH / "settings.json"
CLAUDE_TOKEN = "sk-ant-oat01-" + "A" * 40
GH_TOKEN = "github_pat_" + "B" * 40


def fake(name, body):
    py = SCRATCH / f"{name}.py"
    py.write_text(body)
    exe = SCRATCH / (name + (".cmd" if os.name == "nt" else ""))
    exe.write_text(f'@"{sys.executable}" "{py}" %*\r\n' if os.name == "nt" else f'#!/bin/sh\nexec "{sys.executable}" "{py}" "$@"\n')
    exe.chmod(0o755)
    return str(exe)


FAKE_CLAUDE = '''
import json, os, sys
sys.stdin.read()
tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
if tok.startswith("bad"):
    sys.stderr.write("Invalid OAuth token"); sys.exit(1)
print(json.dumps({"is_error": False, "structured_output": {"ok": True, "token_seen": tok}}))
'''
FAKE_COPILOT = '''
import json, os, sys
sys.stdin.read()
tok = os.environ.get("COPILOT_GITHUB_TOKEN", "")
if tok.startswith("bad"):
    sys.stderr.write("Bad credentials 401"); sys.exit(1)
print(json.dumps({"ok": True, "token_seen": tok}))
'''
analyzer._EXE = fake("claude", FAKE_CLAUDE)
config.COPILOT_CLI_PATH = fake("copilot", FAKE_COPILOT)

# 1. the store round-trips, and on Windows the file does not contain the token
secrets_store.save("claude", CLAUDE_TOKEN)
assert secrets_store.load("claude") == CLAUDE_TOKEN and secrets_store.load("copilot") is None
if os.name == "nt":
    assert CLAUDE_TOKEN not in secrets_store.FILE.read_text(), "token must be encrypted at rest"
secrets_store.clear("claude")
assert secrets_store.load("claude") is None

import server  # noqa: E402

c = server.app.test_client()

# 2. status: nothing pasted yet
st = c.get("/api/ai").get_json()
assert st["tokens"] is True and st["providers"]["claude"]["token"] is False and st["providers"]["claude"]["found"] is True

# 3. a bad token is refused and not saved; a good one is saved and used by the next call
r = c.put("/api/claude", json={"token": "bad" + "x" * 30})
assert r.status_code == 400 and "did not accept" in r.get_json()["detail"] and secrets_store.load("claude") is None
r = c.put("/api/claude", json={"token": "short"})
assert r.status_code == 422
r = c.put("/api/claude", json={"token": CLAUDE_TOKEN})
assert r.status_code == 200 and r.get_json()["providers"]["claude"]["token"] is True
analyzer.set_provider("claude")
out = analyzer._call("hi", "sys", {"type": "object"}, "sonnet")
assert out["token_seen"] == CLAUDE_TOKEN, out

# 4. the same for Copilot
analyzer.set_provider("copilot")
assert c.put("/api/copilot", json={"token": "bad" + "y" * 30}).status_code == 400
assert c.put("/api/copilot", json={"token": GH_TOKEN}).status_code == 200
assert analyzer._call("hi", "sys", {"type": "object", "required": ["ok"]}, "sonnet")["token_seen"] == GH_TOKEN

# 5. removing a token goes back to the CLI's own login
assert c.delete("/api/claude").status_code == 204
analyzer.set_provider("claude")
assert analyzer._call("hi", "sys", {"type": "object"}, "sonnet")["token_seen"] == ""
print("AI TOKENS OK")
