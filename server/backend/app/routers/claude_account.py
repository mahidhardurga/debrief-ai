"""Each user's own AI connection, and which model they use. A user can connect Claude (a long-lived token from
`claude setup-token`) and/or GitHub Copilot (a GitHub token with the "Copilot Requests" permission) and pick one of them.
A token is verified with a tiny test call, then stored encrypted (services/vault.py) and used only for that user's
analyses. It is never sent back to the browser."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import current_user
from ..llm import LLMError, get_llm, get_llm_for
from ..models import User
from ..services import vault
from .auth import _throttle

router = APIRouter(prefix="/api", tags=["claude"])
PING_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}


class TokenIn(BaseModel):
    token: str = Field(min_length=20, max_length=4000)


class ChoiceIn(BaseModel):
    choice: str = Field(pattern="^(claude|copilot)$")


NAMES = {"claude": "Claude", "copilot": "GitHub Copilot"}


def _one(user: User, provider: str) -> dict:
    token_ok = vault.token_for(user, provider) is not None
    return {"required": bool(getattr(get_llm_for(provider), "needs_user_credentials", False)), "connected": token_ok,
            "connected_at": getattr(user, f"{provider}_connected_at") if token_ok else None}


def status_for(user: User) -> dict:
    """The top-level fields describe the model this user picked; `providers` has both."""
    choice = vault.choice_for(user)
    providers = {p: _one(user, p) for p in vault.PROVIDERS}
    return {**providers[choice], "choice": choice, "name": NAMES[choice], "providers": providers,
            "provider": get_settings().llm_provider}


def require_claude(user: User) -> None:
    """Raise 409 when the model this user picked needs their own login and they have not connected it."""
    s = status_for(user)
    if s["required"] and not s["connected"]:
        raise HTTPException(409, f"Connect your {s['name']} account first (menu: AI model).")


@router.get("/config")
def public_config(db: Session = Depends(get_db)):
    """Used by the sign-in page: is sign-up open, and does this server use per-user Claude logins."""
    s = get_settings()
    users = db.scalar(func.count(User.id)) or 0
    return {"registration_open": s.allow_registration or users == 0, "first_user": users == 0,
            "llm_provider": s.llm_provider, "per_user_claude": bool(getattr(get_llm(), "needs_user_credentials", False)),
            # what the shared UI (shared/ui/app.js) reads
            "platform": "web", "mock": s.llm_provider == "mock", "recordings_dir": "", "recordings_cloud_warning": False,
            "require_consent": s.require_consent, "form_url": "", "analyze_interval": s.analyze_interval,
            "watch": {"change": s.watch_change, "stable": s.watch_stable, "force": s.watch_force},
            "asr_partials": s.asr_partials}


@router.get("/claude")
def claude_status(user: User = Depends(current_user)):
    return status_for(user)


def _connect(provider: str, body: TokenIn, user: User, db: Session) -> dict:
    token = "".join(body.token.split())                 # tolerate a token pasted across several lines
    if len(token) < 20:
        raise HTTPException(422, f"That does not look like a {NAMES[provider]} token.")
    _throttle(f"{provider}|{user.id}", limit=5, window=60)
    try:                                                # a tiny real call proves the token works before it is stored
        get_llm_for(provider).structured("Reply with ok=true.", "ping", PING_SCHEMA, get_settings().live_model, auth=token)
    except LLMError as e:
        raise HTTPException(400, f"{NAMES[provider]} did not accept this token: {e}")
    vault.save_token(db, user, token, provider)
    return status_for(user)


@router.put("/claude")
def connect_claude(body: TokenIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _connect("claude", body, user, db)


@router.delete("/claude", status_code=204)
def disconnect_claude(user: User = Depends(current_user), db: Session = Depends(get_db)):
    vault.clear_token(db, user, "claude")


@router.put("/copilot")
def connect_copilot(body: TokenIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _connect("copilot", body, user, db)


@router.delete("/copilot", status_code=204)
def disconnect_copilot(user: User = Depends(current_user), db: Session = Depends(get_db)):
    vault.clear_token(db, user, "copilot")


@router.get("/ai")
def ai_status(user: User = Depends(current_user)):
    return status_for(user)


@router.post("/ai")
def choose_ai(body: ChoiceIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Pick which model analyses this user's calls (takes effect for the next call, including a live one)."""
    user.ai_choice = body.choice
    db.commit()
    return status_for(user)
