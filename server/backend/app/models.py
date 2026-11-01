import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="user")      # admin | user
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    # the user's own Claude login (output of `claude setup-token`), encrypted at rest; see services/vault.py
    claude_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    claude_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ... and optionally a GitHub token that has Copilot (the other model the user can pick), encrypted the same way
    copilot_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    copilot_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ai_choice: Mapped[str] = mapped_column(String(16), default="claude", server_default="claude")   # claude | copilot
