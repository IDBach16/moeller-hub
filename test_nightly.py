"""
test_nightly.py -- Rapsodo silence on the overnight job.

An empty Cloud pull exits 0. That is still true: off-season really is empty.
What used to stay green forever is a trail older than the silence window.
These checks never hit Rapsodo or the real database.

    python test_nightly.py
"""

import importlib.util
import io
import os
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone

FAILS = []


def check(label, condition, detail=""):
    mark = "ok  " if condition else "FAIL"
    extra = f"  -- {detail}" if detail and not condition else ""
    print(f"  [{mark}] {label}{extra}")
    if not condition:
        FAILS.append(label)


HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(tempfile.mkdtemp(), "nightly.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TMP.replace("\\", "/")
os.environ.pop("RAILWAY_ENVIRONMENT", None)
os.environ.pop("HUB_PASSWORD", None)
os.environ.pop("AUTO_SEED", None)
# A developer shell may already pin these. The defaults under test are the
# unset ones: overnight lookback 7, silence = lookback + 2.
os.environ.pop("RAPSODO_LOOKBACK_DAYS", None)
os.environ.pop("RAPSODO_STALE_AFTER_DAYS", None)
os.environ.pop("RAPSODO_BACKFILL_DAYS", None)
sys.path.insert(0, HERE)

from sqlalchemy import delete, insert  # noqa: E402

import db                               # noqa: E402
import nightly                          # noqa: E402

ENGINE = db.get_engine()
db.metadata.create_all(ENGINE)
TODAY = date(2026, 10, 1)


def section(title):
    print(f"\n{title}")


def _daily_module():
    path = os.path.join(HERE, "rapsodo", "daily.py")
    spec = importlib.util.spec_from_file_location("rapsodo_daily_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def player_id():
    with ENGINE.begin() as conn:
        return conn.execute(insert(db.players).values(
            slug="nightly-arm", first_name="Nightly", last_name="Arm",
        )).inserted_primary_key[0]


PID = player_id()
_sha = 0


def reset_rows():
    with ENGINE.begin() as conn:
        conn.execute(delete(db.sessions))
        conn.execute(delete(db.raw_imports))


def add_session(when, source="rapsodo", session_type="bullpen", ref=None):
    global _sha
    _sha += 1
    with ENGINE.begin() as conn:
        conn.execute(insert(db.sessions).values(
            player_id=PID, session_date=when, session_type=session_type,
            source=source, source_ref=ref or f"{source}-{session_type}-{when}-{_sha}",
        ))


def add_file(vendor, when, sha=None):
    global _sha
    _sha += 1
    stamp = when if isinstance(when, datetime) else datetime.combine(when, datetime.min.time())
    with ENGINE.begin() as conn:
        conn.execute(insert(db.raw_imports).values(
            vendor=vendor, filename=f"{vendor}-{_sha}.json",
            sha256=sha or f"sha-{_sha}", uploaded_at=stamp, payload={},
            status="committed",
        ))


def seed_fresh_blast():
    add_file("blast", datetime.now(timezone.utc))


# --------------------------------------------------------------------------
section("1. empty Cloud is still a successful pull, and the counts say so")

daily = _daily_module()
check("direct daily.py default lookback stays 3",
      daily._int_env("RAPSODO_LOOKBACK_DAYS", 3) == 3)

import pandas as pd
import types
fake_pull = types.ModuleType("pull")


def _empty_pull(start, end):
    print(f"[players] 0 active between {start:%Y-%m-%d} and {end:%Y-%m-%d}")
    print("[done] 0 sessions, 0 shots")
    return pd.DataFrame()


fake_pull.pull = _empty_pull
sys.modules["pull"] = fake_pull
buf = io.StringIO()
try:
    with redirect_stdout(buf):
        code = daily.main()
finally:
    sys.modules.pop("pull", None)
empty_log = buf.getvalue()
check("empty window exits 0", code == 0, empty_log)
empty_counts = nightly.rapsodo_counts(empty_log)
check("empty window is sessions_new=0 and 0 players",
      empty_counts == {"sessions_new": 0, "players_active": 0}, str(empty_counts))

loaded_log = (
    "[players] 4 active between 2026-09-24 and 2026-10-01\n"
    "[done] 6 sessions, 40 shots\n"
    "[rapsodo] loaded: 2 new sessions, 4 existing, 30 metrics, 0 failed tracks dropped\n"
)
loaded_counts = nightly.rapsodo_counts(loaded_log)
check("a real load reports sessions_new and players_active",
      loaded_counts == {"sessions_new": 2, "players_active": 4}, str(loaded_counts))

check("zero players reads as '0 players' in the subject phrase",
      nightly.rapsodo_ingest_phrase(
          {"ok": True, "sessions_new": 0, "players_active": 0})
      == "Rapsodo sessions_new=0, 0 players")
check("a non-empty pull keeps the players_active= form",
      nightly.rapsodo_ingest_phrase(
          {"ok": True, "sessions_new": 2, "players_active": 4})
      == "Rapsodo sessions_new=2, players_active=4")


# --------------------------------------------------------------------------
section("2. overnight lookback is 7; a direct run is not")

parent_before = os.environ.get("RAPSODO_LOOKBACK_DAYS")
child, days = nightly.rapsodo_child_env()
check("unset lookback injects 7 for the child only",
      days == 7 and child.get("RAPSODO_LOOKBACK_DAYS") == "7")
check("the parent process is not given that 7",
      os.environ.get("RAPSODO_LOOKBACK_DAYS") == parent_before)

os.environ["RAPSODO_LOOKBACK_DAYS"] = "14"
try:
    child, days = nightly.rapsodo_child_env()
    check("an explicit lookback is what the cron will use",
          days == 14 and child.get("RAPSODO_LOOKBACK_DAYS") == "14")
    check("silence default sits two days past that lookback",
          nightly.rapsodo_stale_after_days() == 16)
finally:
    os.environ.pop("RAPSODO_LOOKBACK_DAYS", None)

os.environ["RAPSODO_LOOKBACK_DAYS"] = "nope"
try:
    child, days = nightly.rapsodo_child_env()
    check("a non-integer lookback falls back to 7 in the child",
          days == 7 and child.get("RAPSODO_LOOKBACK_DAYS") == "7")
finally:
    os.environ.pop("RAPSODO_LOOKBACK_DAYS", None)

check("unset silence window is 9 (overnight 7 + 2)",
      nightly.rapsodo_stale_after_days() == 9)
os.environ["RAPSODO_STALE_AFTER_DAYS"] = "5"
try:
    check("RAPSODO_STALE_AFTER_DAYS overrides the derived window",
          nightly.rapsodo_stale_after_days() == 5)
finally:
    os.environ.pop("RAPSODO_STALE_AFTER_DAYS", None)


# --------------------------------------------------------------------------
section("3. silence is the newer of session date and last file, not one empty night")

limit = 9
fresh = nightly.rapsodo_silence(TODAY - timedelta(days=2), TODAY - timedelta(days=1),
                                 TODAY, limit)
check("a session inside the week is ok", fresh["problem"] is None and fresh["status"] == "ok")

exact = nightly.rapsodo_silence(TODAY - timedelta(days=limit), None, TODAY, limit)
check("exactly at the limit is not yet ATTENTION (age > limit)",
      exact["problem"] is None, str(exact))

over = nightly.rapsodo_silence(TODAY - timedelta(days=limit + 1),
                                TODAY - timedelta(days=limit + 1), TODAY, limit)
check("one day past the limit is silent",
      over["problem"] and "silent for 10 days" in over["problem"], str(over))

landed_today = nightly.rapsodo_silence(
    TODAY - timedelta(days=40), datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc),
    TODAY, limit)
check("a file landed today is not silence, even if the session is old",
      landed_today["problem"] is None, str(landed_today))

never = nightly.rapsodo_silence(None, None, TODAY, limit)
check("nothing ever landed is a problem",
      never["status"] == "NEVER LANDED" and "no file has ever landed" in never["problem"])


def rapsodo_problem(today=TODAY):
    return [p for p in nightly.freshness(ENGINE, today)["stale"] if p.startswith("rapsodo:")]


section("4. freshness writes the problem; HitTrax and a fresh Blast do not come along")

reset_rows()
seed_fresh_blast()
never_problems = rapsodo_problem()
check("no Rapsodo row yet is 'no file has ever landed'",
      never_problems == ["rapsodo: no file has ever landed"], str(never_problems))
fr = nightly.freshness(ENGINE, TODAY)
check("HitTrax with no file is still 'not live yet' and not a problem",
      fr["sources"]["hittrax"]["status"] == "not live yet"
      and not any(p.startswith("hittrax:") for p in fr["stale"]),
      str(fr["sources"]["hittrax"]) + " " + str(fr["stale"]))
check("a Blast file from today is ok",
      fr["sources"]["blast"]["status"] == "ok", str(fr["sources"]["blast"]))

reset_rows()
seed_fresh_blast()
add_session(date(2026, 3, 1), session_type="bullpen", ref="old-pen")
add_session(TODAY - timedelta(days=1), session_type="cage", ref="new-cage")
add_file("rapsodo", TODAY - timedelta(days=1))
fr = nightly.freshness(ENGINE, TODAY)
check("a fresh hitting session keeps the account quiet",
      rapsodo_problem() == [], str(fr["stale"]))
check("pitching is still reported on its own date",
      fr["sources"]["rapsodo"]["newest_pitching"] == "2026-03-01"
      and fr["sources"]["rapsodo"]["newest_hitting"] == str(TODAY - timedelta(days=1)),
      str(fr["sources"]["rapsodo"]))

reset_rows()
seed_fresh_blast()
old = TODAY - timedelta(days=20)
add_session(old, session_type="bullpen", ref="stale-pen")
add_session(old, session_type="cage", ref="stale-cage")
add_file("rapsodo", old)
problems = rapsodo_problem()
check("both sides and the file older than the window is ATTENTION",
      len(problems) == 1 and "silent for 20 days" in problems[0]
      and "limit 9" in problems[0], str(problems))

reset_rows()
seed_fresh_blast()
add_session(old, session_type="bullpen", ref="old-but-filed")
add_file("rapsodo", TODAY)
check("last_file_landed today wins over an old max session date",
      rapsodo_problem() == [], str(nightly.freshness(ENGINE, TODAY)["sources"]["rapsodo"]))


# --------------------------------------------------------------------------
section("5. the subject flips, and the counts are on it")


def report_for(problems, sessions_new, players_active):
    return {
        "ran_at": "2026-10-01 09:00 UTC",
        "problems": problems,
        "steps": {
            "rapsodo": {"ok": True, "sessions_new": sessions_new,
                        "players_active": players_active, "lookback_days": 7},
            "freshness": {"ok": True, "sources": {
                "rapsodo": {"newest_pitching": "2026-09-10",
                            "newest_hitting": "2026-09-11"},
                "blast": {"newest_session": "2026-09-29"},
            }},
            "detect": {"ok": True, "fired": 0},
            "notes": {"ok": True, "skipped": "not Monday"},
        },
    }


subject, body = nightly.compose(report_for(
    ["freshness -- rapsodo: silent for 20 days (limit 9); newest session 2026-09-11, last file 2026-09-11"],
    0, 0))
check("silence makes the subject ATTENTION", subject.startswith("Moeller nightly ATTENTION"))
check("the subject carries sessions_new=0 and 0 players",
      "sessions_new=0" in subject and "0 players" in subject, subject)
check("the body repeats the ingest line with the lookback",
      "Rapsodo ingest: Rapsodo sessions_new=0, 0 players (lookback 7d)" in body, body)
check("the body lists the freshness problem", "silent for 20 days" in body)

ok_subject, _ok_body = nightly.compose(report_for([], 2, 4))
check("a quiet night with new data stays OK and names players_active",
      ok_subject.startswith("Moeller nightly OK")
      and "sessions_new=2" in ok_subject and "players_active=4" in ok_subject,
      ok_subject)

print()
if FAILS:
    print(f"{len(FAILS)} check(s) FAILED:")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("all nightly checks passed\n")
