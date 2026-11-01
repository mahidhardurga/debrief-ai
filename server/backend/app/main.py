import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy import text

from .asr import engine as asr_engine
from .config import get_settings
from .db import Base, engine
from .engine import BACKEND, SHARED
from .engine.store import end_all_live
from .routers import api, audio, auth, claude_account

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
UI = SHARED / "ui"                                            # the same UI the desktop edition serves
PLATFORM_JS = next((p / "platform.js" for p in (BACKEND / "frontend", BACKEND.parent / "frontend") if (p / "platform.js").exists()),
                   BACKEND.parent / "frontend" / "platform.js")


def _add_missing_columns() -> None:
    """SQLite dev databases made by an older version get the columns added since (Postgres uses alembic)."""
    with engine.begin() as c:
        have = {r[1] for r in c.execute(text("pragma table_info(users)"))}
        for col, ddl in (("copilot_token_enc", "TEXT"), ("copilot_connected_at", "DATETIME"),
                         ("ai_choice", "VARCHAR(16) NOT NULL DEFAULT 'claude'")):
            if have and col not in have:
                c.execute(text(f"ALTER TABLE users ADD COLUMN {col} {ddl}"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    if s.database_url.startswith("sqlite"):
        Base.metadata.create_all(engine)         # dev/SQLite convenience; Postgres uses `alembic upgrade head`
        _add_missing_columns()
    asr_engine.start_preload()
    yield
    end_all_live()


def _page(path: Path, media: str) -> FileResponse:
    return FileResponse(path, media_type=media, headers={"Cache-Control": "no-store"})


def create_app() -> FastAPI:
    app = FastAPI(title="Debrief AI", version="2.0.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=get_settings().cors_list, allow_credentials=False,
                       allow_methods=["*"], allow_headers=["*"])
    app.include_router(auth.router)
    app.include_router(claude_account.router)
    app.include_router(api.router)
    app.include_router(audio.router)

    @app.get("/api/health")
    def health():
        with engine.connect() as c:
            c.execute(text("select 1"))
        return {"ok": True, "llm": get_settings().llm_provider, "claude_auth": get_settings().claude_auth,
                "asr": asr_engine.status, "asr_error": asr_engine.model_error}

    @app.get("/", include_in_schema=False)
    def index():
        return _page(UI / "index.html", "text/html")

    @app.get("/app.js", include_in_schema=False)
    def app_js():
        return _page(UI / "app.js", "text/javascript")

    @app.get("/app.css", include_in_schema=False)
    def app_css():
        return _page(UI / "app.css", "text/css")

    @app.get("/browser.js", include_in_schema=False)
    def browser_js():
        return _page(UI / "browser.js", "text/javascript")

    @app.get("/aimodel.js", include_in_schema=False)
    def aimodel_js():
        return _page(UI / "aimodel.js", "text/javascript")

    @app.get("/platform.js", include_in_schema=False)
    def platform_js():
        return _page(PLATFORM_JS, "text/javascript")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        from fastapi import Response
        return Response(status_code=204)

    return app


app = create_app()
