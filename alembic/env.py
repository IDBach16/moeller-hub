"""Alembic environment.

The schema lives in db.py as SQLAlchemy Core Table objects, so autogenerate
compares against db.metadata directly. The URL comes from db.database_url(),
which means migrations follow the same rule the app does: SQLite locally,
Railway Postgres when DATABASE_URL is set.
"""
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402  -- needs the path above

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", db.database_url().replace("%", "%%"))
target_metadata = db.metadata


def _opts(**extra):
    return dict(
        target_metadata=target_metadata,
        compare_type=True,              # catch column type changes, not just adds
        # Off on purpose: SQLite renders server defaults back differently from
        # the way the metadata declares them, so this reports a change on
        # player_baselines.pitch_type on every run. A permanent false positive
        # is worse than not catching default changes.
        compare_server_default=False,
        # SQLite cannot ALTER most things; batch mode rebuilds the table instead,
        # so the same migration runs locally and on Postgres.
        render_as_batch=db.database_url().startswith("sqlite"),
        **extra,
    )


def run_migrations_offline():
    context.configure(url=config.get_main_option("sqlalchemy.url"),
                      literal_binds=True, dialect_opts={"paramstyle": "named"},
                      **_opts())
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, **_opts())
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
