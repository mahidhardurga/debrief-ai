"""The web app's API. It mirrors the desktop edition's /api/* routes (same paths and JSON shapes) so both editions
share one UI (shared/ui), but every route is authenticated and scoped to the signed-in user's own sessions."""
import base64
import re
import shutil
import threading
import time

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from ..asr import engine as asr
from ..config import get_settings
from ..deps import current_user
from ..engine.session import Session
from ..engine.store import get_store, profiles
from ..models import User
from .claude_account import require_claude

router = APIRouter(prefix="/api", tags=["app"])
Body_ = Body(default_factory=dict)


def store_of(user: User):
    st = get_store(user.id)
    if not getattr(st, "_swept", False):                 # recordings past their retention period go away once per load
        st._swept = True
        threading.Thread(target=st.cleanup_recordings, daemon=True).start()
    return st


def sess(user: User, sid: str | None) -> Session:
    s = store_of(user).sessions.get(sid or "")
    if not s:
        raise HTTPException(404, "Unknown session")
    return s


def ok(**extra):
    return {"ok": True, **extra}


# ---------------------------------------------------------------------------- profiles
@router.get("/profiles")
def api_profiles(_: User = Depends(current_user)):
    out = []
    for p in profiles().values():
        out.append({k: p.get(k) for k in ("id", "name", "description", "you_label", "other_label", "subject_label",
                                           "extra", "context_hint", "decisions", "live_sections", "final_sections",
                                           "drafts", "exhibit")}
                   | {"has_rubric": bool(p.get("rubric")), "has_form": bool(p.get("form")),
                      "rubric": p.get("rubric") and {"areas": [{k: a[k] for k in ("id", "name", "weight")} for a in p["rubric"]["areas"]],
                                                     "advance_threshold": p["rubric"]["advance_threshold"]},
                      "form": p.get("form") and {"title": p["form"]["title"], "categories": p["form"]["categories"]}})
    return out


# ---------------------------------------------------------------------------- state (polled every second)
@router.get("/state")
def api_state(sid: str = "", since: int = 0, rev: int = -1, first: str = "", user: User = Depends(current_user)):
    st = store_of(user)
    live = st.live()
    out = {"sessions": [s.list_item() for s in sorted(st.sessions.values(), key=lambda s: s.started, reverse=True)],
           "live_sid": live.sid if live else None,
           "capture_models": "demo" if get_settings().llm_provider == "mock" else asr.status}
    s = st.sessions.get(sid)
    if s:
        out["live"] = s.live_block(since, first == "1")
        out["rev"] = s.rev
        if s.rev != rev or first == "1":
            out["detail"] = s.detail_block()
            if first == "1":
                out["detail"]["notes"] = s.notes
        out["events"] = s.events[-60:]
    return out


# ---------------------------------------------------------------------------- session lifecycle
@router.post("/start")
def api_start(d: dict = Body_, user: User = Depends(current_user)):
    profile = profiles().get(d.get("profile"))
    if not profile:
        raise HTTPException(400, "Unknown profile")
    require_claude(user)
    st = store_of(user)
    cur = st.live()
    if cur and not d.get("end_current"):
        return JSONResponse({"error": "live", "live_sid": cur.sid, "live_name": cur.name}, status_code=409)
    if cur:
        cur.end()
    context = [c for c in (d.get("context") or "").split("\n") if c.strip()]
    s = Session(st, profile, d.get("subject", ""), d.get("extra") or (profile.get("extra") or {}).get("default", ""), context,
                role=(d.get("role") or "")[:200], speakers=bool(d.get("speakers", get_settings().speakers_default)))
    st.sessions[s.sid] = s
    s.start()
    return ok(sid=s.sid)


@router.post("/end")
def api_end(d: dict = Body_, user: User = Depends(current_user)):
    sess(user, d.get("sid")).end()
    return ok()


@router.post("/pause")
def api_pause(d: dict = Body_, user: User = Depends(current_user)):
    sess(user, d.get("sid")).set_paused(bool(d.get("on")))
    return ok()


@router.post("/listen")
def api_listen(d: dict = Body_, user: User = Depends(current_user)):
    sess(user, d.get("sid")).set_listening(bool(d.get("on")))
    return ok()


@router.post("/analyze")
def api_analyze(d: dict = Body_, user: User = Depends(current_user)):
    sess(user, d.get("sid")).force = True
    return ok()


@router.post("/notes")
def api_notes(d: dict = Body_, user: User = Depends(current_user)):
    sess(user, d.get("sid")).set_notes(str(d.get("text", ""))[:100_000])
    return ok()


@router.post("/finish")
def api_finish(d: dict = Body_, user: User = Depends(current_user)):
    return {"ok": sess(user, d.get("sid")).finish()}


@router.post("/regenerate")
def api_regenerate(d: dict = Body_, user: User = Depends(current_user)):
    return {"ok": sess(user, d.get("sid")).regenerate()}


@router.post("/retry")
def api_retry(d: dict = Body_, user: User = Depends(current_user)):
    s = sess(user, d.get("sid"))
    if s.state != "review":
        raise HTTPException(400, "Nothing to retry")
    return {"ok": s.regenerate()}


@router.post("/discard")
def api_discard(d: dict = Body_, user: User = Depends(current_user)):
    sess(user, d.get("sid")).discard(bool(d.get("delete")))
    return ok()


@router.post("/decision")
def api_decision(d: dict = Body_, user: User = Depends(current_user)):
    if not sess(user, d.get("sid")).decide(d.get("decision", "")):
        raise HTTPException(400, "Could not save the decision")
    return ok()


@router.post("/feedback")
def api_feedback(d: dict = Body_, user: User = Depends(current_user)):
    return {"ok": sess(user, d.get("sid")).set_feedback(d.get("state", ""))}


# ---------------------------------------------------------------------------- context and chat
@router.post("/context_add")
def api_context_add(d: dict = Body_, user: User = Depends(current_user)):
    sess(user, d.get("sid")).add_context(str(d.get("text", ""))[:2000])
    return ok()


@router.post("/context_remove")
def api_context_remove(d: dict = Body_, user: User = Depends(current_user)):
    sess(user, d.get("sid")).remove_context(int(d.get("index", -1)))
    return ok()


@router.post("/chat")
def api_chat(d: dict = Body_, user: User = Depends(current_user)):
    s = sess(user, d.get("sid"))
    text = (d.get("text") or "").strip()[:4000]
    if not text:
        raise HTTPException(400, "Empty message")
    if d.get("mode") == "context":
        s.chat_context(text)
    else:
        s.chat_ask(text)
    return ok()


@router.post("/chat_dismiss")
def api_chat_dismiss(d: dict = Body_, user: User = Depends(current_user)):
    sess(user, d.get("sid")).chat_dismiss(d.get("id"))
    return ok()


@router.post("/speaker_name")
def api_speaker_name(d: dict = Body_, user: User = Depends(current_user)):
    """Give a voice a name (for example Speaker 2 -> Priya). The transcript and the AI use it from then on."""
    sess(user, d.get("sid")).set_speaker_name(int(d.get("id", 0)), d.get("name", ""))
    return ok()


# ---------------------------------------------------------------------------- screenshots and screen watching
@router.post("/watch")
def api_watch(d: dict = Body_, user: User = Depends(current_user)):
    src = d.get("source")
    sess(user, d.get("sid")).set_watch(bool(d.get("on")), {"label": str(src.get("label", "Shared screen"))[:60]} if src else None)
    return ok()


@router.post("/watch_interval")
def api_watch_interval(d: dict = Body_, user: User = Depends(current_user)):
    s = sess(user, d.get("sid"))
    s.watch["interval"] = max(5, min(3600, int(d.get("seconds", 30))))
    s.touch()
    return ok()


@router.post("/exhibit")
def api_exhibit(d: dict = Body_, user: User = Depends(current_user)):
    s = sess(user, d.get("sid"))
    try:
        png = base64.b64decode((d.get("image") or "").split(",", 1)[-1])
    except Exception:
        raise HTTPException(400, "Bad image")
    if not png.startswith(b"\x89PNG"):
        raise HTTPException(415, "Only PNG screenshots are accepted")
    if len(png) > 12 * 1024 * 1024:
        raise HTTPException(413, "Image too large (12 MB max)")
    if d.get("auto") and not (s.watch["on"] and s.state == "live"):
        return {"ok": False, "dropped": True}              # a late frame after watching was switched off
    s.add_exhibit(png, d.get("question", ""), d.get("note", ""), auto=bool(d.get("auto")))
    return ok()


@router.post("/exhibit_delete")
def api_exhibit_delete(d: dict = Body_, user: User = Depends(current_user)):
    return {"ok": sess(user, d.get("sid")).exhibit_delete(int(d.get("n", 0)))}


@router.post("/exhibit_retry")
def api_exhibit_retry(d: dict = Body_, user: User = Depends(current_user)):
    return {"ok": sess(user, d.get("sid")).exhibit_retry(int(d.get("n", 0)))}


@router.get("/exhibit_img/{sid}/{n}")
def api_exhibit_img(sid: str, n: int, user: User = Depends(current_user)):
    s = sess(user, sid)
    ex = next((x for x in s.exhibits if x["n"] == n), None)
    if not ex or not (s.dir / ex["file"]).exists():
        raise HTTPException(404, "Not found")
    return FileResponse(s.dir / ex["file"], media_type="image/png")


# ---------------------------------------------------------------------------- recording (the browser records, then uploads)
@router.post("/record")
def api_record(d: dict = Body_, user: User = Depends(current_user)):
    s = sess(user, d.get("sid"))
    if d.get("action") == "stop":
        return {"ok": s.record_stop()}
    if d.get("source"):
        s.source = {"label": str(d["source"].get("label", "Shared screen"))[:60]}
    res = s.record_start(bool(d.get("audio")), bool(d.get("video")), bool(d.get("consent")))
    if res.get("error"):
        return JSONResponse(res, status_code=400)
    return res


@router.post("/record_chunk")
async def api_record_chunk(request: Request, sid: str, n: int, kind: str, seq: int = 0, user: User = Depends(current_user)):
    """The browser sends its recording in small pieces while it records (no giant upload at the end)."""
    s = sess(user, sid)
    path = s.clip_upload_path(n, kind)
    if not path:
        raise HTTPException(404, "No recording is waiting for this file")
    limit = get_settings().max_upload_mb * 1024 * 1024
    key = (n, kind)
    if seq == 0:
        path.unlink(missing_ok=True)                                # a fresh recording starts an empty file
        s.chunk_seq[key] = 0
    expected = s.chunk_seq.get(key, 0)
    if seq == expected - 1:                                         # a retry of a piece that already arrived
        return ok(size=path.stat().st_size if path.exists() else 0)
    if seq != expected:
        raise HTTPException(409, "Recording data is missing earlier pieces")
    body = await request.body()
    if path.exists() and path.stat().st_size + len(body) > limit:
        raise HTTPException(413, "Recording too large")
    with open(path, "ab") as f:
        f.write(body)
    s.chunk_seq[key] = expected + 1
    return ok(size=path.stat().st_size)


@router.post("/record_done")
def api_record_done(d: dict = Body_, user: User = Depends(current_user)):
    """The browser has sent the last piece of one file (audio or video) of the clip."""
    s = sess(user, d.get("sid"))
    if not s.clip_uploaded(int(d.get("n", 0)), d.get("kind", ""), d.get("duration")):
        raise HTTPException(404, "No recording data was received for this file")
    return ok()


def _range_response(path, media_type, request: Request, download: bool, name: str):
    """Serve a file with HTTP Range support so videos can seek."""
    size = path.stat().st_size
    headers = {"Accept-Ranges": "bytes"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    m = re.match(r"bytes=(\d*)-(\d*)", request.headers.get("range", ""))
    if not m or not (m.group(1) or m.group(2)):
        return FileResponse(path, media_type=media_type, headers=headers)
    start = int(m.group(1)) if m.group(1) else max(0, size - int(m.group(2)))
    end = min(int(m.group(2)), size - 1) if m.group(1) and m.group(2) else size - 1
    if start >= size or start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

    def chunks():
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                data = f.read(min(1 << 20, left))
                if not data:
                    break
                left -= len(data)
                yield data
    headers.update({"Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(end - start + 1)})
    return StreamingResponse(chunks(), status_code=206, media_type=media_type, headers=headers)


@router.get("/clip/{sid}/{n}/{kind}")
def api_clip(sid: str, n: int, kind: str, request: Request, download: str = "", user: User = Depends(current_user)):
    s = sess(user, sid)
    path = s.clip_file(n, kind) if kind in ("audio", "video") else None
    if not path:
        raise HTTPException(404, "Not found")
    return _range_response(path, "audio/webm" if kind == "audio" else "video/webm", request, download == "1", path.name)


@router.post("/recording_delete")
def api_recording_delete(d: dict = Body_, user: User = Depends(current_user)):
    s = sess(user, d.get("sid"))
    removed = s.delete_clip(None if d.get("all") else int(d.get("n", 0)))
    s.touch()
    return ok(removed=removed)


@router.get("/export/{sid}")
def api_export(sid: str, rec: str = "", user: User = Depends(current_user)):
    s = sess(user, sid)
    buf = s.zip_bytes(rec == "1")
    name = f"{sid}{'-with-recordings' if rec == '1' else ''}.zip"
    return StreamingResponse(buf, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/day_summary")
def api_day_summary(date: str = "", user: User = Depends(current_user)):
    st = store_of(user)
    date = date or time.strftime("%Y-%m-%d")
    st.update_day_summary(date)
    path = st.sessions_dir / f"day-summary-{date}.csv"
    if not path.exists():
        raise HTTPException(404, "No summary for that day")
    return FileResponse(path, media_type="text/csv", filename=f"day-summary-{date}.csv")


# ---------------------------------------------------------------------------- evaluation form
@router.post("/form")
def api_form(d: dict = Body_, user: User = Depends(current_user)):
    return {"ok": sess(user, d.get("sid")).write_form_answers()}


@router.post("/fill_form")
def api_fill_form(d: dict = Body_, user: User = Depends(current_user)):
    raise HTTPException(400, "The web edition cannot open the portal for you. Use the Fill helper under the evaluation form.")


@router.post("/fill_cancel")
def api_fill_cancel(d: dict = Body_, user: User = Depends(current_user)):
    return ok()


def disk_free_mb(path) -> float:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free / 1048576
