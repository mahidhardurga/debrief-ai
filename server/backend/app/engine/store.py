"""Per-user session registry. Each user's sessions live in their own folder, in the same on-disk format as the
desktop edition (session.json, transcript.jsonl, analysis.json, report.md, ...):

    <DATA_DIR>/users/<user id>/sessions/<session id>/...
    <DATA_DIR>/users/<user id>/recordings/<session id>/...
"""
import json
import threading
import time
from functools import lru_cache

from ..config import get_settings
from ..db import SessionLocal
from ..models import User
from ..services import vault
from . import SHARED, analyzer  # noqa: F401  (importing the package installs the LLM backend)
from .session import Session

_stores: dict[str, "UserStore"] = {}
_stores_lock = threading.Lock()


@lru_cache
def profiles() -> dict[str, dict]:
    """Call profiles (shared/profiles/*.json, the same files the desktop edition uses), interview first."""
    out = {}
    for path in sorted((SHARED / "profiles").glob("*.json")):
        try:
            prof = json.loads(path.read_text(encoding="utf-8"))
            out[prof["id"]] = prof
        except Exception as exc:                      # a broken profile must not stop the server
            print(f"[profiles] skipping {path.name}: {exc}")
    return out


class UserStore:
    def __init__(self, user_id: str):
        self.user_id = user_id
        root = get_settings().data_dir / "users" / user_id
        self.sessions_dir, self.rec_root = root / "sessions", root / "recordings"
        self.sessions: dict[str, Session] = {}
        self.lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if not self.sessions_dir.exists():
            return
        for p in sorted(self.sessions_dir.iterdir()):
            if p.is_dir() and (p / "session.json").exists():
                try:
                    s = Session.load(self, p, profiles())
                    if s:
                        self.sessions[s.sid] = s
                except Exception as exc:
                    print(f"[load] skipping {p.name}: {exc}")

    def auth(self) -> tuple[str, str | None]:
        """(the model this user picked, their own token for it); the token is None when they have not connected one."""
        db = SessionLocal()
        try:
            user = db.get(User, self.user_id)
            choice = vault.choice_for(user)
            return choice, vault.token_for(user, choice)
        finally:
            db.close()

    def live(self) -> Session | None:
        return next((s for s in self.sessions.values() if s.state == "live"), None)

    def new_sid(self, date: str, base: str) -> str:
        with self.lock:
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
            n = 1 + len([p for p in self.sessions_dir.glob(f"{date}_*") if p.is_dir()])
            return f"{date}_{n:02d}_{base}"

    def update_day_summary(self, date: str) -> None:
        import reports  # shared/core

        from . import analyzer as _a  # noqa: F401
        rows = [{"date": s.date, "time": s.time, "name": s.name, "type": s.profile["name"], "decision": s.decision,
                 "score": (s.final or {}).get("weighted_score") or "", "feedback": s.feedback.get("state", "")}
                for s in list(self.sessions.values())]
        csv_text, md = reports.day_summary(rows, date)
        try:
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
            (self.sessions_dir / f"day-summary-{date}.csv").write_text(csv_text, encoding="utf-8")
            (self.sessions_dir / f"day-summary-{date}.md").write_text(md, encoding="utf-8")
        except OSError:
            pass

    def cleanup_recordings(self) -> int:
        """Delete whole recordings older than RECORDING_RETENTION_DAYS (0 keeps everything)."""
        days = get_settings().recording_retention_days
        if days <= 0 or not self.rec_root.exists():
            return 0
        cutoff, removed = time.time() - days * 86400, 0
        for s in self.sessions.values():
            for c in list(s.clips):
                files = [s.rec_dir / c[k] for k in ("audio_file", "video_file") if c.get(k)]
                existing = [f for f in files if f.exists()]
                if existing and all(f.stat().st_mtime < cutoff for f in existing):
                    for f in existing:
                        f.unlink(missing_ok=True)
                    s.clips.remove(c)
                    removed += 1
            if removed:
                s.save()
        return removed


def get_store(user_id: str) -> UserStore:
    with _stores_lock:
        if user_id not in _stores:
            _stores[user_id] = UserStore(user_id)
        return _stores[user_id]


def drop_all() -> None:
    """For tests."""
    with _stores_lock:
        for st in _stores.values():
            for s in st.sessions.values():
                s.stop_evt.set()
        _stores.clear()


def end_all_live() -> None:
    """Server shutdown: stop session loops (sessions are saved after every change and reload as 'review')."""
    with _stores_lock:
        for st in _stores.values():
            for s in st.sessions.values():
                s.stop_evt.set()
