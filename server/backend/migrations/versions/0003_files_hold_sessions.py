"""sessions now live as files per user (shared UI and engine); the database keeps users only

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP TABLE IF EXISTS exhibits")
    op.execute("DROP TABLE IF EXISTS lines")
    op.execute("DROP TABLE IF EXISTS call_sessions")


def downgrade():
    pass  # the old session tables are not recreated; their content was test data from the first server version
