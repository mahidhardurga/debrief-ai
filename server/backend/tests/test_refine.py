"""The optional accurate second pass: it quietly replaces a line's text a few seconds after the fast pass wrote it."""
import numpy as np
import pytest
from conftest import state, wait_for

from app.asr import engine as asr
from app.config import get_settings


@pytest.fixture()
def refiner():
    asr.set_refiner(lambda audio: "a carefully refined sentence")
    yield
    asr.set_refiner(None)


def test_the_second_pass_replaces_the_text_of_longer_lines(client, admin, refiner):
    live = client.get("/api/state", headers=admin).json().get("live_sid")
    if live:
        client.post("/api/end", headers=admin, json={"sid": live})
        wait_for(lambda: client.get("/api/state", headers=admin).json().get("live_sid") is None)
    sid = client.post("/api/start", headers=admin, json={"profile": "meeting", "subject": "refine", "speakers": False}).json()["sid"]
    token = admin["Authorization"].split()[1]
    speech = (np.random.default_rng(0).standard_normal(16000 * 2) * 3000).astype("<i2").tobytes()      # 2 s: long enough to be refined
    silence = np.zeros(16000, dtype="<i2").tobytes()
    with client.websocket_connect(f"/api/sessions/{sid}/audio?token={token}") as ws:
        for chunk in (speech, silence):
            for i in range(0, len(chunk), 8000):
                ws.send_bytes(b"\x01" + chunk[i:i + 8000])
        line = wait_for(lambda: next((ln for ln in state(client, admin, sid, first="1")["live"]["lines"]
                                      if ln["text"] == "a carefully refined sentence"), None))
    assert line["refined"] is True
    client.post("/api/end", headers=admin, json={"sid": sid})


def test_too_many_waiting_lines_are_skipped_not_queued(monkeypatch, refiner):
    monkeypatch.setattr(get_settings(), "asr_refine_backlog", 0)
    assert asr.submit_refine(np.zeros(32000, dtype=np.float32), "", lambda text: None) is False
