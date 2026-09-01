"""Run pending Alembic migrations.

Called at boot from app.py. The schema used to be built by metadata.create_all,
which creates missing TABLES but never adds COLUMNS to tables that already
exist -- so a column added in the code silently never reached the deployed
database. Migrations close that gap.

Safe to call repeatedly: alembic does nothing when the database is already at
head. Never raises; a hub that cannot serve is worse than one running a
migration behind, and the failure is printed loudly enough to find.
"""
import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def upgrade_to_head():
    """-> (ok, message). Never raises."""
    try:
        from alembic import command
        from alembic.config import Config
    except ImportError:
        return False, "alembic is not installed -- schema left as-is"

    ini = os.path.join(APP_DIR, "alembic.ini")
    if not os.path.exists(ini):
        return False, f"no alembic.ini at {ini}"
    try:
        cfg = Config(ini)
        cfg.set_main_option("script_location", os.path.join(APP_DIR, "alembic"))
        command.upgrade(cfg, "head")
        return True, "database at head"
    except Exception as e:                                   # pragma: no cover
        return False, f"{type(e).__name__}: {e}"


def current_revision():
    """The revision the database is stamped at, or None."""
    try:
        from alembic.migration import MigrationContext
        import db
        with db.get_engine().connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    except Exception:
        return None


if __name__ == "__main__":
    ok, msg = upgrade_to_head()
    print(("[migrate] " if ok else "[migrate] FAILED -- ") + msg)
    print(f"[migrate] revision: {current_revision()}")
