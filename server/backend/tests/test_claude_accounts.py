"""Per-user Claude logins: encrypted storage, the per-user CLI environment, gating and isolation between users."""
import sys
import textwrap

import pytest
from conftest import bearer, state, wait_for

from app.config import get_settings
from app.db import SessionLocal
from app.engine.store import get_store
from app.llm import LLMError, set_llm
from app.llm.mock import MockLLM
from app.models import User
from app.services import vault

TOKEN_A = "sk-ant-oat01-" + "A" * 40
TOKEN_B = "sk-ant-oat01-" + "B" * 40


class PerUserStub(MockLLM):
    """Behaves like the cli provider in per_user mode: needs a connected account, rejects tokens that start with 'bad'."""
    needs_user_credentials = True

    def __init__(self):
        super().__init__()
        self.seen: list[dict] = []

    def structured(self, system, prompt, schema, model, images=None, auth=None):
        if not auth:
            raise LLMError("Connect your Claude account first")
        if auth.startswith("bad"):
            raise LLMError("rejected")
        self.seen.append({"auth": auth, "prompt": prompt})
        return super().structured(system, prompt, schema, model, images, auth)


@pytest.fixture()
def per_user_llm(monkeypatch):
    """A real (non-mock) analysis path whose model calls are recorded together with the token that made them."""
    monkeypatch.setattr(get_settings(), "llm_provider", "stub")
    stub = PerUserStub()
    set_llm(stub)
    yield stub
    set_llm(None)


def add_lines(client, headers, sid, texts):
    uid = client.get("/api/auth/me", headers=headers).json()["id"]
    s = get_store(uid).sessions[sid]
    for i, text in enumerate(texts):
        s.add_line("other", text, i)


def test_public_config_and_status_with_mock(client, admin):
    cfg = client.get("/api/config").json()
    assert cfg["registration_open"] is True and cfg["per_user_claude"] is False
    assert client.get("/api/claude", headers=admin).json()["required"] is False
    client.cookies.clear()
    assert client.get("/api/claude").status_code == 401


def test_token_is_stored_encrypted_and_never_returned(client):
    h = bearer(client, "vault@example.com")
    r = client.put("/api/claude", headers=h, json={"token": TOKEN_A})
    assert r.status_code == 200 and r.json()["connected"] is True
    assert TOKEN_A not in r.text and TOKEN_A not in client.get("/api/claude", headers=h).text
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == "vault@example.com").one()
        assert u.claude_token_enc and TOKEN_A not in u.claude_token_enc
        assert vault.decrypt(u.claude_token_enc) == TOKEN_A and vault.token_for(u) == TOKEN_A
        u.claude_token_enc = "not-a-valid-ciphertext"                      # e.g. the encryption key changed
        db.commit()
        assert vault.token_for(u) is None
    finally:
        db.close()
    assert client.get("/api/claude", headers=h).json()["connected"] is False
    client.put("/api/claude", headers=h, json={"token": TOKEN_A})
    assert client.delete("/api/claude", headers=h).status_code == 204
    assert client.get("/api/claude", headers=h).json()["connected"] is False
    assert client.put("/api/claude", headers=h, json={"token": "short"}).status_code == 422


def test_session_requires_connection_and_bad_token_is_rejected(client, per_user_llm):
    h = bearer(client, "gate@example.com")
    body = {"profile": "meeting", "subject": "gate"}
    assert client.get("/api/claude", headers=h).json()["required"] is True
    assert client.post("/api/start", headers=h, json=body).status_code == 409
    bad = client.put("/api/claude", headers=h, json={"token": "bad" + "x" * 30})
    assert bad.status_code == 400 and "did not accept" in bad.json()["detail"]
    assert client.post("/api/start", headers=h, json=body).status_code == 409
    assert client.put("/api/claude", headers=h, json={"token": TOKEN_A}).status_code == 200
    r = client.post("/api/start", headers=h, json=body)
    assert r.status_code == 200
    client.post("/api/end", headers=h, json={"sid": r.json()["sid"]})


def test_every_user_is_analysed_with_their_own_login(client, per_user_llm):
    ha, hb = bearer(client, "alice@example.com"), bearer(client, "bob@example.com")
    assert client.put("/api/claude", headers=ha, json={"token": TOKEN_A}).status_code == 200
    assert client.put("/api/claude", headers=hb, json={"token": TOKEN_B}).status_code == 200

    def run_call(name, h):
        sid = client.post("/api/start", headers=h, json={"profile": "meeting", "subject": name}).json()["sid"]
        add_lines(client, h, sid, [f"hello from {name}", f"{name} says more", f"{name} again", f"{name} last"])
        client.post("/api/analyze", headers=h, json={"sid": sid})
        wait_for(lambda: state(client, h, sid)["detail"]["analysis"])
        client.post("/api/end", headers=h, json={"sid": sid})
        wait_for(lambda: state(client, h, sid)["detail"]["state"] == "review")
        return sid

    sids = {"alice-call": run_call("alice-call", ha), "bob-call": run_call("bob-call", hb)}
    real = [c for c in per_user_llm.seen if c["prompt"] != "ping"]
    assert real and all(("alice-call" in c["prompt"]) == (c["auth"] == TOKEN_A) for c in real)
    assert all(("bob-call" in c["prompt"]) == (c["auth"] == TOKEN_B) for c in real)
    assert {c["auth"] for c in real} == {TOKEN_A, TOKEN_B}
    assert state(client, ha, sids["bob-call"]).get("detail") is None                     # users cannot see each other's calls


def test_a_user_without_a_login_gets_a_clear_analysis_error(client, per_user_llm):
    h = bearer(client, "nologin@example.com")
    assert client.put("/api/claude", headers=h, json={"token": TOKEN_A}).status_code == 200
    sid = client.post("/api/start", headers=h, json={"profile": "meeting", "subject": "x"}).json()["sid"]
    client.delete("/api/claude", headers=h)                                               # disconnected mid-call
    add_lines(client, h, sid, ["hello", "there", "again", "more"])
    client.post("/api/analyze", headers=h, json={"sid": sid})
    st = wait_for(lambda: (lambda s: s if s["live"]["analysis_error"] else None)(state(client, h, sid)))
    assert "Connect your Claude account" in st["live"]["analysis_error"]
    client.post("/api/end", headers=h, json={"sid": sid})


# ---- the real CLI provider, driven against a fake `claude` executable ----------------------------------------
FAKE_CLI = textwrap.dedent('''
    import json, os, sys
    sys.stdin.read()
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if token.startswith("bad"):
        sys.stderr.write("Invalid OAuth token")
        sys.exit(1)
    print(json.dumps({"is_error": False, "structured_output": {
        "token": token, "config_dir": os.environ.get("CLAUDE_CONFIG_DIR", ""),
        "api_key_visible": "ANTHROPIC_API_KEY" in os.environ}}))
''')


@pytest.fixture()
def cli(tmp_path, monkeypatch):
    from app.llm.cli import ClaudeCLI
    fake = tmp_path / "fake_claude.py"
    fake.write_text(FAKE_CLI)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-must-not-leak")
    c = ClaudeCLI()
    c.prefix = [sys.executable, str(fake)]
    return c


def test_cli_runs_each_call_with_that_users_token_and_private_config(cli):
    a = cli.structured("s", "p", {}, "sonnet", auth=TOKEN_A)
    b = cli.structured("s", "p", {}, "sonnet", auth=TOKEN_B)
    assert a["token"] == TOKEN_A and b["token"] == TOKEN_B
    assert a["config_dir"] != b["config_dir"] and a["config_dir"] and b["config_dir"]
    assert a["config_dir"] == cli.structured("s", "p", {}, "sonnet", auth=TOKEN_A)["config_dir"]      # stable per user
    assert a["api_key_visible"] is False                                                                # API key stripped


def test_cli_refuses_to_run_without_a_users_own_token(cli):
    with pytest.raises(LLMError, match="Connect your Claude account"):
        cli.structured("s", "p", {}, "sonnet")


def test_cli_rejected_token_gives_a_reconnect_message(cli):
    with pytest.raises(LLMError, match="claude setup-token"):
        cli.structured("s", "p", {}, "sonnet", auth="bad-token-" + "z" * 20)


@pytest.mark.parametrize("key", ["# optional Fernet key, comment text copied from a .env line", "any passphrase works", "   "])
def test_vault_survives_a_bad_encryption_key(monkeypatch, key):
    monkeypatch.setattr(get_settings(), "encryption_key", key)
    assert vault.decrypt(vault.encrypt(TOKEN_A)) == TOKEN_A
