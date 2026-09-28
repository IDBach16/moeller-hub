"""
test_pitching_agent.py -- the investigating pitching analyst, without spending.

A fake client stands in for the API: it answers each turn with the tool calls a
scripted investigation would make, and finally with submit_report. What is
tested is everything around the model -- that the tools compute the doctrine
correctly on a known arsenal, that a bad report is rejected and a good one
stored, that the call cap forces a report, that every call is logged, and that
the teammate-naming tools are not on the menu.

    python test_pitching_agent.py
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
import pitching_agent as pa  # noqa: E402
import summaries  # noqa: E402

pa.LOG_DIR = Path(TMP) / "logs"
ENGINE = db.get_engine()
db.metadata.create_all(ENGINE)

# ---------------------------------------------------------------------------
section("a known arsenal")
# ---------------------------------------------------------------------------
# FB: ride shape (17.5 ride, 8 run) at 1:16 (38 deg). CH: same axis, so it
# fails the tilt test. CB at 218 deg: a perfect mirror. SL: gyro-ish, its own
# pitch. Twelve of each across three sessions, so every pitch clears MIN_N.
PITCHES = {
    "FB": dict(velocity=85.0, spin_rate=2100.0, spin_efficiency=92.0, induced_vertical_break=17.5,
               horizontal_break=8.0, release_height=5.9, release_side=-1.2, spin_axis=38.0),
    "CH": dict(velocity=77.0, spin_rate=1500.0, spin_efficiency=90.0, induced_vertical_break=12.0,
               horizontal_break=11.0, release_height=5.9, release_side=-1.2, spin_axis=45.0),
    "CB": dict(velocity=72.0, spin_rate=2300.0, spin_efficiency=80.0, induced_vertical_break=-12.0,
               horizontal_break=-4.0, release_height=5.9, release_side=-1.2, spin_axis=218.0),
    "SL": dict(velocity=78.0, spin_rate=2200.0, spin_efficiency=12.0, induced_vertical_break=1.0,
               horizontal_break=-5.0, release_height=5.9, release_side=-1.2, spin_axis=330.0),
}
with ENGINE.begin() as conn:
    pid = conn.execute(insert(db.players).values(
        slug="cooper-homoelle", first_name="Cooper", last_name="Homoelle",
        is_pitcher=True, is_active=True, throws="R").returning(db.players.c.id)).scalar()
    seq = 0
    for s in range(3):
        d = date(2026, 9, 1) + timedelta(days=7 * s)
        sid = conn.execute(insert(db.sessions).values(
            player_id=pid, session_date=d, session_type="bullpen", source="rapsodo",
            source_ref=f"sess{s}").returning(db.sessions.c.id)).scalar()
        rows = []
        for pt, vals in PITCHES.items():
            for i in range(4):
                seq += 1
                for k, v in vals.items():
                    # a little session drift on FB velocity so windows differ
                    val = v + (0.5 * s if (pt == "FB" and k == "velocity") else 0)
                    rows.append({"session_id": sid, "player_id": pid, "seq": seq,
                                 "pitch_type": pt, "metric_key": k, "value": val})
                    if pt == "FB" and k == "velocity":
                        rows.append({"session_id": sid, "player_id": pid, "seq": seq,
                                     "pitch_type": pt, "metric_key": "fb_velocity", "value": val})
        conn.execute(insert(db.pitch_metrics), rows)

inv = pa.Investigation(ENGINE, pid)

shape = inv.pitch_shape_report()
fb = next(p for p in shape["pitches"] if p["pitch"] == "FB")
check("pitch_shape_report names the fastball shape from the taxonomy", fb["fastball_shape"] == "ride", str(fb))
check("  Bauer Units are spin over velocity", fb["bauer_units"] == round(2100 / (85 + 0.5), 1) or 24 <= fb["bauer_units"] <= 25.5, str(fb["bauer_units"]))
check("  the axis comes as degrees and a clock", fb["spin_axis_degrees"] == 38 and fb["spin_axis_clock"] == "1:16", str((fb["spin_axis_degrees"], fb["spin_axis_clock"])))
check("  horizontal break is signed, not a magnitude", fb["horizontal_break_signed"] == 8.0)

sep = inv.separation_and_mirror()
pairs = {p["pitch"]: p for p in sep["pairs"] if p.get("versus") == "FB"}
check("separation: the changeup's 7 degrees of tilt fails the 30-degree rule",
      pairs["CH"]["axis_tilt_degrees"] == 7 and pairs["CH"]["changeup_tilt_ok"] is False, str(pairs["CH"]))
check("  and its 5.5 inches less ride passes the depth rule", pairs["CH"]["changeup_depth_ok"] is True)
check("  the curveball at 218 is a mirror of a 38-degree fastball", pairs["CB"]["mirrors_fastball"] is True, str(pairs["CB"]))
check("  the slider is its own pitch", pairs["SL"]["reads_as_one_pitch"] is False)
check("  no pitch here reads as one with the fastball", not any(p["reads_as_one_pitch"] for p in pairs.values()))

h = inv.metric_history("velocity")
check("metric_history refuses a pitch-specific metric without a pitch type", "error" in h and "his_pitch_types" in h)
h = inv.metric_history("velocity", "FB")
check("  and returns session averages newest first for a pitch",
      len(h["sessions_newest_first"]) == 3 and h["sessions_newest_first"][0]["mean"] == 86.0, str(h))
h = inv.metric_history("release_height")
check("  release height is pooled across pitches", h["pitch_type"].startswith("pooled") and len(h["sessions_newest_first"]) == 3)

cw = inv.compare_windows("velocity", "FB", recent_sessions=1, baseline_days=60)
check("compare_windows gives a delta against his own earlier sessions",
      cw["delta"] == 0.8 and cw["recent"]["sessions"] == 1 and cw["baseline"]["sessions"] == 2, str(cw))
check("  and says whether it clears the minimum meaningful change", cw["clears_mmc"] in (True, False) and cw["minimum_meaningful_change"] is not None)

check("the teammate and coach-action tools are not offered", not (pa.TOOL_NAMES & pa._DENIED)
      and "compare_pitchers" not in pa.TOOL_NAMES and "propose_goal" not in pa.TOOL_NAMES)

# ---------------------------------------------------------------------------
section("the loop, with a scripted model")
# ---------------------------------------------------------------------------
GOOD = {
    "read": "He is a ride arm with 17.5 inches of carry, and the changeup is the pitch in the way.",
    "overview": "Cooper is a ride guy. His fastball carries 17.5 inches at 1:16 on the clock and that plays at the top of the zone. "
                "The curveball mirrors the fastball almost exactly, which is a real weapon. The changeup tilts only seven degrees off the "
                "fastball, so out of the hand it reads as the same pitch. Next step is a pen of changeups working the tilt past an hour.",
    "findings": [
        {"parent": "FB", "items": [{"text": "17.5 inches of ride, a ride shape.", "tone": "good"}]},
        {"parent": "CH", "items": [{"text": "Seven degrees of tilt off the fastball; the rule wants thirty.", "tone": "bad"}]},
        {"parent": "CB", "items": [{"text": "A mirror of the fastball within a degree.", "tone": "good"}]},
    ],
    "watch": ["Worth a pen of changeups, watching whether the tilt clears thirty degrees."],
    "caveat": "",
}
BAD = dict(GOOD, findings=GOOD["findings"] + [{"parent": "FB", "items": [{"text": "again", "tone": "good"}]}])


class FakeClient:
    """Answers each turn from a script of tool calls. Honours a forced
    tool_choice by submitting, as the real model would."""

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
    [("get_pitcher_context", {}), ("what_changed", {})],
    [("separation_and_mirror", {})],
    [("submit_report", {"note": BAD})],          # rejected: FB parent repeated
    [("submit_report", {"note": GOOD})],
])
res = pa.investigate(ENGINE, pid, client=fake)
check("a scripted investigation ends with a valid report", res["note"]["read"].startswith("He is a ride arm"))
check("  the rejected report was sent back with reasons, not stored",
      res["calls"] == 5 and res["note"]["findings"][0]["parent"] == "FB" and len(res["note"]["findings"]) == 3)
log = Path(res["log"]).read_text(encoding="utf-8").splitlines()
events = [json.loads(l) for l in log]
check("  every model turn and tool call is logged", sum(1 for e in events if e["event"] == "tool") == 4
      and sum(1 for e in events if e["event"] == "model") == 4 and events[0]["event"] == "run", str([e["event"] for e in events]))
check("  the log carries the rejection reasons", any(e.get("rejected") for e in events))
check("  token usage is accumulated", res["usage"]["input"] == 4000 and res["usage"]["output"] == 400)
check("  the system prompt keeps the judgment and the report contract",
      "<how_to_investigate>" in pa.SYSTEM_AGENT and "<rules>" in pa.SYSTEM_AGENT and "read      TWO clauses" in pa.SYSTEM_AGENT
      and "which_shape_should_he_chase" in pa.SYSTEM_AGENT)
check("  and is written about him, never to him", "Write for coaches about him, never to him" in pa.SYSTEM_AGENT
      and '"your slider"' not in pa.SYSTEM_AGENT)

# the cap: a model that never submits on its own
greedy = FakeClient([[("pitch_shape_report", {})]] * 40)
res = pa.investigate(ENGINE, pid, client=greedy)
check("the call cap forces a report", res["capped"] is True and res["calls"] <= pa.MAX_CALLS + 1, f"calls={res['calls']}")
check("  and says so in the caveat", "call limit" in res["note"]["caveat"])

# storage through the note's own path
res = pa.investigate_and_store(ENGINE, pid, client=FakeClient([]))
with ENGINE.connect() as conn:
    row = conn.execute(select(db.ai_summaries).where(db.ai_summaries.c.player_id == pid)).first()
check("investigate_and_store writes the same ai_summaries row the note uses",
      row is not None and "agent" in (row.model or "") and summaries.parse_note(row.summary)["read"].startswith("He is"))
check("  and the cached read returns it without a model call",
      pa.investigate_and_store(ENGINE, pid, force=False, client=None).get("cached") is True)

print()
if FAILS:
    print(f"{len(FAILS)} check(s) FAILED:")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("all pitching-agent checks passed\n")
