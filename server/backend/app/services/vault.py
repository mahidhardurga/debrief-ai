"""Encrypted storage for each user's Claude login token (and, optionally, a GitHub token with Copilot) and which of the two they use.

Tokens are encrypted with Fernet (AES-128-CBC + HMAC) before they reach the database. The key is ENCRYPTION_KEY,
or derived from SECRET_KEY when it is empty, so back up and keep that value stable: without it stored tokens
cannot be decrypted and users must reconnect. Tokens are never returned by the API or written to logs.
"""
import base64
import hashlib
from datetime import UTC, datetime

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import User


def _fernet() -> Fernet:
    s = get_settings()
    material = s.secret_key
    key = s.encryption_key.strip()
    if key and not key.startswith("#"):          # a stray "# comment" from a .env line is not a key
        try:
            return Fernet(key.encode())          # a proper Fernet key is used as is
        except ValueError:
            material = key                       # any other string is treated as a passphrase
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"debrief-claude-vault|" + material.encode()).digest()))


def encrypt(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def decrypt(blob: str) -> str | None:
    try:
        return _fernet().decrypt(blob.encode()).decode()
    except (InvalidToken, ValueError):
        return None


PROVIDERS = ("claude", "copilot")


def save_token(db: Session, user: User, token: str, provider: str = "claude") -> None:
    setattr(user, f"{provider}_token_enc", encrypt(token))
    setattr(user, f"{provider}_connected_at", datetime.now(UTC))
    db.commit()


def clear_token(db: Session, user: User, provider: str = "claude") -> None:
    setattr(user, f"{provider}_token_enc", None)
    setattr(user, f"{provider}_connected_at", None)
    db.commit()


def token_for(user: User | None, provider: str = "claude") -> str | None:
    """The user's own token for that model, or None when they have not connected one (or it can no longer be decrypted)."""
    blob = getattr(user, f"{provider}_token_enc", None) if user else None
    return decrypt(blob) if blob else None


def choice_for(user: User | None) -> str:
    c = getattr(user, "ai_choice", None) if user else None
    return c if c in PROVIDERS else "claude"
