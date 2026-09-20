"""widen session_type and purpose to 64

Rapsodo's sessionType is vendor-controlled text, and two of the values RECON.md
already documents are 21 characters:

    "Live Batting Practice"
    "Soft Toss/Front Flips"

Both overflow String(20). SQLite silently accepts the overflow, so every local
test and dry-run passed; Postgres raises StringDataRightTruncation and aborts the
transaction. The nightly Rapsodo load therefore died the first time a Live BP
session appeared in its window and never recovered -- production Rapsodo data
stops at 2026-03-24 while the API had sessions through today.

This is the same failure mode CLAUDE.md records for pitch_type: a width that is
correct for the values someone had in front of them, wrong for the values the
vendor actually sends, and invisible outside Postgres.

64 rather than 22: the point is to stop sizing this column to today's list. It is
free-text from a vendor who adds session types without telling us.

`purpose` is widened alongside it -- same kind of field, same exposure, and
leaving it at 20 just moves the next outage.

Revision ID: c3f5a91d4e27
Revises: a1c7f2e9b035
Create Date: 2026-09-20
"""
from alembic import op
import sqlalchemy as sa

revision = "c3f5a91d4e27"
down_revision = "a1c7f2e9b035"
branch_labels = None
depends_on = None

# (table, column, nullable) -- nullable must be preserved or alter_column will
# quietly drop the NOT NULL on sessions.session_type.
COLS = [
    ("raw_imports", "session_type", True),
    ("raw_imports", "purpose", True),
    ("sessions", "session_type", False),
    ("sessions", "purpose", True),
]


def upgrade():
    for table, col, nullable in COLS:
        op.alter_column(table, col,
                        existing_type=sa.String(20),
                        type_=sa.String(64),
                        existing_nullable=nullable)


def downgrade():
    # Narrowing truncates. Any row already holding a >20 char session type would
    # fail or be cut, so this is deliberately lossy and only safe on a database
    # that never took a wide value.
    for table, col, nullable in COLS:
        op.alter_column(table, col,
                        existing_type=sa.String(64),
                        type_=sa.String(20),
                        existing_nullable=nullable)
