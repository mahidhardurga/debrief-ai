"""The web app's API (mirrors the desktop routes): lifecycle, dynamic topics, isolation, recording upload, audio WebSocket."""
import io
import json
import zipfile

import numpy as np
import pytest
from conftest import bearer, register, state, wait_for
from starlette.websockets import WebSocketDisconnect

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def post(client, h, path, **body):
    return client.post("/api" + path, headers=h, json=body)


def start(client, h, **kw):
    r = post(client, h, "/start", profile=kw.pop("profile", "interview"), subject=kw.pop("subject", "Asha"), **kw)
    assert r.status_code == 200, r.text
    return r.json()["sid"]


def test_health_and_ui_assets(client):
    assert client.get("/api/health").json()["ok"] is True
    for path, kind in (("/", "text/html"), ("/app.js", "javascript"), ("/app.css", "text/css"), ("/platform.js", "javascript")):
        r = client.get(path)
        assert r.status_code == 200 and kind in r.headers["content-type"], path
    assert "PLATFORM" in client.get("/app.js").text and "window.PLATFORM" in client.get("/platform.js").text
    cfg = client.get("/api/config").json()
    assert cfg["platform"] == "web" and cfg["registration_open"] is True


def test_requires_auth(client):
    client.cookies.clear()
    for path in ("/api/state", "/api/profiles", "/api/claude"):
        assert client.get(path).status_code == 401, path
    assert client.post("/api/start", json={"profile": "meeting"}).status_code == 401


def test_cookie_login_logout_and_first_user_is_admin(client, admin):
    client.cookies.clear()
    r = register(client, "cookie@example.com")
    assert r.status_code == 201
    sc = r.headers["set-cookie"].lower()
    assert "dbf_session=" in sc and "httponly" in sc and "samesite=strict" in sc
    assert client.get("/api/auth/me").json()["email"] == "cookie@example.com"          # the cookie alone authenticates
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers=admin).json()["role"] == "admin"           # the first account is the administrator
    assert register(client, "admin@example.com").status_code == 409
    assert client.post("/api/auth/login", json={"email": "cookie@example.com", "password": "wrong-wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "COOKIE@example.com", "password": "password123"}).status_code == 200
    client.cookies.clear()


def test_login_is_throttled(client):
    codes = [client.post("/api/auth/login", json={"email": "t@example.com", "password": "wrongwrong"}).status_code for _ in range(10)]
    assert 429 in codes


def test_profiles_hide_prompts(client, admin):
    ps = {p["id"]: p for p in client.get("/api/profiles", headers=admin).json()}
    assert set(ps) == {"interview", "meeting"}
    assert "system" not in ps["interview"] and ps["interview"]["has_rubric"] and ps["interview"]["has_form"]


def test_full_lifecycle_like_the_desktop(client, admin):
    sid = start(client, admin, subject="Asha", role="Golang backend engineer", context="5 yrs Go")
    st = wait_for(lambda: (lambda s: s if s["detail"].get("rubric") and not s["detail"]["rubric"]["building"] else None)(state(client, admin, sid, first="1")))
    assert st["detail"]["rubric"]["role"] == "Golang backend engineer" and st["detail"]["rubric"]["custom"]
    live = wait_for(lambda: (lambda s: s if len(s["live"]["lines"]) >= 7 and s["detail"]["analysis"] else None)(state(client, admin, sid, first="1")), 30)
    assert live["live_sid"] == sid and live["live"]["capture"]["models"] == "demo"
    # a topic that is not on the role's list came up: it was added live and scored
    rub = live["detail"]["rubric"]["areas"]
    assert any(a.get("dynamic") and a["off_role"] for a in rub)
    # notes, context, pause, listening
    post(client, admin, "/notes", sid=sid, text="nervous")
    post(client, admin, "/context_add", sid=sid, text="he is a lead")
    assert state(client, admin, sid)["detail"]["context"] == ["5 yrs Go", "he is a lead"]
    post(client, admin, "/context_remove", sid=sid, index=0)
    assert state(client, admin, sid)["detail"]["context"] == ["he is a lead"]
    post(client, admin, "/pause", sid=sid, on=True)
    assert state(client, admin, sid)["live"]["paused"] is True
    post(client, admin, "/pause", sid=sid, on=False)
    post(client, admin, "/listen", sid=sid, on=False)
    assert state(client, admin, sid)["live"]["listening"] is False
    post(client, admin, "/listen", sid=sid, on=True)
    # chat: ask and add context
    post(client, admin, "/chat", sid=sid, text="How is the candidate doing?", mode="ask")
    post(client, admin, "/chat", sid=sid, text="He used Kafka at his last job", mode="context")
    d = wait_for(lambda: (lambda x: x if len(x["chat"]) >= 4 and not state(client, admin, sid)["live"]["chat_busy"] else None)(state(client, admin, sid)["detail"]))
    assert "He used Kafka at his last job" in d["context"]
    # screenshot exhibit: validation, analysis, retry/delete
    bad = post(client, admin, "/exhibit", sid=sid, image="data:image/png;base64," + "aGVsbG8=")
    assert bad.status_code == 415
    import base64
    ok = post(client, admin, "/exhibit", sid=sid, image="data:image/png;base64," + base64.b64encode(PNG).decode(), question="Reverse a string")
    assert ok.status_code == 200
    ex = wait_for(lambda: (lambda x: x[0] if x and x[0]["status"] == "done" else None)(state(client, admin, sid)["detail"]["exhibits"]))
    assert ex["analysis"]["title"] and client.get(ex["url"], headers=admin).content == PNG
    # end -> final analysis -> decide -> files
    assert post(client, admin, "/end", sid=sid).status_code == 200
    d = wait_for(lambda: (lambda x: x if x["state"] == "review" and x["final"] else None)(state(client, admin, sid)["detail"]), 30)
    assert d["score"]["score"] and d["final"]["summary"].startswith("Mock")
    assert post(client, admin, "/decision", sid=sid, decision="maybe").status_code == 400
    assert post(client, admin, "/decision", sid=sid, decision="advance").status_code == 200
    d = state(client, admin, sid)["detail"]
    assert d["state"] == "decided" and d["feedback"]["state"] == "pending" and {"report.md", "evaluation.json", "result.json"} <= set(d["files"])
    assert post(client, admin, "/feedback", sid=sid, state="submitted").json()["ok"] is True
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/export/{sid}", headers=admin).content))
    assert any(n.endswith("report.md") for n in z.namelist())
    assert "Asha" in client.get("/api/day_summary", headers=admin).text
    assert post(client, admin, "/discard", sid=sid, delete=True).status_code == 200
    assert post(client, admin, "/end", sid=sid).status_code == 404


def test_isolation_between_users(client, admin, other):
    sid = start(client, admin, profile="meeting", subject="private")
    assert sid not in [s["id"] for s in client.get("/api/state", headers=other).json()["sessions"]]
    assert client.get("/api/state", headers=other, params={"sid": sid}).json().get("detail") is None
    for path, body in (("/end", {}), ("/pause", {"on": True}), ("/notes", {"text": "x"}), ("/chat", {"text": "hi"}),
                       ("/discard", {"delete": True}), ("/record", {"audio": True})):
        assert client.post("/api" + path, headers=other, json={"sid": sid, **body}).status_code == 404, path
    assert client.get(f"/api/export/{sid}", headers=other).status_code == 404
    assert client.get(f"/api/exhibit_img/{sid}/1", headers=other).status_code == 404
    assert sid in [s["id"] for s in client.get("/api/state", headers=admin).json()["sessions"]]
    post(client, admin, "/end", sid=sid)


def test_recording_is_uploaded_in_pieces_and_served_with_ranges(client, admin):
    sid = start(client, admin, profile="meeting", subject="rec")
    first = post(client, admin, "/record", sid=sid, action="start", audio=True, video=True)
    assert first.json() == {"need_consent": True}                                    # consent is required, like the desktop edition
    r = post(client, admin, "/record", sid=sid, action="start", audio=True, video=True, consent=True)
    n = r.json()["n"]
    assert post(client, admin, "/record", sid=sid, action="start", audio=True).status_code == 400    # already recording
    pieces = {"audio": [b"AUDIO0", b"AUDIO1", b"AUDIO2"], "video": [b"VIDEO0" * 100, b"VIDEO1" * 100]}
    for kind, parts in pieces.items():
        for seq, data in enumerate(parts):
            assert client.post(f"/api/record_chunk?sid={sid}&n={n}&kind={kind}&seq={seq}", headers=admin, content=data).status_code == 200
    assert client.post(f"/api/record_chunk?sid={sid}&n={n}&kind=video&seq=5", headers=admin, content=b"x").status_code == 409     # a gap is refused
    assert client.post(f"/api/record_chunk?sid={sid}&n={n}&kind=video&seq=1", headers=admin, content=b"y").status_code == 200     # a retry is harmless
    assert post(client, admin, "/record", sid=sid, action="stop").json()["ok"] is True
    assert state(client, admin, sid)["detail"]["clips"][0]["state"] == "saving"
    for kind in pieces:
        assert post(client, admin, "/record_done", sid=sid, n=n, kind=kind, duration=12.5).status_code == 200
    clip = wait_for(lambda: (lambda c: c[0] if c and c[0]["state"] == "saved" else None)(state(client, admin, sid)["detail"]["clips"]))
    assert clip["audio_url"] and clip["video_url"] and clip["size"] == 18 + 1200 and clip["duration"] == 12.5
    assert client.get(clip["audio_url"], headers=admin).content == b"AUDIO0AUDIO1AUDIO2"
    rng = client.get(clip["video_url"], headers={**admin, "Range": "bytes=6-11"})
    assert rng.status_code == 206 and rng.content == b"VIDEO0" and rng.headers["content-range"].startswith("bytes 6-11/")
    assert "attachment" in client.get(clip["audio_url"] + "?download=1", headers=admin).headers["content-disposition"]
    assert client.post(f"/api/record_chunk?sid={sid}&n={n}&kind=audio&seq=0", headers=admin, content=b"late").status_code == 404   # saved clips are closed
    assert state(client, admin, sid)["detail"]["consent"]["confirmed"] is True
    assert post(client, admin, "/recording_delete", sid=sid, n=n).json()["removed"] == 1
    assert state(client, admin, sid)["detail"]["clips"] == []
    post(client, admin, "/end", sid=sid)


def test_audio_websocket_feeds_the_session(client, admin):
    sid = start(client, admin, profile="meeting", subject="ws")
    token = admin["Authorization"].split()[1]
    rng = np.random.default_rng(0)
    speech = (rng.standard_normal(16000 * 2) * 3000).astype("<i2").tobytes()          # 2 s of loud noise
    silence = np.zeros(16000, dtype="<i2").tobytes()                                   # 1 s of silence ends the utterance

    def stream(ws, role):
        for chunk in (speech, silence):
            for i in range(0, len(chunk), 8000):
                ws.send_bytes(bytes([role]) + chunk[i:i + 8000])

    with client.websocket_connect(f"/api/sessions/{sid}/audio?token={token}") as ws:
        ws.send_text(json.dumps({"sources": [{"role": "you", "name": "Microphone (browser)"}, {"role": "other", "name": "Shared tab"}]}))
        stream(ws, 1)
        wait_for(lambda: any("fake transcriber" in ln["text"] for ln in state(client, admin, sid, first="1")["live"]["lines"]))
        live = state(client, admin, sid)["live"]
        assert live["capture"]["connected"] is True and [s["name"] for s in live["capture"]["sources"]] == ["Microphone (browser)", "Shared tab"]
        line = next(ln for ln in state(client, admin, sid, first="1")["live"]["lines"] if "fake transcriber" in ln["text"])
        assert line["label"] == "other"
        before = len(state(client, admin, sid, first="1")["live"]["lines"])
        post(client, admin, "/pause", sid=sid, on=True)                                 # audio is dropped while paused
        stream(ws, 0)
        ws.send_text("flush")
        import time
        time.sleep(0.8)
        assert not any(ln["label"] == "you" and "fake" in ln["text"] for ln in state(client, admin, sid, first="1")["live"]["lines"][before:])
    from app.engine.store import get_store
    uid = client.get("/api/auth/me", headers=admin).json()["id"]
    wait_for(lambda: get_store(uid).sessions[sid].audio_clients == 1)         # back to the demo feed alone: the browser left
    post(client, admin, "/end", sid=sid)


def test_audio_websocket_rejects_strangers(client, admin, other):
    sid = start(client, admin, profile="meeting", subject="ws2")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/sessions/{sid}/audio?token=nope") as ws:
            ws.receive_text()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/sessions/{sid}/audio?token={other['Authorization'].split()[1]}") as ws:
            ws.receive_text()
    post(client, admin, "/end", sid=sid)


def test_one_live_session_at_a_time_and_end_current(client):
    h = bearer(client, "solo@example.com")
    a = start(client, h, profile="meeting", subject="first")
    r = post(client, h, "/start", profile="meeting", subject="second")
    assert r.status_code == 409 and r.json()["error"] == "live" and r.json()["live_sid"] == a
    b = post(client, h, "/start", profile="meeting", subject="second", end_current=True).json()["sid"]
    assert b != a
    wait_for(lambda: state(client, h, a)["detail"]["state"] in ("review", "finalizing", "decided"))
    post(client, h, "/end", sid=b)
