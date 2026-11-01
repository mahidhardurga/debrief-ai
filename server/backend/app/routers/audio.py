"""Audio ingest WebSocket for a live session.

Client -> server
  binary frames: byte 0 is the source (0 = you/microphone, 1 = other/shared tab or system audio), the rest is
                 16 kHz mono signed 16-bit little-endian PCM;
  text "flush": cut the current utterances now;
  text JSON {"sources": [{"role": "you"|"other", "name": "..."}]}: what the browser is capturing (shown in the UI).
Server -> client: JSON text frames {"type":"status","asr":...}, {"type":"level",...} ~4x/s, {"type":"error",...}.
Auth: the same-origin session cookie, or `?token=<access token>` for other clients.
Audio is dropped while the session is paused or listening is switched off, exactly like the desktop edition.
"""
import asyncio
import json
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..asr import engine
from ..config import get_settings
from ..db import SessionLocal
from ..deps import COOKIE, user_from_token
from ..engine.store import get_store

router = APIRouter()
LABELS = {0: "you", 1: "other"}


@router.websocket("/api/sessions/{sid}/audio")
async def audio_ws(ws: WebSocket, sid: str, token: str = ""):
    db = SessionLocal()
    try:
        user = user_from_token(db, token or ws.cookies.get(COOKIE, ""))
    finally:
        db.close()
    s = get_store(user.id).sessions.get(sid) if user else None
    if not s:
        await ws.close(code=4401)
        return
    if s.state != "live":
        await ws.close(code=4409)
        return
    if not get_settings().asr_enabled:
        await ws.close(code=4503, reason="Speech recognition is disabled on this server")
        return
    await ws.accept()
    loop = asyncio.get_running_loop()
    outbox: asyncio.Queue = asyncio.Queue()
    inflight: set[int] = set()

    def vocab() -> str:
        return s.profile.get("vocabulary", "")

    def who_spoke(key: int, audio):
        return s._speaker_of(LABELS[key], audio)

    def make_final(key: int):
        label = LABELS[key]

        def on_utterance(audio, start_t):
            def work():
                try:
                    text = engine.transcribe(audio, vocab())
                except Exception as exc:
                    loop.call_soon_threadsafe(outbox.put_nowait, {"type": "error", "message": engine.model_error or str(exc)})
                    return
                s.on_partial(key, label, None)
                if text and text.lower() not in engine.HALLUCINATIONS:
                    line_id = s.on_utterance(label, text, max(0.0, start_t - s.started_wall), speaker=who_spoke(key, audio))
                    if line_id is not None and len(audio) >= 24000 and engine.refine_enabled():       # 1.5 s or more: worth a second look
                        engine.submit_refine(audio, vocab(), lambda better, lid=line_id: s.override_line(lid, better, "refined"))
            engine.pool().submit(work)
        return on_utterance

    def make_partial(key: int):
        label = LABELS[key]

        def on_partial(audio):
            if key in inflight:                                 # only one draft per source at a time
                return
            inflight.add(key)

            def work():
                try:
                    text = engine.transcribe(audio, vocab(), fast=True)
                    if text and segs[key].buf and text.lower() not in engine.HALLUCINATIONS:   # still speaking
                        s.on_partial(key, label, text)
                except Exception:
                    pass
                finally:
                    inflight.discard(key)
            engine.pool().submit(work)
        return on_partial

    partials = get_settings().asr_partials
    segs = {k: engine.Segmenter(make_final(k), make_partial(k) if partials else None) for k in LABELS}
    s.audio_clients += 1
    s.touch(save=False)

    async def sender():
        shown = None
        while True:
            state = (engine.status, engine.model_error)
            if state != shown:                                   # tell the browser while the speech model downloads / loads / fails
                shown = state
                await ws.send_text(json.dumps({"type": "status", "asr": state[0], "error": state[1]}))
            try:
                await ws.send_text(json.dumps(await asyncio.wait_for(outbox.get(), 0.25)))
            except TimeoutError:
                pass
            s.levels = {"you": round(segs[0].level, 4), "other": round(segs[1].level, 4)}

    send_task = asyncio.create_task(sender())
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes"):
                data = msg["bytes"]
                if len(data) > 2 and data[0] in segs:
                    if s.state != "live" or s.paused or not s.listening:
                        segs[data[0]].reset()
                    else:
                        segs[data[0]].push(engine.pcm16_to_float(data[1:]), time.time())
            elif msg.get("text") == "flush":
                for sg in segs.values():
                    sg.flush()
            elif msg.get("text"):
                try:
                    j = json.loads(msg["text"])
                    if isinstance(j.get("sources"), list):
                        s.sources = [{"role": "other" if x.get("role") == "other" else "you", "name": str(x.get("name", ""))[:80]}
                                     for x in j["sources"][:4]]
                        s.touch(save=False)
                except ValueError:
                    pass
    except WebSocketDisconnect:
        pass
    finally:
        if s.state == "live" and not s.paused and s.listening:
            for sg in segs.values():
                sg.flush()                                        # do not lose the last words
        send_task.cancel()
        s.audio_clients = max(0, s.audio_clients - 1)
        if s.audio_clients == 0:
            s.sources, s.levels = [], {"you": 0.0, "other": 0.0}
        s.touch(save=False)
