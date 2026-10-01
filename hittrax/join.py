"""Join a HitTrax PlaysExport to its SessionExport.

Locked (phase-1, import #200):

    Plays.SnId == Session.Id     the session
    Plays.UsId == Session.UsId   the player, also on the session row
    Plays.SId  / Session.SId     facility / site — not the session

Exit velocity is Plays.Velo. It matches Session.MEV. EBV1, EBV2 and EBV3 are
vector components of the ball flight, not a speed, and must not be mapped.

The bytes returned here are what ``raw_imports.sha256`` dedupes. Raw Plays and
Session files stay on the droplet.
"""

from __future__ import annotations

import csv
import io
import re
import sys
from datetime import datetime, timezone

from . import units

# ingest.sniff stops at MAX_ROWS and would store a silently truncated day.
# Keep this equal to ingest.MAX_ROWS; test_hittrax.py pins the two together.
MAX_PLAY_ROWS = 200_000

# Collect-ready header, in the phase-1 column order. The second element is the
# column_maps metric or role. Scale is not stored here: it is always 1.0,
# because units.py already converted. See seed.seed_hittrax_column_maps.
COLLECT_COLUMNS = (
    ("player", "player"),
    ("vendor_id", "vendor_id"),
    ("date", "date"),
    ("session", "session"),
    ("exit_velocity", "exit_velocity"),
    ("launch_angle", "launch_angle"),
    ("distance", "distance"),
)

PLAYS_REQUIRED = ("SnId", "Velo", "Elv", "Dist", "TS", "UsId")
SESSION_REQUIRED = ("Id", "UserName", "UsId")

# Below this, the join key is wrong or the pair is not a pair. Fail before
# store() so the bad bytes are not sha-deduped and a corrected run can proceed.
MIN_MATCH_RATE = 0.50

_TS_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%m/%d/%Y",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M:%S.%f",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%y",
    "%m/%d/%y %H:%M:%S",
    "%m/%d/%y %I:%M:%S %p",
)


class JoinError(Exception):
    """The pair cannot be turned into a collect-ready CSV."""


class OversizedExport(Exception):
    """Plays file is over the byte cap or the row cap. Do not ingest.

    The message starts with ``oversized_export_skipped size=`` so a log scrape
    can see why the cron exited non-zero. A 700 MB September dump must take
    the human / chunking path, never a partial commit.
    """

    def __init__(self, name, size=None, limit=None, rows=None, row_limit=None):
        self.name = name
        self.size = size
        self.limit = limit
        self.rows = rows
        self.row_limit = row_limit
        parts = ["oversized_export_skipped"]
        if size is not None:
            parts.append(f"size={size}")
        if limit is not None:
            parts.append(f"limit={limit}")
        if rows is not None:
            parts.append(f"rows={rows}")
        if row_limit is not None:
            parts.append(f"row_limit={row_limit}")
        parts.append(f"file={name}")
        super().__init__(" ".join(parts))


def collect_fieldnames():
    return [name for name, _role in COLLECT_COLUMNS]


def read_csv(raw, label):
    """(fieldnames, rows) from CSV bytes. Header is line 1; cells are stripped."""
    if isinstance(raw, str):
        text = raw
    else:
        text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise JoinError(f"no header in {label}")
    fields = list(reader.fieldnames)
    if len(fields) != len(set(fields)):
        print(f"[hittrax] warn: duplicate headers in {label}: {fields}", file=sys.stderr)
    rows = []
    for row in reader:
        cleaned = {}
        for key, value in row.items():
            if key is None:
                continue
            cleaned[key] = (value or "").strip()
        if any(cleaned.values()):
            rows.append(cleaned)
    return fields, rows


def ts_to_date(value):
    """Plays.TS → ``YYYY-MM-DD``.

    A datetime string keeps the calendar date written in the file. We do not
    shift it into another timezone: the export does not say which zone TS is.
    A bare number is Unix time, seconds or milliseconds, read as UTC.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"-", "--", "n/a", "na", "null", "none", "nan"}:
        return None
    if text.endswith("Z"):
        text = text[:-1].strip()
    text = re.sub(r"[+-]\d{2}:\d{2}$", "", text).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        number = float(text)
        if number > 10**12:          # milliseconds
            number /= 1000.0
        if number < 10**9 or number > 10**11:
            return None
        try:
            return datetime.fromtimestamp(number, timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    match = re.match(r"^(\d{4}-\d{2}-\d{2})\b", text)
    if not match:
        return None
    try:
        datetime.strptime(match.group(1), "%Y-%m-%d")
    except ValueError:
        return None
    return match.group(1)


def _require(fields, required, label):
    missing = [col for col in required if col not in fields]
    if missing:
        raise JoinError(f"{label} missing {', '.join(missing)}; have {fields[:40]}")


def join_exports(plays_bytes, session_bytes, max_rows=MAX_PLAY_ROWS):
    """Return ``(csv_bytes, stats)``.

    Unmatched plays are dropped, not written with an empty player. A match
    rate under 50% raises ``JoinError`` before any bytes a caller might store.
    """
    play_fields, plays = read_csv(plays_bytes, "Plays")
    session_fields, sessions = read_csv(session_bytes, "Session")
    _require(play_fields, PLAYS_REQUIRED, "Plays")
    _require(session_fields, SESSION_REQUIRED, "Session")
    # Velo is required above, so a file that only has EBV1/EBV2/EBV3 cannot
    # sneak through as exit velocity. Those columns are never read below.
    if not plays:
        raise JoinError("Plays export has no data rows")
    if len(plays) > max_rows:
        raise OversizedExport(
            "Plays", rows=len(plays), row_limit=max_rows,
            size=len(plays_bytes) if isinstance(plays_bytes, (bytes, bytearray)) else None)

    by_id = {}
    duplicate_sessions = 0
    for session in sessions:
        sid = (session.get("Id") or "").strip()
        if not sid:
            continue
        if sid in by_id:
            duplicate_sessions += 1
        by_id[sid] = session                 # last writer wins, as the reference joiner did

    written_rows = []
    matched = unmatched = bad_date = empty_measures = 0
    for play in plays:
        # SnId, not SId. SId is the facility and will cheerfully match the
        # wrong session when a site id collides with some other session's Id.
        key = (play.get("SnId") or "").strip()
        session = by_id.get(key)
        if session is None:
            unmatched += 1
            continue
        matched += 1
        day = ts_to_date(play.get("TS"))
        if not day:
            bad_date += 1
            continue
        # Velo only. EBV* stays on the play row and is not copied out.
        velo = units.ms_to_mph(play.get("Velo"))
        elevation = units.as_degrees(play.get("Elv"))
        distance = units.m_to_ft(play.get("Dist"))
        if velo is None and elevation is None and distance is None:
            empty_measures += 1
            continue
        vendor_id = (session.get("UsId") or "").strip() or (play.get("UsId") or "").strip()
        written_rows.append({
            "player": (session.get("UserName") or "").strip(),
            "vendor_id": vendor_id,
            "date": day,
            "session": key,
            "exit_velocity": units.format_measure(velo),
            "launch_angle": units.format_measure(elevation),
            "distance": units.format_measure(distance),
        })

    total = matched + unmatched
    rate = (matched / total) if total else 0.0
    if rate < MIN_MATCH_RATE:
        raise JoinError(
            "join_match_too_low key=Plays.SnId->Session.Id "
            f"matched={matched} unmatched={unmatched} rate={rate:.1%}")
    if not written_rows:
        raise JoinError(
            "no collect-ready rows "
            f"(matched={matched} bad_date={bad_date} empty_measures={empty_measures})")

    # LF only, so the sha256 does not change between a Windows dry run and
    # the Linux cron for the same pair.
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=collect_fieldnames(), lineterminator="\n")
    writer.writeheader()
    for row in written_rows:
        writer.writerow(row)

    stats = {
        "plays": len(plays),
        "session_index": len(by_id),
        "duplicate_session_ids": duplicate_sessions,
        "matched": matched,
        "unmatched": unmatched,
        "bad_date": bad_date,
        "empty_measures": empty_measures,
        "written": len(written_rows),
        "match_rate": round(rate, 4),
        "join_key": "Plays.SnId->Session.Id",
        "ev_column": "Velo",
    }
    return buffer.getvalue().encode("utf-8"), stats
