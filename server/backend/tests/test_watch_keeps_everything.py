"""Screen watching keeps every frame by default; the AI's "not related to the call" verdict only deletes one when WATCH_DROP_IRRELEVANT is on."""
import base64

import pytest
from conftest import bearer, state, wait_for

from app.config import get_settings
from app.llm import set_llm
from app.llm.mock import MockLLM

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class Unrelated(MockLLM):
    def structured(self, system, prompt, schema, model, images=None, auth=None):
        out = super().structured(system, prompt, schema, model, images, auth)
        if "relevant" in out:
            out["relevant"] = False                     # the AI says this screen is not part of the call
        return out


@pytest.fixture()
def unrelated(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "stub")      # a real (non-mock) analysis path, so the AI's verdict is used
    set_llm(Unrelated())
    yield
    set_llm(None)


def watched_frame(client, h):
    sid = client.post("/api/start", headers=h, json={"profile": "meeting", "subject": "w"}).json()["sid"]
    assert client.post("/api/watch", headers=h, json={"sid": sid, "on": True, "source": {"label": "Shared screen"}}).status_code == 200
    img = "data:image/png;base64," + base64.b64encode(PNG).decode()
    assert client.post("/api/exhibit", headers=h, json={"sid": sid, "image": img, "question": "(screen watch)", "auto": True}).status_code == 200
    return sid


def test_an_unrelated_frame_stays_by_default(client, unrelated):
    h = bearer(client, "keep@example.com")
    sid = watched_frame(client, h)
    wait_for(lambda: state(client, h, sid)["detail"]["exhibits"] and state(client, h, sid)["detail"]["exhibits"][0]["status"] == "done")
    d = state(client, h, sid)
    assert len(d["detail"]["exhibits"]) == 1 and d["live"]["watch"]["kept"] == 1
    client.post("/api/end", headers=h, json={"sid": sid})


def test_the_option_deletes_unrelated_frames(client, unrelated, monkeypatch):
    monkeypatch.setattr(get_settings(), "watch_drop_irrelevant", True)
    h = bearer(client, "drop@example.com")
    sid = watched_frame(client, h)
    wait_for(lambda: not state(client, h, sid)["detail"]["exhibits"])
    assert state(client, h, sid)["live"]["watch"]["kept"] == 0
    client.post("/api/end", headers=h, json={"sid": sid})
