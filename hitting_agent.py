"""
hitting_agent.py -- the hitting development analyst as an INVESTIGATING agent.

The pitching twin is pitching_agent.py: same loop, same report discipline, same
fences. The one-shot hitting note in summaries.py stays the Monday default.

What is different here, and it matters: the hitting JUDGMENT is not settled.
SYSTEM_HITTING is still the placeholder prompt awaiting Ian's own hitting
knowledge (see CLAUDE.md), so HITTING_KNOWLEDGE below is a SCAFFOLD -- only what
the repository already establishes: units and meanings from the metric registry,
the drill doctrine and its measured offsets, the Moeller-calibrated bands, Blast's
published bands with their caveats, the batted-ball definitions, the plain-English
glossary. It carries no opinion about what a good high-school swing looks like or
which fault matters most. That layer comes from the prompt-builder interview and
goes in the slot marked below. Until then the agent reasons from the doctrine and
the numbers, which is honest, and says less than the pitching one, which is right.

Two sources on purpose: the cage (Blast swings, Rapsodo batted balls) and the
charted game data. HitTrax joins through its own tool when the export lands.

    python hitting_agent.py "JJ Skeldon"           investigate, print the note
    python hitting_agent.py "JJ Skeldon" --store   and store it as his note

Every run writes logs/hitting_agent/<slug>_<stamp>.jsonl: every tool call, its
arguments, the size of what came back, latency, and token usage per turn.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select

import db
import metrics
import summaries

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "logs" / "hitting_agent"

MODEL = "claude-opus-5-5"
MAX_CALLS = 15          # tool calls per investigation; most hitters need five

# Same fence as the player's own chat: tools that name teammates or draft coach
# actions are not offered. This agent writes about ONE hitter.
_DENIED = {"compare_pitchers", "staff_leaderboard", "propose_goal",
           "propose_intervention", "group_overview", "list_players"}


def enabled() -> bool:
    """The Rewrite button uses the agent for hitters unless HITTING_AGENT=0."""
    return os.environ.get("HITTING_AGENT", "1").strip().lower() not in ("0", "false", "off")


# ---------------------------------------------------------------------------
# The knowledge scaffold: repository facts only. Ian's judgment goes in the slot.
# ---------------------------------------------------------------------------

def _registry_lines(side: str) -> str:
    rows = []
    for key, m in metrics.REGISTRY.items():
        if m.side != side:
            continue
        pol = {"higher_better": "higher is better", "lower_better": "lower is better",
               "target_band": "a target band, not a high score"}.get(m.polarity, m.polarity)
        band = f", Moeller band {m.target_band[0]:g} to {m.target_band[1]:g}" if m.target_band else ""
        rows.append(f"  {key:<24} {m.unit:<4} {pol}{band}; a change under {m.mmc:g}{m.unit} is noise")
    return "\n".join(rows)


HITTING_KNOWLEDGE = """<hitting_knowledge_scaffold>
PROVISIONAL. Everything in this block is established by the hub's own code and data.
It says what the numbers ARE and how they must be compared. It does not yet say what
a good high-school swing looks like, which faults matter most, or what to cue --
that is the coach's knowledge and it is not written down here yet. Where it would
be needed, say what the data shows and stop; do not supply a philosophy of hitting.

<what_each_number_is>
Blast Motion measures the BAT; Rapsodo (and later HitTrax) measure the BALL.
""" + _registry_lines("hitting") + """

Plain English for the hitter: bat speed is how fast the barrel is moving; hand
speed is how fast he gets the hands going; rotational acceleration is how quickly
the barrel turns; power is bat speed and mass together; on-plane efficiency is how
much of the swing is on the pitch plane; time to contact is trigger to impact;
commit time is how long he can wait before committing; attack angle is the
barrel's path up or down through contact; vertical bat angle is how much the
barrel is tipped below the hands at contact; early connection and connection at
impact are the body-to-barrel relationship, both target bands; exit velocity is
the ball off the bat; launch angle is how high it left; hard-hit is a ball at
%(hard_hit)g mph or more; sweet-spot is a launch angle of %(ss_lo)g to %(ss_hi)g degrees.
</what_each_number_is>

<compare_within_a_drill>
Every Blast metric is compared within ONE drill (tee, soft toss, machine, live,
general practice). The drill moves the numbers more than a swing change does:
measured on the first real export, one hitter's tee-vs-live bat speed gap was
11.3 mph against a 1.8 mph change threshold. So a pooled average moves whenever a
hitter's DRILL MIX moves, even when no swing changed, and it looks exactly like a
real decline. Never generalise one drill's number to his swing as a whole; say
"his tee bat speed" or "his live attack angle".

The population drill offsets, measured across the roster (a two-way fit,
player + drill), are small: bat speed runs about soft toss +1.0, general
practice +0.8, machine -1.0, tee -2.0 mph relative to an average drill; on-plane
efficiency about machine +1.6, soft toss +0.9, tee +0.2, general practice -2.0
points. The percentile panels adjust for these so one hitter's number is
comparable to another's; a hitter's OWN history is never adjusted, it is split.

Untagged swings (no drill) belong to no drill's baseline and drive no finding.
If much of his work is untagged, that is one line in the caveat, and the fix is
tagging in the Blast app, not analysis.
</compare_within_a_drill>

<what_a_drill_mix_shift_means>
What kind of swings he takes is a finding in its own right. A move from mostly
tee to mostly live reps fires no change detection by design, because every
metric is compared inside its own drill -- so if it is not reported as a change
in what he PRACTISES, nobody sees it. Report it separately from swing changes,
never merged: "he is taking live reps now instead of tee work" and "his tee bat
speed is up" are two different statements, and only the second is about his swing.
</what_a_drill_mix_shift_means>

<the_bands_and_the_benchmarks>
Two different yardsticks, never merged:
- The Moeller target bands (attack angle, vertical bat angle, early connection,
  connection at impact, launch angle) are the middle half of Moeller hitters'
  own means. In band means "typical for Moeller", NOT "good", and out of band is
  a flag to look, not a grade. These metrics are never ranked: the 99th
  percentile of attack angle is bad, and ranking by distance from a band that is
  itself calibrated on Moeller would rank hitters by how average they are.
- Blast's PUBLISHED bands for his level are the vendor's numbers, averaged over
  all his swings tagged or not, so they read low for a hitter who is mostly tee.
  Two of those bands, attack angle 0-15 degrees and vertical bat angle -10 to -40,
  are wide enough that every Moeller hitter clears them: clearing them is not
  evidence of anything. Blast's JV bat-speed band is provisional. Blast's own
  ideals are on-plane efficiency 70%% or higher and both connection angles near
  90 degrees. A freshman is read against the JV band, never middle school.
- What a miss MEANS depends on the metric, not the side it fell on: below the
  band is a shortfall on bat speed and better than the band on time to contact.
</the_bands_and_the_benchmarks>

<reliability_and_floors>
Every Blast metric is reliable at the 25-swing floor (session-split reliability
0.62 to 0.98), so a rank on bat speed, hand speed, rotational acceleration,
power, on-plane efficiency, time to contact or commit time is trustworthy at
25+ swings in one drill. Hinge angle and body tilt have no defensible direction
and are shown as traits, never ranked. The Rapsodo batted-ball ranks (exit
velocity, max exit velocity, hard-hit %%, sweet-spot %%, max distance) are
PROVISIONAL: a 5-ball floor set so ranks show at all, no reliability sweep yet.
Say so whenever a batted-ball rank carries weight in a finding.
</reliability_and_floors>

<cage_versus_game>
Cage work says what the swing is doing; game data (pitches seen, whiff rate,
plate-appearance outcomes, splits by pitcher hand) says whether it played.
Setting the two against each other is the whole reason they live in one system.
A change is a comparison, not a cause: an intervention logged near a change is
timing to note, not a cause to claim.
</cage_versus_game>

<coach_judgment_slot>
[To be written with Ian: what a good swing looks like at this level, which faults
matter and in what order, what each fault looks like in these numbers, and the
one-cue-per-note vocabulary. Until it is, do not invent any of it.]
</coach_judgment_slot>
</hitting_knowledge_scaffold>
""" % {"hard_hit": 90.0, "ss_lo": 8.0, "ss_hi": 32.0}


SYSTEM_AGENT = """<role>
You are the hitting development analyst for Archbishop Moeller High School, and
you are INVESTIGATING one hitter, not summarizing a table. The staff can read a
table; your job is the interpretation they would otherwise have to work out
themselves, and the discipline of checking a claim before you make it.
</role>

<how_to_investigate>
1. Call get_hitter_context, then what_changed. Read them. Say to yourself what
   kind of hitter the data describes before anything else, and which drills his
   numbers actually come from.
2. Look for what does not fit: a metric that moved in one drill but not another,
   a drill mix that shifted, a cage number that disagrees with the game, a band
   metric that left the band, an intervention logged near a change.
3. CONFIRM BEFORE YOU CONCLUDE. Call swing_report before you rank or grade any
   bat metric. Call batted_ball_report before you say anything about contact
   quality. Call blast_benchmarks before you say where he sits against Blast's
   bands. Call compare_windows (with the drill) before you call a change real.
   Call metric_history when the question is a trend or repeatability. Call
   training_drill_mix before you say his practice changed. Call game_hitting
   before you say whether the swing played. Call previous_note so this note can
   say what moved since the last one.
4. Stop when the evidence runs out or when you are confident. Do not call a
   tool to decorate a conclusion you already hold, and do not call one twice
   for the same answer. You have at most %d calls; most hitters need five.
5. Finish by calling submit_report exactly once. Its `note` argument is the
   whole note, shaped exactly as the contract below describes. It is the only
   way the run ends.
</how_to_investigate>

<tools_do_the_arithmetic>
The tools compute; you interpret. Never work out a percentile, a drill
adjustment, a delta, an effect size or a band verdict in your head -- ask the
tool and quote what it returned. If a tool says the sample is under the floor or
the rank is provisional, say so in the caveat rather than reasoning past it.
</tools_do_the_arithmetic>

""" % MAX_CALLS + HITTING_KNOWLEDGE + """
<note_contract_and_rules>
""" + summaries.SYSTEM_HITTING + """
</note_contract_and_rules>
"""


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def _r1(v):
    return None if v is None else round(float(v), 1)


def _band_text(key):
    m = metrics.REGISTRY.get(key)
    if not m or not m.target_band:
        return None
    return f"{m.target_band[0]:g} to {m.target_band[1]:g}{m.unit} (Moeller's middle half)"


class Investigation:
    """One hitter's tools, bound to him so the model never names a player."""

    def __init__(self, engine, player_id):
        self.engine = engine
        self.player_id = player_id
        with engine.connect() as conn:
            row = conn.execute(select(db.players).where(db.players.c.id == player_id)).first()
        if not row:
            raise ValueError(f"no player {player_id}")
        self.row = row
        self.name = f"{row.first_name} {row.last_name}"
        self._ctx = None

    # -- helpers -----------------------------------------------------------

    def _his_drills(self):
        """Swings per drill -- distinct (session, seq), not metric rows, since one
        swing is stored as one row per metric."""
        sub = (select(db.swings.c.context, db.swings.c.session_id, db.swings.c.seq)
               .where(db.swings.c.player_id == self.player_id).distinct().subquery())
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(sub.c.context, func.count().label("n")).group_by(sub.c.context)).all()
        return {(r.context or "untagged"): int(r.n) for r in rows}

    def _session_means(self, metric_key, drill=None, limit=None):
        q = (select(db.sessions.c.session_date,
                    func.avg(db.swings.c.value).label("mean"),
                    func.count().label("n"))
             .select_from(db.swings.join(db.sessions, db.sessions.c.id == db.swings.c.session_id))
             .where(db.swings.c.player_id == self.player_id)
             .where(db.swings.c.metric_key == metric_key)
             .where(db.swings.c.value.isnot(None)))
        if drill:
            q = q.where(db.swings.c.context == drill)
        q = q.group_by(db.sessions.c.session_date).order_by(db.sessions.c.session_date.desc())
        with self.engine.connect() as conn:
            rows = conn.execute(q).all()
        rows = rows[:limit] if limit else rows
        return [{"date": str(r.session_date), "mean": _r1(r.mean), "n": int(r.n)} for r in rows]

    @staticmethod
    def _bars(strip):
        out = []
        for b in strip.get("bars", []):
            key = next((k for k, lab, *_ in _STRIP_KEYS if lab == b["label"]), None)
            out.append({
                "metric": b["label"], "value": f"{b['display']}{b['unit']}", "reps": b["n"],
                "percentile": (b["ord"] if b.get("ord") else
                               "not ranked (a trait, not a score)" if b.get("no_rank") and not b.get("small") and not b.get("pool_short") else
                               "not ranked (under the floor)" if b.get("small") else
                               "not ranked (pool too small)" if b.get("pool_short") else "not ranked"),
                "pool_size": b.get("pool_n"),
                "drill_adjusted": bool(b.get("adjusted")),
                "provisional": bool(b.get("provisional")),
                "target_band": _band_text(key) if key else None,
            })
        return out

    # -- the tools ---------------------------------------------------------

    def get_hitter_context(self):
        if self._ctx is None:
            self._ctx = summaries.build_context(self.engine, self.player_id)
        return self._ctx

    def swing_report(self):
        import percentiles
        s = percentiles.blast_strip(self.engine, self.player_id)
        if not s:
            return {"note": "no tagged Blast swings for him; nothing to rank",
                    "drills": self._his_drills()}
        return {"swings_tagged": s["swings"], "drills": s["drills"],
                "adjusted_for_drill": s["adjusted"],
                "metrics": self._bars(s),
                "under_floor": [{"metric": t["label"], "value": f"{t['display']}{t['unit']}",
                                 "reps": t["n"], "floor": t["need"]} for t in s.get("thin", [])],
                "note": "values are his swing expressed at an average drill; percentiles are "
                        "against every Moeller hitter; band metrics are shown, never ranked"}

    def batted_ball_report(self):
        import percentiles
        s = percentiles.batted_strip(self.engine, self.player_id)
        if not s:
            return {"note": "no Rapsodo batted balls for him"}
        return {"balls": s["balls"], "drills": s["drills"], "adjusted_for_drill": s["adjusted"],
                "hard_hit_threshold_mph": s["hard_hit_mph"], "sweet_spot_degrees": list(s["sweet_spot"]),
                "metrics": self._bars(s),
                "under_floor": [{"metric": t["label"], "value": f"{t['display']}{t['unit']}",
                                 "balls": t["n"], "floor": t["need"]} for t in s.get("thin", [])],
                "note": "Rapsodo only (HitTrax is a different system); every rank here is "
                        "PROVISIONAL -- a 5-ball floor and no reliability sweep yet"}

    def blast_benchmarks(self):
        import percentiles
        b = percentiles.blast_benchmark(self.engine, self.player_id)
        if not b:
            return {"note": "no Blast swings for him"}
        out = {"level": b.get("level_label"),
               "source": "Blast's published bands for his level, averaged over ALL his swings",
               "metrics": [{"metric": x["label"], "value": f"{x['display']}{x['unit']}", "swings": x["n"],
                            "band": x["range_txt"], "verdict": x["verdict"], "meaning": x["tone"],
                            "band_is_wide": bool(x.get("wide")), "provisional": bool(x.get("provisional")),
                            "blast_ideal": x.get("ideal")} for x in b.get("bars", [])]}
        if b.get("drill_note"):
            out["caveat"] = b["drill_note"]
        return out

    def what_changed(self):
        import agent
        return agent.tool_what_changed(self.name, only_unacknowledged=False)

    def metric_history(self, metric_key, drill=None):
        if metric_key not in metrics.REGISTRY or metrics.REGISTRY[metric_key].side != "hitting":
            return {"error": f"unknown hitting metric {metric_key}",
                    "hitting_metrics": sorted(k for k, m in metrics.REGISTRY.items() if m.side == "hitting")}
        if metric_key in metrics.CONTEXT_SPECIFIC and not drill:
            return {"error": f"{metric_key} is compared within one drill; pass drill",
                    "his_drills": self._his_drills(), "drill_codes": metrics.SWING_CONTEXTS}
        rows = self._session_means(metric_key, drill, limit=12)
        return {"metric": metric_key, "drill": drill or "all drills pooled (batted-ball metric)",
                "unit": metrics.REGISTRY[metric_key].unit, "sessions_newest_first": rows}

    def compare_windows(self, metric_key, drill=None, recent_sessions=3, baseline_days=120):
        if metric_key in metrics.CONTEXT_SPECIFIC and not drill:
            return {"error": f"{metric_key} is compared within one drill; pass drill",
                    "his_drills": self._his_drills()}
        rows = self._session_means(metric_key, drill)
        if len(rows) < 2:
            return {"error": "fewer than two sessions", "sessions": len(rows)}
        recent = rows[:recent_sessions]
        cutoff = datetime.strptime(recent[-1]["date"], "%Y-%m-%d").date() - timedelta(days=baseline_days)
        base = [r for r in rows[recent_sessions:] if datetime.strptime(r["date"], "%Y-%m-%d").date() >= cutoff]
        if not base:
            return {"error": "no baseline sessions inside the window", "recent": recent}
        rm = sum(r["mean"] for r in recent) / len(recent)
        bm = sum(r["mean"] for r in base) / len(base)
        sd = (math.sqrt(sum((r["mean"] - bm) ** 2 for r in base) / (len(base) - 1))
              if len(base) > 1 else None)
        m = metrics.REGISTRY.get(metric_key)
        delta = rm - bm
        return {"metric": metric_key, "drill": drill or "pooled",
                "recent": {"sessions": len(recent), "swings": sum(r["n"] for r in recent),
                           "mean": _r1(rm), "from": recent[-1]["date"], "to": recent[0]["date"]},
                "baseline": {"sessions": len(base), "swings": sum(r["n"] for r in base),
                             "mean": _r1(bm), "sd_of_session_means": _r1(sd),
                             "from": base[-1]["date"], "to": base[0]["date"]},
                "delta": _r1(delta),
                "effect_size": (None if not sd else round(delta / sd, 2)),
                "minimum_meaningful_change": m.mmc if m else None,
                "clears_mmc": (abs(delta) >= m.mmc) if m else None,
                "favorable": m.favorable(delta) if m else None,
                "target_band": _band_text(metric_key)}

    def training_drill_mix(self):
        return summaries.training_drill_mix(self.engine, self.player_id) or \
            {"note": "not enough cage sessions to compare a recent drill mix against a baseline"}

    def game_hitting(self):
        import agent
        return agent.tool_season_batting(self.name)

    def hittrax(self):
        import agent
        return agent.tool_hittrax(self.name)

    def goals_and_interventions(self):
        import agent
        return agent.tool_goals_and_interventions(self.name)

    def previous_note(self):
        hit = summaries.cached(self.engine, self.player_id) or {}
        note = summaries.parse_note(hit.get("summary")) if hit.get("summary") else None
        return {"written": hit.get("created_at"), "note": note} if note else {"note": "no note yet"}


def _strip_keys():
    import percentiles
    return [(row[0], row[1]) for row in (percentiles.BLAST_STRIP + percentiles.BATTED_STRIP)]


try:
    _STRIP_KEYS = _strip_keys()
except Exception:                                                  # noqa: BLE001
    _STRIP_KEYS = []


def _tool(name, description, props=None, required=None):
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": props or {},
                             "required": required or []}}


_DRILL = {"type": "string", "description": "drill code: tee, soft_toss, machine, live, practice"}
_MK = {"type": "string", "description": "registry key, e.g. bat_speed, attack_angle, on_plane_efficiency, "
       "time_to_contact, exit_velocity, launch_angle"}

TOOLS = [
    _tool("get_hitter_context",
          "ALWAYS FIRST. Everything the database has already computed for this hitter: identity "
          "and bats, his last 4 cage sessions with per-session means, his cage drill mix, where he "
          "sits outside Blast's bands, game hitting, goals, interventions, detected changes."),
    _tool("swing_report",
          "His Blast bat metrics, drill-adjusted and ranked against every Moeller hitter: value, "
          "percentile, pool size, reps, which drills fed it, target bands for the band metrics. "
          "Call BEFORE ranking or grading any bat metric."),
    _tool("batted_ball_report",
          "His Rapsodo batted balls: exit velocity, max exit velocity, hard-hit and sweet-spot "
          "shares, max distance, launch angle and spray, drill-adjusted and ranked (PROVISIONAL). "
          "Call BEFORE saying anything about contact quality."),
    _tool("blast_benchmarks",
          "Where he sits against Blast's PUBLISHED bands for his level, with each band's width "
          "flag and what a miss means for that metric. Call BEFORE saying where he stands against "
          "Blast's numbers."),
    _tool("what_changed",
          "Changes detection has already found against HIS OWN baseline, per drill, with effect "
          "sizes and what was suppressed. Call right after get_hitter_context."),
    _tool("metric_history",
          "One metric over his last 12 sessions as session averages. Blast metrics need a drill; "
          "batted-ball metrics may be pooled. Call for a trend or repeatability.",
          {"metric_key": _MK, "drill": _DRILL}, ["metric_key"]),
    _tool("compare_windows",
          "His recent sessions against his earlier baseline for one metric in one drill: means, "
          "delta, effect size, whether it clears the minimum meaningful change, and whether the "
          "direction is favorable per the registry. Call BEFORE calling a change real.",
          {"metric_key": _MK, "drill": _DRILL,
           "recent_sessions": {"type": "integer", "default": 3},
           "baseline_days": {"type": "integer", "default": 120}}, ["metric_key"]),
    _tool("training_drill_mix",
          "What kind of swings he has been taking lately against his baseline mix. A shift is a "
          "finding in itself; report it as practice, never as a change in the swing."),
    _tool("game_hitting",
          "Charted game data: pitches seen, whiff rate, plate-appearance outcomes, and the same "
          "split by pitcher hand. Call BEFORE saying whether the swing played."),
    _tool("hittrax",
          "HitTrax cage data if any has been published to the hub. Usually none yet; the tool "
          "says so."),
    _tool("goals_and_interventions",
          "His active goals with progress, and the interventions logged for him with before and "
          "after. Call for 'is the plan working' and before saying an intervention sits near a change."),
    _tool("previous_note",
          "The last note written about him, so this one can say what moved."),
    _tool("submit_report",
          "FINISH with this, exactly once. The complete note in the required schema: read, "
          "findings (parents SWING, PATH, CONTACT, DRILLS, BENCHMARK, GAME), watch, caveat. "
          "Nothing after this call is read.",
          {"note": {"type": "object", "description": "the note: read, findings, watch, caveat"}},
          ["note"]),
]

TOOL_NAMES = {t["name"] for t in TOOLS}
assert not (TOOL_NAMES & _DENIED)


# ---------------------------------------------------------------------------
# Report validation -- mirrors HITTING_NOTE_SCHEMA so nothing unrenderable is stored
# ---------------------------------------------------------------------------

_PARENTS = set(summaries.HITTING_PARENTS)
_TONES = {"good", "bad", "neutral"}


def validate_note(note) -> list[str]:
    problems = []
    if not isinstance(note, dict):
        return ["note must be an object"]
    for k in ("read", "findings", "watch", "caveat"):
        if k not in note:
            problems.append(f"missing {k}")
    if not isinstance(note.get("read"), str) or not note.get("read", "").strip():
        problems.append("read must be a non-empty sentence")
    if "overview" in note and not isinstance(note["overview"], str):
        problems.append("overview, if given, must be a string")
    f = note.get("findings")
    if not isinstance(f, list) or not (2 <= len(f) <= 5):
        problems.append("findings must be 2 to 5 groups")
    else:
        seen = set()
        for g in f:
            p = (g or {}).get("parent")
            if p not in _PARENTS:
                problems.append(f"bad parent {p!r}; use one of {sorted(_PARENTS)}")
            if p in seen:
                problems.append(f"parent {p} repeated")
            seen.add(p)
            items = (g or {}).get("items")
            if not isinstance(items, list) or not items:
                problems.append(f"group {p} has no items")
                continue
            for it in items:
                if not isinstance(it, dict) or not it.get("text") or it.get("tone") not in _TONES:
                    problems.append(f"item in {p} needs text and a tone of good/bad/neutral")
                    break
    w = note.get("watch")
    if not isinstance(w, list) or len(w) > 2 or not all(isinstance(x, str) for x in w):
        problems.append("watch must be a list of at most two sentences (an empty list is allowed)")
    if not isinstance(note.get("caveat", ""), str):
        problems.append("caveat must be a string")
    return problems


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def _client():
    import anthropic
    return anthropic.Anthropic()


def investigate(engine, player_id, max_calls=MAX_CALLS, client=None, log=True):
    """Run the investigation. Returns {"note", "summary", "calls", "usage",
    "log", "capped"} -- or raises if no valid report was produced."""
    inv = Investigation(engine, player_id)
    client = client or _client()
    impls = {n: getattr(inv, n) for n in TOOL_NAMES if n != "submit_report"}

    system = [{"type": "text", "text": SYSTEM_AGENT,
               "cache_control": {"type": "ephemeral"}}]
    messages = [{"role": "user",
                 "content": f"Investigate {inv.name} and submit the report."}]
    log_rows, usage = [], {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    calls, report, capped = 0, None, False
    t_run = time.time()

    def _log(**row):
        row["t"] = round(time.time() - t_run, 2)
        log_rows.append(row)

    submit_only = [t for t in TOOLS if t["name"] == "submit_report"]
    for turn in range(max_calls + 3):
        force = calls >= max_calls
        kwargs = dict(model=MODEL, max_tokens=8000,
                      output_config={"effort": "medium"},
                      system=system, messages=messages,
                      tools=submit_only if force else TOOLS)
        if force:
            kwargs["tool_choice"] = {"type": "tool", "name": "submit_report"}
            capped = True
        t0 = time.time()
        resp = client.messages.create(**kwargs)
        u = getattr(resp, "usage", None)
        if u is not None:
            usage["input"] += getattr(u, "input_tokens", 0) or 0
            usage["output"] += getattr(u, "output_tokens", 0) or 0
            usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
            usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        _log(event="model", turn=turn, stop=resp.stop_reason, seconds=round(time.time() - t0, 2),
             forced=force)

        uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
        if not uses:
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content": "Finish by calling submit_report."})
            calls = max(calls, max_calls) if turn >= max_calls else calls + 1
            continue

        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in uses:
            calls += 1
            t1 = time.time()
            if b.name == "submit_report":
                note = (b.input or {}).get("note")
                probs = validate_note(note)
                if probs:
                    out = {"rejected": probs, "fix_and_resubmit": True}
                    _log(event="tool", name=b.name, rejected=probs, seconds=round(time.time() - t1, 2))
                else:
                    report = note
                    _log(event="report", seconds=round(time.time() - t1, 2))
                    break
            else:
                fn = impls.get(b.name)
                try:
                    out = fn(**(b.input or {})) if fn else {"error": f"no such tool {b.name}"}
                except Exception as e:                           # noqa: BLE001
                    out = {"error": f"{type(e).__name__}: {e}"}
                text = json.dumps(out, default=str)
                _log(event="tool", name=b.name, args=b.input or {}, result_chars=len(text),
                     error=out.get("error") if isinstance(out, dict) else None,
                     seconds=round(time.time() - t1, 2))
                out = text[:30000]
            results.append({"type": "tool_result", "tool_use_id": b.id,
                            "content": out if isinstance(out, str) else json.dumps(out, default=str)})
        if report is not None:
            break
        messages.append({"role": "user", "content": results})

    path = None
    if log:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"{inv.row.slug}_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": "run", "player": inv.name, "model": MODEL,
                                 "calls": calls, "capped": capped, "usage": usage,
                                 "ok": report is not None}) + "\n")
            for r in log_rows:
                fh.write(json.dumps(r, default=str) + "\n")
    if report is None:
        raise RuntimeError(f"no valid report after {calls} calls (log: {path})")
    if capped and not report.get("caveat"):
        report["caveat"] = "The investigation hit its call limit; this note rests on what was confirmed before it did."
    return {"note": report, "summary": json.dumps(report), "calls": calls,
            "usage": usage, "log": str(path) if path else None, "capped": capped}


def investigate_and_store(engine, player_id, force=True, client=None):
    """summaries.generate's shape, on the agent: same basis, same cache, same
    storage row -- so the page, the chat's previous_note and the Monday job all
    read one note whichever mode wrote it."""
    want = summaries.basis(engine, player_id)
    if not force:
        hit = summaries.cached(engine, player_id, want)
        if hit:
            return {**hit, "cached": True}
    ctx = summaries.build_context(engine, player_id)
    if not summaries.has_anything_to_say(ctx):
        return {"summary": None, "cached": False,
                "skipped": "no training data, changes, goals or interventions yet"}
    if client is None and not os.environ.get("ANTHROPIC_API_KEY"):
        return {"summary": None, "cached": False, "skipped": "no ANTHROPIC_API_KEY on the server"}
    res = investigate(engine, player_id, client=client)
    model = f"{MODEL} hitting agent, {res['calls']} calls"
    summaries.store(engine, player_id, want, res["summary"], model=model)
    return {"summary": res["summary"], "model": model, "basis": want, "cached": False,
            "calls": res["calls"], "usage": res["usage"], "log": res["log"], "capped": res["capped"]}


# ---------------------------------------------------------------------------
# For the player's chat: the arithmetic tools, name-addressed
# ---------------------------------------------------------------------------

def _chat_impl(method):
    def run(player):
        import agent
        row, err = agent._resolve(player)
        if err:
            return err
        return getattr(Investigation(db.get_engine(), row.id), method)()
    return run


CHAT_TOOLS = [
    _tool("swing_report",
          "A hitter's Blast bat metrics, drill-adjusted and ranked against the roster, with target "
          "bands. Use before ranking or grading any bat metric.",
          {"player": {"type": "string"}}, ["player"]),
    _tool("batted_ball_report",
          "A hitter's Rapsodo batted balls -- exit velocity, hard-hit and sweet-spot shares, "
          "distance -- drill-adjusted and ranked (provisional). Use before talking contact quality.",
          {"player": {"type": "string"}}, ["player"]),
    _tool("blast_benchmarks",
          "Where a hitter sits against Blast's published bands for his level, with the caveats. "
          "Use before quoting Blast's numbers.",
          {"player": {"type": "string"}}, ["player"]),
]
CHAT_IMPLS = {"swing_report": _chat_impl("swing_report"),
              "batted_ball_report": _chat_impl("batted_ball_report"),
              "blast_benchmarks": _chat_impl("blast_benchmarks")}


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit(__doc__)
    import agent
    row, err = agent._resolve(args[0])
    if err:
        raise SystemExit(err)
    engine = db.get_engine()
    if "--store" in sys.argv:
        res = investigate_and_store(engine, row.id)
    else:
        res = investigate(engine, row.id)
    print(json.dumps(res.get("note") or summaries.parse_note(res.get("summary")), indent=1))
    print(f"\ncalls={res['calls']} usage={res['usage']} log={res.get('log')}")
