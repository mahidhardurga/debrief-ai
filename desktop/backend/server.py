"""Debrief AI: Flask app, all /api routes.  Run with scripts/run-desktop.bat.

capture is imported first, in the main thread, so COM is initialised before Flask starts threads.
"""
import base64
import logging
import os
import subprocess
import sys

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "shared" / "core"))   # analyzer.py, reports.py
import capture  # noqa: F401  (must come before Flask starts its threads)
import config

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

import analyzer
import session as S

analyzer.set_provider(config.AI_CHOICE)          # claude | copilot, as last chosen in the app
import windows_capture as wc

app = Flask(__name__, static_folder=None)
PROFILES = config.load_profiles()
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def body():
    return request.get_json(silent=True) or {}


def get_session(data=None):
    sid = (data or body()).get("sid") or request.args.get("sid")
    s = S.SESSIONS.get(sid)
    return s


def err(msg, code=400):
    return jsonify({"error": msg}), code


def need(data=None):
    s = get_session(data)
    return (s, None) if s else (None, err("Unknown session", 404))


# ---------------------------------------------------------------------------- pages and config
def _ui(folder, name):
    resp = send_from_directory(folder, name)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
def index():
    return _ui(config.SHARED_DIR / "ui", "index.html")


@app.get("/app.js")
def ui_js():
    return _ui(config.SHARED_DIR / "ui", "app.js")


@app.get("/app.css")
def ui_css():
    return _ui(config.SHARED_DIR / "ui", "app.css")


@app.get("/aimodel.js")
def ui_aimodel():
    return _ui(config.SHARED_DIR / "ui", "aimodel.js")


@app.get("/browser.js")
def ui_browser():
    return _ui(config.SHARED_DIR / "ui", "browser.js")


@app.get("/platform.js")
def ui_platform():
    return _ui(config.APP_DIR / "frontend", "platform.js")


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


@app.get("/api/config")
def api_config():
    return jsonify(config.public_config())


PING_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}


def _ai_status():
    """Which model analyses calls. For each: is its CLI installed on this PC, and has a token been pasted into the app
    (a pasted token is used instead of the CLI's own login, which expires)."""
    import copilot
    import secrets_store
    choice = analyzer.current_provider()
    found = {"claude": bool(analyzer._claude_exe()), "copilot": bool(copilot.find_exe(config.COPILOT_CLI_PATH))}
    one = lambda p: {"required": False, "connected": found[p], "found": found[p], "token": secrets_store.load(p) is not None}  # noqa: E731
    return {"choice": choice, "name": NAMES[choice], "tokens": True, "providers": {p: one(p) for p in analyzer.PROVIDERS}}


NAMES = {"claude": "Claude", "copilot": "GitHub Copilot"}


@app.get("/api/ai")
@app.get("/api/claude")
def api_ai():
    return jsonify(_ai_status())


@app.post("/api/ai")
def api_ai_set():
    choice = (request.get_json(silent=True) or {}).get("choice")
    if choice not in analyzer.PROVIDERS:
        return jsonify({"error": "Choose claude or copilot"}), 400
    analyzer.set_provider(choice)
    config.save_setting("ai", choice)
    return jsonify(_ai_status())


def _ping(provider, token=None):
    """A tiny real request to the model (with `token`, or the saved one, or the CLI's own login). Returns '' or the reason."""
    try:
        with analyzer.auth_scope(token, provider):
            analyzer._call("ping", "Reply with ok=true.", PING_SCHEMA, config.LIVE_MODEL, timeout=90)
        return ""
    except analyzer.AnalyzerError as exc:
        return str(exc)


@app.post("/api/ai_test")
def api_ai_test():
    """So a missing install, login or token shows up now and not in the middle of a call."""
    err = _ping(analyzer.current_provider())
    return jsonify({"ok": not err, "error": err})


def _token_routes(provider):
    def put():
        import secrets_store
        token = "".join(str((request.get_json(silent=True) or {}).get("token", "")).split())
        if len(token) < 20:
            return jsonify({"detail": f"That does not look like a {NAMES[provider]} token."}), 422
        err = _ping(provider, token)
        if err:
            return jsonify({"detail": f"{NAMES[provider]} did not accept this token: {err}"}), 400
        secrets_store.save(provider, token)
        return jsonify(_ai_status())

    def delete():
        import secrets_store
        secrets_store.clear(provider)
        return "", 204
    put.__name__, delete.__name__ = f"put_{provider}", f"delete_{provider}"
    app.add_url_rule(f"/api/{provider}", view_func=put, methods=["PUT"])
    app.add_url_rule(f"/api/{provider}", view_func=delete, methods=["DELETE"])


for _p in analyzer.PROVIDERS:
    _token_routes(_p)


@app.get("/api/profiles")
def api_profiles():
    out = []
    for p in PROFILES.values():
        out.append({k: p.get(k) for k in ("id", "name", "description", "you_label", "other_label", "subject_label",
                                           "extra", "context_hint", "decisions", "live_sections", "final_sections",
                                           "drafts", "exhibit")}
                   | {"has_rubric": bool(p.get("rubric")), "has_form": bool(p.get("form")),
                      "rubric": p.get("rubric") and {"areas": [{k: a[k] for k in ("id", "name", "weight")} for a in p["rubric"]["areas"]],
                                                     "advance_threshold": p["rubric"]["advance_threshold"]},
                      "form": p.get("form") and {"title": p["form"]["title"], "categories": p["form"]["categories"]}})
    return jsonify(out)


# ---------------------------------------------------------------------------- state (polled every second)
@app.get("/api/state")
def api_state():
    sid = request.args.get("sid", "")
    since = int(request.args.get("since", 0) or 0)
    rev = int(request.args.get("rev", -1) or -1)
    first = request.args.get("first") == "1"
    sessions = [s.list_item() for s in sorted(S.SESSIONS.values(), key=lambda s: s.started, reverse=True)]
    out = {"sessions": sessions, "live_sid": (S.live_session() or None) and S.live_session().sid,
           "capture_models": capture.MODELS.status}
    s = S.SESSIONS.get(sid)
    if s:
        out["live"] = s.live_block(since, first)
        out["rev"] = s.rev
        if s.rev != rev or first:
            out["detail"] = s.detail_block()
            if first:
                out["detail"]["notes"] = s.notes
        out["events"] = s.events[-60:]
    return jsonify(out)


# ---------------------------------------------------------------------------- session lifecycle
@app.post("/api/start")
def api_start():
    d = body()
    profile = PROFILES.get(d.get("profile"))
    if not profile:
        return err("Unknown profile")
    cur = S.live_session()
    if cur and not d.get("end_current"):
        return jsonify({"error": "live", "live_sid": cur.sid, "live_name": cur.name}), 409
    if cur:
        cur.end()
    context = [c for c in (d.get("context") or "").split("\n") if c.strip()]
    s = S.Session(profile, d.get("subject", ""), d.get("extra") or (profile.get("extra") or {}).get("default", ""), context,
                  role=(d.get("role") or "")[:200], speakers=bool(d.get("speakers", config.SPEAKERS_DEFAULT)))
    S.SESSIONS[s.sid] = s
    s.start()
    return jsonify({"ok": True, "sid": s.sid})


@app.post("/api/end")
def api_end():
    s, e = need()
    if e:
        return e
    s.end()
    return jsonify({"ok": True})


@app.post("/api/pause")
def api_pause():
    s, e = need()
    if e:
        return e
    s.set_paused(bool(body().get("on")))
    return jsonify({"ok": True})


@app.post("/api/listen")
def api_listen():
    s, e = need()
    if e:
        return e
    s.set_listening(bool(body().get("on")))
    return jsonify({"ok": True})


@app.post("/api/analyze")
def api_analyze():
    s, e = need()
    if e:
        return e
    s.force = True
    return jsonify({"ok": True})


@app.post("/api/notes")
def api_notes():
    s, e = need()
    if e:
        return e
    s.set_notes(body().get("text", ""))
    return jsonify({"ok": True})


@app.post("/api/finish")
def api_finish():
    s, e = need()
    if e:
        return e
    return jsonify({"ok": s.finish()})


@app.post("/api/regenerate")
def api_regenerate():
    s, e = need()
    if e:
        return e
    return jsonify({"ok": s.regenerate()})


@app.post("/api/retry")
def api_retry():
    s, e = need()
    if e:
        return e
    if s.state == "review":
        return jsonify({"ok": s.regenerate()})
    return err("Nothing to retry")


@app.post("/api/discard")
def api_discard():
    s, e = need()
    if e:
        return e
    s.discard(bool(body().get("delete")))
    return jsonify({"ok": True})


@app.post("/api/decision")
def api_decision():
    s, e = need()
    if e:
        return e
    if not s.decide(body().get("decision", "")):
        return err("Could not save the decision")
    return jsonify({"ok": True})


@app.post("/api/feedback")
def api_feedback():
    s, e = need()
    if e:
        return e
    return jsonify({"ok": s.set_feedback(body().get("state", ""))})


# ---------------------------------------------------------------------------- context and chat
@app.post("/api/context_add")
def api_context_add():
    s, e = need()
    if e:
        return e
    s.add_context(body().get("text", ""))
    return jsonify({"ok": True})


@app.post("/api/context_remove")
def api_context_remove():
    s, e = need()
    if e:
        return e
    s.remove_context(int(body().get("index", -1)))
    return jsonify({"ok": True})


@app.post("/api/chat")
def api_chat():
    d = body()
    s, e = need(d)
    if e:
        return e
    text = (d.get("text") or "").strip()
    if not text:
        return err("Empty message")
    if d.get("mode") == "context":
        s.chat_context(text)
    else:
        s.chat_ask(text)
    return jsonify({"ok": True})


@app.post("/api/speaker_name")
def api_speaker_name():
    """Give a voice a name (for example Speaker 2 -> Priya). The transcript and the AI use it from then on."""
    d = body()
    s, e = need(d)
    if e:
        return e
    s.set_speaker_name(int(d.get("id", 0)), d.get("name", ""))
    return jsonify({"ok": True})


@app.get("/api/day_summary")
def api_day_summary():
    import time as _time
    date = request.args.get("date") or _time.strftime("%Y-%m-%d")
    return Response(S.day_summary_csv(date), mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="day-summary-{date}.csv"'})


@app.post("/api/chat_dismiss")
def api_chat_dismiss():
    d = body()
    s, e = need(d)
    if e:
        return e
    s.chat_dismiss(d.get("id"))
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------- screenshots, watching, picker
@app.get("/api/targets")
def api_targets():
    return jsonify({"windows": wc.list_windows(), "screens": wc.list_screens()})


@app.get("/api/thumb")
def api_thumb():
    s = S.SESSIONS.get(request.args.get("sid", ""))
    kind = request.args.get("kind")
    if kind == "window":
        src = {"kind": "window", "hwnd": int(request.args.get("hwnd", 0))}
    elif kind == "screen":
        src = {"kind": "screen", "index": int(request.args.get("index", 1))}
    else:
        src = s.source if s else None
    try:
        png = wc.thumb_png(src, int(request.args.get("w", 320)))
    except Exception:
        png = None
    if not png:
        return Response(status=204)
    resp = Response(png, mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/api/restore_window")
def api_restore_window():
    return jsonify({"ok": bool(wc.restore_window(int(body().get("hwnd", 0))))})


def _source_from(d):
    src = d.get("source")
    if not src:
        return None
    if src.get("kind") == "window":
        return {"kind": "window", "hwnd": int(src["hwnd"]), "label": src.get("label", "Window")}
    return {"kind": "screen", "index": int(src.get("index", 1)), "label": src.get("label", "Screen")}


@app.post("/api/watch")
def api_watch():
    d = body()
    s, e = need(d)
    if e:
        return e
    s.set_watch(bool(d.get("on")), _source_from(d))
    return jsonify({"ok": True})


@app.post("/api/watch_interval")
def api_watch_interval():
    d = body()
    s, e = need(d)
    if e:
        return e
    s.watch["interval"] = max(5, min(3600, int(d.get("seconds", 30))))
    s.touch()
    return jsonify({"ok": True})


@app.post("/api/capture_screen")
def api_capture_screen():
    """Grab the picked source now. Returns a token; the exhibit is created by /api/exhibit."""
    d = body()
    s, e = need(d)
    if e:
        return e
    src = _source_from(d) or s.source
    if d.get("source"):
        s.source = src
    try:
        png = wc.frame_png(src)
    except Exception as exc:
        return err(f"Capture failed: {exc}")
    if not png:
        return err("Could not capture that window. Pick the window again.")
    token = base64.urlsafe_b64encode(os.urandom(6)).decode()
    s.pending_shots[token] = png
    return jsonify({"ok": True, "token": token, "image": "data:image/png;base64," + base64.b64encode(png).decode()})


@app.post("/api/exhibit")
def api_exhibit():
    d = body()
    s, e = need(d)
    if e:
        return e
    png = None
    if d.get("token"):
        png = s.pending_shots.pop(d["token"], None)
    elif d.get("image"):
        try:
            png = base64.b64decode(d["image"].split(",", 1)[-1])
        except Exception:
            return err("Bad image")
    if not png:
        return err("No image")
    s.add_exhibit(png, d.get("question", ""), d.get("note", ""))
    return jsonify({"ok": True})


@app.post("/api/exhibit_delete")
def api_exhibit_delete():
    s, e = need()
    if e:
        return e
    return jsonify({"ok": s.exhibit_delete(int(body().get("n", 0)))})


@app.post("/api/exhibit_retry")
def api_exhibit_retry():
    s, e = need()
    if e:
        return e
    return jsonify({"ok": s.exhibit_retry(int(body().get("n", 0)))})


@app.get("/api/exhibit_img/<sid>/<int:n>")
def api_exhibit_img(sid, n):
    s = S.SESSIONS.get(sid)
    ex = s and next((x for x in s.exhibits if x["n"] == n), None)
    if not ex or not (s.dir / ex["file"]).exists():
        return Response(status=404)
    return send_file(s.dir / ex["file"], mimetype="image/png")


# ---------------------------------------------------------------------------- recording
@app.post("/api/record")
def api_record():
    d = body()
    s, e = need(d)
    if e:
        return e
    if d.get("action") == "stop":
        return jsonify({"ok": s.record_stop()})
    if d.get("source"):
        s.source = _source_from(d)
    res = s.record_start(bool(d.get("audio")), bool(d.get("video")), bool(d.get("consent")))
    if res.get("error"):
        return jsonify(res), 400
    return jsonify(res)


@app.get("/api/clip/<sid>/<int:n>/<kind>")
def api_clip(sid, n, kind):
    s = S.SESSIONS.get(sid)
    if not s or kind not in ("audio", "video"):
        return Response(status=404)
    path = s.clip_file(n, kind)
    if not path:
        return Response(status=404)
    mime = "audio/ogg" if kind == "audio" else "video/mp4"
    resp = send_file(path, mimetype=mime, conditional=True,
                     as_attachment=request.args.get("download") == "1", download_name=path.name)
    return resp


@app.post("/api/recording_open")
def api_recording_open():
    s, e = need()
    if e:
        return e
    folder = s.rec_dir if s.rec_dir.exists() else config.RECORDINGS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    n = body().get("n")
    clip_path = n and s.clip_file(int(n), "video") or n and s.clip_file(int(n), "audio")
    try:
        if clip_path:
            subprocess.Popen(["explorer", "/select,", str(clip_path)])
        else:
            os.startfile(str(folder))
    except OSError as exc:
        return err(str(exc))
    return jsonify({"ok": True})


@app.post("/api/recording_delete")
def api_recording_delete():
    d = body()
    s, e = need(d)
    if e:
        return e
    n = d.get("n")
    removed = s.delete_clip(None if d.get("all") else int(n))
    s.touch()
    return jsonify({"ok": True, "removed": removed})


@app.get("/api/export/<sid>")
def api_export(sid):
    s = S.SESSIONS.get(sid)
    if not s:
        return Response(status=404)
    buf = s.zip_bytes(request.args.get("rec") == "1")
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{sid}{'-with-recordings' if request.args.get('rec') == '1' else ''}.zip")


@app.post("/api/open_folder")
def api_open_folder():
    s, e = need()
    if e:
        return e
    try:
        os.startfile(str(s.dir))
    except OSError as exc:
        return err(str(exc))
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------- evaluation form
@app.post("/api/form")
def api_form():
    s, e = need()
    if e:
        return e
    return jsonify({"ok": s.write_form_answers()})


@app.post("/api/fill_form")
def api_fill_form():
    d = body()
    s, e = need(d)
    if e:
        return e
    for m in s.chat:  # a permission card that was accepted
        if m.get("kind") == "permission" and m.get("status") == "pending":
            m["status"] = "accepted"
    if d.get("url"):
        _remember_form_url(d["url"])
    if not s.form_data and s.profile.get("form"):
        return err("Write the form answers first (Fill the evaluation form).")
    res = s.start_fill(d.get("url"))
    if res.get("error"):
        return jsonify(res), 400
    return jsonify(res)


@app.post("/api/fill_cancel")
def api_fill_cancel():
    s, e = need()
    if e:
        return e
    if s.fill:
        s.fill.cancel()
    return jsonify({"ok": True})


def _remember_form_url(url):
    config.FORM_URL = url


# ---------------------------------------------------------------------------- startup
def main():
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    S.load_all(PROFILES)
    removed = S.cleanup_recordings()
    if removed:
        print(f"[recordings] removed {removed} recording(s) older than {config.RECORDING_RETENTION_DAYS} days")
    capture.MODELS.preload() if not config.MOCK_ANALYZER else None
    print(f"Debrief AI on http://127.0.0.1:{config.PORT}  (mock analyzer: {config.MOCK_ANALYZER})")
    app.run(host="127.0.0.1", port=config.PORT, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
