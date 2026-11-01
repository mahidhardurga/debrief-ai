"""GitHub Copilot as the second model: each user picks Claude or Copilot, with their own token for it."""
import json
import os
import stat
import sys
import textwrap

import pytest
from conftest import bearer, state, wait_for
from test_claude_accounts import TOKEN_A, PerUserStub, add_lines

from app.config import get_settings
from app.engine import SHARED  # noqa: F401  (puts shared/core on sys.path)
from app.llm import LLMError, set_copilot_llm, set_llm
from app.llm.mock import MockLLM

GH_A = "github_pat_" + "A" * 40
GH_B = "github_pat_" + "B" * 40


class CopilotStub(PerUserStub):
    """Stands in for the Copilot provider: it needs the user's GitHub token and records every call."""


@pytest.fixture()
def both(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "stub")
    claude, copilot = PerUserStub(), CopilotStub()
    set_llm(claude)
    set_copilot_llm(copilot)
    yield claude, copilot
    set_llm(None)
    set_copilot_llm(None)


def test_default_choice_is_claude_and_switching_is_saved(client, both):
    h = bearer(client, "pick@example.com")
    s = client.get("/api/ai", headers=h).json()
    assert s["choice"] == "claude" and s["providers"]["claude"]["required"] and s["providers"]["copilot"]["required"]
    s = client.post("/api/ai", headers=h, json={"choice": "copilot"}).json()
    assert s["choice"] == "copilot" and s["name"] == "GitHub Copilot" and s["connected"] is False
    assert client.get("/api/ai", headers=h).json()["choice"] == "copilot"
    assert client.post("/api/ai", headers=h, json={"choice": "gemini"}).status_code == 422


def test_a_call_needs_the_login_of_the_model_that_was_picked(client, both):
    h = bearer(client, "gate2@example.com")
    body = {"profile": "meeting", "subject": "gate"}
    assert client.put("/api/claude", headers=h, json={"token": TOKEN_A}).status_code == 200
    client.post("/api/ai", headers=h, json={"choice": "copilot"})
    r = client.post("/api/start", headers=h, json=body)
    assert r.status_code == 409 and "GitHub Copilot" in r.json()["detail"]                 # Claude is connected, Copilot is not
    bad = client.put("/api/copilot", headers=h, json={"token": "bad" + "x" * 30})
    assert bad.status_code == 400 and "GitHub Copilot did not accept" in bad.json()["detail"]
    assert client.put("/api/copilot", headers=h, json={"token": GH_A}).status_code == 200
    r = client.post("/api/start", headers=h, json=body)
    assert r.status_code == 200
    client.post("/api/end", headers=h, json={"sid": r.json()["sid"]})


def test_each_user_is_analysed_by_the_model_they_picked(client, both):
    claude, copilot = both
    ha, hb = bearer(client, "ann@example.com"), bearer(client, "ben@example.com")
    client.put("/api/claude", headers=ha, json={"token": TOKEN_A})
    client.put("/api/copilot", headers=hb, json={"token": GH_B})
    client.post("/api/ai", headers=hb, json={"choice": "copilot"})

    def run_call(name, h):
        sid = client.post("/api/start", headers=h, json={"profile": "meeting", "subject": name}).json()["sid"]
        add_lines(client, h, sid, [f"hello from {name}", f"{name} says more", f"{name} again", f"{name} last"])
        client.post("/api/analyze", headers=h, json={"sid": sid})
        wait_for(lambda: state(client, h, sid)["detail"]["analysis"])
        client.post("/api/end", headers=h, json={"sid": sid})
        wait_for(lambda: state(client, h, sid)["detail"]["state"] == "review")

    run_call("ann-call", ha)
    run_call("ben-call", hb)
    real = lambda stub: [c for c in stub.seen if c["prompt"] != "ping"]  # noqa: E731
    assert real(claude) and all("ann-call" in c["prompt"] and c["auth"] == TOKEN_A for c in real(claude))
    assert real(copilot) and all("ben-call" in c["prompt"] and c["auth"] == GH_B for c in real(copilot))


def test_switching_during_a_live_call_applies_to_the_next_analysis(client, both):
    claude, copilot = both
    h = bearer(client, "mid@example.com")
    client.put("/api/claude", headers=h, json={"token": TOKEN_A})
    client.put("/api/copilot", headers=h, json={"token": GH_A})
    sid = client.post("/api/start", headers=h, json={"profile": "meeting", "subject": "mid-call"}).json()["sid"]
    add_lines(client, h, sid, ["one", "two", "three", "four"])
    client.post("/api/ai", headers=h, json={"choice": "copilot"})
    client.post("/api/analyze", headers=h, json={"sid": sid})
    wait_for(lambda: state(client, h, sid)["detail"]["analysis"])
    assert any("mid-call" in c["prompt"] for c in copilot.seen) and not any("mid-call" in c["prompt"] for c in claude.seen)
    client.post("/api/end", headers=h, json={"sid": sid})


def test_mock_mode_needs_no_login_for_either_model(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "mock")
    set_llm(MockLLM())
    h = bearer(client, "mock@example.com")
    client.post("/api/ai", headers=h, json={"choice": "copilot"})
    s = client.get("/api/ai", headers=h).json()
    assert s["choice"] == "copilot" and s["required"] is False
    assert client.post("/api/start", headers=h, json={"profile": "meeting", "subject": "m"}).status_code == 200
    set_llm(None)


# ---- the real provider, driven against a fake `copilot` executable ----------------------------------------------
FAKE = textwrap.dedent('''
    import json, os, sys
    text = sys.stdin.read()
    token = os.environ.get("COPILOT_GITHUB_TOKEN", "")
    if token.startswith("bad"):
        sys.stderr.write("Bad credentials (401)")
        sys.exit(1)
    if "--available-tools" not in sys.argv:
        sys.stderr.write("tools must be switched off")
        sys.exit(1)
    flag = os.environ.get("FAKE_FIRST_REPLY_BAD")
    if flag and not os.path.exists(flag):
        open(flag, "w").write("x")
        print("Sure! Here is the thing, no JSON for you")
    else:
        reply = {"token": token, "home": os.environ.get("COPILOT_HOME", ""), "schema_in_prompt": '"required"' in text,
                 "other_login_visible": "GH_TOKEN" in os.environ, "images": sys.argv.count("--attachment")}
        print("Here you go:\\n```json\\n" + json.dumps(reply) + "\\n```")
''')


@pytest.fixture()
def cli(tmp_path, monkeypatch):
    from app.llm.copilot_cli import CopilotCLI
    py = tmp_path / "fake_copilot.py"
    py.write_text(FAKE)
    if os.name == "nt":
        exe = tmp_path / "fake_copilot.cmd"
        exe.write_text(f'@"{sys.executable}" "{py}" %*\r\n')
    else:
        exe = tmp_path / "fake_copilot"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{py}" "$@"\n')
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("GH_TOKEN", "someone-elses-token")
    c = CopilotCLI()
    c.exe = str(exe)
    return c


SCHEMA = {"type": "object", "properties": {"token": {"type": "string"}}, "required": ["token"]}


def test_each_users_call_gets_their_own_token_and_private_home(cli):
    a = cli.structured("sys", "prompt", SCHEMA, "sonnet", auth=GH_A)
    b = cli.structured("sys", "prompt", SCHEMA, "sonnet", auth=GH_B)
    assert a["token"] == GH_A and b["token"] == GH_B
    assert a["home"] != b["home"] and a["home"] and a["home"] == cli.structured("s", "p", SCHEMA, "x", auth=GH_A)["home"]
    assert a["schema_in_prompt"] is True and a["other_login_visible"] is False             # another login in the environment is not used


def test_images_are_attached_as_files(cli):
    out = cli.structured("s", "p", SCHEMA, "x", images=[b"\x89PNG1", b"\x89PNG2"], auth=GH_A)
    assert out["images"] == 2


def test_no_token_and_bad_token_give_clear_messages(cli):
    with pytest.raises(LLMError, match="Connect your GitHub Copilot token"):
        cli.structured("s", "p", SCHEMA, "x")
    with pytest.raises(LLMError, match="not signed in, or the token was rejected"):
        cli.structured("s", "p", SCHEMA, "x", auth="bad-" + "z" * 20)


def test_a_reply_that_is_not_json_is_repaired_once(cli, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FIRST_REPLY_BAD", str(tmp_path / "flag"))
    assert cli.structured("s", "p", SCHEMA, "x", auth=GH_A)["token"] == GH_A


def test_json_is_found_inside_chatty_replies():
    import copilot
    assert copilot.json_from_text('Sure!\n```json\n{"a": {"b": "}"}}\n```\nDone') == {"a": {"b": "}"}}
    assert copilot.json_from_text("no json here") is None
    assert json.loads(json.dumps(copilot.json_from_text('x {"n": 1} y {"n": 2}'))) == {"n": 1}
