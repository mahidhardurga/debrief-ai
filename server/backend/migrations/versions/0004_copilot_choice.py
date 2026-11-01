"""GitHub Copilot as a second model, chosen per user

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("copilot_token_enc", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("copilot_connected_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("ai_choice", sa.String(length=16), nullable=False, server_default="claude"))


def downgrade():
    op.drop_column("users", "ai_choice")
    op.drop_column("users", "copilot_connected_at")
    op.drop_column("users", "copilot_token_enc")
