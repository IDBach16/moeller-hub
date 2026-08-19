"""
Entrypoint for the scheduled Rapsodo job.

    python src/daily.py

Pulls a window, archives the raw JSON, and commits to the player-dev database.
Designed to run as a Railway cron service, where DATABASE_URL resolves to the
private Postgres domain and no public proxy is needed.

Environment:
    RAPSODO_EMAIL / RAPSODO_PASSWORD   required -- the job logs in for itself
    RAPSODO_BACKFILL_DAYS              optional -- pull this many days back instead
                                       of just yesterday. Set to 365 for the first
                                       run, then remove it.
    RAPSODO_LOOKBACK_DAYS              optional, default 3. Re-pulls the last N days
                                       every night rather than only yesterday.
    DATABASE_URL                       the player-dev Postgres
    HUB_PATH                           where db.py / metrics.py live (default: cwd)

Exit codes: 0 ok, 2 auth problem, 1 anything else. The scheduler treats non-zero
as a failed run, so don't swallow errors.
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rapsodo_client import RapsodoAuthError, load_dotenv  # noqa: E402


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def main() -> int:
    load_dotenv()

    # Sessions get uploaded from the device late, and a device that was offline can
    # backdate. Re-pulling a few days each night costs almost nothing and is
    # idempotent -- (source, source_ref) dedupes sessions, so a re-pull overwrites
    # rather than duplicates. Only pulling "yesterday" would silently miss late data.
    backfill = _int_env("RAPSODO_BACKFILL_DAYS", 0)
    lookback = _int_env("RAPSODO_LOOKBACK_DAYS", 3)
    days = backfill or lookback

    today = datetime.now(timezone.utc).date()
    start = datetime.combine(today - timedelta(days=days), datetime.min.time())
    end = datetime.combine(today, datetime.max.time().replace(microsecond=0))

    mode = f"BACKFILL {days}d" if backfill else f"daily (lookback {lookback}d)"
    print(f"[rapsodo] {mode}: {start:%Y-%m-%d} .. {end:%Y-%m-%d}", flush=True)

    import pull

    df = pull.pull(start, end)

    if df.empty:
        print("[rapsodo] no shots in window; nothing to load")
        return 0

    # Always keep a CSV alongside the DB write -- it's what a coach can actually open.
    out_dir = Path(__file__).resolve().parent.parent / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"shots_{start:%Y%m%d}_{end:%Y%m%d}.csv"
    df.to_csv(csv_path, index=False)
    print(f"[rapsodo] wrote {csv_path} ({len(df)} rows)")

    if not os.environ.get("DATABASE_URL"):
        print("[rapsodo] DATABASE_URL unset -- pulled and wrote CSV, skipped DB load")
        return 0

    import load_db

    stats = load_db.load(dry_run=False)
    print(
        f"[rapsodo] loaded: {stats['sessions_written']} new sessions, "
        f"{stats['sessions_existing']} existing, {stats['metrics_written']} metrics, "
        f"{stats['shots_dropped']} failed tracks dropped"
    )
    if stats["unresolved"]:
        print(f"[rapsodo] UNRESOLVED PLAYERS (queued, not guessed): {stats['unresolved']}")
    if stats["unmapped_pitch_types"]:
        print(f"[rapsodo] UNMAPPED pitchType codes: {stats['unmapped_pitch_types']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RapsodoAuthError as e:
        print(f"[rapsodo][auth] {e}", file=sys.stderr)
        raise SystemExit(2)
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
