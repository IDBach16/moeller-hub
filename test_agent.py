"""
test_agent.py -- Phase F: the Coach Assistant on the player timeline.

No ANTHROPIC_API_KEY is needed. Every tool is exercised directly, and the one
place that calls the model takes an injectable stub, so the whole pipeline is
testable without spending anything.

The two risks worth testing:
  1. A tool leaking raw rows. Roadmap section 9 is the reason this system is
     affordable; one tool returning 900 swings undoes it.
  2. A summary regenerating on every page view. That is the same failure wearing
     a different hat.

    python test_agent.py
"""

import json
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


TMP = os.path.join(tempfile.mkdtemp(), "agent.db")
os.environ["DATABASE_URL"] = "sqlite:///" + TMP.replace("\\", "/")
os.environ.pop("HUB_PASSWORD", None)
os.environ.pop("RAILWAY_ENVIRONMENT", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import insert, select  # noqa: E402

import agent         # noqa: E402
import changes       # noqa: E402
import db            # noqa: E402
import development   # noqa: E402
import summaries     # noqa: E402

engine = db.get_engine()
RNG = random.Random(19)
TODAY = date(2026, 8, 17)

with engine.begin() as conn:
    JACK = conn.execute(insert(db.players).values(
        slug="jack-ujvagi", first_name="Jack", last_name="Ujvagi",
        is_pitcher=True, throws="R")).inserted_primary_key[0]
    MATT = conn.execute(insert(db.players).values(
        slug="matt-ponatoski", first_name="Matt", last_name="Ponatoski",
        bats="L", throws="R")).inserted_primary_key[0]
    QUIET = conn.execute(insert(db.players).values(
        slug="quiet-guy", first_name="Quiet", last_name="Guy")).inserted_primary_key[0]
    conn.execute(insert(db.player_aliases).values(
        player_id=JACK, source="awre", alias="Ujvagi, Jack"))

# 900 individual pitches across 9 sessions -- enough that a tool returning raw
# rows would be obvious.
for i, mean in enumerate([85, 84.8, 85.2, 85.1, 84.9, 88.1, 88.3, 88.2, 88.0]):
    with engine.begin() as conn:
        sid = conn.execute(insert(db.sessions).values(
            player_id=JACK, session_date=TODAY - timedelta(days=7 * (8 - i)),
            session_type="bullpen", source="rapsodo",
            purpose=("baseline" if i == 0 else None),
            source_ref=f"jack-{i}")).inserted_primary_key[0]
        conn.execute(insert(db.pitch_metrics), [
            {"session_id": sid, "player_id": JACK, "seq": j + 1, "pitch_type": "FB",
             "metric_key": "fb_velocity", "value": RNG.gauss(mean, 1.0)}
            for j in range(100)])

changes.compute_all(engine, write=True)
GID = development.create_goal(engine, JACK, title="Add a tick to the fastball",
                              metric_key="fb_velocity", direction="increase",
                              target_value=88.0, set_by="Ian")
IID = development.create_intervention(
    engine, JACK, title="New four-seam grip",
    intervention_date=str(TODAY - timedelta(days=28)), category="grip",
    coach="Ian", goal_id=GID)


def size(obj):
    return len(json.dumps(obj, default=str))


# ===========================================================================
print("\n1. resolving a player")
# ===========================================================================

r = agent.tool_find_player("Jack Ujvagi")
check("exact name resolves", r.get("player_id") == JACK, str(r))
check("  with his role", r.get("role") == "pitcher")
r = agent.tool_find_player("ujvagi")
check("a surname fragment resolves", r.get("player_id") == JACK, str(r))
r = agent.tool_find_player("Ujvagi, Jack")
check("an AWRE-style alias resolves", r.get("player_id") == JACK, str(r))
r = agent.tool_find_player("Nobody Here")
check("an unknown name returns an error, not a guess", "error" in r, str(r))
check("  and hands back the roster to pick from", "roster" in r and r["roster"])
r = agent.tool_find_player("")
check("an empty name is refused", "error" in r)


# ===========================================================================
print("\n2. no tool leaks raw rows")
# ===========================================================================
# This is the whole cost argument. Jack has 900 tracked pitches.

with engine.connect() as conn:
    raw = conn.execute(select(db.pitch_metrics)).all()
check("the fixture really does have ~900 raw rows", len(raw) == 900, str(len(raw)))

payloads = {
    "player_snapshot": agent.tool_player_snapshot("Jack Ujvagi"),
    "what_changed": agent.tool_what_changed("Jack Ujvagi"),
    "metric_history": agent.tool_metric_history("Jack Ujvagi", "fb_velocity"),
    "compare_windows": agent.tool_compare_windows("Jack Ujvagi", "fb_velocity"),
    "goals_and_interventions": agent.tool_goals_and_interventions("Jack Ujvagi"),
    "roster_alerts": agent.tool_roster_alerts(),
    "protocol_status": agent.tool_protocol_status(),
}
for name, payload in payloads.items():
    n = size(payload)
    # ~4 chars per token; every one of these must stay well under a few thousand.
    check(f"{name} stays compact ({n} chars)", n < 6000, f"{n} chars")

hist = payloads["metric_history"]
check("metric_history returns SESSIONS, not pitches",
      len(hist["sessions"]) == 9, f"{len(hist['sessions'])} rows for 900 pitches")
check("  each with a count and a mean",
      all("n" in s and "mean" in s for s in hist["sessions"]))
check("  and never an individual value",
      not any(isinstance(v, list) for s in hist["sessions"] for v in s.values()))


# ===========================================================================
print("\n3. the tools say something useful")
# ===========================================================================

snap = payloads["player_snapshot"]
check("snapshot names the player", snap["player"] == "Jack Ujvagi")
check("  reports the last session", snap["last_session"] is not None)
check("  counts his sessions", snap["training_sessions"] == 9, str(snap["training_sessions"]))
check("  and carries a status tile with a baseline",
      any(t.get("baseline") is not None for t in snap["current_status"]),
      str(snap["current_status"]))

wc = payloads["what_changed"]
check("what_changed found the velo gain", len(wc["changes"]) >= 1, str(wc))
check("  with a plain-English summary", "baseline" in wc["changes"][0]["summary"])
check("  and a favorable flag", wc["changes"][0]["favorable"] is True)

cmpw = payloads["compare_windows"]
check("compare_windows returns a finished comparison",
      "recent_mean" in cmpw and "effect_size" in cmpw and "p_value" in cmpw, str(cmpw))
check("  and hides the internal gate bookkeeping", "gates" not in cmpw)

cmpw5 = agent.tool_compare_windows("Jack Ujvagi", "fb_velocity", recent_sessions=5)
check("  window size is respected",
      cmpw5["recent_sessions"] == 5, str(cmpw5.get("recent_sessions")))

gi = payloads["goals_and_interventions"]
check("goals come back with progress", gi["goals"][0]["progress"] in
      ("met", "in_progress"), str(gi["goals"][0]))
check("  interventions carry a before/after",
      gi["interventions"][0]["before_after"], str(gi["interventions"][0]))
check("  and the payload disclaims causation", "not proof of a cause" in gi["note"])

ps = payloads["protocol_status"]
check("protocol_status names players with no data",
      "Quiet Guy" in ps["players_with_no_training_data"], str(ps))
check("  and knows Jack has a baseline session",
      "Jack Ujvagi" not in ps["players_without_a_baseline_session"], str(ps))

ra = payloads["roster_alerts"]
check("roster_alerts lists the change", len(ra["changes_needing_review"]) >= 1)
check("  attributed to the right player",
      ra["changes_needing_review"][0]["player"] == "Jack Ujvagi")


# ===========================================================================
print("\n4. empty is explained, not silent")
# ===========================================================================

r = agent.tool_what_changed("Quiet Guy")
check("a player with no changes gets an explanation",
      r["changes"] == [] and "note" in r and "thresholds" in r["note"], str(r))
r = agent.tool_metric_history("Quiet Guy", "bat_speed")
check("no data for a metric says so", r["sessions"] == [] and "note" in r, str(r))
r = agent.tool_metric_history("Jack Ujvagi", "not_a_metric")
check("an unknown metric returns the list of real ones",
      "error" in r and "fb_velocity" in r["available"], str(r)[:120])
r = agent.tool_compare_windows("Quiet Guy", "fb_velocity")
check("comparing with no data is handled", "note" in r, str(r))
r = agent.tool_player_snapshot("Quiet Guy")
check("a bare snapshot flags that it's game data only", r.get("note") is not None)


# ===========================================================================
print("\n5. every tool is registered and callable")
# ===========================================================================

check("registry and schema list agree",
      {t["name"] for t in agent.TOOLS} == set(agent.TOOL_IMPLS),
      str({t["name"] for t in agent.TOOLS} ^ set(agent.TOOL_IMPLS)))
check("all nine development tools are present",
      {"find_player", "player_snapshot", "what_changed", "metric_history",
       "compare_windows", "goals_and_interventions", "roster_alerts",
       "protocol_status", "player_summary"} <= set(agent.TOOL_IMPLS))
check("the game-performance tools survived",
      {"season_pitching", "season_batting", "team_stats", "charting_report",
       "hittrax", "list_players"} <= set(agent.TOOL_IMPLS))
for t in agent.TOOLS:
    check(f"  {t['name']} has a description and schema",
          len(t.get("description", "")) > 40 and "input_schema" in t)
    req = t["input_schema"].get("required", [])
    props = t["input_schema"].get("properties", {})
    check(f"  {t['name']} required fields exist in properties",
          all(r in props for r in req), str(req))
check("the system prompt allows the markdown subset the chat renders",
      "FORMATTING" in agent.SYSTEM and "## Section headers" in agent.SYSTEM)
check("the system prompt still rules out what the renderer cannot draw",
      "no tables" in agent.SYSTEM and "no code fences" in agent.SYSTEM)
check("the system prompt carries the report templates",
      "REPORTS" in agent.SYSTEM and "Hitter report template:" in agent.SYSTEM
      and "Pitcher report template:" in agent.SYSTEM)
check("build_report is wired up", "build_report" in agent.TOOL_IMPLS)
check("the system prompt warns against claiming cause",
      "not proof of a cause" in agent.SYSTEM or "do not say it caused" in agent.SYSTEM)
check("the system prompt explains that empty != failure",
      "does NOT mean" in agent.SYSTEM)


# ===========================================================================
print("\n6. summary context is compact and honest")
# ===========================================================================

ctx = summaries.build_context(engine, JACK)
n = size(ctx)
check(f"context built from 900 pitches is small ({n} chars)", n < 4000, f"{n}")
check("  it names the player", ctx["player"] == "Jack Ujvagi")
check("  carries the detected change", len(ctx["changes"]) >= 1)
check("  carries the goal", len(ctx["goals"]) >= 1)
check("  carries the intervention", len(ctx["interventions"]) >= 1)
check("  session entries are aggregates",
      all(isinstance(v, str) for s in ctx["recent_sessions"] for v in s["metrics"].values()),
      str(ctx["recent_sessions"][:1]))
check("  and it caps recent sessions", len(ctx["recent_sessions"]) <= 4)

empty_ctx = summaries.build_context(engine, QUIET)
check("a player with nothing has nothing to say",
      not summaries.has_anything_to_say(empty_ctx))
check("  and Jack does", summaries.has_anything_to_say(ctx))


# ===========================================================================
print("\n7. summaries are cached, not regenerated")
# ===========================================================================

CALLS = []


def stub(context):
    CALLS.append(context)
    return "Velo is up about three ticks on the fastball since June."


res = summaries.generate(engine, JACK, call_model=stub)
check("first generation calls the model once", len(CALLS) == 1, str(len(CALLS)))
check("  and returns the text", res["summary"].startswith("Velo is up"))
check("  marked not cached", res.get("cached") is not True)

res2 = summaries.generate(engine, JACK, call_model=stub)
check("a second call does NOT hit the model", len(CALLS) == 1, str(len(CALLS)))
check("  and comes back marked cached", res2.get("cached") is True, str(res2))
check("  with the same text", res2["summary"] == res["summary"])

# Viewing the profile must never generate one.
import profiles  # noqa: E402
prof = profiles.profile(engine, "jack-ujvagi")
check("opening the profile does not call the model", len(CALLS) == 1, str(len(CALLS)))
check("  but it does show the cached summary",
      prof["ai_summary"] and prof["ai_summary"]["summary"] == res["summary"],
      str(prof["ai_summary"]))

# New data must invalidate it.
before_basis = summaries.basis(engine, JACK)
with engine.begin() as conn:
    sid = conn.execute(insert(db.sessions).values(
        player_id=JACK, session_date=TODAY + timedelta(days=7),
        session_type="bullpen", source="rapsodo",
        source_ref="jack-new")).inserted_primary_key[0]
    conn.execute(insert(db.pitch_metrics), [
        {"session_id": sid, "player_id": JACK, "seq": j + 1, "pitch_type": "FB",
         "metric_key": "fb_velocity", "value": RNG.gauss(90.0, 1.0)} for j in range(30)])
check("new data changes the basis", summaries.basis(engine, JACK) != before_basis)
summaries.generate(engine, JACK, call_model=stub)
check("  so the summary regenerates", len(CALLS) == 2, str(len(CALLS)))

with engine.connect() as conn:
    rows = conn.execute(select(db.ai_summaries)
                        .where(db.ai_summaries.c.player_id == JACK)).all()
check("  and only the current one is kept", len(rows) == 1, str(len(rows)))

res = summaries.generate(engine, QUIET, call_model=stub)
check("a player with nothing is skipped without a call",
      res["summary"] is None and "skipped" in res and len(CALLS) == 2, str(res))

os.environ.pop("ANTHROPIC_API_KEY", None)
res = summaries.generate(engine, MATT, force=True)
check("no API key degrades politely instead of crashing",
      res.get("summary") is None and "skipped" in res, str(res))


# ===========================================================================
print("\n8. the weekly job")
# ===========================================================================

CALLS.clear()
# Clear what section 7 already wrote, so the first weekly run genuinely writes.
with engine.begin() as conn:
    conn.execute(db.ai_summaries.delete())

res = summaries.run_weekly(engine, days=7, call_model=stub, on=TODAY)
check("the weekly run considers players with new data",
      res["considered"] >= 1, str(res))
check("  and writes for them", res["written"] >= 1, str(res))
check("  calling the model once per player written",
      len(CALLS) == res["written"], f"{len(CALLS)} calls, {res['written']} written")

CALLS.clear()
res = summaries.run_weekly(engine, days=7, call_model=stub, on=TODAY)
check("running it again costs nothing", len(CALLS) == 0, str(len(CALLS)))
check("  reported as cached", res["cached"] >= 1, str(res))

res = summaries.run_weekly(engine, everyone=True, dry_run=True, on=TODAY)
check("a dry run makes no calls and shows the context",
      all("context" in r for r in res["results"]))


# ===========================================================================
print("\n9. routes")
# ===========================================================================

import app as hubapp  # noqa: E402
client = hubapp.app.test_client()

r = client.get(f"/api/players/{JACK}/summary")
check("GET summary returns the cached one", r.status_code == 200
      and r.get_json().get("summary"), str(r.get_json())[:100])
r = client.get(f"/api/players/{QUIET}/summary")
check("  and reports absence plainly", r.status_code == 200
      and r.get_json().get("summary") is None)

r = client.get("/players/jack-ujvagi")
check("the profile renders with a summary", r.status_code == 200)
body = r.get_data(as_text=True)
check("  showing the text", "Velo is up" in body)
check("  and a rewrite button", 'id="sumBtn"' in body)

r = client.post("/api/summaries/weekly", json={"dry_run": True})
check("the weekly endpoint works", r.status_code == 200, f"got {r.status_code}")

hubapp.os.environ["RAILWAY_ENVIRONMENT"] = "production"
try:
    r = client.post(f"/api/players/{JACK}/summary")
    check("writing a summary is blocked when writes are off", r.status_code == 403)
    r = client.get(f"/api/players/{JACK}/summary")
    check("  but reading one still works", r.status_code == 200)
finally:
    hubapp.os.environ.pop("RAILWAY_ENVIRONMENT", None)

# The assistant endpoint must degrade, not crash, with no API key.
r = client.post("/api/agent", json={"messages": [{"role": "user", "text": "hi"}]})
check("the assistant endpoint answers without an API key",
      r.status_code == 200 and "configured" in r.get_json().get("reply", ""),
      str(r.get_json())[:120])


print()
if FAILS:
    print(f"{len(FAILS)} check(s) FAILED:")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("all agent checks passed\n")
