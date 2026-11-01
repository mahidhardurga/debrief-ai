"""Password hashing (bcrypt) and signed access tokens (JWT)."""
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from .config import get_settings

ALGO = "HS256"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode()[:72], hashed.encode())
    except ValueError:
        return False


def create_token(user_id: str) -> str:
    s = get_settings()
    exp = datetime.now(UTC) + timedelta(minutes=s.access_token_minutes)
    return jwt.encode({"sub": user_id, "exp": exp}, s.secret_key, algorithm=ALGO)


def decode_token(token: str) -> str | None:
    try:
        return jwt.decode(token, get_settings().secret_key, algorithms=[ALGO]).get("sub")
    except jwt.PyJWTError:
        return None
