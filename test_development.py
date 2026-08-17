"""
test_development.py -- Phase E: goals and interventions.

The risks here are quieter than in the other phases. A goal form that accepts
anything produces a development record nobody can measure against, and a
progress bar with an invented denominator is actively misleading. So most of
these tests are about what gets REFUSED, and about progress being honest when
it doesn't know.

    python test_development.py
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


TMP = os.path.join(tempfile.mkdtemp(), "dev.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TMP.replace("\\", "/")
os.environ.pop("HUB_PASSWORD", None)
os.environ.pop("RAILWAY_ENVIRONMENT", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import insert, select  # noqa: E402

import changes       # noqa: E402
import db            # noqa: E402
import development   # noqa: E402
import profiles      # noqa: E402

engine = db.get_engine()
RNG = random.Random(11)
TODAY = date(2026, 8, 17)

with engine.begin() as conn:
    PID = conn.execute(insert(db.players).values(
        slug="test-arm", first_name="Test", last_name="Arm",
        is_pitcher=True)).inserted_primary_key[0]
    BARE = conn.execute(insert(db.players).values(
        slug="bare-player", first_name="Bare", last_name="Player",
        is_pitcher=True)).inserted_primary_key[0]


def add_session(player_id, when, key, mean, sd=1.0, n=25, side="pitching"):
    table = db.swings if side == "hitting" else db.pitch_metrics
    with engine.begin() as conn:
        sid = conn.execute(insert(db.sessions).values(
            player_id=player_id, session_date=when, session_type="bullpen",
            source="rapsodo", source_ref=f"{player_id}|{when}|{key}",
        )).inserted_primary_key[0]
        rows = [{"session_id": sid, "player_id": player_id, "seq": i + 1,
                 "metric_key": key, "value": RNG.gauss(mean, sd)} for i in range(n)]
        if side == "pitching":
            for r in rows:
                r["pitch_type"] = "FB"
        conn.execute(insert(table), rows)


# Four weeks at 84, so a goal set now snapshots ~84.
for i in range(4):
    add_session(PID, TODAY - timedelta(days=7 * (4 - i)), "fb_velocity", 84.0)


# ===========================================================================
print("\n1. creating a goal")
# ===========================================================================

gid = development.create_goal(
    engine, PID, title="Add a tick to the fastball", metric_key="fb_velocity",
    direction="increase", target_value=88.0, set_by="Ian",
    review_on=str(TODAY + timedelta(days=30)), detail="New grip plus long toss")
check("a measurable goal is created", isinstance(gid, int))

with engine.connect() as conn:
    g = conn.execute(select(db.goals).where(db.goals.c.id == gid)).first()
check("  it snapshots where he started", g.start_value is not None
      and 83.0 < g.start_value < 85.0, str(g.start_value))
check("  target stored", g.target_value == 88.0)
check("  starts active", g.status == "active")
check("  set_on defaults to today", g.set_on is not None)

nid = development.create_goal(engine, PID, title="Compete deeper into games",
                              set_by="Ian")
check("a narrative goal is allowed", isinstance(nid, int))
with engine.connect() as conn:
    n = conn.execute(select(db.goals).where(db.goals.c.id == nid)).first()
check("  and stays honestly unmeasurable",
      n.metric_key is None and n.target_value is None and n.direction is None)


# ===========================================================================
print("\n2. what it refuses")
# ===========================================================================

def refuses(label, fn):
    try:
        fn()
        check(label, False, "it was accepted")
    except development.DevError as e:
        check(label, True, str(e))


refuses("a goal with no title",
        lambda: development.create_goal(engine, PID, title="   "))
refuses("a goal on a metric that doesn't exist",
        lambda: development.create_goal(engine, PID, title="x",
                                        metric_key="made_up_metric",
                                        direction="increase", target_value=1))
refuses("a measurable goal with no direction",
        lambda: development.create_goal(engine, PID, title="x",
                                        metric_key="fb_velocity", target_value=90))
refuses("an increase goal with no target",
        lambda: development.create_goal(engine, PID, title="x",
                                        metric_key="fb_velocity", direction="increase"))
refuses("a target that isn't a number",
        lambda: development.create_goal(engine, PID, title="x",
                                        metric_key="fb_velocity",
                                        direction="increase", target_value="fast"))
refuses("a review date before the set date",
        lambda: development.create_goal(
            engine, PID, title="x", set_on=str(TODAY),
            review_on=str(TODAY - timedelta(days=5))))
refuses("a malformed date",
        lambda: development.create_goal(engine, PID, title="x", review_on="soon"))
refuses("a goal for a player who doesn't exist",
        lambda: development.create_goal(engine, 99999, title="x"))
refuses("an unknown goal status",
        lambda: development.update_goal(engine, gid, status="kinda-done"))
refuses("an unknown intervention category",
        lambda: development.create_intervention(engine, PID, title="x",
                                                category="vibes"))
refuses("an intervention with no title",
        lambda: development.create_intervention(engine, PID, title=""))
refuses("an unknown outcome",
        lambda: development.update_intervention(engine, 1, outcome="great"))


# ===========================================================================
print("\n3. progress is honest")
# ===========================================================================

prof = profiles.profile(engine, "test-arm")
goal = next(g for g in prof["goals"] if g["id"] == gid)
pr = goal["progress"]
check("progress is in_progress at the start", pr["state"] == "in_progress", str(pr))
check("  0% of the way with no movement yet", pr["pct"] == 0, str(pr["pct"]))
check("  and it knows what remains", 3.0 < pr["remaining"] < 5.0, str(pr["remaining"]))

narrative = next(g for g in prof["goals"] if g["id"] == nid)
check("a narrative goal claims no progress",
      narrative["progress"]["state"] == "narrative", str(narrative["progress"]))

# Half-way there
add_session(PID, TODAY + timedelta(days=7), "fb_velocity", 86.0)
prof = profiles.profile(engine, "test-arm")
pr = next(g for g in prof["goals"] if g["id"] == gid)["progress"]
check("progress moves with the data", 40 <= pr["pct"] <= 60, str(pr["pct"]))

# Target reached
add_session(PID, TODAY + timedelta(days=14), "fb_velocity", 88.6)
prof = profiles.profile(engine, "test-arm")
pr = next(g for g in prof["goals"] if g["id"] == gid)["progress"]
check("reaching the target is recognised", pr["state"] == "met", str(pr))

# A goal on a metric with no data must NOT invent a denominator.
bare_goal = development.create_goal(
    engine, BARE, title="Bat speed up", metric_key="bat_speed",
    direction="increase", target_value=75.0)
bprof = profiles.profile(engine, "bare-player")
bpr = next(g for g in bprof["goals"] if g["id"] == bare_goal)["progress"]
check("a goal with no data says so rather than showing 0%",
      bpr["state"] == "no_data", str(bpr))
with engine.connect() as conn:
    sv = conn.execute(select(db.goals.c.start_value)
                      .where(db.goals.c.id == bare_goal)).scalar()
check("  and records no start value it doesn't have", sv is None)

# lower_better: progress must count DOWNWARD movement as progress
add_session(PID, TODAY - timedelta(days=3), "time_to_contact", 0.160,
            sd=0.008, side="hitting")
ttc_goal = development.create_goal(
    engine, PID, title="Quicker to the ball", metric_key="time_to_contact",
    direction="decrease", target_value=0.140)
add_session(PID, TODAY + timedelta(days=10), "time_to_contact", 0.150,
            sd=0.008, side="hitting")
prof = profiles.profile(engine, "test-arm")
tpr = next(g for g in prof["goals"] if g["id"] == ttc_goal)["progress"]
check("a decrease goal counts falling values as progress",
      tpr["state"] == "in_progress" and 30 <= tpr["pct"] <= 70, str(tpr))


# ===========================================================================
print("\n4. goal status")
# ===========================================================================

development.update_goal(engine, nid, status="abandoned")
prof = profiles.profile(engine, "test-arm")
check("a goal can be dropped",
      next(g for g in prof["goals"] if g["id"] == nid)["status"] == "abandoned")

ov = profiles.team_overview(engine)
check("dropped goals stop counting as active",
      ov["counts"]["active_goals"] >= 1)


# ===========================================================================
print("\n5. interventions")
# ===========================================================================

when = TODAY - timedelta(days=21)
iid = development.create_intervention(
    engine, PID, title="New four-seam grip", intervention_date=str(when),
    category="grip", coach="Ian", goal_id=gid,
    review_on=str(TODAY - timedelta(days=1)))
check("an intervention is logged", isinstance(iid, int))

refuses("an intervention linked to another player's goal",
        lambda: development.create_intervention(
            engine, BARE, title="x", goal_id=gid))

with engine.connect() as conn:
    iv = conn.execute(select(db.interventions)
                      .where(db.interventions.c.id == iid)).first()
check("  it starts pending", iv.outcome == "pending")
check("  and carries the date the comparison needs", iv.intervention_date == when)

# The engine's pre/post shows up on the profile, read-only.
prof = profiles.profile(engine, "test-arm")
shown = next(i for i in prof["interventions"] if i["id"] == iid)
check("the profile shows a before/after", len(shown["evaluation"]) > 0,
      str(shown["evaluation"]))
fb = next((m for m in shown["evaluation"] if m["metric_key"] == "fb_velocity"), None)
check("  including the goal metric, flagged as such", fb and fb["is_goal_metric"])
check("  with pre below post", fb and fb["pre_mean"] < fb["post_mean"], str(fb))
with engine.connect() as conn:
    still = conn.execute(select(db.interventions.c.outcome)
                         .where(db.interventions.c.id == iid)).scalar()
check("  and viewing the page did NOT rewrite the outcome", still == "pending",
      str(still))

# Explicitly evaluating it does write.
res = changes.evaluate_intervention(engine, iid)
check("evaluating writes the outcome", res["outcome"] == "working", str(res["outcome"]))
with engine.connect() as conn:
    stored = conn.execute(select(db.interventions.c.outcome)
                          .where(db.interventions.c.id == iid)).scalar()
check("  persisted", stored == "working")


# ===========================================================================
print("\n6. the review queue")
# ===========================================================================

# gid's review date is 30 days out, so it must NOT be in the queue yet.
due = development.due_for_review(engine, on=TODAY)
check("a goal whose review is still in the future stays out of the queue",
      not any(g["id"] == gid for g in due["goals"]), str(due["goals"]))

overdue = development.create_goal(
    engine, PID, title="Command the changeup",
    set_on=str(TODAY - timedelta(days=40)),
    review_on=str(TODAY - timedelta(days=5)))
due = development.due_for_review(engine, on=TODAY)
check("an overdue goal surfaces",
      any(g["id"] == overdue for g in due["goals"]), str(due["goals"]))
check("  with the player's slug to link to",
      all(g.get("slug") for g in due["goals"]))
check("a resolved intervention drops off the queue",
      not any(i["id"] == iid for i in due["interventions"]), str(due["interventions"]))

development.update_goal(engine, overdue, status="met")
due = development.due_for_review(engine, on=TODAY)
check("a met goal drops off the queue",
      not any(g["id"] == overdue for g in due["goals"]))


# ===========================================================================
print("\n7. deleting doesn't destroy history")
# ===========================================================================

tmp_goal = development.create_goal(engine, PID, title="Temporary mistake")
tmp_iv = development.create_intervention(engine, PID, title="Linked to it",
                                         goal_id=tmp_goal)
development.delete_goal(engine, tmp_goal)
with engine.connect() as conn:
    gone = conn.execute(select(db.goals.c.id)
                        .where(db.goals.c.id == tmp_goal)).first()
    kept = conn.execute(select(db.interventions.c.id, db.interventions.c.goal_id)
                        .where(db.interventions.c.id == tmp_iv)).first()
check("the goal is gone", gone is None)
check("  but the intervention survives it", kept is not None)
check("  with the link cleared, not dangling", kept.goal_id is None)

development.delete_intervention(engine, tmp_iv)
with engine.connect() as conn:
    check("an intervention can be deleted too",
          conn.execute(select(db.interventions.c.id)
                       .where(db.interventions.c.id == tmp_iv)).first() is None)


# ===========================================================================
print("\n8. routes")
# ===========================================================================

import app as hubapp  # noqa: E402
client = hubapp.app.test_client()

r = client.get("/players/test-arm")
check("the profile renders", r.status_code == 200, f"got {r.status_code}")
body = r.get_data(as_text=True)
check("  the goal form is on the page", 'id="goalForm"' in body)
check("  the intervention form is on the page", 'id="ivForm"' in body)
check("  goals are listed", "Add a tick to the fastball" in body)
check("  interventions are listed", "New four-seam grip" in body)
check("  the before/after is shown", "Before vs after" in body)
check("  and it disclaims causation", "not a cause" in body)

r = client.post("/api/goals", json={"player_id": PID, "title": "From the API",
                                    "metric_key": "fb_velocity",
                                    "direction": "increase", "target_value": 92})
check("POST /api/goals works", r.status_code == 200, f"got {r.status_code}")
new_id = r.get_json()["result"]

r = client.post("/api/goals", json={"player_id": PID, "title": ""})
check("  and rejects a bad one with 400", r.status_code == 400, f"got {r.status_code}")

r = client.post(f"/api/goals/{new_id}", json={"status": "met"})
check("POST /api/goals/<id> updates status", r.status_code == 200)
r = client.delete(f"/api/goals/{new_id}")
check("DELETE /api/goals/<id> works", r.status_code == 200)

r = client.post("/api/interventions", json={"player_id": PID, "title": "API drill",
                                            "category": "drill"})
check("POST /api/interventions works", r.status_code == 200, f"got {r.status_code}")
iv_id = r.get_json()["result"]
r = client.post(f"/api/interventions/{iv_id}", json={"outcome": "working"})
check("POST /api/interventions/<id> updates outcome", r.status_code == 200)
r = client.delete(f"/api/interventions/{iv_id}")
check("DELETE /api/interventions/<id> works", r.status_code == 200)

r = client.get("/api/reviews-due")
check("/api/reviews-due returns json", r.status_code == 200
      and "goals" in r.get_json())

r = client.get("/")
check("the home page renders with the review queue", r.status_code == 200)

# Writes must stay off on a public production URL.
hubapp.os.environ["RAILWAY_ENVIRONMENT"] = "production"
try:
    r = client.post("/api/goals", json={"player_id": PID, "title": "sneaky"})
    check("goal creation is blocked when writes are off", r.status_code == 403,
          f"got {r.status_code}")
    r = client.post("/api/interventions", json={"player_id": PID, "title": "sneaky"})
    check("intervention creation is blocked too", r.status_code == 403,
          f"got {r.status_code}")
    r = client.get("/players/test-arm")
    check("  but the profile still READS fine", r.status_code == 200)
finally:
    hubapp.os.environ.pop("RAILWAY_ENVIRONMENT", None)


print()
if FAILS:
    print(f"{len(FAILS)} check(s) FAILED:")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("all development checks passed\n")
