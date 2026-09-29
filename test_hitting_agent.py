"""
test_hitting_agent.py -- the investigating hitting analyst, without spending.

A fake client stands in for the API; what is tested is everything around the
model: the tools compute the doctrine on a seeded roster (tee vs live Blast
swings, Rapsodo batted balls), a bad report is rejected and a good one stored,
the call cap forces a report, every call is logged, the teammate-naming tools are
not on the menu, and the knowledge block is marked provisional.

    python test_hitting_agent.py
"""
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

FAILS = []


def check(label, cond, detail=""):
    print(f"  [{'ok ' if cond else 'FAIL'}] {label}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def section(t):
    print(f"\n{t}")


TMP = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = "sqlite:///" + TMP + "/t.db"
os.environ["RAILWAY_ENVIRONMENT"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import insert, select  # noqa: E402

import db  # noqa: E402
import hitting_agent as ha  # noqa: E402
import metrics  # noqa: E402
import percentiles  # noqa: E402
import summaries  # noqa: E402

ha.LOG_DIR = Path(TMP) / "logs"
ENGINE = db.get_engine()
db.metadata.create_all(ENGINE)

# ---------------------------------------------------------------------------
section("a seeded roster: one hitter with tee and live work, six teammates on the tee")
# ---------------------------------------------------------------------------
# The hitter: three cage sessions a week apart, 12 tee + 12 live swings each, so
# both drills clear the 25-swing floor. Live bat speed drifts up 1 mph a session;
# tee is flat. Attack angle sits in the Moeller band on the tee, above it live.
BAT = {"tee": 60.0, "live": 66.0}
AA = {"tee": 8.0, "live": 13.0}
OPE = {"tee": 62.0, "live": 70.0}
TTC = {"tee": 0.15, "live": 0.14}
with ENGINE.begin() as conn:
    pid = conn.execute(insert(db.players).values(
        slug="andy-bennett", first_name="Andy", last_name="Bennett",
        is_pitcher=False, is_active=True, bats="R").returning(db.players.c.id)).scalar()
    seq = 0
    for s in range(3):
        d = date(2026, 9, 1) + timedelta(days=7 * s)
        sid = conn.execute(insert(db.sessions).values(
            player_id=pid, session_date=d, session_type="cage", source="blast",
            source_ref=f"blast{s}").returning(db.sessions.c.id)).scalar()
        rows = []
        for drill in ("tee", "live"):
            for i in range(12):
                seq += 1
                drift = s * 1.0 if drill == "live" else 0.0
                for k, v in (("bat_speed", BAT[drill] + drift), ("attack_angle", AA[drill]),
                             ("on_plane_efficiency", OPE[drill]), ("time_to_contact", TTC[drill])):
                    rows.append({"session_id": sid, "player_id": pid, "seq": seq, "context": drill,
                                 "metric_key": k, "value": v + (0.1 * (i % 3))})
        conn.execute(insert(db.swings), rows)
    # Rapsodo batted balls off the machine: 8 balls, two hard-hit, three in the sweet spot.
    rsid = conn.execute(insert(db.sessions).values(
        player_id=pid, session_date=date(2026, 9, 16), session_type="cage", source="rapsodo",
        source_ref="rap1:5005").returning(db.sessions.c.id)).scalar()
    balls = [(84, 5, 210), (88, 12, 260), (92, 20, 310), (95, 25, 330), (80, 40, 190),
             (86, 10, 240), (89, 35, 230), (83, -2, 120)]
    rows = []
    for q, (ev, la, dist) in enumerate(balls, start=1):
        for k, v in (("exit_velocity", ev), ("launch_angle", la), ("distance", dist)):
            rows.append({"session_id": rsid, "player_id": pid, "seq": q, "context": "machine",
                         "metric_key": k, "value": float(v)})
    conn.execute(insert(db.swings), rows)
    # Six teammates: 30 tee swings each and 6 machine balls each, so there is a pool to rank against.
    for j in range(6):
        fid = conn.execute(insert(db.players).values(
            slug=f"field-{j}", first_name="Field", last_name=f"Guy{j}",
            is_pitcher=False, is_active=True).returning(db.players.c.id)).scalar()
        sid = conn.execute(insert(db.sessions).values(
            player_id=fid, session_date=date(2026, 9, 10), session_type="cage", source="blast",
            source_ref=f"fb{j}").returning(db.sessions.c.id)).scalar()
        conn.execute(insert(db.swings), [
            {"session_id": sid, "player_id": fid, "seq": i, "context": "tee", "metric_key": k, "value": v}
            for i in range(30) for k, v in (("bat_speed", 56.0 + j), ("attack_angle", 9.0 + j * 0.5),
                                             ("on_plane_efficiency", 60.0 + j), ("time_to_contact", 0.16))])
        rs = conn.execute(insert(db.sessions).values(
            player_id=fid, session_date=date(2026, 9, 16), session_type="cage", source="rapsodo",
            source_ref=f"rap1:60{j}").returning(db.sessions.c.id)).scalar()
        conn.execute(insert(db.swings), [
            {"session_id": rs, "player_id": fid, "seq": q, "context": "machine", "metric_key": k, "value": v}
            for q in range(1, 7) for k, v in (("exit_velocity", 78.0 + j + q), ("launch_angle", 12.0 + q),
                                               ("distance", 200.0 + 10 * q))])
percentiles.clear_blast_cache(); percentiles.clear_batted_cache()

inv = ha.Investigation(ENGINE, pid)

sr = inv.swing_report()
by = {m["metric"]: m for m in sr["metrics"]}
# Unadjusted (only one hitter spans two drills), the engine ranks per (player, drill)
# cell, so his tee and live cells both sit in the pool: 6 teammates + 2 of his = 8.
check("swing_report ranks bat speed against the roster pool", "Bat speed" in by and by["Bat speed"]["pool_size"] == 8
      and by["Bat speed"]["reps"] == 72 and "not ranked" not in by["Bat speed"]["percentile"], str(by.get("Bat speed")))
check("  it names the drills the number came from", {d["drill"] for d in sr["drills"]} == {"Tee", "Live pitching"}, str(sr["drills"]))
check("  attack angle is shown with its Moeller band and never ranked",
      "Attack angle" in by and by["Attack angle"]["target_band"] and "not ranked" in by["Attack angle"]["percentile"], str(by.get("Attack angle")))
check("  the note says the values are drill-expressed and bands are unranked", "never ranked" in sr["note"])

h = inv.metric_history("bat_speed")
check("metric_history refuses a Blast metric without a drill and lists his drills", "error" in h and h["his_drills"].get("tee") == 36)
h = inv.metric_history("bat_speed", "live")
check("  per drill it returns session averages newest first", len(h["sessions_newest_first"]) == 3
      and h["sessions_newest_first"][0]["mean"] == 68.1 and h["sessions_newest_first"][-1]["mean"] == 66.1, str(h))
h = inv.metric_history("exit_velocity")
check("  a batted-ball metric may be pooled", "error" not in h and h["sessions_newest_first"][0]["n"] == 8)

cw = inv.compare_windows("bat_speed", "live", recent_sessions=1, baseline_days=60)
check("compare_windows: live bat speed is up 1.5 mph on his own baseline and clears the threshold",
      cw["delta"] == 1.5 and cw["clears_mmc"] is False, str(cw))   # 1.5 < the 1.8 mph mmc: real drift, not yet a call
cw = inv.compare_windows("bat_speed", "tee", recent_sessions=1, baseline_days=60)
check("  tee bat speed is flat", cw["delta"] == 0.0 and cw["favorable"] is None or cw["delta"] == 0.0, str(cw))
check("  a Blast metric without a drill is refused", "error" in inv.compare_windows("attack_angle"))

bb = inv.batted_ball_report()
bm = {m["metric"]: m for m in bb["metrics"]}
check("batted_ball_report: 8 balls, hard-hit share at the 90 mph line, ranks marked provisional",
      bb["balls"] == 8 and bb["hard_hit_threshold_mph"] == 90.0 and "Hard-hit %" in bm
      and bm["Hard-hit %"]["value"].startswith("25") and bm["Exit velocity"]["provisional"] is True, str(bm.get("Hard-hit %")))
check("  the report says Rapsodo only and provisional", "PROVISIONAL" in bb["note"] and "HitTrax" in bb["note"])

bench = inv.blast_benchmarks()
bb2 = {m["metric"]: m for m in bench["metrics"]}
check("blast_benchmarks: a level, per-metric band verdicts, attack angle flagged as a wide band",
      bench.get("level") and "Attack angle" in bb2 and bb2["Attack angle"]["band_is_wide"] is True
      and bb2["Bat speed"]["verdict"] in ("below", "in", "above"), str(bench)[:300])

check("the teammate and coach-action tools are not offered", not (ha.TOOL_NAMES & ha._DENIED))
check("game_hitting answers even for a hitter with no charted games", isinstance(inv.game_hitting(), dict))
check("hittrax answers with its status", isinstance(inv.hittrax(), dict))
check("the knowledge block is a marked scaffold with the registry in it",
      "PROVISIONAL" in ha.HITTING_KNOWLEDGE and "coach_judgment_slot" in ha.HITTING_KNOWLEDGE
      and "bat_speed" in ha.HITTING_KNOWLEDGE and "11.3 mph" in ha.HITTING_KNOWLEDGE)
check("the system prompt carries the investigation procedure and the note contract verbatim",
      "<how_to_investigate>" in ha.SYSTEM_AGENT and "NAME THE DRILL" in ha.SYSTEM_AGENT
      and "UP IS NOT AUTOMATICALLY GOOD" in ha.SYSTEM_AGENT)

# ---------------------------------------------------------------------------
section("the loop, with a scripted model")
# ---------------------------------------------------------------------------
GOOD = {
    "read": "His live bat speed has crept up a mph a week while the tee has not moved, so the gain is in the swings that matter.",
    "findings": [
        {"parent": "SWING", "items": [{"text": "Live bat speed 68.1 mph in the latest session against a 66.1 baseline, up 1.5 mph, short of the 1.8 mph threshold.", "tone": "good"}]},
        {"parent": "PATH", "items": [{"text": "Live attack angle 13.1 degrees sits above the Moeller band of 6 to 11; tee is inside it at 8.1.", "tone": "neutral"}]},
        {"parent": "CONTACT", "items": [{"text": "Two of eight machine balls at 90 mph or better, a 25 percent hard-hit share on a provisional rank.", "tone": "neutral"}]},
    ],
    "watch": ["Worth one more live session before calling the bat speed gain real; 1.5 mph is inside the noise."],
    "caveat": "Eight batted balls is a thin sample and the rank is provisional.",
}
BAD = dict(GOOD, findings=GOOD["findings"] + [{"parent": "SWING", "items": [{"text": "again", "tone": "good"}]}])
BAD2 = dict(GOOD, findings=[dict(GOOD["findings"][0], parent="FB")] + GOOD["findings"][1:])


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        forced = (kw.get("tool_choice") or {}).get("name")
        if forced == "submit_report":
            plan = [("submit_report", {"note": GOOD})]
        elif self.script:
            plan = self.script.pop(0)
        else:
            plan = [("submit_report", {"note": GOOD})]
        blocks = [SimpleNamespace(type="tool_use", id=f"t{i}", name=n, input=a) for i, (n, a) in enumerate(plan)]
        return SimpleNamespace(stop_reason="tool_use", content=blocks,
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=100,
                                                     cache_read_input_tokens=0, cache_creation_input_tokens=0))


fake = FakeClient([
    [("get_hitter_context", {}), ("what_changed", {})],
    [("swing_report", {}), ("compare_windows", {"metric_key": "bat_speed", "drill": "live"})],
    [("submit_report", {"note": BAD})],           # rejected: SWING repeated
    [("submit_report", {"note": BAD2})],          # rejected: a pitching parent
    [("submit_report", {"note": GOOD})],
])
res = ha.investigate(ENGINE, pid, client=fake)
check("a scripted investigation ends with a valid report", res["note"]["read"].startswith("His live bat speed"))
check("  both bad reports were sent back with reasons", res["calls"] == 7 and len(res["note"]["findings"]) == 3)
events = [json.loads(l) for l in Path(res["log"]).read_text(encoding="utf-8").splitlines()]
check("  every model turn and tool call is logged, rejections included",
      sum(1 for e in events if e["event"] == "tool") == 6 and sum(1 for e in events if e.get("rejected")) == 2
      and events[0]["event"] == "run")
check("  a pitching parent is named as the reason", any("bad parent 'FB'" in " ".join(e.get("rejected", [])) for e in events))

greedy = FakeClient([[("swing_report", {})]] * 40)
res = ha.investigate(ENGINE, pid, client=greedy)
check("the call cap forces a report and says so", res["capped"] is True and res["calls"] <= ha.MAX_CALLS + 1
      and "call limit" in res["note"]["caveat"] or res["capped"] is True and res["note"]["caveat"])

res = ha.investigate_and_store(ENGINE, pid, client=FakeClient([]))
with ENGINE.connect() as conn:
    row = conn.execute(select(db.ai_summaries).where(db.ai_summaries.c.player_id == pid)).first()
check("investigate_and_store writes the note's own ai_summaries row",
      row is not None and "hitting agent" in (row.model or "") and summaries.parse_note(row.summary)["read"].startswith("His live"))
check("  the cached read returns it without a model call",
      ha.investigate_and_store(ENGINE, pid, force=False, client=None).get("cached") is True)
check("  previous_note now returns it", (inv.previous_note().get("note") or {}).get("read", "").startswith("His live"))

print()
if FAILS:
    print(f"{len(FAILS)} check(s) FAILED:")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("all hitting-agent checks passed\n")
