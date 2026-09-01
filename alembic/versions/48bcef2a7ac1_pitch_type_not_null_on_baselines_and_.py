"""pitch_type not null on baselines and change events

Revision ID: 48bcef2a7ac1
Revises: b4413b1e0d33
Create Date: 2026-09-01 16:35:38.593435

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '48bcef2a7ac1'
down_revision: Union[str, Sequence[str], None] = 'b4413b1e0d33'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # batch_alter_table, not alter_column: SQLite cannot ALTER a column in
    # place, so batch mode rebuilds the table instead. On Postgres it emits the
    # plain ALTER. Autogenerate wrote this against Postgres and would have
    # produced SQL sqlite refuses to parse.
    with op.batch_alter_table("player_baselines") as batch:
        batch.alter_column("pitch_type",
                           existing_type=sa.VARCHAR(length=4),
                           nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("player_baselines") as batch:
        batch.alter_column("pitch_type",
                           existing_type=sa.VARCHAR(length=4),
                           nullable=True)
