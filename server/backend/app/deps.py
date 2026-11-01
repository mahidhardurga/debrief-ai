from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .db import get_db
from .models import User
from .security import decode_token

bearer = HTTPBearer(auto_error=False)
COOKIE = "dbf_session"        # HttpOnly + SameSite=Strict: lets <img>/<video>/WebSocket requests authenticate without headers


def user_from_token(db: Session, token: str) -> User | None:
    uid = decode_token(token) if token else None
    return db.get(User, uid) if uid else None


def current_user(request: Request, cred: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)) -> User:
    """The signed-in user: `Authorization: Bearer <token>` (API clients) or the session cookie (the web app)."""
    user = user_from_token(db, cred.credentials if cred else request.cookies.get(COOKIE, ""))
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    return user
