"""
test_changes.py -- Phase D: does the What Changed engine fire on real changes
and stay quiet on noise?

The failure mode that matters here is a dashboard that cries wolf. A coach who
gets told about three "changes" a week that are really measurement noise stops
reading it, and then the real one goes unnoticed. So most of these tests are
about NOT firing.

    python test_changes.py
"""

import os
import random
import sys
import tempfile
from datetime import date, timedelta

FAILS = []


def check(label, condition, detail=""):
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILS.append(label)


TMP = os.path.join(tempfile.mkdtemp(), "changes.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TMP.replace("\\", "/")
os.environ.pop("HUB_PASSWORD", None)
os.environ.pop("RAILWAY_ENVIRONMENT", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import func, insert, select  # noqa: E402

import changes    # noqa: E402
import db         # noqa: E402
import metrics    # noqa: E402

engine = db.get_engine()
RNG = random.Random(42)
TODAY = date(2026, 8, 17)


def make_player(first, last, is_pitcher=False):
    with engine.begin() as conn:
        return conn.execute(insert(db.players).values(
            slug=db.slugify(first, last), first_name=first, last_name=last,
            is_pitcher=is_pitcher)).inserted_primary_key[0]


def add_session(player_id, when, metric_key, mean, sd, n=10,
                side="hitting", purpose=None, ref=None):
    """One session of n observations drawn around `mean`."""
    table = db.swings if side == "hitting" else db.pitch_metrics
    with engine.begin() as conn:
        sid = conn.execute(insert(db.sessions).values(
            player_id=player_id, session_date=when, session_type="cage",
            source="blast", purpose=purpose,
            source_ref=ref or f"{player_id}|{when}|{metric_key}|{RNG.random()}",
        )).inserted_primary_key[0]
        rows = [{"session_id": sid, "player_id": player_id, "seq": i + 1,
                 "metric_key": metric_key, "value": RNG.gauss(mean, sd)}
                for i in range(n)]
        if side == "pitching":
            for r in rows:
                r["pitch_type"] = "FB"
        conn.execute(insert(table), rows)
    return sid


def series(player_id, metric_key, means, sd, side="hitting", n=10, spacing=7,
           first_purpose=None, end_offset=0):
    """A run of weekly sessions, oldest first, one mean per session.

    `end_offset` is days before TODAY for the LAST session, so a follow-up run
    can be placed genuinely after an earlier one.
    """
    out = []
    total = len(means)
    for i, m in enumerate(means):
        when = TODAY - timedelta(days=end_offset + spacing * (total - 1 - i))
        out.append(add_session(player_id, when, metric_key, m, sd, n=n, side=side,
                               purpose=(first_purpose if i == 0 else None)))
    return out


def events_for(player_id, metric_key=None):
    q = select(db.change_events).where(db.change_events.c.player_id == player_id)
    if metric_key:
        q = q.where(db.change_events.c.metric_key == metric_key)
    with engine.connect() as conn:
        return conn.execute(q).all()


# ===========================================================================
print("\n1. the statistics are right")
# ===========================================================================
# Two-tailed p-values against standard Student's t tables.
for t, dfree, expected in [(2.228, 10, 0.05), (1.812, 10, 0.10), (3.169, 10, 0.01),
                           (2.086, 20, 0.05), (12.706, 1, 0.05), (2.0, 10, 0.0734)]:
    got = changes.t_test_p(t, dfree)
    check(f"t={t}, df={dfree} -> p={expected}", abs(got - expected) < 0.0015,
          f"got {got:.4f}")
check("t=0 gives p=1", abs(changes.t_test_p(0.0, 10) - 1.0) < 1e-9)

# Welch's worked example (Wikipedia A1/A2 samples).
a = [27.5, 21.0, 19.0, 23.6, 17.0, 17.9, 16.9, 20.1, 21.9, 22.6, 23.1, 19.6, 19.0, 21.7, 21.4]
b = [27.1, 22.0, 20.8, 23.4, 23.4, 23.5, 25.8, 22.0, 24.8, 20.2, 21.9, 22.1, 22.9, 20.5, 24.4]
t, dfree = changes.welch(a, b)
check("Welch t matches the worked example", abs(t - (-2.4554)) < 0.01, f"{t:.4f}")
check("Welch df matches the worked example", abs(dfree - 24.98) < 0.6, f"{dfree:.2f}")

m, sd, n = changes.mean_sd([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])
check("mean/sd uses the sample sd (n-1)", abs(m - 5.0) < 1e-9 and abs(sd - 2.138) < 0.01,
      f"{m}, {sd}")
check("mean_sd of one value has no sd", changes.mean_sd([3.0])[1] == 0.0)
check("mean_sd of nothing is safe", changes.mean_sd([]) == (None, None, 0))


# ===========================================================================
print("\n2. it stays quiet when it should")
# ===========================================================================

# --- too little data
p_thin = make_player("Thin", "Data")
series(p_thin, "bat_speed", [70, 70, 70], sd=1.5, n=3)   # 9 obs, min_n is 20
v, fired = changes.compute_player(engine, p_thin, write=False)
bs = next(x for x in v if x["metric_key"] == "bat_speed")
check("insufficient data does not fire", not fired)
check("  and says why", bs["status"] == "insufficient_data", str(bs))
check("  reporting the shortfall", "needs 20" in bs.get("reason", ""), bs.get("reason"))

# --- flat player: no change at all
p_flat = make_player("Flat", "Line")
series(p_flat, "bat_speed", [70, 70.2, 69.8, 70.1, 69.9, 70, 70.1, 69.9], sd=1.5)
v, fired = changes.compute_player(engine, p_flat, write=False)
check("a flat player produces no change", not fired,
      str([f["summary"] for f in fired]))

# --- real movement, but smaller than measurement noise (mmc gate)
p_tiny = make_player("Tiny", "Move")
series(p_tiny, "bat_speed", [70, 70, 70, 70, 70.6, 70.6, 70.6], sd=0.30)
v, fired = changes.compute_player(engine, p_tiny, write=False)
bs = next(x for x in v if x["metric_key"] == "bat_speed")
check("a move smaller than the mmc does not fire", not fired, str(bs))
check("  blocked by the mmc gate specifically",
      bs.get("gates", {}).get("mmc") is False, str(bs.get("gates")))

# --- big move, but the player is wildly inconsistent (effect-size gate)
p_noisy = make_player("Noisy", "Hitter")
series(p_noisy, "bat_speed", [68, 72, 66, 74, 67, 73, 71], sd=9.0, n=14)
v, fired = changes.compute_player(engine, p_noisy, write=False)
check("noise inside a wide personal range does not fire", not fired,
      str([f["summary"] for f in fired]))


# ===========================================================================
print("\n3. it fires on a real change")
# ===========================================================================

p_jump = make_player("Jumpy", "Velo", is_pitcher=True)
# five weeks around 85, then three weeks around 88 -- a genuine velo gain.
# Ends three weeks ago so section 5 can add genuinely later sessions on top.
series(p_jump, "fb_velocity", [85, 84.8, 85.2, 85.1, 84.9, 88.1, 88.3, 88.2],
       sd=1.1, n=25, side="pitching", end_offset=21)
v, fired = changes.compute_player(engine, p_jump, write=True)
check("a real velo gain fires", len(fired) == 1, str([f["summary"] for f in fired]))
e = fired[0]
check("  direction is up", e["direction"] == "up", str(e))
check("  marked favorable (higher is better for velo)", e["favorable"] is True)
check("  severity is significant", e["severity"] == "significant", str(e["severity"]))
check("  delta is about +3.3", 2.8 < e["delta"] < 3.8, str(e["delta"]))
check("  summary reads like a coach note",
      "Fastball velocity" in e["summary"] and "baseline" in e["summary"], e["summary"])
check("  written to change_events", len(events_for(p_jump)) == 1)
check("  a re-anchored baseline was stored",
      len([r for r in engine.connect().execute(
          select(db.player_baselines).where(
              db.player_baselines.c.player_id == p_jump))]) == 1)


# ===========================================================================
print("\n4. polarity: 'up' is not the same as 'good'")
# ===========================================================================

# lower_better -- time to contact coming DOWN is an improvement
p_ttc = make_player("Quick", "Hands")
series(p_ttc, "time_to_contact", [0.160, 0.158, 0.162, 0.159, 0.131, 0.129, 0.130],
       sd=0.008, n=25)
v, fired = changes.compute_player(engine, p_ttc, write=False)
ttc = next((f for f in fired if f["metric_key"] == "time_to_contact"), None)
check("a falling time-to-contact fires", ttc is not None,
      str([x.get("reason") for x in v]))
if ttc:
    check("  direction is down", ttc["direction"] == "down")
    check("  but it is FAVORABLE (lower is better)", ttc["favorable"] is True)

# target_band -- moving toward the band is good even though the value falls
p_band = make_player("Steep", "Swing")
series(p_band, "attack_angle", [21, 21.5, 20.8, 21.2, 13.5, 13.0, 13.4], sd=2.0, n=25)
v, fired = changes.compute_player(engine, p_band, write=False)
aa = next((f for f in fired if f["metric_key"] == "attack_angle"), None)
check("attack angle moving into the band fires", aa is not None,
      str([x.get("reason") for x in v]))
if aa:
    check("  direction is down", aa["direction"] == "down")
    check("  and it is favorable -- it moved INTO the 5-15 band",
          aa["favorable"] is True)

# ...and moving out of the band is not, even though the number went up
p_band2 = make_player("Flat", "Swing")
series(p_band2, "attack_angle", [10, 10.4, 9.8, 10.1, 21.5, 22.0, 21.7], sd=2.0, n=25)
v, fired = changes.compute_player(engine, p_band2, write=False)
aa2 = next((f for f in fired if f["metric_key"] == "attack_angle"), None)
check("attack angle leaving the band fires", aa2 is not None)
if aa2:
    check("  direction is up", aa2["direction"] == "up")
    check("  but it is NOT favorable -- it left the band",
          aa2["favorable"] is False)


# ===========================================================================
print("\n5. it does not repeat itself")
# ===========================================================================

before = len(events_for(p_jump))
v, fired2 = changes.compute_player(engine, p_jump, write=True)
check("running again fires nothing new", not fired2,
      str([f["summary"] for f in fired2]))
check("  no duplicate row written", len(events_for(p_jump)) == before)
sup = next((x for x in v if x.get("status") == "suppressed"), None)
check("  and it says it was suppressed, not that nothing happened", sup is not None,
      str([x.get("status") for x in v]))

# A NEW jump on top of the old one must still be reported.
series(p_jump, "fb_velocity", [91.5, 91.8, 91.6], sd=1.1, n=25, side="pitching")
v, fired3 = changes.compute_player(engine, p_jump, write=True)
check("a further gain is still reported", len(fired3) == 1,
      str([x.get("status") + ':' + str(x.get('reason', '')) for x in v]))
if fired3:
    check("  compared against the re-anchored baseline, not the original",
          fired3[0]["baseline_mean"] > 86.0, str(fired3[0]["baseline_mean"]))


# ===========================================================================
print("\n6. acknowledging")
# ===========================================================================

ev = events_for(p_jump)[0]
changes.acknowledge(engine, ev.id)
with engine.connect() as conn:
    got = conn.execute(select(db.change_events.c.acknowledged)
                       .where(db.change_events.c.id == ev.id)).scalar()
check("acknowledge sets the flag", bool(got))

import profiles  # noqa: E402
ov = profiles.team_overview(engine)
check("acknowledged events drop off the roster feed",
      all(c["summary"] != ev.summary for c in ov["changes"]))
with engine.connect() as conn:
    slug = conn.execute(select(db.players.c.slug)
                        .where(db.players.c.id == p_jump)).scalar()
prof = profiles.profile(engine, slug)
check("  but stay on the player's own timeline",
      any(c["summary"] == ev.summary for c in prof["changes"]))
check("  the profile's status tiles now show a baseline",
      any(t.get("baseline") is not None for t in prof["status"]),
      str(prof["status"]))


# ===========================================================================
print("\n7. intervention pre/post")
# ===========================================================================

p_iv = make_player("Grip", "Change", is_pitcher=True)
when = TODAY - timedelta(days=28)
# four weeks of 84 before the change, four weeks of 87.5 after
for i in range(4):
    add_session(p_iv, when - timedelta(days=7 * (4 - i)), "fb_velocity", 84.0, 1.0,
                n=25, side="pitching")
for i in range(4):
    add_session(p_iv, when + timedelta(days=7 * i), "fb_velocity", 87.5, 1.0,
                n=25, side="pitching")

with engine.begin() as conn:
    gid = conn.execute(insert(db.goals).values(
        player_id=p_iv, metric_key="fb_velocity", direction="increase",
        target_value=87.0, title="Add a tick to the fastball",
        set_by="Ian", set_on=when, status="active")).inserted_primary_key[0]
    iid = conn.execute(insert(db.interventions).values(
        player_id=p_iv, intervention_date=when, category="grip",
        title="New four-seam grip", coach="Ian", goal_id=gid,
        outcome="pending")).inserted_primary_key[0]

res = changes.evaluate_intervention(engine, iid)
check("the intervention was evaluated", res is not None)
check("  outcome written back as 'working'", res["outcome"] == "working", str(res["outcome"]))
fb = next((m for m in res["metrics"] if m["metric_key"] == "fb_velocity"), None)
check("  the goal metric is reported first", res["metrics"][0]["is_goal_metric"])
check("  pre/post means are right", fb and abs(fb["pre_mean"] - 84) < 0.6
      and abs(fb["post_mean"] - 87.5) < 0.6, str(fb))
check("  and it moved", fb and fb["moved"] is True)
with engine.connect() as conn:
    stored = conn.execute(select(db.interventions.c.outcome)
                          .where(db.interventions.c.id == iid)).scalar()
check("  persisted on the intervention row", stored == "working", str(stored))

# An intervention that changed nothing must say so rather than claiming success.
p_iv2 = make_player("No", "Effect", is_pitcher=True)
when2 = TODAY - timedelta(days=28)
for i in range(4):
    add_session(p_iv2, when2 - timedelta(days=7 * (4 - i)), "fb_velocity", 84.0, 1.0,
                n=25, side="pitching")
for i in range(4):
    add_session(p_iv2, when2 + timedelta(days=7 * i), "fb_velocity", 84.1, 1.0,
                n=25, side="pitching")
with engine.begin() as conn:
    iid2 = conn.execute(insert(db.interventions).values(
        player_id=p_iv2, intervention_date=when2, category="drill",
        title="Long toss block", coach="Ian", outcome="pending")).inserted_primary_key[0]
res2 = changes.evaluate_intervention(engine, iid2)
check("an intervention that did nothing reports 'no_change'",
      res2["outcome"] == "no_change", str(res2["outcome"]))


# ===========================================================================
print("\n8. the whole-roster run and the feeds")
# ===========================================================================

summary = changes.compute_all(engine, write=True)
check("compute_all examines every player", summary["players"] >= 9,
      str(summary["players"]))
check("  and reports a status breakdown", "by_status" in summary and summary["by_status"])

ov = profiles.team_overview(engine)
check("the team feed shows unacknowledged changes", len(ov["changes"]) > 0,
      str(len(ov["changes"])))
check("  each carries a player slug to link to",
      all(c.get("slug") for c in ov["changes"]))
check("  and a plain-English summary",
      all(c.get("summary") for c in ov["changes"]))

import app as hubapp  # noqa: E402
client = hubapp.app.test_client()
r = client.get("/")
check("home page renders the change feed", r.status_code == 200)
check("  and shows a real change", "baseline" in r.get_data(as_text=True))
r = client.get("/team")
check("/team renders", r.status_code == 200)
r = client.get("/api/changes")
check("/api/changes returns the feed", r.status_code == 200 and len(r.get_json()) > 0)

ev2 = events_for(p_ttc) or events_for(p_band)
if ev2:
    r = client.post(f"/api/changes/{ev2[0].id}/acknowledge")
    check("acknowledge endpoint works", r.status_code == 200, f"got {r.status_code}")

r = client.post("/api/changes/detect")
check("detect endpoint works", r.status_code == 200, f"got {r.status_code}")


print()
if FAILS:
    print(f"{len(FAILS)} check(s) FAILED:")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("all change-detection checks passed\n")
