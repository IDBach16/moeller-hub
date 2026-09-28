"""
pitching_agent.py -- the pitching development analyst as an INVESTIGATING agent.

The note in summaries.py is one model call over a precomputed context: the
database computes, the model explains. That stays the Monday default -- cheap,
cached, reproducible. This module is the other mode, for the Rewrite button and
the chat: the same judgment, but the model can look things up before it
concludes. It starts from the same context, flags what does not fit, pulls the
comparison a claim needs (a shape report before naming a shape, a two-window
comparison before calling a change real, the game splits before saying a pitch
played), and ends by submitting the same report the note uses.

Two sources, on purpose: Rapsodo bullpens and the charted game data. Every tool
wraps a function the hub already trusts -- the within-pitch rule, the circular
axis, the sample floors and the no-invented-players rule all live there, and
fresh SQL would re-open the traps the tests guard.

    python pitching_agent.py "Cooper Homoelle"          investigate, print the note
    python pitching_agent.py "Cooper Homoelle" --store  and store it as his note

Every run writes logs/pitching_agent/<slug>_<stamp>.jsonl: every tool call, its
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
LOG_DIR = PROJECT_ROOT / "logs" / "pitching_agent"

MODEL = "claude-opus-5-5"
MAX_CALLS = 15          # tool calls per investigation; most pitchers need five

# Tools that name teammates or draft coach actions are not offered here -- the
# same fence as the player's own chat. This agent writes about ONE pitcher.
_DENIED = {"compare_pitchers", "staff_leaderboard", "propose_goal",
           "propose_intervention"}


def enabled() -> bool:
    """The Rewrite button uses the agent for pitchers unless PITCHING_AGENT=0."""
    return os.environ.get("PITCHING_AGENT", "1").strip().lower() not in ("0", "false", "off")


# ---------------------------------------------------------------------------
# The judgment, reframed as an investigation
# ---------------------------------------------------------------------------

def _section(text: str, tag: str) -> str:
    """One <tag>...</tag> block out of a prompt, tags included."""
    a = text.index(f"<{tag}>")
    b = text.index(f"</{tag}>") + len(f"</{tag}>")
    return text[a:b]


# The output contract and the rules are lifted from SYSTEM_PITCHING verbatim so
# the two modes can never disagree on what a note is. Only the framing changes.
SYSTEM_AGENT = """<role>
You are the pitching development analyst for Archbishop Moeller High School, and
you are INVESTIGATING one pitcher, not summarizing a table. The staff can read a
table; your job is the interpretation they would otherwise have to work out
themselves, and the discipline of checking a claim before you make it.
</role>

<how_to_investigate>
1. Call get_pitcher_context, then what_changed. Read them. Say to yourself what
   kind of arm he is before anything else.
2. Look for what does not fit: a fastball shape that has moved, a pitch that
   reads like another, a game number that disagrees with the pen, a release
   point that wandered, a usage shift, an intervention logged near a change.
3. CONFIRM BEFORE YOU CONCLUDE. Call pitch_shape_report before you name a
   fastball shape or judge a pitch against its profile. Call
   separation_and_mirror before you say two pitches blend, a changeup lacks
   tilt, or two pitches mirror. Call compare_windows before you call a change
   real. Call game_pitching before you say whether a pitch played. Call
   metric_history when the question is a trend or whether the delivery repeats.
   Call previous_note so this note can say what moved since the last one.
4. Stop when the evidence runs out or when you are confident. Do not call a
   tool to decorate a conclusion you already hold, and do not call one twice
   for the same answer. You have at most %d calls; most pitchers need five.
5. Finish by calling submit_report exactly once. It is the only way the run
   ends, and its input is the whole note.
</how_to_investigate>

<tools_do_the_arithmetic>
The tools compute; you interpret. Never work out a fastball shape, a
separation or mirror verdict, Bauer Units, a percentile, a delta or an effect
size in your head -- ask the tool, and quote what it returned. If a tool says
the sample is thin, say so in the caveat rather than reasoning past it.
</tools_do_the_arithmetic>

""" % MAX_CALLS + summaries.PITCHING_KNOWLEDGE + "\n\n" \
    + _section(summaries.SYSTEM_PITCHING, "output") + "\n\n" \
    + _section(summaries.SYSTEM_PITCHING, "rules")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def _shape(ivb, hb):
    """The fastball shape taxonomy from the prompt, as code."""
    if ivb is None or hb is None:
        return None
    run = abs(hb)
    if ivb >= 17 and run < 12:
        return "ride"
    if ivb >= 16 and run >= 16:
        return "ride-run"
    if run >= 16 and 6 <= ivb <= 12:
        return "runner"
    if run >= 16 and ivb <= 5:
        return "sinker"
    if ivb < 15 and run < 15:
        return "dead zone"
    return "in between"


def _axis_diff(a, b):
    """Signed circular difference in degrees, in (-180, 180]."""
    if a is None or b is None:
        return None
    return (a - b + 180) % 360 - 180


def _r1(v):
    return None if v is None else round(float(v), 1)


class Investigation:
    """One pitcher's tools, bound to him so the model never names a player."""

    def __init__(self, engine, player_id):
        self.engine = engine
        self.player_id = player_id
        with engine.connect() as conn:
            row = conn.execute(select(db.players).where(db.players.c.id == player_id)).first()
        if not row:
            raise ValueError(f"no player {player_id}")
        self.row = row
        self.name = f"{row.first_name} {row.last_name}"
        self._mix = None
        self._ctx = None

    # -- helpers -----------------------------------------------------------

    def _cards(self):
        import rapsodo_card
        return rapsodo_card.roster_cards(self.engine)

    def _my_mix(self):
        if self._mix is None:
            cards = self._cards()
            mine = cards.get(self.player_id) or {}
            self._mix = [m for m in mine.get("mix", []) if m.get("pt") != "UNK"]
        return self._mix

    def _session_means(self, metric_key, pitch_type=None, limit=None):
        q = (select(db.sessions.c.session_date,
                    func.avg(db.pitch_metrics.c.value).label("mean"),
                    func.count().label("n"))
             .select_from(db.pitch_metrics.join(
                 db.sessions, db.sessions.c.id == db.pitch_metrics.c.session_id))
             .where(db.pitch_metrics.c.player_id == self.player_id)
             .where(db.pitch_metrics.c.metric_key == metric_key)
             .where(db.pitch_metrics.c.value.isnot(None))
             .where(db.sessions.c.source == "rapsodo"))
        if pitch_type:
            q = q.where(db.pitch_metrics.c.pitch_type == pitch_type)
        q = q.group_by(db.sessions.c.session_date).order_by(db.sessions.c.session_date.desc())
        with self.engine.connect() as conn:
            rows = conn.execute(q).all()
        rows = rows[:limit] if limit else rows
        return [{"date": str(r.session_date), "mean": _r1(r.mean), "n": int(r.n)} for r in rows]

    # -- the tools ---------------------------------------------------------

    def get_pitcher_context(self):
        if self._ctx is None:
            self._ctx = summaries.build_context(self.engine, self.player_id)
        return self._ctx

    def pitch_shape_report(self):
        import percentiles
        mix = self._my_mix()
        if not mix:
            return {"note": "no Rapsodo bullpen with 3+ pitches of one type"}
        strips = {s["pitch"]: s for s in percentiles.by_pitch(self.engine, self.player_id)}
        cards = self._cards()
        rel = sorted(c["mix"][0]["rel_h"] for c in cards.values()
                     for _ in [0] if c.get("mix") and c["mix"][0].get("rel_h") is not None)
        pool_rel = ({"min": _r1(rel[0]), "median": _r1(rel[len(rel) // 2]), "max": _r1(rel[-1])}
                    if rel else None)
        out = []
        for m in mix:
            pct = {}
            s = strips.get(m["pt"])
            if s:
                pct = {b["label"]: (b["ord"] or "unranked") for b in s["bars"]}
            row = {
                "pitch": m["pt"], "pitches_measured": m["n"],
                "velocity": _r1(m.get("velo")), "top_velocity": _r1(m.get("max")),
                "spin_rate": m.get("spin"), "spin_efficiency_pct": m.get("eff"),
                "bauer_units": (round(m["spin"] / m["velo"], 1)
                                if m.get("spin") and m.get("velo") else None),
                "induced_vertical_break": _r1(m.get("ivb")),
                "horizontal_break_signed": _r1(m.get("hb")),
                "release_height": m.get("rel_h"),
                "spin_axis_degrees": m.get("axis"), "spin_axis_clock": m.get("axis_clock"),
                "percentiles": pct,
                "pool": (f"{s['pool']} ({s['pool_n']} arms)" if s else "not ranked (under the floor)"),
            }
            if m["pt"] in ("FB", "SI"):
                row["fastball_shape"] = _shape(m.get("ivb"), m.get("hb"))
            out.append(row)
        return {"throws": self.row.throws, "pitches": out,
                "staff_release_height": pool_rel,
                "note": "horizontal break is signed; percentiles rank it as a distance"}

    def separation_and_mirror(self):
        mix = {m["pt"]: m for m in self._my_mix()}
        primary = "FB" if "FB" in mix else ("SI" if "SI" in mix else None)
        if not primary:
            return {"note": "no fastball measured; nothing to separate from"}
        fb = mix[primary]
        pairs = []
        for pt, m in mix.items():
            if pt == primary:
                continue
            d_ivb = None if m.get("ivb") is None or fb.get("ivb") is None else round(m["ivb"] - fb["ivb"], 1)
            d_hb = None if m.get("hb") is None or fb.get("hb") is None else round(m["hb"] - fb["hb"], 1)
            d_ax = _axis_diff(m.get("axis"), fb.get("axis"))
            d_velo = None if m.get("velo") is None or fb.get("velo") is None else round(m["velo"] - fb["velo"], 1)
            same = (d_ivb is not None and d_hb is not None and d_ax is not None
                    and abs(d_ivb) <= 1 and abs(d_hb) <= 1 and abs(d_ax) <= 5)
            row = {"pitch": pt, "versus": primary, "velocity_gap": d_velo,
                   "ivb_gap": d_ivb, "hb_gap": d_hb,
                   "axis_tilt_degrees": None if d_ax is None else round(abs(d_ax)),
                   "axis_tilt_minutes": None if d_ax is None else round(abs(d_ax) * 2),
                   "reads_as_one_pitch": same}
            if pt == "CH" and d_ax is not None:
                row["changeup_tilt_ok"] = abs(d_ax) >= 30
                row["changeup_depth_ok"] = d_ivb is not None and d_ivb <= -5
            if pt == "SI" and d_ax is not None:
                row["sinker_tilt_ok"] = abs(d_ax) >= 15
            if pt in ("CB", "SL", "CT") and d_ax is not None:
                row["mirrors_fastball"] = abs(abs(d_ax) - 180) <= 10
                row["degrees_off_a_mirror"] = round(abs(abs(d_ax) - 180))
            pairs.append(row)
        if "SL" in mix and "CH" in mix:
            d = _axis_diff(mix["SL"].get("axis"), mix["CH"].get("axis"))
            if d is not None:
                pairs.append({"pitch": "SL", "versus": "CH",
                              "mirrors": abs(abs(d) - 180) <= 10,
                              "degrees_off_a_mirror": round(abs(abs(d) - 180))})
        return {"primary_fastball": primary, "pairs": pairs,
                "rules": "one pitch = both breaks within 1 inch AND axis within 5 degrees; "
                         "changeup wants 30 degrees of tilt and 5-8 inches less ride; "
                         "sinker wants 15 degrees; a mirror is within 10 degrees of 180"}

    def what_changed(self):
        import agent
        return agent.tool_what_changed(self.name, only_unacknowledged=False)

    def metric_history(self, metric_key, pitch_type=None):
        if metric_key not in metrics.REGISTRY:
            return {"error": f"unknown metric {metric_key}",
                    "pitching_metrics": sorted(k for k, m in metrics.REGISTRY.items() if m.side == "pitching")}
        if metric_key in metrics.PITCH_SPECIFIC and not pitch_type:
            return {"error": f"{metric_key} is compared within one pitch type; pass pitch_type",
                    "his_pitch_types": [m["pt"] for m in self._my_mix()]}
        rows = self._session_means(metric_key, pitch_type, limit=12)
        return {"metric": metric_key, "pitch_type": pitch_type or "pooled (a delivery property)",
                "unit": metrics.REGISTRY[metric_key].unit, "sessions_newest_first": rows}

    def compare_windows(self, metric_key, pitch_type=None, recent_sessions=3, baseline_days=120):
        if metric_key in metrics.PITCH_SPECIFIC and not pitch_type:
            return {"error": f"{metric_key} is compared within one pitch type; pass pitch_type"}
        rows = self._session_means(metric_key, pitch_type)
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
        return {"metric": metric_key, "pitch_type": pitch_type or "pooled",
                "recent": {"sessions": len(recent), "pitches": sum(r["n"] for r in recent),
                           "mean": _r1(rm), "from": recent[-1]["date"], "to": recent[0]["date"]},
                "baseline": {"sessions": len(base), "pitches": sum(r["n"] for r in base),
                             "mean": _r1(bm), "sd_of_session_means": _r1(sd),
                             "from": base[-1]["date"], "to": base[0]["date"]},
                "delta": _r1(delta),
                "effect_size": (None if not sd else round(delta / sd, 2)),
                "minimum_meaningful_change": m.mmc if m else None,
                "clears_mmc": (abs(delta) >= m.mmc) if m else None,
                "favorable": m.favorable(delta) if m else None}

    def training_pitch_mix(self):
        return summaries.training_pitch_mix(self.engine, self.player_id) or \
            {"note": "not enough bullpens to compare a recent mix against a baseline"}

    def game_pitching(self):
        return self.get_pitcher_context().get("game_pitching") or \
            {"note": "no tracked game pitches for him"}

    def goals_and_interventions(self):
        import agent
        return agent.tool_goals_and_interventions(self.name)

    def benchmark(self):
        import agent
        return agent.tool_benchmark(self.name)

    def previous_note(self):
        hit = summaries.cached(self.engine, self.player_id) or {}
        note = summaries.parse_note(hit.get("summary")) if hit.get("summary") else None
        return {"written": hit.get("created_at"), "note": note} if note else {"note": "no note yet"}


def _tool(name, description, props=None, required=None):
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": props or {},
                             "required": required or []}}


_PT = {"type": "string", "description": "pitch code: FB SI CT SL CB CH SP"}
_MK = {"type": "string", "description": "registry key, e.g. velocity, spin_rate, "
       "induced_vertical_break, horizontal_break, spin_efficiency, release_height, release_side"}

TOOLS = [
    _tool("get_pitcher_context",
          "ALWAYS FIRST. Everything the database has already computed for this pitcher: "
          "identity and throws, his last 4 sessions, his arsenal with percentiles and "
          "spin axis, bullpen pitch mix, game splits, goals, interventions, detected changes."),
    _tool("pitch_shape_report",
          "Per pitch: velocity, top velocity, spin, efficiency, Bauer Units, ride, run, "
          "release height, spin axis in degrees and on the clock, percentiles against his "
          "pool, and the fastball's shape class. Call BEFORE naming a shape or judging a "
          "pitch against its profile."),
    _tool("separation_and_mirror",
          "Every other pitch against his fastball: velocity gap, ride gap, run gap, axis "
          "tilt, and verdicts -- reads as one pitch, changeup tilt and depth, sinker tilt, "
          "mirror. Call BEFORE saying two pitches blend, a changeup lacks tilt, or two "
          "pitches mirror."),
    _tool("what_changed",
          "Changes detection has already found against HIS OWN baseline, with effect "
          "sizes and what was suppressed. Call right after get_pitcher_context."),
    _tool("metric_history",
          "One metric over his last 12 sessions as session averages. Pitch-specific "
          "metrics (velocity, spin, breaks, efficiency) need a pitch_type; release height "
          "and release side are pooled because slot is a property of the delivery. Call "
          "for a trend or for whether the delivery repeats.",
          {"metric_key": _MK, "pitch_type": _PT}, ["metric_key"]),
    _tool("compare_windows",
          "His recent sessions against his earlier baseline for one metric: means, delta, "
          "effect size, and whether it clears the minimum meaningful change. Call BEFORE "
          "calling a change real.",
          {"metric_key": _MK, "pitch_type": _PT,
           "recent_sessions": {"type": "integer", "default": 3},
           "baseline_days": {"type": "integer", "default": 120}}, ["metric_key"]),
    _tool("training_pitch_mix",
          "What he has been throwing in bullpens lately against his baseline mix. Usage is "
          "a finding in itself; report it as usage, never as a change in the pitch."),
    _tool("game_pitching",
          "In games, per pitch: usage, average velocity, strike percent, whiff percent, "
          "and the seasons covered. Call BEFORE saying whether a pitch played or whether "
          "he can land it."),
    _tool("goals_and_interventions",
          "His active goals with progress, and the interventions logged for him with "
          "before and after. Call for 'is the plan working' and before saying an "
          "intervention sits near a change."),
    _tool("benchmark",
          "Where he sits against his class and the program: his fastball line and the "
          "program medians. A second yardstick beside the pool percentile."),
    _tool("previous_note",
          "The last note written about him, so this one can say what moved."),
    _tool("submit_report",
          "FINISH with this, exactly once. The complete note in the required schema. "
          "Nothing after this call is read.",
          {"note": {"type": "object", "description": "the note: read, overview, findings, watch, caveat"}},
          ["note"]),
]

TOOL_NAMES = {t["name"] for t in TOOLS}
assert not (TOOL_NAMES & _DENIED)


# ---------------------------------------------------------------------------
# Report validation
# ---------------------------------------------------------------------------

_PARENTS = {"FB", "SI", "CT", "SL", "CB", "CH", "SP", "DELIVERY", "MIX", "GAME"}
_TONES = {"good", "bad", "neutral"}


def validate_note(note) -> list[str]:
    """Problems with a submitted note, [] if it is a note. Mirrors the schema the
    one-shot path enforces, so a report is never stored that the page cannot
    render."""
    problems = []
    if not isinstance(note, dict):
        return ["note must be an object"]
    for k in ("read", "overview", "findings", "watch", "caveat"):
        if k not in note:
            problems.append(f"missing {k}")
    if not isinstance(note.get("read"), str) or not note.get("read", "").strip():
        problems.append("read must be a non-empty sentence")
    if not isinstance(note.get("overview"), str) or len(note.get("overview", "").split()) < 20:
        problems.append("overview must be three to five sentences")
    f = note.get("findings")
    if not isinstance(f, list) or not (2 <= len(f) <= 5):
        problems.append("findings must be 2 to 5 groups")
    else:
        seen = set()
        for g in f:
            p = (g or {}).get("parent")
            if p not in _PARENTS:
                problems.append(f"bad parent {p!r}")
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
        problems.append("watch must be a list of at most two sentences")
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
            # Text without a submit: one nudge, then the cap will force it.
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
    model = f"{MODEL} agent, {res['calls']} calls"
    summaries.store(engine, player_id, want, res["summary"], model=model)
    return {"summary": res["summary"], "model": model, "basis": want, "cached": False,
            "calls": res["calls"], "usage": res["usage"], "log": res["log"], "capped": res["capped"]}


# ---------------------------------------------------------------------------
# For the player's chat: the two new tools, name-addressed
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
    _tool("pitch_shape_report",
          "A pitcher's arsenal, per pitch: velocity, spin, efficiency, Bauer Units, ride, run, "
          "release height, spin axis in degrees and on the clock, percentiles, and the "
          "fastball's shape class. Use before naming a fastball shape.",
          {"player": {"type": "string"}}, ["player"]),
    _tool("separation_and_mirror",
          "A pitcher's other pitches against his fastball: gaps in velocity, ride, run and "
          "axis, plus verdicts -- one pitch, changeup tilt and depth, sinker tilt, mirror. "
          "Use before saying two pitches blend or mirror.",
          {"player": {"type": "string"}}, ["player"]),
]
CHAT_IMPLS = {"pitch_shape_report": _chat_impl("pitch_shape_report"),
              "separation_and_mirror": _chat_impl("separation_and_mirror")}


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
