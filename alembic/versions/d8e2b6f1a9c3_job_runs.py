"""job_runs: a durable record of every scheduled-job run

Two scheduled jobs were silently dead for months in 2026 (Rapsodo from March,
change detection never scheduled at all). Nothing recorded that they had not
run, so nothing could notice. This table is written as the LAST step of the
nightly job: a row means the job finished, `ok` says whether every step
succeeded, and `summary` holds per-step results plus the newest data date per
source. The home page and the freshness alert read it.

Revision ID: d8e2b6f1a9c3
Revises: c3f5a91d4e27
Create Date: 2026-09-21
"""
from alembic import op
import sqlalchemy as sa

revision = "d8e2b6f1a9c3"
down_revision = "c3f5a91d4e27"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "job_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("job", sa.String(40), nullable=False),
        sa.Column("ran_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("ok", sa.Boolean, nullable=False),
        sa.Column("summary", sa.JSON),
    )
    op.create_index("ix_job_runs_job_ran", "job_runs", ["job", "ran_at"])


def downgrade():
    op.drop_index("ix_job_runs_job_ran", table_name="job_runs")
    op.drop_table("job_runs")
