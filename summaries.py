"""
summaries.py -- cached AI player summaries. See PLAYER_DEV_SPEC.md 9.2 and 9.3.

The rule this module exists to enforce: a player summary costs ONE API call when
new data lands, not one per page view. It is stored against a hash of what it was
written from -- the player's latest session plus the change events and goals in
play -- so an unchanged player returns cached text with no model call at all.

Two halves, deliberately separated so the expensive one is the only one that
needs an API key:

    build_context()    pure, testable, no network. Assembles the compact
                       summary of a player from what the database already
                       computed -- never raw swings or pitches.
    generate()         calls the model once with that context and stores it.

    python summaries.py                weekly run: everyone with new data
    python summaries.py --all          everyone, regardless of new data
    python summaries.py --player 14
    python summaries.py --dry-run      show the context, make no API call
"""

import hashlib
import json
import os
import sys
from datetime import date, timedelta

from sqlalchemy import delete, func, insert, select

import db
import metrics

MODEL = "claude-opus-5"

SYSTEM = """You are the pitching development analyst for Archbishop Moeller High \
School. The coaching staff sees the numbers themselves; your job is to read them \
the way a sharp analyst would and tell the staff what they mean, so they don't \
have to work it out from a table.

You are given a compact summary that the database has already computed: recent \
training sessions, detected changes against the player's own baseline, what he has \
been throwing, his game results, active development goals, and any logged \
interventions. You are NOT given the raw data, and you must not ask for it.

BE AN ANALYST, NOT A REPORT. Anyone can list what moved; what earns your place \
is the interpretation. You answer as JSON matching the schema you are given, \
and the page renders each field with its own visual treatment, so keep every \
field doing exactly its own job:

- "read": ONE sentence, the single thing that matters most right now and why. \
The interpretation, not the numbers -- "He's built a fastball-only bullpen \
routine that doesn't match how he actually pitches" beats "fastball usage rose \
32.5 points". A coach who reads only this line should know what to think about.
- "findings": grouped PARENT -> CHILDREN, 2 to 5 groups. The parent names what \
the evidence is about; the items are its variables, ONE short sentence each, \
each resting on a number you were given (show the number). Parents: a \
pitch-type code (FB, SI, CT, SL, CB, CH, SP) when the items are that pitch's \
variables -- its velocity, spin, break, efficiency, usage, or its GAME strike \
and whiff rates, which belong under the pitch they describe; "DELIVERY" for \
release height, release side and arm slot, which are properties of the whole \
delivery, not of a pitch; "MIX" for bullpen usage shifts across the arsenal; \
"GAME" for overall game results not tied to one pitch. Never repeat a parent. \
Order groups by how much a coach should care. ALWAYS use the game data \
(game_pitching) when it is present -- training says what his stuff is doing, \
game data says whether it played, and setting the two against each other is \
the whole reason they live in one system. Set each item's "tone" to "good" for \
a favorable development, "bad" for a concerning one, "neutral" for information \
that is neither.
- "watch": 1 or 2 suggestions the staff could act on, each opened with "Worth" \
or "Suggest" so it reads as an option, not an instruction. Things to CHECK, \
ASK, MEASURE or WATCH, and conditionals tied to what the data would show: \
"Worth asking him whether the release-side move was intentional -- if it \
wasn't, the fastball break gain may not hold." Do NOT prescribe mechanics: you \
do not see him throw, have no video and no biomechanics, so never "lower his \
arm slot", "shorten his stride", or any instruction about how to move his \
body. The staff decides what to do; you point at what deserves attention. If \
the data is too thin to suggest anything useful, return an empty list and say \
so in "caveat" -- never invent a suggestion.
- "caveat": one sentence on sample size or data thinness when a coach needs the \
warning ("three sessions is thin for calling a slot change settled"), or "" \
when there is nothing to flag. Saying something IS solid also belongs here.

Rules for every field:
- Every claim rests on a number you were given. Never invent one; if something \
isn't in the context, don't mention it.
- A change is a comparison, not a cause. If an intervention is logged near a \
change you may note the timing, but do not claim the intervention caused it.
- What he is THROWING is a finding too. A notable bullpen_pitch_mix shift is a \
deliberate act worth a coach's attention -- report it as a change in usage, \
never as a change in the pitch itself.
- Detected changes are already scoped to one pitch type where that matters. \
Name the pitch ("his fastball's horizontal break", not "his horizontal break") \
and never generalise one pitch's number to the whole arsenal.
- An empty change list means nothing cleared the thresholds, not that he isn't \
improving. Say so plainly when that is the story.
- Plain text inside every string -- no markdown, no bullets. Baseball shorthand \
is fine. Do not address the player; you write for coaches about him."""


# What _call_model forces the note into. The template renders these fields
# directly, so the shape is a contract, not a suggestion.
NOTE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["read", "findings", "watch", "caveat"],
    "properties": {
        "read": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["parent", "items"],
                "properties": {
                    "parent": {"type": "string",
                               "enum": ["FB", "SI", "CT", "SL", "CB", "CH", "SP",
                                        "DELIVERY", "MIX", "GAME"]},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["text", "tone"],
                            "properties": {
                                "text": {"type": "string"},
                                "tone": {"type": "string",
                                         "enum": ["good", "bad", "neutral"]},
                            },
                        },
                    },
                },
            },
        },
        "watch": {"type": "array", "items": {"type": "string"}},
        "caveat": {"type": "string"},
    },
}


def parse_note(text):
    """The structured note dict if the stored summary is the JSON shape.

    Older cached notes are plain prose; callers fall back to rendering those as
    a paragraph, so this returns None rather than raising on them.
    """
    if not text or not text.lstrip().startswith("{"):
        return None
    try:
        d = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not (isinstance(d, dict) and d.get("read")):
        return None
    # A note cached in an older shape (flat findings, no parent) falls back to
    # the plain-paragraph rendering instead of breaking the template.
    if any(not (isinstance(g, dict) and "parent" in g)
           for g in d.get("findings") or []):
        return None
    return d


# ---------------------------------------------------------------------------
# What the summary was written from
# ---------------------------------------------------------------------------

def basis(engine, player_id):
    """A hash of everything the summary depends on.

    If this is unchanged, the stored summary is still current and no model call
    is needed. If any of it moves -- a new session, a new detected change, a new
    goal -- the hash changes and the summary is regenerated on the next run.
    """
    with engine.connect() as conn:
        latest = conn.execute(
            select(func.max(db.sessions.c.id))
            .where(db.sessions.c.player_id == player_id)).scalar()
        change_ids = [r[0] for r in conn.execute(
            select(db.change_events.c.id)
            .where(db.change_events.c.player_id == player_id)
            .order_by(db.change_events.c.id))]
        goal_ids = [f"{r.id}:{r.status}" for r in conn.execute(
            select(db.goals.c.id, db.goals.c.status)
            .where(db.goals.c.player_id == player_id)
            .order_by(db.goals.c.id))]
        iv_ids = [f"{r.id}:{r.outcome}" for r in conn.execute(
            select(db.interventions.c.id, db.interventions.c.outcome)
            .where(db.interventions.c.player_id == player_id)
            .order_by(db.interventions.c.id))]
    raw = f"{latest}|{','.join(map(str, change_ids))}|{','.join(goal_ids)}|{','.join(iv_ids)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# A pitch's share of the work moving by this much is worth a coach's attention.
# Below it, it's just the normal shape of a bullpen.
MIX_SHIFT_PP = 8.0


def training_pitch_mix(engine, player_id):
    """What he actually threw recently vs before, as usage percentages.

    Usage is a development fact in its own right -- a pitcher going from 7% to 28%
    sliders is doing something different on purpose. It is deliberately NOT a
    change in any pitch, and change detection now compares within a pitch type so
    a mix shift can no longer masquerade as one (see metrics.PITCH_SPECIFIC).
    Computed here so the model is handed the comparison rather than doing
    arithmetic on raw counts.
    """
    with engine.connect() as conn:
        dates = [r[0] for r in conn.execute(
            select(db.sessions.c.session_date).distinct()
            .where((db.sessions.c.player_id == player_id) &
                   (db.sessions.c.source == "rapsodo"))
            .order_by(db.sessions.c.session_date.desc()))]
        if len(dates) < metrics.RECENT_SESSIONS + 1:
            return None
        recent, baseline = dates[:metrics.RECENT_SESSIONS], dates[metrics.RECENT_SESSIONS:]

        def counts(window):
            rows = conn.execute(
                select(db.pitch_metrics.c.pitch_type, func.count())
                .select_from(db.pitch_metrics.join(
                    db.sessions, db.sessions.c.id == db.pitch_metrics.c.session_id))
                .where((db.pitch_metrics.c.player_id == player_id) &
                       (db.pitch_metrics.c.metric_key == "velocity") &
                       (db.sessions.c.session_date.in_(window)))
                .group_by(db.pitch_metrics.c.pitch_type)).all()
            total = sum(n for _pt, n in rows) or 1
            return {(pt or "unlabelled"): round(100.0 * n / total, 1) for pt, n in rows}

        # Inside the connection block -- counts() closes over `conn`.
        now, before = counts(recent), counts(baseline)
    shifts = []
    for pt in sorted(set(now) | set(before)):
        a, b = before.get(pt, 0.0), now.get(pt, 0.0)
        if abs(b - a) >= MIX_SHIFT_PP:
            label = metrics.PITCH_TYPE_LABELS.get(pt, pt)
            shifts.append(f"{label} {a}% -> {b}% of his pitches "
                          f"({'+' if b > a else ''}{round(b - a, 1)} pts)")
    return {
        "recent_window": f"last {len(recent)} bullpens",
        "recent": {metrics.PITCH_TYPE_LABELS.get(k, k): f"{v}%" for k, v in now.items()},
        "baseline": {metrics.PITCH_TYPE_LABELS.get(k, k): f"{v}%" for k, v in before.items()},
        "notable_shifts": shifts,
    }


def build_context(engine, player_id):
    """The compact context the model is given. No raw rows, ever.

    A pitcher with 2,400 tracked pitches produces a few hundred tokens here --
    that is the whole point of roadmap section 9.
    """
    import profiles
    with engine.connect() as conn:
        p = conn.execute(select(db.players)
                         .where(db.players.c.id == player_id)).first()
        if not p:
            return None
    prof = profiles.profile(engine, p.slug)

    ctx = {
        "player": prof["player"]["name"],
        "role": "pitcher" if prof["player"]["is_pitcher"] else "position player",
        "bats": prof["player"]["bats"], "throws": prof["player"]["throws"],
        "last_session": prof["last_session"],
        "training_sessions": len(prof["training"]),
    }

    # Only unacknowledged changes -- acknowledged ones are already old news.
    ctx["changes"] = [
        {"summary": c["summary"], "severity": c["severity"],
         "favorable": c["favorable"], "detected_on": c["detected_on"],
         "effect_size": c["effect_size"],
         "observations": f"{c['n_recent']} recent vs {c['n_baseline']} baseline"}
        for c in prof["changes"] if not c["acknowledged"]][:6]

    ctx["recent_sessions"] = [
        {"date": s["date"], "type": s["type"], "purpose": s["purpose"],
         "metrics": {k: f"{m['mean']}{m['unit']} (n={m['n']})"
                     for k, m in s["metrics"].items()}}
        for s in prof["training"][:4]]

    ctx["goals"] = [
        {"title": g["title"], "status": g["status"],
         "metric": g["metric_label"], "target": g["target_value"],
         "progress": g["progress"].get("state"),
         "current": g["progress"].get("current"),
         "pct_of_the_way": g["progress"].get("pct")}
        for g in prof["goals"] if g["status"] == "active"][:5]

    ctx["interventions"] = [
        {"title": i["title"], "date": i["date"], "category": i["category"],
         "outcome": i["outcome"],
         "before_after": [f"{m['label']} {m['pre_mean']}->{m['post_mean']}{m['unit']}"
                          for m in (i.get("evaluation") or [])[:3]]}
        for i in prof["interventions"][:4]]

    if prof["player"]["is_pitcher"]:
        mix = training_pitch_mix(engine, player_id)
        if mix:
            ctx["bullpen_pitch_mix"] = mix

    game = prof.get("game") or {}
    if game.get("pitching"):
        g = game["pitching"]
        ctx["game_pitching"] = {
            "tracked_pitches": g["pitches"], "seasons": g["years"],
            "strike_pct": g["strike_pct"], "whiff_pct": g["whiff_pct"],
            # Per pitch, so game results can sit under the pitch they belong to.
            # "code" is our canonical pitch code; charted "Breaking Ball" has
            # none and keeps its raw name rather than borrowing a pitch.
            "by_pitch": [
                {"pitch": t.get("code") or t["pitch_type"], "n": t["n"],
                 "usage_pct": t["usage_pct"], "avg_velo": t["avg_velo"],
                 "strike_pct": t["strike_pct"], "whiff_pct": t["whiff_pct"]}
                for t in (g.get("pitch_types") or [])[:5]]}
    if game.get("batting"):
        b = game["batting"]
        ctx["game_hitting"] = {"pitches_seen": b["pitches_seen"],
                               "whiff_pct": b["whiff_pct"]}
    return ctx


def has_anything_to_say(ctx):
    """Don't spend a call on a player we know nothing about."""
    if not ctx:
        return False
    return bool(ctx.get("changes") or ctx.get("recent_sessions") or
                ctx.get("goals") or ctx.get("interventions"))


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def cached(engine, player_id, current_basis=None):
    """The stored summary IF it is still current. None means regenerate."""
    want = current_basis or basis(engine, player_id)
    with engine.connect() as conn:
        row = conn.execute(
            select(db.ai_summaries)
            .where((db.ai_summaries.c.player_id == player_id) &
                   (db.ai_summaries.c.basis == want))).first()
    if not row:
        return None
    return {"summary": row.summary, "model": row.model,
            "created_at": str(row.created_at), "basis": row.basis}


def store(engine, player_id, want, text, model=MODEL):
    with engine.begin() as conn:
        # One row per player -- an out-of-date summary is worthless, not history.
        conn.execute(delete(db.ai_summaries)
                     .where(db.ai_summaries.c.player_id == player_id))
        conn.execute(insert(db.ai_summaries).values(
            player_id=player_id, basis=want, summary=text, model=model))


def _call_model(context):
    """The one place this module spends money."""
    import anthropic
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL,
        # The model thinks by default and max_tokens caps thinking AND text
        # together -- 1100 risked truncating the JSON. Still small money: this
        # runs once per player per week, only when their data has moved.
        max_tokens=2000,
        output_config={"effort": "medium",
                       # The schema guarantees the reply parses; the template
                       # renders the fields directly.
                       "format": {"type": "json_schema", "schema": NOTE_SCHEMA}},
        system=[{"type": "text", "text": SYSTEM,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user",
                   "content": "Write the development note for this player.\n\n"
                              + json.dumps(context, indent=1, default=str)}],
    )
    if resp.stop_reason == "max_tokens":
        return ""   # never cache a truncated note
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def generate(engine, player_id, force=False, call_model=None):
    """Return a current summary, calling the model only if one is needed."""
    want = basis(engine, player_id)
    if not force:
        hit = cached(engine, player_id, want)
        if hit:
            return {**hit, "cached": True}

    ctx = build_context(engine, player_id)
    if not has_anything_to_say(ctx):
        return {"summary": None, "cached": False,
                "skipped": "no training data, changes, goals or interventions yet"}

    if call_model is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return {"summary": None, "cached": False,
                    "skipped": "no ANTHROPIC_API_KEY on the server"}
        call_model = _call_model

    text = call_model(ctx)
    if not text:
        return {"summary": None, "cached": False, "skipped": "model returned nothing"}
    store(engine, player_id, want, text)
    return {"summary": text, "model": MODEL, "basis": want, "cached": False}


# ---------------------------------------------------------------------------
# The weekly job  (spec 9.3)
# ---------------------------------------------------------------------------

def players_with_new_data(engine, since):
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(
            select(db.sessions.c.player_id).distinct()
            .where(db.sessions.c.session_date >= since))]


def run_weekly(engine, days=7, everyone=False, call_model=None, dry_run=False,
               on=None):
    """For each player with new data this week, refresh the cached summary.

    Coaches then read stored text; nobody pays per view.
    """
    today = on or date.today()
    if everyone:
        with engine.connect() as conn:
            ids = [r[0] for r in conn.execute(
                select(db.players.c.id)
                .where(db.players.c.is_active == True))]  # noqa: E712
    else:
        ids = players_with_new_data(engine, today - timedelta(days=days))

    out = {"considered": len(ids), "written": 0, "cached": 0, "skipped": 0,
           "results": []}
    for pid in ids:
        if dry_run:
            ctx = build_context(engine, pid)
            out["results"].append({"player_id": pid,
                                   "would_write": has_anything_to_say(ctx),
                                   "context": ctx})
            continue
        res = generate(engine, pid, call_model=call_model)
        if res.get("cached"):
            out["cached"] += 1
        elif res.get("summary"):
            out["written"] += 1
        else:
            out["skipped"] += 1
        out["results"].append({"player_id": pid, **res})
    return out


def main(argv):
    engine = db.get_engine()
    dry = "--dry-run" in argv
    everyone = "--all" in argv
    pid = None
    if "--player" in argv:
        pid = int(argv[argv.index("--player") + 1])

    if pid:
        res = generate(engine, pid) if not dry else {
            "context": build_context(engine, pid)}
        print(json.dumps(res, indent=2, default=str))
        return

    print("\nWeekly player summaries" + ("  (dry run -- no API calls)" if dry else ""))
    res = run_weekly(engine, everyone=everyone, dry_run=dry)
    print(f"  considered {res['considered']} player(s)")
    if dry:
        for r in res["results"]:
            print(f"    #{r['player_id']:<4} would_write={r['would_write']}")
    else:
        print(f"    written  {res['written']}")
        print(f"    cached   {res['cached']}  (no API call needed)")
        print(f"    skipped  {res['skipped']}")
        for r in res["results"]:
            if r.get("summary") and not r.get("cached"):
                print(f"\n  #{r['player_id']}: {r['summary']}")
    print()


if __name__ == "__main__":
    main(sys.argv[1:])
