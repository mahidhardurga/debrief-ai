"""Telling the people on the other side of a call apart (shared/core/speakers.py + the audio pipeline)."""
import json

import analyzer
import numpy as np
import pytest
import speakers
from conftest import state, wait_for

BASIS = np.eye(16, dtype=np.float32)


def fake_by_loudness(audio):
    """A stand-in for the neural model: voices of different loudness are different 'people'."""
    rms = float(np.sqrt(np.mean(np.square(audio))))
    return BASIS[1 if rms < 0.15 else 2 if rms < 0.4 else 3]


@pytest.fixture()
def fake_model():
    import itertools

    from app.asr import engine as asr
    n = itertools.count(1)
    speakers.set_embedder(fake_by_loudness)
    asr.set_transcriber(lambda audio: f"utt{next(n)} lorem{next(n)} ipsum{next(n)} dolor{next(n)}")      # every utterance has its own words
    yield
    speakers.set_embedder(None)
    asr.set_transcriber(lambda audio: "hello from the fake transcriber")


@pytest.fixture(autouse=True)
def no_live_session_left(client, admin):
    def end_live():
        live = client.get("/api/state", headers=admin).json().get("live_sid")
        if live:
            client.post("/api/end", headers=admin, json={"sid": live})
            wait_for(lambda: client.get("/api/state", headers=admin).json().get("live_sid") is None)
    end_live()
    yield
    end_live()


def noise(std, seconds=2.0, seed=0):
    return (np.random.default_rng(seed).standard_normal(int(16000 * seconds)) * std).astype(np.float32)


def test_book_recognises_returning_voices_and_ignores_short_clips(fake_model):
    book = speakers.SpeakerBook()
    assert book.assign(noise(0.09)) == 1
    assert book.assign(noise(0.27)) == 2
    assert book.assign(noise(0.09, seed=3)) == 1                     # the first voice comes back
    assert book.assign(noise(0.27, seed=4)) == 2
    assert book.assign(noise(0.9, seconds=0.4)) == 2                 # too short to judge: the previous person, no new speaker
    assert len(book.centroids) == 2
    assert book.assign(noise(0.9, seconds=1.0)) in (1, 2)           # unknown voice but under 1.2 s: some known voice, never a new speaker
    assert len(book.centroids) == 2
    assert book.assign(noise(0.9, seconds=2.0)) == 3                 # a long unknown voice is a new person


def test_book_caps_the_number_of_people(fake_model):
    book = speakers.SpeakerBook(max_speakers=2)
    book.assign(noise(0.09))
    book.assign(noise(0.27))
    assert book.assign(noise(0.9)) in (1, 2) and len(book.centroids) == 2


def test_the_ai_reads_who_said_what_only_when_several_people_spoke():
    prof = {"you_label": "Interviewer", "other_label": "Candidate"}
    lines = [{"label": "you", "text": "Hi", "ts": 1, "speaker": None}, {"label": "other", "text": "Hello", "ts": 2, "speaker": 1},
             {"label": "other", "text": "Hey", "ts": 3, "speaker": 2}]
    one = analyzer.transcript_text(prof, lines, speakers={"multi": False, "names": {}})
    assert "Candidate:" in one and "Speaker" not in one
    many = analyzer.transcript_text(prof, lines, speakers={"multi": True, "names": {"2": "Priya"}})
    assert "Candidate (Speaker 1): Hello" in many and "Candidate (Priya): Hey" in many and "Interviewer: Hi" in many
    ctx = {"profile": {**prof, "subject_label": "Candidate", "extra": None}, "lines": lines, "speakers": {"multi": True, "names": {}},
           "subject": "", "extra": "", "notes": "", "context": [], "exhibits": [], "previous": None}
    assert "SEVERAL PEOPLE ARE ON THE CANDIDATE SIDE" in analyzer.build_context(ctx)


def start(client, headers, **kw):
    r = client.post("/api/start", headers=headers, json={"profile": "meeting", "subject": "spk", **kw})
    assert r.status_code == 200, r.text
    return r.json()["sid"]


def talk(client, ws, headers, sid, role, std, seed):
    """One utterance on one channel; waits until its line has arrived so the order is deterministic."""
    n = len([ln for ln in state(client, headers, sid, first="1")["live"]["lines"] if not ln["marker"]])
    for chunk in (np.clip(noise(std, 2.0, seed), -1, 1), np.zeros(16000, dtype=np.float32)):
        data = (chunk * 32767).astype("<i2").tobytes()
        for i in range(0, len(data), 8000):
            ws.send_bytes(bytes([role]) + data[i:i + 8000])
    wait_for(lambda: len([ln for ln in state(client, headers, sid, first="1")["live"]["lines"] if not ln["marker"]]) > n)


def test_other_side_lines_are_tagged_and_can_be_renamed(client, admin, fake_model):
    sid = start(client, admin, speakers=True)
    token = admin["Authorization"].split()[1]
    with client.websocket_connect(f"/api/sessions/{sid}/audio?token={token}") as ws:
        ws.send_text(json.dumps({"sources": [{"role": "you", "name": "Mic"}, {"role": "other", "name": "Call"}]}))
        talk(client, ws, admin, sid, 1, 0.09, 1)
        talk(client, ws, admin, sid, 0, 0.09, 2)                     # the interviewer on the microphone: not tagged
        talk(client, ws, admin, sid, 1, 0.27, 3)
        talk(client, ws, admin, sid, 1, 0.09, 4)
    lines = [ln for ln in state(client, admin, sid, first="1")["live"]["lines"] if not ln["marker"] and "utt" in ln["text"]]
    assert [(ln["label"], ln["speaker"]) for ln in lines if ln["label"] == "other"] == [("other", 1), ("other", 2), ("other", 1)]
    assert [ln["speaker"] for ln in lines if ln["label"] == "you"] == [None]
    assert state(client, admin, sid)["detail"]["speakers"] == {"on": True, "names": {}}
    assert client.post("/api/speaker_name", headers=admin, json={"sid": sid, "id": 2, "name": "Priya"}).status_code == 200
    d = state(client, admin, sid)["detail"]
    assert d["speakers"]["names"] == {"2": "Priya"}
    client.post("/api/end", headers=admin, json={"sid": sid})
    transcript = wait_for(lambda: (lambda x: x if "Priya" in x else None)(
        __import__("pathlib").Path(__import__("os").environ["DATA_DIR"]).joinpath("users").glob("*/sessions/" + sid + "/transcript.txt").__next__().read_text(encoding="utf-8")))
    assert "(Speaker 1)" in transcript and "(Priya)" in transcript


def test_speakers_can_be_switched_off(client, admin, fake_model):
    sid = start(client, admin, speakers=False)
    token = admin["Authorization"].split()[1]
    with client.websocket_connect(f"/api/sessions/{sid}/audio?token={token}") as ws:
        talk(client, ws, admin, sid, 1, 0.09, 5)
        talk(client, ws, admin, sid, 1, 0.27, 6)
    other = [ln for ln in state(client, admin, sid, first="1")["live"]["lines"] if ln["label"] == "other" and "utt" in ln["text"]]
    assert len(other) == 2 and all(ln["speaker"] is None for ln in other)
    assert state(client, admin, sid)["detail"]["speakers"]["on"] is False
    client.post("/api/end", headers=admin, json={"sid": sid})


def test_a_missing_speaker_model_never_loses_the_words(client, admin, monkeypatch):
    monkeypatch.setattr(speakers, "_embed", None)
    monkeypatch.setattr(speakers, "_extractor", None)
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "speaker_model", "/nonexistent/model.onnx")
    monkeypatch.setattr(get_settings(), "asr_allow_download", False)
    sid = start(client, admin, speakers=True)
    token = admin["Authorization"].split()[1]
    with client.websocket_connect(f"/api/sessions/{sid}/audio?token={token}") as ws:
        talk(client, ws, admin, sid, 1, 0.09, 7)
    st = state(client, admin, sid, first="1")
    assert any("fake transcriber" in ln["text"] for ln in st["live"]["lines"])       # the default test transcriber
    assert any("Speakers are not told apart" in e["text"] for e in st["events"])
    assert st["detail"]["speakers"]["on"] is False
    client.post("/api/end", headers=admin, json={"sid": sid})
