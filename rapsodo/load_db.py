"""
Load pulled Rapsodo sessions into the player-development Postgres.

This writes into the schema defined by Moeller_Hub/db.py (branch
`player-development-system`), reusing its tables rather than inventing parallel
ones: players / player_vendor_ids / raw_imports / sessions / pitch_metrics /
name_review.

    python src/load_db.py --dry-run     inspect what would be written (default)
    python src/load_db.py --commit      actually write

Nothing is guessed. A Rapsodo player we can't resolve to a Moeller Player ID is
queued in name_review, never attributed to a best-guess player -- same rule as
ingest.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "raw"

# db.py and metrics.py sit in the repo root, one level up from this package --
# this pipeline lives inside Player_Dev_Hub precisely so there is exactly one
# copy of the schema and it can never drift from what the app uses.
HUB_PATH = Path(os.environ.get("HUB_PATH", PROJECT_ROOT)).resolve()

if not (HUB_PATH / "db.py").exists():
    raise SystemExit(
        f"Can't find db.py under {HUB_PATH}.\n"
        "This script expects to live in rapsodo/ inside the Player_Dev_Hub repo,\n"
        "alongside db.py and metrics.py. Set HUB_PATH if it's somewhere else."
    )

sys.path.insert(0, str(HUB_PATH))
import db  # noqa: E402
import metrics  # noqa: E402
from sqlalchemy import delete, insert, select  # noqa: E402

VENDOR = "rapsodo"


# ---------------------------------------------------------------------------
# Field mapping
# ---------------------------------------------------------------------------
# Rapsodo API field -> canonical metric key in metrics.REGISTRY.
# Only mapped fields become metrics; the other ~78 stay in the raw JSON archive
# and can be promoted later by adding a line here.

PITCH_METRIC_MAP = {
    "speed": "velocity",
    "spin": "spin_rate",
    "verticalBreak": "induced_vertical_break",
    "horizontalBreak": "horizontal_break",
    "spinEfficiency": "spin_efficiency",
    "releaseHeight": "release_height",
    "releaseSide": "release_side",
}

# 0 and 4 were confirmed by reproducing the Rapsodo UI's own per-type averages
# against the raw shots. 6, 3 and 5 were confirmed by Ian from their pitch
# profiles across an 873-pitch sample (see RECON.md for the numbers).
#
# Codes NOT listed here resolve to None, which routes the pitch to QC rather than
# coercing it into a pitch we made up. Code 1 (n=13) and code 2 (n=1) are
# deliberately absent -- too few pitches and too ambiguous a shape to call.
PITCH_TYPE_CODES = {
    0: "FB",   # 81.1 mph, 1939 rpm, +13.9 VB, 91% eff
    3: "CB",   # 72.6 mph, -9.4 VB -- the most downward break in the set
    4: "SL",   # 71.9 mph, -3.4 VB, -4.5 HB, 58 deg gyro
    5: "SI",   # 79.8 mph, fastball shape with more armside run
    6: "CH",   # 75.0 mph, 1177 rpm -- low spin, armside, ~6 off the fastball
}


def map_pitch_type(code):
    return PITCH_TYPE_CODES.get(code)


def is_valid_shot(shot: dict) -> bool:
    """
    Failed tracks come back with speed=null / isValidForStrike=false and would
    drag a pitcher's averages down hard (Eli Singer's session: 3 of 16 rows,
    82.5 mph -> 67.0 if kept). The Rapsodo UI excludes them; so do we.
    """
    return shot.get("speed") is not None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def _name_key(name: str) -> str:
    import re

    s = str(name or "").strip()
    if "," in s:
        parts = [p.strip() for p in s.split(",", 1)]
        if len(parts) == 2 and parts[1]:
            s = f"{parts[1]} {parts[0]}"
    return re.sub(r"\s+", " ", s).lower()


def _vendor_lookup(conn) -> dict:
    return {
        str(r.vendor_id): r.player_id
        for r in conn.execute(
            select(db.player_vendor_ids.c.vendor_id, db.player_vendor_ids.c.player_id)
            .where(db.player_vendor_ids.c.vendor == VENDOR)
        )
    }


def _player_lookup(conn) -> dict:
    lookup = {}
    for r in conn.execute(
        select(db.players.c.id, db.players.c.first_name, db.players.c.last_name)
    ):
        lookup[_name_key(f"{r.first_name} {r.last_name}")] = r.id
    for r in conn.execute(
        select(db.player_aliases.c.player_id, db.player_aliases.c.alias)
    ):
        lookup.setdefault(_name_key(r.alias), r.player_id)
    return lookup


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load(dry_run: bool = True) -> dict:
    engine = db.get_engine()
    files = sorted(RAW_DIR.glob("*/*.json"))
    if not files:
        raise SystemExit(f"No raw sessions under {RAW_DIR}. Run pull.py first.")

    stats = {
        "files": len(files),
        "sessions_written": 0,
        "sessions_existing": 0,
        "metrics_written": 0,
        "shots_kept": 0,
        "shots_dropped": 0,
        "vendor_links": 0,
        "unresolved": {},
        "unmapped_pitch_types": {},
    }

    new_vendor_links: dict[str, tuple] = {}

    # One transaction for the whole load: a backfill either lands completely or not
    # at all, so a failure partway through can't leave half a season in the database.
    ctx = engine.connect() if dry_run else engine.begin()
    with ctx as conn:
        vendor_ids = _vendor_lookup(conn)
        names = _player_lookup(conn)

        for path in files:
            payload = json.loads(path.read_text())
            sess = payload["session"]
            shots = payload["shots"]
            shot_type = path.parent.name  # pitch | hit

            # The session-list payload carries only `playerId` (camelCase) and no
            # player object -- `player_id` / nested `player` exist on the /v3/session
            # detail endpoint, not this one. pull.py stashes the player record at the
            # top level so the archive is self-describing.
            p = payload.get("player") or sess.get("player") or {}
            rap_player_id = str(
                p.get("_id")
                or sess.get("playerId")
                or sess.get("player_id")
                or ""
            )
            # /v3/reports spells these camelCase; /v3/session spells them snake_case.
            first = p.get("firstName") or p.get("first_name") or ""
            last = p.get("lastName") or p.get("last_name") or ""
            raw_name = f"{first} {last}".strip()

            # Vendor id first -- it's stable. Fall back to name only if we've
            # never seen this Rapsodo id before.
            player_id = vendor_ids.get(rap_player_id) or names.get(_name_key(raw_name))
            if not player_id:
                # Cut / JV / alumni arms who aren't on the roster. We do NOT invent a
                # player record for them -- the name is queued instead, and the raw
                # archive keeps their sessions so a later roster update can pick them
                # up by re-running the load.
                stats["unresolved"].setdefault(
                    raw_name or rap_player_id, {"rapsodo_id": rap_player_id, "sessions": 0}
                )["sessions"] += 1
                continue

            # Resolved by name this time -- record the vendor id so every future load
            # matches on a stable id and never has to guess from a name again.
            if rap_player_id not in vendor_ids:
                new_vendor_links[rap_player_id] = (player_id, raw_name)

            epoch = sess.get("date") or sess.get("startedAt")
            session_date = datetime.fromtimestamp(epoch, tz=timezone.utc).date()
            source_ref = sess.get("_id") or sess.get("objectID")

            existing = conn.execute(
                select(db.sessions.c.id)
                .where(db.sessions.c.source == VENDOR)
                .where(db.sessions.c.source_ref == source_ref)
            ).first()

            # Provenance: keep the untouched payload in raw_imports too, so the
            # database alone is enough to re-derive everything.
            sha = hashlib.sha256(path.read_bytes()).hexdigest()

            valid = [s for s in shots if is_valid_shot(s)]
            stats["shots_kept"] += len(valid)
            stats["shots_dropped"] += len(shots) - len(valid)

            rows = []
            for seq, shot in enumerate(sorted(valid, key=lambda s: s.get("pitch_id") or 0), 1):
                code = shot.get("pitchType")
                ptype = map_pitch_type(code)
                if ptype is None and code is not None:
                    stats["unmapped_pitch_types"][code] = (
                        stats["unmapped_pitch_types"].get(code, 0) + 1
                    )

                ts = None
                if shot.get("pitch_id"):
                    ts = datetime.fromtimestamp(shot["pitch_id"], tz=timezone.utc)

                for field, key in PITCH_METRIC_MAP.items():
                    val = shot.get(field)
                    if val is None:
                        continue
                    rows.append(
                        {
                            "player_id": player_id,
                            "seq": seq,
                            "ts": ts,
                            "pitch_type": ptype,
                            "metric_key": key,
                            "value": float(val),
                        }
                    )
                    # The registry tracks fastball velocity separately from
                    # overall velocity, and it's a headline metric.
                    if key == "velocity" and ptype == "FB":
                        rows.append(
                            {
                                "player_id": player_id,
                                "seq": seq,
                                "ts": ts,
                                "pitch_type": ptype,
                                "metric_key": "fb_velocity",
                                "value": float(val),
                            }
                        )

            if existing:
                stats["sessions_existing"] += 1
            else:
                stats["sessions_written"] += 1
            stats["metrics_written"] += len(rows)

            if dry_run:
                continue

            import_row = conn.execute(
                select(db.raw_imports.c.id).where(db.raw_imports.c.sha256 == sha)
            ).first()
            if import_row:
                import_id = import_row.id
            else:
                import_id = conn.execute(
                    insert(db.raw_imports).values(
                        vendor=VENDOR,
                        filename=path.name,
                        sha256=sha,
                        uploaded_by="rapsodo_pull",
                        row_count=len(shots),
                        payload=payload,
                        status="committed",
                        side="pitching" if shot_type == "pitch" else "hitting",
                        session_type=sess.get("sessionType"),
                        note=f"API pull, session {source_ref}",
                    ).returning(db.raw_imports.c.id)
                ).scalar()

            if existing:
                session_id = existing.id
                # Re-ingest is idempotent: replace this session's metrics.
                conn.execute(
                    delete(db.pitch_metrics).where(
                        db.pitch_metrics.c.session_id == session_id
                    )
                )
            else:
                session_id = conn.execute(
                    insert(db.sessions)
                    .values(
                        player_id=player_id,
                        session_date=session_date,
                        session_type="bullpen",
                        source=VENDOR,
                        source_ref=source_ref,
                        notes=sess.get("sessionName"),
                        import_id=import_id,
                    )
                    .returning(db.sessions.c.id)
                ).scalar()

            if rows:
                conn.execute(
                    insert(db.pitch_metrics),
                    [{**r, "session_id": session_id} for r in rows],
                )

        stats["vendor_links"] = len(new_vendor_links)

        if not dry_run:
            # Pin each resolved player to their Rapsodo id.
            for rap_id, (player_id, _name) in new_vendor_links.items():
                conn.execute(
                    insert(db.player_vendor_ids).values(
                        player_id=player_id, vendor=VENDOR, vendor_id=rap_id
                    )
                )

            # Queue the unmatched names once each (not once per session), so a
            # roster update can accept them later and a re-run picks up their
            # sessions from the raw archive.
            already = {
                r.raw_name
                for r in conn.execute(
                    select(db.name_review.c.raw_name)
                    .where(db.name_review.c.vendor == VENDOR)
                )
            }
            for name in stats["unresolved"]:
                if name in already:
                    continue
                conn.execute(
                    insert(db.name_review).values(
                        vendor=VENDOR,
                        raw_name=name,
                        status="open",
                    )
                )

    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Load Rapsodo pulls into the player-dev DB.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True)
    g.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    dry = not args.commit
    stats = load(dry_run=dry)

    print(f"{'DRY RUN' if dry else 'COMMITTED'} -- {db.database_url().split('@')[-1]}")
    for k in ("files", "sessions_written", "sessions_existing",
              "shots_kept", "shots_dropped", "metrics_written", "vendor_links"):
        print(f"  {k:<20} {stats[k]}")
    if stats["unresolved"]:
        stranded = sum(i["sessions"] for i in stats["unresolved"].values())
        print(f"  not on the roster -- queued for review, NOT created "
              f"({len(stats['unresolved'])} names, {stranded} sessions held):")
        for name, info in stats["unresolved"].items():
            print(f"    {name:<24} rapsodo_id={info['rapsodo_id']}  {info['sessions']} sessions")
    if stats["unmapped_pitch_types"]:
        print(f"  unmapped pitchType codes (QC): {stats['unmapped_pitch_types']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
