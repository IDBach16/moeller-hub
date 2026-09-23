"""
rapsodo/reload_from_db.py -- re-run the loader over payloads already stored in
raw_imports, without touching the API.

Why this exists: the nightly pull runs in a cron container whose raw/ folder
does not survive the run. The one copy of every payload that does survive is
raw_imports.payload. So when the LOADER changes -- as it did on 2026-09-23,
when hitting sessions stopped being filed as bullpens -- this is how the
already-pulled sessions get re-filed under the new rules. load_db.load() is
idempotent per session and repairs a session's type and rows in place, so
running this is safe to repeat.

    python rapsodo/reload_from_db.py            dry run over every payload
    python rapsodo/reload_from_db.py --hits     only shotType "hit" payloads
    python rapsodo/reload_from_db.py --commit   write
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from sqlalchemy import select  # noqa: E402


def _shot_type(payload: dict) -> str:
    sess = payload.get("session") or {}
    st = sess.get("shotType") or payload.get("shotType")
    if st in ("pitch", "hit"):
        return st
    # Older archives predate the shotType field: a batted ball has a launch
    # angle and no pitch type.
    shots = payload.get("shots") or []
    return "hit" if shots and "launchAngle" in shots[0] and "pitchType" not in shots[0] else "pitch"


def main(argv: list[str]) -> int:
    commit = "--commit" in argv
    only_hits = "--hits" in argv
    engine = db.get_engine()

    with engine.connect() as conn:
        rows = conn.execute(
            select(db.raw_imports.c.id, db.raw_imports.c.filename, db.raw_imports.c.payload)
            .where(db.raw_imports.c.vendor == "rapsodo")
            .order_by(db.raw_imports.c.id)).all()

    tmp = Path(tempfile.mkdtemp(prefix="rapsodo_reload_"))
    written = {"pitch": 0, "hit": 0}
    for r in rows:
        payload = r.payload if isinstance(r.payload, dict) else json.loads(r.payload)
        kind = _shot_type(payload)
        if only_hits and kind != "hit":
            continue
        d = tmp / kind
        d.mkdir(parents=True, exist_ok=True)
        # Keyed by import id: group hitting sessions share a filename per session.
        (d / f"{r.id}__{r.filename or 'import.json'}").write_text(
            json.dumps(payload), encoding="utf-8")
        written[kind] += 1

    print(f"staged from raw_imports: {written['pitch']} pitch, {written['hit']} hit payload(s)")
    if not sum(written.values()):
        print("nothing to reload")
        return 0

    import load_db
    load_db.RAW_DIR = tmp
    stats = load_db.load(dry_run=not commit)
    print(f"{'COMMITTED' if commit else 'DRY RUN'}")
    for k in ("files", "sessions_written", "sessions_existing", "shots_kept",
              "shots_dropped", "metrics_written", "hit_sessions", "swings_written"):
        print(f"  {k:<20} {stats[k]}")
    if stats.get("untagged_hit_types"):
        print(f"  untagged hitting session types: {stats['untagged_hit_types']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
