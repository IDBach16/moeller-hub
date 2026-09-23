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


# ---------------------------------------------------------------------------
section("hitting sessions go to swings, not pitch_metrics")
# ---------------------------------------------------------------------------
# Rapsodo's Hitting unit returns shotType "hit" with a batted ball's exit speed
# in the same `speed` field a pitch uses. On 2026-09-20 five such sessions went
# through the pitching map and four position players got a "bullpen". This
# reproduces that state, then proves a plain re-load repairs it.
from datetime import date as _date  # noqa: E402

(load_db.RAW_DIR / "hit").mkdir(parents=True, exist_ok=True)
hitter = seed_player("Donovan", "Glosser", is_pitcher=False)


def hit(hit_id, speed, **kw):
    s = {"_id": f"h@{hit_id}", "hit_id": hit_id, "speed": speed,
         "launchAngle": None if speed is None else 18.5,
         "distance": None if speed is None else 193.0,
         "direction": None if speed is None else -4.0,
         "pitchBallSpeed": None if speed is None else 62.0,
         "spin": None if speed is None else 1850.0, "xwobaScore": None if speed is None else 0.61}
    s.update(kw)
    return s


def write_hit_session(session_id, player, shots, session_type="Live Batting Practice"):
    payload = {"player": player,
               "session": {"_id": session_id, "id": session_id, "playerId": player["_id"],
                           "date": 1789072316, "startedAt": 1789072316,
                           "sessionName": "untitled", "sessionType": session_type,
                           "shotType": "hit", "deviceName": "hitting2.0"},
               "shots": shots}
    (load_db.RAW_DIR / "hit" / f"{session_id}.json").write_text(json.dumps(payload), encoding="utf-8")


# (5005 is a fresh Rapsodo id -- 3003 already belongs to a pitcher above.)
# The wrong state first: the same session already filed as a bullpen with a
# batted ball stored as a pitch velocity.
with ENGINE.begin() as conn:
    bad_sid = conn.execute(insert(db.sessions).values(
        player_id=hitter, session_date=_date(2026, 9, 10), session_type="bullpen",
        source="rapsodo", source_ref="hit_live").returning(db.sessions.c.id)).scalar()
    conn.execute(insert(db.pitch_metrics).values(
        session_id=bad_sid, player_id=hitter, seq=1, metric_key="velocity", value=75.1))

write_hit_session("hit_live", make_player(5005, "Donovan", "Glosser"),
                  [hit(500 + i, 75.0 + i) for i in range(6)] + [hit(599, None)])
write_hit_session("hit_machine", make_player(5005, "Donovan", "Glosser"),
                  [hit(600 + i, 80.0) for i in range(3)], session_type="Pitching Machine")
stats = load_db.load(dry_run=False)
with ENGINE.connect() as conn:
    live = conn.execute(select(db.sessions).where(db.sessions.c.source_ref == "hit_live:5005")).first()
    mach = conn.execute(select(db.sessions).where(db.sessions.c.source_ref == "hit_machine:5005")).first()
    sw_live = conn.execute(select(db.swings).where(db.swings.c.session_id == live.id)).all()
    pm_live = count(db.pitch_metrics, session_id=live.id)
    sw_mach = conn.execute(select(db.swings).where(db.swings.c.session_id == mach.id)).all()

check("a hitting session is stored as a cage session", live.session_type == "cage", live.session_type)
check("  and the mis-filed bullpen was repaired in place, not duplicated",
      live.id == bad_sid and count(db.sessions, source_ref="hit_live") == 0)
check("  its batted balls are swings, not pitches",
      pm_live == 0 and len(sw_live) > 0, f"pitch_metrics={pm_live} swings={len(sw_live)}")
check("  exit speed lands as exit_velocity, one per valid batted ball",
      sum(1 for r in sw_live if r.metric_key == "exit_velocity") == 6)
check("  the failed track (speed=null) is dropped",
      not any(r.seq == 7 for r in sw_live))
check("  'Live Batting Practice' is the live drill", {r.context for r in sw_live} == {"live"},
      str({r.context for r in sw_live}))
check("  'Pitching Machine' is the machine drill", {r.context for r in sw_mach} == {"machine"},
      str({r.context for r in sw_mach}))
check("  the loader counts them separately from pitching",
      stats["hit_sessions"] == 2 and stats["swings_written"] == len(sw_live) + len(sw_mach))
check("  a batted ball never becomes a fastball velocity",
      count(db.pitch_metrics, player_id=hitter) == 0)

# A hitting session is a GROUP session: one Rapsodo id, many hitters. Two
# hitters under the same id must become two sessions, not one overwriting the
# other -- which is what lost ~400 batted balls from 2026-09-17 in production.
hitter2 = seed_player("Shane", "Green", is_pitcher=False)
write_hit_session("hit_group", make_player(5005, "Donovan", "Glosser"),
                  [hit(700 + i, 78.0) for i in range(4)])
payload = json.loads((load_db.RAW_DIR / "hit" / "hit_group.json").read_text())
payload["player"] = make_player(4004, "Shane", "Green"); payload["session"]["playerId"] = 4004
(load_db.RAW_DIR / "hit" / "hit_group__4004.json").write_text(json.dumps(payload), encoding="utf-8")
load_db.load(dry_run=False)
check("two hitters sharing one Rapsodo session id get two sessions",
      count(db.sessions, source_ref="hit_group:5005") == 1 and count(db.sessions, source_ref="hit_group:4004") == 1)
check("  and each keeps his own batted balls",
      count(db.swings, player_id=hitter2) == 4 * len(load_db.HIT_METRIC_MAP))

# ---------------------------------------------------------------------------
section("the Rapsodo batted-ball strip")
# ---------------------------------------------------------------------------
# 16 live balls at known values: exit velocity alternating 95/85 (hard-hit 50%
# at the 90 mph line, mean 90, max 95) and launch angle alternating 10/40
# (sweet-spot 50% in 8-32). Above the per-drill floor, so he qualifies; only
# one hitter in the field, so nothing can be RANKED -- the values must still
# show, and the shares must be exact.
import percentiles  # noqa: E402
slugger = seed_player("Noah", "Kattus", is_pitcher=False)
write_hit_session("hit_known", make_player(6006, "Noah", "Kattus"),
                  [hit(800 + i, 95.0 if i % 2 == 0 else 85.0,
                       launchAngle=10.0 if i % 2 == 0 else 40.0, distance=300.0 + i)
                   for i in range(16)])
load_db.load(dry_run=False)
percentiles.clear_batted_cache()
strip = percentiles.batted_strip(ENGINE, slugger)
check("a hitter with Rapsodo cage data gets a batted-ball strip", strip is not None)
vals = {b["label"]: b for b in (strip or {}).get("bars", [])}
check("  exit velocity is his mean off the bat", vals.get("Exit velocity", {}).get("value") == 90.0, str(vals.get("Exit velocity")))
check("  max exit velocity is his hardest ball", vals.get("Max exit velocity", {}).get("value") == 95.0)
check("  hard-hit %% is the share at %g+ mph" % percentiles.HARD_HIT_MPH,
      vals.get("Hard-hit %", {}).get("value") == 50.0, str(vals.get("Hard-hit %")))
check("  sweet-spot % is the share inside the launch-angle band",
      vals.get("Sweet-spot %", {}).get("value") == 50.0, str(vals.get("Sweet-spot %")))
check("  max distance is his longest ball", vals.get("Max distance", {}).get("value") == 315.0)
check("  launch angle is shown, never ranked",
      vals.get("Launch angle", {}).get("no_rank") is True and vals.get("Launch angle", {}).get("pct") is None)
check("  with one hitter in the field nothing is ranked, but nothing is hidden",
      all(b.get("no_rank") for b in strip["bars"]) and len(strip["bars"]) >= 5)
check("  the strip says how many balls fed it, and from which drill",
      strip["balls"] == 16 and strip["drills"][0]["drill"] == "Live pitching", str(strip.get("drills")))
check("  the Blast strip still works on the shared engine",
      percentiles.blast_strip(ENGINE, slugger) is None)   # no Blast swings for him
# A batted ball off a pitching-side hitter must never leak into the pitching
# card: the strip reads swings, the card reads pitch_metrics.
check("  and none of it touched pitch_metrics", count(db.pitch_metrics, player_id=slugger) == 0)

print()
if FAILS:
    print(f"{len(FAILS)} check(s) FAILED:")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("all rapsodo checks passed\n")
