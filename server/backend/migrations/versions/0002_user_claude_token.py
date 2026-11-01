"""per-user Claude login

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("claude_token_enc", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("claude_connected_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("users", "claude_connected_at")
    op.drop_column("users", "claude_token_enc")
