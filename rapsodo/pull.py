"""
Rapsodo puller.

    Daily:    python src/pull.py                      (yesterday)
    Backfill: python src/pull.py --start 2025-08-19 --end 2026-08-19

Writes two things per run:
  raw/<shot_type>/<session_id>.json   the untouched API payload, kept forever so we can
                                      re-derive everything without re-hitting the API
  out/shots_<start>_<end>.csv         a flat table ready for the DB loader

Dry-run by default in spirit: it only reads from Rapsodo and writes locally. Nothing
touches Postgres until load_db.py runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rapsodo_client import SHOT_TYPES, RapsodoClient, RapsodoAuthError  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "raw"
OUT_DIR = PROJECT_ROOT / "out"

# Nested dicts we flatten rather than drop -- the confidence sub-scores are useful for
# filtering bad tracks later.
NESTED_PREFIXES = ("debugInfo", "confidences", "videoInfo", "strikeZoneBreakdown")


def flatten_shot(shot: dict) -> dict:
    """One shot -> one flat row. Nested objects become dotted columns."""
    row: dict = {}
    for key, val in shot.items():
        if isinstance(val, dict):
            for sub, subval in val.items():
                row[f"{key}.{sub}"] = subval
        elif isinstance(val, list):
            row[key] = json.dumps(val)  # keep, don't explode
        else:
            row[key] = val
    return row


def pull(start: datetime, end: datetime) -> pd.DataFrame:
    client = RapsodoClient.from_env()

    players = client.fetch_players(start, end)
    print(f"[players] {len(players)} active between {start:%Y-%m-%d} and {end:%Y-%m-%d}")

    all_rows: list[dict] = []
    n_sessions = 0

    for row in players:
        p = row.get("player", {})
        player_id = p.get("_id")
        if player_id is None:
            continue
        name = f"{p.get('firstName','?')} {p.get('lastName','?')}".strip()

        for shot_type in SHOT_TYPES:
            sessions = client.fetch_sessions(player_id, shot_type, start, end)
            if not sessions:
                continue

            for sess in sessions:
                session_id = sess.get("_id") or sess.get("objectID")
                if not session_id:
                    continue

                shots = client.fetch_shots(session_id, player_id, shot_type)
                n_sessions += 1

                # Archive the raw payload before touching it. The session-list
                # payload carries only `playerId` -- no name, no email -- so we
                # stash the player record alongside it. Without this the archive
                # can't be loaded on its own without re-querying /v3/reports.
                raw_path = RAW_DIR / shot_type / f"{session_id}.json"
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_text(
                    json.dumps(
                        {"player": p, "session": sess, "shots": shots}, indent=1
                    )
                )

                for shot in shots:
                    flat = flatten_shot(shot)
                    # Session/player context the shot payload doesn't carry itself.
                    flat.update(
                        {
                            "shot_type": shot_type,
                            "session_id": session_id,
                            "session_name": sess.get("sessionName"),
                            "session_type": sess.get("sessionType"),
                            "session_date_epoch": sess.get("date") or sess.get("startedAt"),
                            "device_name": sess.get("deviceName"),
                            "player_id": player_id,
                            "player_name": name,
                            "player_email": p.get("email"),
                            "grad_year": p.get("highSchoolGradYear"),
                        }
                    )
                    all_rows.append(flat)

                print(f"  {name:<22} {shot_type:<6} {session_id}  {len(shots):>4} shots")

    df = pd.DataFrame(all_rows)
    if not df.empty:
        df["session_date"] = pd.to_datetime(
            df["session_date_epoch"], unit="s", errors="coerce"
        )
    print(f"[done] {n_sessions} sessions, {len(df)} shots")
    return df


def main() -> int:
    yesterday = datetime.utcnow().date() - timedelta(days=1)

    ap = argparse.ArgumentParser(description="Pull Rapsodo data.")
    ap.add_argument("--start", help="YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--end", help="YYYY-MM-DD (default: yesterday)")
    args = ap.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d") if args.start else datetime.combine(
        yesterday, datetime.min.time()
    )
    end = datetime.strptime(args.end, "%Y-%m-%d") if args.end else datetime.combine(
        yesterday, datetime.max.time().replace(microsecond=0)
    )

    try:
        df = pull(start, end)
    except RapsodoAuthError as e:
        print(f"[auth] {e}", file=sys.stderr)
        return 2

    if df.empty:
        print("[out] nothing to write")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"shots_{start:%Y%m%d}_{end:%Y%m%d}.csv"
    df.to_csv(out_path, index=False)
    print(f"[out] {out_path}  ({len(df)} rows x {len(df.columns)} cols)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
