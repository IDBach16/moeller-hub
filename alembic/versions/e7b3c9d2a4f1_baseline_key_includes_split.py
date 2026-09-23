"""player_baselines: the primary key includes pitch_type

The model has always keyed a baseline on (player, metric, pitch_type, window_end)
-- a fastball's ride and a slider's are different measurements, and on the
hitting side a tee swing and a live one are too. Production never got that key.
b4413b1e0d33 created the table with (player, metric, window_end); 48bcef2a7ac1
made pitch_type NOT NULL and a1c7f2e9b035 widened it, and neither touched the
key. Every local database is built from the model, so every test passed.

It surfaced on 2026-09-23 when a Blast load left one hitter with two drills
whose windows ended on the same day: the second baseline insert collided on
the three-column key and change detection aborted for the whole run.

Rebuilding the key cannot fail on existing rows -- the old key already forbids
any two rows that the new one would consider duplicates.

Revision ID: e7b3c9d2a4f1
Revises: d8e2b6f1a9c3
Create Date: 2026-09-23
"""
from alembic import op

revision = "e7b3c9d2a4f1"
down_revision = "d8e2b6f1a9c3"
branch_labels = None
depends_on = None

KEY = ["player_id", "metric_key", "pitch_type", "window_end"]


def upgrade():
    # SQLite databases are created from the model and already carry this key;
    # rebuilding a SQLite table for it would only add risk.
    if op.get_bind().dialect.name == "sqlite":
        return
    op.drop_constraint("player_baselines_pkey", "player_baselines", type_="primary")
    op.create_primary_key("player_baselines_pkey", "player_baselines", KEY)


def downgrade():
    if op.get_bind().dialect.name == "sqlite":
        return
    # Would fail if two drills of one metric share a window_end -- which is the
    # exact state the upgrade exists to allow. Clear those rows first if you
    # genuinely need to go back; they are recomputed by the next detection run.
    op.drop_constraint("player_baselines_pkey", "player_baselines", type_="primary")
    op.create_primary_key("player_baselines_pkey", "player_baselines",
                          ["player_id", "metric_key", "window_end"])
