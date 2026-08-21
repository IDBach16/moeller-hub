"""
test_rapsodo.py -- does the Rapsodo pipeline still hold up?

Every check here corresponds to a bug that actually happened, or to a rule that
is easy to break silently. None of them hit the network or the real database:
they build synthetic archive files shaped like real Rapsodo payloads and load
them into a throwaway SQLite database.

The failures these guard against were all SILENT -- the load reported success and
wrote the wrong thing, or wrote nothing at all:

  * the session LIST payload uses `playerId` with no player object, while the
    session DETAIL payload uses `player_id` and a nested `player`. Coding against
    the wrong one resolved every session to nobody.
  * the sessions envelope is `sessions`, not `data` like /v3/reports.
  * failed radar tracks come back as ordinary rows with speed=null. Keeping them
    dropped one pitcher's average fastball from 82.5 to 67.0.
  * pitchType is an int enum. Two codes are unconfirmed and must stay unmapped
    rather than being guessed into a real pitch.
  * a name that doesn't resolve must be queued, never attributed to a best guess.
  * re-running a load must not duplicate anything.

    python test_rapsodo.py
"""

import json
import os
import sys
import tempfile

FAILS = []


def check(label, condition, detail=""):
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILS.append(label)


def section(title):
    print(f"\n{title}")


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "rapsodo"))

# A scratch database, so a test run can never touch the real one.
TMP = tempfile.mkdtemp(prefix="rapsodo_test_")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "t.db").replace("\\", "/")
os.environ["HUB_PATH"] = HERE

import db          # noqa: E402
import metrics     # noqa: E402
from sqlalchemy import func, insert, select  # noqa: E402

import load_db     # noqa: E402
import rapsodo_card  # noqa: E402

from pathlib import Path  # noqa: E402

# RAW_DIR is a Path in load_db -- keep the type, or .glob() blows up.
load_db.RAW_DIR = Path(TMP) / "raw"
(load_db.RAW_DIR / "pitch").mkdir(parents=True, exist_ok=True)

ENGINE = db.get_engine()


# ---------------------------------------------------------------------------
# Synthetic archive, shaped like what pull.py writes
# ---------------------------------------------------------------------------

def shot(pitch_id, pitch_type, speed, **kw):
    """One Rapsodo shot. speed=None is a failed radar track."""
    s = {
        "_id": f"999@{pitch_id}", "pitch_id": pitch_id, "pitchType": pitch_type,
        "speed": speed, "spin": None if speed is None else 2000.0,
        "verticalBreak": None if speed is None else 14.0,
        "horizontalBreak": None if speed is None else 9.0,
        "spinEfficiency": None if speed is None else 92.0,
        "releaseHeight": None if speed is None else 6.0,
        "releaseSide": None if speed is None else -1.5,
        "strikeZoneX": None if speed is None else -3.0,
        "strikeZoneY": None if speed is None else 24.0,
        "isValidForStrike": speed is not None,
    }
    s.update(kw)
    return s


def write_session(session_id, player, shots, date=1774381299):
    """Mirror pull.py's archive: player at the top level, session-LIST shape."""
    payload = {
        "player": player,
        "session": {
            # NOTE: camelCase playerId and NO nested player object -- this is the
            # session-list shape, which is what pull.py actually archives.
            "_id": session_id, "id": session_id, "object_id": session_id,
            "playerId": player["_id"], "date": date, "startedAt": date,
            "sessionName": "untitled", "sessionType": "High Intent",
            "shotType": "pitch", "deviceName": "pitching2.0",
        },
        "shots": shots,
    }
    path = load_db.RAW_DIR / "pitch" / f"{session_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def make_player(pid, first, last, email=None):
    return {"_id": pid, "firstName": first, "lastName": last,
            "email": email or f"{last.lower()}@moeller.org"}


def count(table, **where):
    with ENGINE.connect() as conn:
        q = select(func.count()).select_from(table)
        for k, v in where.items():
            q = q.where(table.c[k] == v)
        return conn.execute(q).scalar()


def seed_player(first, last, is_pitcher=True):
    with ENGINE.begin() as conn:
        return conn.execute(
            insert(db.players).values(
                slug=db.slugify(first, last), first_name=first, last_name=last,
                is_pitcher=is_pitcher, is_active=True
            ).returning(db.players.c.id)).scalar()


# ---------------------------------------------------------------------------
section("pitch-type mapping")
# ---------------------------------------------------------------------------

for code, expected in [(0, "FB"), (3, "CB"), (4, "SL"), (5, "SI"), (6, "CH")]:
    check(f"pitchType {code} maps to {expected}",
          load_db.map_pitch_type(code) == expected,
          f"got {load_db.map_pitch_type(code)}")

# The whole point: an unconfirmed code must NOT become a real pitch.
for code in (1, 2, 7, 99):
    check(f"unconfirmed pitchType {code} stays unmapped",
          load_db.map_pitch_type(code) is None,
          f"got {load_db.map_pitch_type(code)}")

# The game export logs a fifth of its pitches generically; that can't be resolved
# to a slider or a curve after the fact, so it must not be guessed.
check("'Breaking Ball' does not resolve to a pitch type",
      metrics.normalize_pitch_type("Breaking Ball") is None,
      f"got {metrics.normalize_pitch_type('Breaking Ball')}")
check("'Fast Ball' (AWRE spelling) resolves to FB",
      metrics.normalize_pitch_type("Fast Ball") == "FB")
check("'Two Seam Fast Ball' resolves to SI",
      metrics.normalize_pitch_type("Two Seam Fast Ball") == "SI")


# ---------------------------------------------------------------------------
section("failed radar tracks")
# ---------------------------------------------------------------------------

check("a null-speed shot is rejected",
      load_db.is_valid_shot(shot(1, 0, None)) is False)
check("a tracked shot is kept",
      load_db.is_valid_shot(shot(2, 0, 84.0)) is True)

# The regression in numbers: 3 good fastballs at ~84 plus 1 failed track. If the
# failed track were kept as a zero the average would collapse.
good = [shot(10 + i, 0, 84.0) for i in range(3)] + [shot(20, 0, None)]
kept = [s for s in good if load_db.is_valid_shot(s)]
avg = sum(s["speed"] for s in kept) / len(kept)
check("failed tracks don't drag the average down", abs(avg - 84.0) < 0.001,
      f"avg {avg}")


# ---------------------------------------------------------------------------
section("player resolution")
# ---------------------------------------------------------------------------

pid = seed_player("Rudy", "Glotfelty")
write_session("sess_known", make_player(1001, "Rudy", "Glotfelty"),
              [shot(100 + i, 0, 85.0 + i) for i in range(4)])
# A pitcher who is on no roster -- must be queued, never invented.
write_session("sess_unknown", make_player(2002, "Nobody", "Atall"),
              [shot(200 + i, 0, 70.0) for i in range(3)])

stats = load_db.load(dry_run=False)

check("session for a rostered player is written", stats["sessions_written"] == 1,
      f"wrote {stats['sessions_written']}")
check("unresolved pitcher is queued, not created",
      "Nobody Atall" in stats["unresolved"], str(list(stats["unresolved"])))
check("no player record was invented for the unresolved name",
      count(db.players) == 1, f"{count(db.players)} players")
check("the unresolved name reached the review queue",
      count(db.name_review, vendor="rapsodo") == 1)
check("resolving by name records a vendor id so it never guesses again",
      count(db.player_vendor_ids, vendor="rapsodo") == 1)

# The bug that resolved every session to nobody: reading the wrong payload shape.
check("player resolved from the session-LIST shape (playerId, no player object)",
      count(db.sessions, source="rapsodo") == 1)


# ---------------------------------------------------------------------------
section("idempotency")
# ---------------------------------------------------------------------------

before_sessions = count(db.sessions)
before_metrics = count(db.pitch_metrics)
again = load_db.load(dry_run=False)

check("re-running writes no new sessions", again["sessions_written"] == 0,
      f"wrote {again['sessions_written']}")
check("re-running sees the existing session", again["sessions_existing"] == 1)
check("session count unchanged", count(db.sessions) == before_sessions,
      f"{before_sessions} -> {count(db.sessions)}")
check("metric count unchanged (re-ingest replaces, never duplicates)",
      count(db.pitch_metrics) == before_metrics,
      f"{before_metrics} -> {count(db.pitch_metrics)}")


# ---------------------------------------------------------------------------
section("dry run writes nothing")
# ---------------------------------------------------------------------------

write_session("sess_dry", make_player(1001, "Rudy", "Glotfelty"),
              [shot(300 + i, 4, 74.0) for i in range(3)])
snapshot = (count(db.sessions), count(db.pitch_metrics))
load_db.load(dry_run=True)
check("dry run leaves sessions untouched", count(db.sessions) == snapshot[0])
check("dry run leaves metrics untouched", count(db.pitch_metrics) == snapshot[1])


# ---------------------------------------------------------------------------
section("metrics written")
# ---------------------------------------------------------------------------

with ENGINE.connect() as conn:
    keys = {r[0] for r in conn.execute(select(db.pitch_metrics.c.metric_key).distinct())}

for key in ("velocity", "spin_rate", "induced_vertical_break", "horizontal_break",
            "release_height", "release_side", "plate_side", "plate_height"):
    check(f"{key} is loaded", key in keys)

# Fastball velocity is tracked separately from overall velocity -- it's the
# headline metric and mixing it with offspeed would flatten it.
check("fb_velocity is derived for fastballs", "fb_velocity" in keys)

with ENGINE.connect() as conn:
    fb = conn.execute(
        select(func.count()).select_from(db.pitch_metrics)
        .where(db.pitch_metrics.c.metric_key == "fb_velocity")).scalar()
    sl = conn.execute(
        select(func.count()).select_from(db.pitch_metrics)
        .where(db.pitch_metrics.c.metric_key == "fb_velocity")
        .where(db.pitch_metrics.c.pitch_type != "FB")).scalar()
check("fb_velocity only ever tagged FB", sl == 0, f"{sl} non-FB rows")
check("fb_velocity rows exist", fb > 0)


# ---------------------------------------------------------------------------
section("report card")
# ---------------------------------------------------------------------------

card = rapsodo_card.card(ENGINE, pid)
check("card reports data for a pitcher with sessions", card["has_data"] is True)
check("card counts only tracked pitches", card["n_pitches"] == 4,
      f"got {card.get('n_pitches')}")
check("arsenal is grouped by pitch type", len(card["arsenal"]) >= 1)

# Raw floats in a tooltip ("75.18448396704001 mph") are unreadable.
sample = card["pitches"][0]
for field in ("velo", "ivb", "hb"):
    v = sample.get(field)
    check(f"{field} is rounded for display",
          v is None or abs(v - round(v, 1)) < 1e-9, f"got {v}")

missing = rapsodo_card.card(ENGINE, 99999)
check("a player with no Rapsodo data reports has_data False",
      missing["has_data"] is False)


# ---------------------------------------------------------------------------
section("pitch mix must not masquerade as a change")
# ---------------------------------------------------------------------------
# The real case (Seth Maybury, 2026-02-24): sliders went 7% -> 28% of his work
# while every individual pitch stayed flat. Pooled, that fired "spin efficiency
# down 17.5, SIGNIFICANT" and "velocity down 2.7 mph" -- three of four findings
# were artefacts of usage. This builds the same trap and checks it stays quiet.

import changes as C  # noqa: E402

mix_pid = seed_player("Mixy", "McMixface")

# Identical fastballs and sliders throughout -- nothing about either pitch moves.
FB_V, SL_V = 85.0, 72.0
for i, (day, n_fb, n_sl) in enumerate([
        # baseline: almost all fastballs
        ("mix_b1", 20, 1), ("mix_b2", 20, 1), ("mix_b3", 20, 1), ("mix_b4", 20, 1),
        # recent: same pitches, far more sliders
        ("mix_r1", 10, 12), ("mix_r2", 10, 12), ("mix_r3", 10, 12)]):
    shots = ([shot(9000 + i * 100 + j, 0, FB_V) for j in range(n_fb)] +
             [shot(9500 + i * 100 + j, 4, SL_V) for j in range(n_sl)])
    write_session(day, make_player(3003, "Mixy", "McMixface"), shots,
                  date=1767225600 + i * 7 * 86400)

load_db.load(dry_run=False)

with ENGINE.connect() as conn:
    obs = C._observations(conn, mix_pid)

check("pitch-specific metrics are keyed by pitch type",
      ("velocity", "FB") in obs and ("velocity", "SL") in obs,
      f"keys: {sorted(k for k in obs if k[0] == 'velocity')}")
check("velocity is NOT pooled across pitch types",
      ("velocity", None) not in obs)
# Release point is a property of the delivery, not of a pitch, and a real slot
# change shows up on every pitch at once -- so it stays pooled deliberately.
check("release side stays pooled", ("release_side", None) in obs)

fb = [v for _d, _s, v in obs[("velocity", "FB")]]
sl = [v for _d, _s, v in obs[("velocity", "SL")]]
check("fastball series holds only fastball velocities",
      abs(sum(fb) / len(fb) - FB_V) < 0.01, f"mean {sum(fb)/len(fb)}")
check("slider series holds only slider velocities",
      abs(sum(sl) / len(sl) - SL_V) < 0.01, f"mean {sum(sl)/len(sl)}")

# Pooled, this data looks like a 4+ mph collapse. It is not.
pooled = fb + sl
check("pooled would have looked like a big drop (the trap)",
      abs(sum(pooled) / len(pooled) - FB_V) > 2.0,
      "pooled mean is close to FB, so the fixture isn't reproducing the trap")

verdicts, fired = C.compute_player(ENGINE, mix_pid, write=False)
velocity_fired = [v for v in fired if v["metric_key"] in ("velocity", "spin_efficiency")]
check("no velocity/efficiency change fires from a mix shift alone",
      not velocity_fired,
      f"fired: {[(v['metric_key'], v.get('pitch_type'), v['summary']) for v in velocity_fired]}")

# And when something does fire, it says which pitch.
labelled = [v for v in verdicts
            if v.get("pitch_type") and v.get("summary")
            and metrics.PITCH_TYPE_LABELS[v["pitch_type"]].lower() in v["summary"].lower()]
check("a pitch-specific verdict names the pitch in its summary",
      bool(labelled) or not [v for v in verdicts if v.get("pitch_type") and v.get("summary")],
      "summaries exist but none name their pitch")


print()
if FAILS:
    print(f"{len(FAILS)} check(s) FAILED:")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("all rapsodo checks passed\n")
