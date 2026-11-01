import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import COOKIE, current_user
from ..models import User
from ..schemas import Credentials, TokenOut, UserOut
from ..security import create_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])
_attempts: dict[str, deque] = defaultdict(deque)


def _throttle(key: str, limit: int = 8, window: int = 60) -> None:
    """Tiny in-memory login throttle (per email+ip). Put a real rate limiter in front for internet exposure."""
    q, now = _attempts[key], time.time()
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "Too many attempts. Try again in a minute.")
    q.append(now)


def _set_cookie(request: Request, response: Response, token: str) -> None:
    mode = get_settings().cookie_secure
    secure = mode == "always" or (mode == "auto" and request.url.scheme == "https")
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=secure,
                        max_age=get_settings().access_token_minutes * 60, path="/")


@router.post("/register", response_model=TokenOut, status_code=201)
def register(body: Credentials, request: Request, response: Response, db: Session = Depends(get_db)):
    first = db.query(User).count() == 0
    if not first and not get_settings().allow_registration:
        raise HTTPException(403, "Registration is closed.")
    email = body.email.lower()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(409, "That email is already registered.")
    user = User(email=email, password_hash=hash_password(body.password), role="admin" if first else "user")
    db.add(user)
    db.commit()
    token = create_token(user.id)
    _set_cookie(request, response, token)
    return TokenOut(access_token=token)


@router.post("/login", response_model=TokenOut)
def login(body: Credentials, request: Request, response: Response, db: Session = Depends(get_db)):
    email = body.email.lower()
    _throttle(f"{email}|{request.client.host if request.client else ''}")
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Wrong email or password.")
    token = create_token(user.id)
    _set_cookie(request, response, token)
    return TokenOut(access_token=token)


@router.post("/logout", status_code=204)
def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)):
    return user
