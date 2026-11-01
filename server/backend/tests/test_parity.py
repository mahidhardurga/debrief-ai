"""Keeps the desktop and server editions in step. Both implement the same session engine and the same /api routes for the
shared UI (shared/ui), so a feature added to one must be added to the other. This test compares them (by reading the
source, nothing from the desktop edition is imported) and fails on any difference that is not listed below with a reason.
To add a feature to both editions, just do so; to add one that can only exist in one edition, list it here and say why."""
import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
DESKTOP_SESSION = REPO / "desktop/backend/session.py"
DESKTOP_ROUTES = REPO / "desktop/backend/server.py"
SERVER_SESSION = REPO / "server/backend/app/engine/session.py"
SERVER_ROUTERS = REPO / "server/backend/app/routers"

pytestmark = pytest.mark.skipif(not DESKTOP_SESSION.exists(), reason="the desktop edition is not part of this checkout")

# Session methods that exist in one edition only, and why
DESKTOP_ONLY_METHODS = {
    "start_fill": "opens the portal in an in-app window and fills the form (a browser cannot drive another window)",
    "_fill_changed": "progress of that form-filling window",
    "_watch_loop": "grabs frames of a window itself; in the browser the page grabs them and uploads them",
    "on_audio": "feeds the local audio recorder",
    "_new_sid": "the server's per-user store names sessions",
    "update_day_summary": "the server's per-user store writes the day summary",
}
SERVER_ONLY_METHODS = {
    "_demo_feed": "replays the sample call in mock mode (the desktop has DemoCapture)",
    "_upload_watchdog": "browser recordings are uploaded in pieces",
    "clip_upload_path": "browser recordings are uploaded in pieces",
    "clip_uploaded": "browser recordings are uploaded in pieces",
    "is_echo": "echo suppression runs in the desktop's capture code",
    "llm": "every call uses the signed-in user's own Claude login",
    "rec_root_ok": "per-user recordings folder",
}
# /api routes that exist in one edition only, and why
DESKTOP_ONLY_ROUTES = {
    "/api/targets": "lists windows and screens (the browser has its own picker)",
    "/api/thumb": "window thumbnails",
    "/api/capture_screen": "grabs a window itself (the browser grabs the frame)",
    "/api/restore_window": "restores a minimised window",
    "/api/open_folder": "opens a folder on this PC",
    "/api/recording_open": "opens a folder on this PC",
    "/api/ai_test": "tests the CLI and login on this PC (the server tests a token when the user connects it)",
}
SERVER_ONLY_ROUTES = {
    "/api/auth/register": "accounts", "/api/auth/login": "accounts", "/api/auth/logout": "accounts", "/api/auth/me": "accounts",
    "/api/record_chunk": "browser recordings are uploaded in pieces", "/api/record_done": "browser recordings are uploaded in pieces",
}


def session_methods(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Session")
    return {f.name for f in cls.body if isinstance(f, ast.FunctionDef)}


def returned_keys(path: Path, func: str) -> set[str]:
    """The string keys of the dict a Session method returns (what the UI receives)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Session")
    fn = next(f for f in cls.body if isinstance(f, ast.FunctionDef) and f.name == func)
    keys = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            keys |= {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    return keys


def desktop_routes() -> set[str]:
    src = DESKTOP_ROUTES.read_text(encoding="utf-8")
    out = {re.sub(r"<(?:int:)?(\w+)>", r"{\1}", p) for p in re.findall(r'@app\.(?:get|post)\("(/api/[^"]*)"', src)}
    if 'add_url_rule(f"/api/{provider}"' in src:         # the pasted-token routes are registered in a loop over both models
        out |= {"/api/claude", "/api/copilot"}
    return out


def server_routes() -> set[str]:
    out = set()
    for f in SERVER_ROUTERS.glob("*.py"):
        src = f.read_text(encoding="utf-8")
        m = re.search(r'APIRouter\(prefix="([^"]*)"', src)
        if m:
            out |= {m.group(1) + p for p in re.findall(r'@router\.(?:get|post|put|delete)\("([^"]*)"', src)}
    return out


def test_session_engines_have_the_same_methods():
    d, s = session_methods(DESKTOP_SESSION), session_methods(SERVER_SESSION)
    assert d - s == set(DESKTOP_ONLY_METHODS), f"desktop has methods the server lacks (add them or explain them above): {sorted(d - s - set(DESKTOP_ONLY_METHODS))}"
    assert s - d == set(SERVER_ONLY_METHODS), f"server has methods the desktop lacks (add them or explain them above): {sorted(s - d - set(SERVER_ONLY_METHODS))}"


def test_both_editions_serve_the_same_api():
    d, s = desktop_routes(), server_routes()
    assert d - s == set(DESKTOP_ONLY_ROUTES), f"desktop-only routes not explained: {sorted(d - s - set(DESKTOP_ONLY_ROUTES))}"
    assert s - d == set(SERVER_ONLY_ROUTES), f"server-only routes not explained: {sorted(s - d - set(SERVER_ONLY_ROUTES))}"


@pytest.mark.parametrize("func", ["list_item", "live_block", "detail_block"])
def test_the_ui_receives_the_same_data(func):
    d, s = returned_keys(DESKTOP_SESSION, func), returned_keys(SERVER_SESSION, func)
    assert d == s, f"{func}: only desktop {sorted(d - s)}, only server {sorted(s - d)}"


def test_the_start_form_and_settings_match():
    """Options the start screen offers must be accepted by both /api/start routes."""
    d = DESKTOP_ROUTES.read_text(encoding="utf-8")
    s = (SERVER_ROUTERS / "api.py").read_text(encoding="utf-8")
    for option in ("role", "speakers", "end_current", "context", "extra", "subject"):
        assert f'"{option}"' in d, f"desktop /api/start ignores {option}"
        assert f'"{option}"' in s, f"server /api/start ignores {option}"


# ---- the GitHub Pages edition (pages/engine/*.js) has to keep up with the same features ----
PAGES_SESSION = REPO / "pages/engine/session.js"
PAGES_API = REPO / "pages/engine/api.js"
PAGES_MISSING_METHODS = {                # desktop methods the Pages engine does not have, with the reason
    "_append_jsonl": "sessions are saved whole into IndexedDB, not appended to files",
    "rec_dir": "recordings are blobs in IndexedDB, not a folder",
}
PAGES_MISSING_ROUTES = {
    "/api/targets": "lists windows (the browser has its own picker)", "/api/thumb": "window thumbnails",
    "/api/capture_screen": "grabs a window itself", "/api/restore_window": "restores a minimised window",
    "/api/open_folder": "opens a folder on this PC", "/api/recording_open": "opens a folder on this PC",
    "/api/export": "the page builds the zip itself and downloads it", "/api/day_summary": "the page builds the CSV itself",
    "/api/exhibit_img": "images are blob URLs inside the page", "/api/clip": "recordings are blob URLs inside the page",
    "/api/ai": "the Pages edition picks its model in Settings (an Anthropic API key)", "/api/claude": "same: Settings", "/api/copilot": "same: Settings", "/api/ai_test": "the Pages edition picks its model in Settings",
    "/api/record_chunk": "no server: the page keeps the recording", "/api/record_done": "no server: the page keeps the recording",
}


def pages_methods() -> set[str]:
    src = PAGES_SESSION.read_text(encoding="utf-8")
    body = src[src.index("export class Session "):]
    names = set(re.findall(r"^  (?:static )?(?:async )?(?:get )?([A-Za-z_]\w*)\(.*\) \{", body, re.M))
    return (names - {"constructor"}) | {"__init__"}


def pages_routes() -> set[str]:
    return {re.sub(r"/:\w+", "/{id}", p) for p in re.findall(r'"(?:GET|POST) (/api/[^"]*)"', PAGES_API.read_text(encoding="utf-8"))}


@pytest.mark.skipif(not PAGES_SESSION.exists(), reason="the Pages edition is not part of this checkout")
def test_pages_engine_has_the_session_methods():
    d, p = session_methods(DESKTOP_SESSION), pages_methods()
    missing = d - p - set(DESKTOP_ONLY_METHODS) - set(PAGES_MISSING_METHODS)
    assert not missing, f"the Pages engine lacks these Session methods (port them or explain them above): {sorted(missing)}"


@pytest.mark.skipif(not PAGES_API.exists(), reason="the Pages edition is not part of this checkout")
def test_pages_engine_answers_the_same_api():
    d, p = desktop_routes(), pages_routes()
    missing = {r for r in d - p if not any(r == m or r.startswith(m + "/") for m in PAGES_MISSING_ROUTES)} - set(DESKTOP_ONLY_ROUTES)
    assert not missing, f"the Pages engine does not answer these routes (add them or explain them above): {sorted(missing)}"
