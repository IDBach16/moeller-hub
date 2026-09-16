# -*- coding: utf-8 -*-
"""
test_blast.py -- does the Blast / hitting pipeline still hold up?

Same discipline as test_rapsodo.py: every check here corresponds to a bug that
really happened, or to a rule that is easy to break silently. Nothing touches the
network or the real database -- it builds a synthetic CSV shaped like a real
Blast export and loads it into a throwaway SQLite file.

The failures this guards against all report SUCCESS while being wrong:

  * DRILL CONTEXT POOLED. The big one, and the hitting twin of the Maybury
    pitch-mix trap. A hitter's tee bat speed sits ~11 mph under his live bat
    speed. Pooled, every shift in his cage plan fires as a bat-speed collapse and
    the AI then explains a decline that never happened. The fixture below
    reproduces it: a hitter whose swing is IDENTICAL in both drills and whose tee
    share merely rises must produce NO finding.

  * ON PLANE EFFICIENCY SCALE. The export ships it as a 0-1 fraction under a
    header that says (%). Loaded unscaled it sits near 0.7, its 5-point threshold
    is never cleared by anything, and the metric silently never fires for anyone
    -- indistinguishable from "nothing changed".

  * SPLIT COLUMN WIDTH. change_events.pitch_type and player_baselines.pitch_type
    carry a pitch code (2 chars) OR a drill context ("soft_toss", 9). At the old
    String(4), Postgres rejects the write and SQLite silently accepts it, so the
    bug exists only in production.

  * UNTAGGED SWINGS. 20% of the first export carried no Environment Tag. They
    must be stored, and must contribute to no drill's baseline -- the same rule
    unlabelled pitches follow.

  * AIR SWINGS. Sensor readings with no ball. Blast leaves them out of a player's
    averages; so must we.

  * NAMES. 'Caleb Williams28' is a real Blast account spelling. It must queue for
    review with a suggestion, never be guessed into a player.

    python test_blast.py
"""

import csv
import io
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


def section(title):
    print(f"\n{title}")


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="blasttest_")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "t.db").replace("\\", "/")
os.environ.pop("RAILWAY_ENVIRONMENT", None)

import db                      # noqa: E402
import ingest                  # noqa: E402
import metrics                 # noqa: E402
import changes as C            # noqa: E402
import seed                    # noqa: E402
from sqlalchemy import select, insert, func   # noqa: E402

ENGINE = db.get_engine()
db.metadata.create_all(ENGINE)

DEG = "°"
HEADER = ["captureid", "Swing Timestamp", "timezone", "first_name", "last_name",
          "user_id", "Environment Tag", "Bat Nickname", "sensorserialnumber",
          "Bat Speed (MPH)", "Peak Hand Speed (MPH)", "Rotational Acceleration (G's)",
          "Power (kW)", "On Plane Efficiency (%)", f"Attack Angle ({DEG}'s)",
          f"Vert. Bat Angle ({DEG}'s)", "Time to Contact (s)", "Commit Time (s)",
          f"Early Connection ({DEG}'s)", f"Hinge Angle at Impact ({DEG}'s)",
          f"Connection at Impact ({DEG}'s)", f"Body Tilt Angle ({DEG}'s)",
          "Upload Timestamp", "actiontype"]

RNG = random.Random(20260916)
_CAP = [0]


def swing(first, last, uid, when, env, bat_speed, ope=0.70, action="swing"):
    """One export row. `ope` is a FRACTION, exactly as Blast ships it."""
    _CAP[0] += 1
    ts = when.strftime("%Y-%m-%d %H:%M:%S")
    return {
        "captureid": f"CAP{_CAP[0]:06d}", "Swing Timestamp": ts,
        # Real embedded newlines: the live export puts three lines in this field,
        # which is why the row count looked like 21,594 until csv.reader saw it.
        "timezone": "America/New_York\ndaylight\nGMT-4",
        "first_name": first, "last_name": last, "user_id": uid,
        "Environment Tag": env, "Bat Nickname": "",
        "sensorserialnumber": "472403002132",
        "Bat Speed (MPH)": round(bat_speed, 2),
        "Peak Hand Speed (MPH)": round(RNG.gauss(19, 1), 2),
        "Rotational Acceleration (G's)": round(RNG.gauss(12, 2), 2),
        "Power (kW)": round(RNG.gauss(3.2, 0.3), 2),
        "On Plane Efficiency (%)": round(ope, 6),
        f"Attack Angle ({DEG}'s)": round(RNG.gauss(9, 2), 2),
        f"Vert. Bat Angle ({DEG}'s)": round(RNG.gauss(-29, 3), 2),
        "Time to Contact (s)": round(RNG.gauss(0.155, 0.01), 3),
        "Commit Time (s)": round(RNG.gauss(0.09, 0.01), 3),
        f"Early Connection ({DEG}'s)": round(RNG.gauss(95, 4), 2),
        f"Hinge Angle at Impact ({DEG}'s)": round(RNG.gauss(55, 5), 2),
        f"Connection at Impact ({DEG}'s)": round(RNG.gauss(85, 3), 2),
        f"Body Tilt Angle ({DEG}'s)": round(RNG.gauss(34, 4), 2),
        "Upload Timestamp": ts, "actiontype": action,
    }


def as_csv(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=HEADER, quoting=csv.QUOTE_MINIMAL)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return ("﻿" + buf.getvalue()).encode("utf-8")      # BOM, as Blast sends


def air_swing_filter(row):
    return str(row.get("actiontype", "")).strip().lower() in metrics.BLAST_ACTION_TYPES


# ===========================================================================
section("Registry and vocabulary")
# ===========================================================================

check("every export column is mapped",
      len(metrics.BLAST_CSV_COLUMNS) == len(HEADER),
      f"{len(metrics.BLAST_CSV_COLUMNS)} mapped vs {len(HEADER)} columns")
check("no export column is left unmapped by name",
      not (set(HEADER) - set(metrics.BLAST_CSV_COLUMNS)),
      f"unmapped: {sorted(set(HEADER) - set(metrics.BLAST_CSV_COLUMNS))}")
check("on_plane_efficiency carries the fraction->percent scale",
      metrics.BLAST_CSV_COLUMNS["On Plane Efficiency (%)"][2] == 100.0)
check("no OTHER column carries a scale",
      all(v[2] == 1.0 for k, v in metrics.BLAST_CSV_COLUMNS.items()
          if k != "On Plane Efficiency (%)"))
check("every mapped key is a registry metric or a structural role",
      all(v[0] in metrics.REGISTRY or v[0] in db.COLUMN_ROLES
          for v in metrics.BLAST_CSV_COLUMNS.values()))
check("the two Blast schemas share no column name",
      not (set(metrics.BLAST_COLUMNS) & set(metrics.BLAST_CSV_COLUMNS)),
      "a shared name would make the seeded map ambiguous")

for raw, want in [("tee", "tee"), ("soft toss underhand", "soft_toss"),
                  ("soft toss overhand", "soft_toss"), ("pitching machine", "machine"),
                  ("general practice", "practice"), ("live pitch", "live")]:
    check(f"context {raw!r} -> {want}", metrics.normalize_context(raw) == want,
          repr(metrics.normalize_context(raw)))
check("an untagged swing normalizes to None, not a guess",
      all(metrics.normalize_context(v) is None for v in (None, "", "  ", "nan")))
check("an unknown tag normalizes to None rather than a plausible drill",
      metrics.normalize_context("cage work vs lefty") is None)

check("every Blast metric is context-specific",
      all(metrics.is_context_specific(m.key) for m in metrics.REGISTRY.values()
          if m.side == "hitting" and "blast" in m.sources))
check("split_label renders both vocabularies",
      metrics.split_label("FB") == "Fastball" and metrics.split_label("tee") == "Tee")

# The split column has to hold the LONGEST context, not the longest pitch code.
widest = max(len(c) for c in metrics.SWING_CONTEXTS)
for tbl in (db.player_baselines, db.change_events):
    length = tbl.c["pitch_type"].type.length
    check(f"{tbl.name}.pitch_type is wide enough for a drill context",
          length is not None and length >= widest,
          f"String({length}) but '{max(metrics.SWING_CONTEXTS, key=len)}' needs {widest}")
check("swings.context is wide enough",
      db.swings.c.context.type.length >= widest)


# ===========================================================================
section("Ingest: a real-shaped export")
# ===========================================================================

with ENGINE.begin() as conn:
    conn.execute(insert(db.players).values(
        id=1, slug="caleb-williams", first_name="Caleb", last_name="Williams"))
    conn.execute(insert(db.players).values(
        id=2, slug="jj-skeldon", first_name="JJ", last_name="Skeldon"))
seed.seed_blast_column_maps(ENGINE)

D0 = date(2026, 3, 1)
rows = []
# JJ, resolvable by name. One tagged block, one untagged, one air swing.
for _ in range(30):
    rows.append(swing("JJ", "Skeldon", 979428, D0, "tee", RNG.gauss(53, 2)))
for _ in range(30):
    rows.append(swing("JJ", "Skeldon", 979428, D0, "", RNG.gauss(64, 2)))
rows.append(swing("JJ", "Skeldon", 979428, D0, "tee", 99.0, action="air Swing"))
# Caleb, whose Blast account name carries a digit suffix.
for _ in range(20):
    rows.append(swing("Caleb", "Williams28", 990972, D0, "pitching machine",
                      RNG.gauss(62, 2)))

import_id, sniffed = ingest.store(
    ENGINE, "blast", "export.csv", as_csv(rows), side="hitting",
    session_type="cage", row_filter=air_swing_filter)

check("the BOM and the embedded newlines in `timezone` parse",
      sniffed["header_row"] == 0 and sniffed["columns"][0] == "captureid",
      f"header_row={sniffed['header_row']} first={sniffed['columns'][0]!r}")
check("the air swing is dropped", sniffed.get("rows_filtered") == 1,
      f"filtered {sniffed.get('rows_filtered')}")
check("every real swing is kept", sniffed["row_count"] == 80,
      f"kept {sniffed['row_count']}")

info = ingest.analyze(ENGINE, import_id)
check("a first/last pair satisfies the player requirement",
      info["ready"], f"missing: {info['missing_roles']}")
check("nothing is left unmapped", not info["unmapped"], str(info["unmapped"]))

stats = ingest.commit(ENGINE, import_id)
check("'Caleb Williams28' is queued, not guessed into a player",
      stats["names_queued"] == 1 and stats["rows_unresolved_player"] == 20,
      f"queued={stats['names_queued']} rows={stats['rows_unresolved_player']}")

with ENGINE.connect() as conn:
    rv = conn.execute(select(db.name_review).where(
        db.name_review.c.raw_name == "Caleb Williams28")).first()
check("...and it is queued WITH a suggestion for a human to accept",
      rv is not None and rv.suggested_player_id == 1 and rv.suggestion_score > 0.8,
      f"{rv and (rv.suggested_player_id, rv.suggestion_score)}")

check("untagged swings are reported for QC", stats.get("untagged_swings") == 30,
      f"{stats.get('untagged_swings')}")

with ENGINE.connect() as conn:
    ope = [r.value for r in conn.execute(
        select(db.swings.c.value).where(
            db.swings.c.metric_key == "on_plane_efficiency"))]
    ctxs = dict(conn.execute(
        select(db.swings.c.context, func.count())
        .where(db.swings.c.metric_key == "bat_speed")
        .group_by(db.swings.c.context)).all())

check("on-plane efficiency is stored as a PERCENT, not a fraction",
      ope and 30 <= min(ope) and max(ope) <= 100,
      f"range {min(ope):.3f}-{max(ope):.3f} -- unscaled fractions never clear "
      f"a 5-point threshold, so the metric would silently never fire")
check("tagged swings carry their drill", ctxs.get("tee") == 30, str(ctxs))
check("untagged swings are STORED with a NULL context, not dropped",
      ctxs.get(None) == 30, str(ctxs))


# ===========================================================================
section("The trap: a drill-mix shift must not fire as a swing change")
# ===========================================================================
# This hitter's swing does not change at all. His DRILL MIX does: early on he is
# mostly live, later he is mostly tee. Pooled, his bat speed 'collapses'. Split by
# drill, both series are flat and nothing should fire.

TEE, LIVE = 53.0, 64.0
mix = []
plan = [  # (day offset, tee swings, live swings)
    (0, 5, 55), (7, 5, 55), (14, 5, 55), (21, 5, 55), (28, 5, 55), (35, 5, 55),
    (90, 55, 5), (97, 55, 5), (104, 55, 5),
]
for offset, n_tee, n_live in plan:
    when = D0 + timedelta(days=offset)
    for _ in range(n_tee):
        mix.append(swing("Mix", "Hitter", 555001, when, "tee", RNG.gauss(TEE, 2)))
    for _ in range(n_live):
        mix.append(swing("Mix", "Hitter", 555001, when, "live pitch", RNG.gauss(LIVE, 2)))

with ENGINE.begin() as conn:
    conn.execute(insert(db.players).values(
        id=3, slug="mix-hitter", first_name="Mix", last_name="Hitter"))

mix_id, _ = ingest.store(ENGINE, "blast", "mix.csv", as_csv(mix),
                         side="hitting", session_type="cage")
ingest.commit(ENGINE, mix_id)

CUT = D0 + timedelta(days=90)
with ENGINE.connect() as conn:
    def pooled(where):
        return [r.value for r in conn.execute(
            select(db.swings.c.value).select_from(
                db.sessions.join(db.swings,
                                 db.swings.c.session_id == db.sessions.c.id))
            .where((db.swings.c.player_id == 3)
                   & (db.swings.c.metric_key == "bat_speed") & where))]
    pooled_recent = pooled(db.sessions.c.session_date >= CUT)
    pooled_base = pooled(db.sessions.c.session_date < CUT)

drop = sum(pooled_base) / len(pooled_base) - sum(pooled_recent) / len(pooled_recent)
check("the fixture really does reproduce the trap", drop > 5.0,
      f"pooled bat speed only moves {drop:.2f} mph -- fixture isn't exercising the bug")

verdicts, fired = C.compute_player(ENGINE, 3, write=False)
bat = [v for v in fired if v["metric_key"] == "bat_speed"]
check("NO bat-speed change fires from a drill-mix shift alone", not bat,
      f"fired {[(v.get('pitch_type'), v['summary']) for v in bat]} "
      f"-- pooled drop was {drop:.1f} mph")

keys = {(v["metric_key"], v.get("pitch_type")) for v in verdicts}
check("each drill is evaluated as its own series",
      ("bat_speed", "tee") in keys and ("bat_speed", "live") in keys,
      f"{sorted(k for k in keys if k[0] == 'bat_speed')}")
check("no context-specific hitting series is evaluated pooled",
      not any(k[1] is None for k in keys if metrics.is_context_specific(k[0])),
      f"pooled: {sorted(str(k) for k in keys if k[1] is None)}")

# And when a real change does fire, the finding must NAME the drill. "Bat speed is
# up" reads as a fact about the hitter; "Tee bat speed is up" is what a coach can
# act on, and is the only honest reading when the other drills did not move.
real = []
for offset in (150, 157, 164):
    when = D0 + timedelta(days=offset)
    for _ in range(40):
        real.append(swing("Mix", "Hitter", 555001, when, "tee", RNG.gauss(TEE + 6, 2)))
rid, _ = ingest.store(ENGINE, "blast", "real.csv", as_csv(real),
                      side="hitting", session_type="cage")
ingest.commit(ENGINE, rid)

verdicts2, fired2 = C.compute_player(ENGINE, 3, write=False)
tee_fire = [v for v in fired2 if v["metric_key"] == "bat_speed"
            and v.get("pitch_type") == "tee"]
check("a real within-drill gain DOES fire", bool(tee_fire),
      f"fired: {[(v['metric_key'], v.get('pitch_type')) for v in fired2]}")
check("...and the finding names the drill",
      bool(tee_fire) and "tee" in tee_fire[0]["summary"].lower(),
      tee_fire[0]["summary"] if tee_fire else "nothing fired")


# ===========================================================================
section("Display layer: the same rule, one level up")
# ===========================================================================
# Splitting the change engine but leaving the pages pooled would put the artefact
# back on the screen the coach actually reads.

import profiles                                          # noqa: E402
profiles.clear_cache()

cards = profiles.hitter_cards(ENGINE)
card = cards.get(3)
check("a hitter card reports ONE drill, not a blend of all of them",
      card is not None and card.get("drill") in metrics.SWING_CONTEXTS,
      f"{card and card.get('drill')}")
check("...and the card says which drill it is",
      card is not None and card.get("drill_label"),
      f"{card and card.get('drill_label')}")
# Mix Hitter is 53 off a tee and 64 live. A pooled card lands between the two and
# describes neither swing; a per-drill card sits on one of them.
check("the card's bat speed belongs to a real drill, not the average of two",
      card is not None and (abs(card["bat"] - TEE) < 2.0
                            or abs(card["bat"] - LIVE) < 2.0),
      f"bat={card and card['bat']} -- expected ~{TEE} or ~{LIVE}, "
      f"a value between them is the pooling bug")

series, ctx = profiles.metric_series(ENGINE, 3, "bat_speed", with_context=True)
check("a hitting sparkline is drawn within one drill", ctx in metrics.SWING_CONTEXTS,
      f"context={ctx!r}")
vals = [pt["mean"] for pt in series]
check("...so the line does not swing between drills",
      vals and (max(vals) - min(vals)) < abs(LIVE - TEE) - 2,
      f"range {min(vals) if vals else '-'}-{max(vals) if vals else '-'} spans both "
      f"drills, which is the cage plan plotted as though it were the swing")

# Usage is a finding in its own right -- and it is invisible to change detection
# BY DESIGN, since every metric is compared within its own drill.
import summaries                                         # noqa: E402
mix = summaries.training_drill_mix(ENGINE, 3)
check("a drill-mix shift is reported", mix is not None and mix["notable_shifts"],
      f"{mix}")
check("...and it is labelled as usage, not as a swing change",
      mix is not None and "not a swing change" in mix.get("note", "").lower(),
      f"{mix and mix.get('note')}")

# The profile itself.
with ENGINE.begin() as conn:
    conn.execute(db.players.update().where(db.players.c.id == 3)
                 .values(slug="mix-hitter"))
prof = profiles.profile(ENGINE, "mix-hitter")
check("the session log splits a mixed day into one row per drill",
      any(len(drills) > 1 for _d, drills in prof["training_by_date"]),
      "no day produced two drill rows, so a mixed day is still being pooled")
check("every status tile names its drill",
      all(t.get("context_label") for t in prof["status"]),
      f"{[(t['label'], t.get('context_label')) for t in prof['status']]}")



# ===========================================================================
section("Percentiles: where a swing ranks")
# ===========================================================================
# The rule percentiles.py enforces everywhere: nothing is ranked that has not
# been reliability-tested AND has a defensible direction. The direction half is
# what these guard -- a target band has no good end, and a bar implies one.

import percentiles                                       # noqa: E402

DIRS = {k: h for k, _l, _u, h, _n, _b in percentiles.BLAST_STRIP}

check("bat speed, hand speed, power, rotation and on-plane rank high-is-good",
      all(DIRS[k] is True for k in ("bat_speed", "peak_hand_speed", "power",
                                    "rotational_acceleration",
                                    "on_plane_efficiency")))
check("time to contact and commit time rank LOW-is-good",
      DIRS["time_to_contact"] is False and DIRS["commit_time"] is False)
# The whole point. Being at the 99th percentile of attack angle is bad.
for k in ("attack_angle", "vertical_bat_angle", "early_connection",
          "connection_at_impact"):
    m = metrics.get(k)
    check(f"{k} is a target band and is therefore NOT ranked",
          m.polarity == metrics.TARGET_BAND and DIRS[k] is None,
          f"polarity={m.polarity} direction={DIRS[k]}")
for k in ("hinge_angle", "body_tilt"):
    check(f"{k} is neutral and is therefore NOT ranked",
          metrics.get(k).polarity == metrics.NEUTRAL and DIRS[k] is None)
check("every ranked bar has a direction, and every unranked one has none",
      all((d is None) or isinstance(d, bool) for d in DIRS.values()))
check("every bar carries a plain-English blurb",
      all(b and len(b) > 5 for *_r, b in percentiles.BLAST_STRIP))
check("no registry hitting metric is ranked without appearing in the registry",
      all(k in metrics.REGISTRY for k, *_ in percentiles.BLAST_STRIP))

# --- a real field, so the pool rules and the drill adjustment can be exercised
FIELD_D0 = date(2027, 1, 4)
field = []
with ENGINE.begin() as conn:
    for i in range(8):
        conn.execute(insert(db.players).values(
            id=100 + i, slug=f"field-{i}", first_name="Field", last_name=f"Guy{i}"))

# Eight hitters, each with his own true bat speed. Everyone swings off a tee.
# The tee is built SLOWER by a fixed 6 mph for everyone -- a pure drill offset,
# no reordering -- which is the thing the adjustment has to remove.
TEE_OFFSET = -6.0
for i in range(8):
    true = 50 + 2 * i
    for d in range(2):
        when = FIELD_D0 + timedelta(days=d * 7)
        for _ in range(20):
            field.append(swing("Field", f"Guy{i}", 700000 + i, when, "tee",
                               RNG.gauss(true + TEE_OFFSET, 1.5)))
# Five of them ALSO do machine work at their true speed. Those five are what
# identifies the offset: only a hitter measured in two drills carries any
# information about the difference between them.
for i in range(5):
    true = 50 + 2 * i
    for d in range(2):
        when = FIELD_D0 + timedelta(days=d * 7)
        for _ in range(20):
            field.append(swing("Field", f"Guy{i}", 700000 + i, when,
                               "pitching machine", RNG.gauss(true, 1.5)))

fid, _ = ingest.store(ENGINE, "blast", "field.csv", as_csv(field),
                      side="hitting", session_type="cage")
ingest.commit(ENGINE, fid)
percentiles.clear_blast_cache()

table = percentiles._blast_table(ENGINE)
cells = percentiles._cells(table, "bat_speed")
grand, pe, de, linked = percentiles.drill_effects(cells)

check("the drill offset is recovered from within-player contrasts",
      abs((de["machine"] - de["tee"]) - abs(TEE_OFFSET)) < 1.0,
      f"recovered {de['machine'] - de['tee']:.2f} mph, built {abs(TEE_OFFSET)}")
check("...and it is identified only by hitters who span two drills",
      linked >= 5, f"linked={linked}")

# THE POINT OF THE WHOLE CHANGE: a hitter who only ever swings off a tee must
# not be punished for it. Guy7 is the hardest swinger in the fixture and does
# tee work only; unadjusted he would sit below machine hitters who are weaker.
top = percentiles.blast_strip(ENGINE, 107)          # true 64, tee only
mid = percentiles.blast_strip(ENGINE, 102)          # true 54, tee + machine
bot = percentiles.blast_strip(ENGINE, 100)          # true 50, tee + machine

def bat(strip):
    return next(b for b in strip["bars"] if b["label"] == "Bat speed")

check("the hardest swinger ranks top even though he only uses a tee",
      bat(top)["pct"] == 100,
      f"{bat(top)['pct']}th on {bat(top)['display']} -- a tee-only hitter being "
      f"marked down is exactly what the adjustment exists to prevent")
check("the softest ranks bottom", bat(bot)["pct"] == 0, f"{bat(bot)['pct']}th")
check("a middling hitter lands in between",
      0 < bat(mid)["pct"] < 100, f"{bat(mid)['pct']}th")
check("the bar says it was adjusted", bat(top).get("adjusted") is True)

check("everyone is ranked in ONE pool, not one pool per drill",
      len({bat(s)["pool_n"] for s in (top, mid, bot)}) == 1
      and bat(top)["pool_n"] >= 8,
      f"pools {[bat(s)['pool_n'] for s in (top, mid, bot)]}")
check("nothing is ranked against fewer than MIN_POOL hitters",
      all((b.get("pool_n") or 0) >= percentiles.MIN_POOL
          for b in top["bars"] if not b.get("no_rank")))

# Lower-is-better must invert, or a slow bat reads as elite.
ttc = [b for b in top["bars"] if b["label"] == "Time to contact"]
check("a lower-is-better bar is inverted, not shown raw",
      ttc and ttc[0].get("lower_better") is True)

check("the strip reports which drills fed it",
      top.get("drills") and all(d["drill"] and d["n"] for d in top["drills"]),
      str(top.get("drills")))

# The adjustment must refuse to run on an estimate it cannot make.
solo = {(1, "tee"): 60.0, (2, "tee"): 62.0, (3, "machine"): 70.0}
_g, _p, _d, solo_linked = percentiles.drill_effects(solo)
check("with nobody spanning two drills the offset is unidentified",
      solo_linked == 0,
      "an offset estimated from drill means alone is confounded by who does them")
check("...and MIN_LINKED is what stops it being applied anyway",
      percentiles.MIN_LINKED >= 2)


# ===========================================================================
section("Blast's own benchmarks -- the second pool")
# ===========================================================================
# These bands come from the vendor, not from us, so the checks are about
# applying them honestly: the right band for the level, the right MEANING for a
# miss, and the two caveats surfaced rather than buried.

check("a varsity hitter is read against the varsity band, not JV",
      metrics.blast_band("bat_speed", "varsity") == (60.0, 70.0)
      and metrics.blast_band("bat_speed", "jv") == (55.0, 65.0))
check("a freshman gets the JV band, not Middle School",
      metrics.blast_band("bat_speed", "freshman")
      == metrics.blast_band("bat_speed", "jv"),
      "a high-school freshman is a JV-level hitter; Middle School is younger")
check("an unknown level falls back to JV, the more forgiving band",
      metrics.blast_band("bat_speed", None) == metrics.blast_band("bat_speed", "jv"),
      "we would rather understate a shortfall than invent one")
check("level-independent bands ignore the level entirely",
      metrics.blast_band("on_plane_efficiency", "varsity")
      == metrics.blast_band("on_plane_efficiency", "freshman") == (65.0, 85.0))
check("a metric Blast publishes no band for returns None, not a guess",
      metrics.blast_band("rotational_acceleration", "varsity") is None,
      "Blast's Rotation Score is a different measurement and must not be "
      "approximated with rotational acceleration")
check("the two uninformative bands are flagged as wide",
      metrics.BLAST_WIDE == {"attack_angle", "vertical_bat_angle"},
      "every Moeller hitter clears both; passing is not evidence")
check("the misprinted JV bat-speed band is flagged provisional",
      ("bat_speed", "jv") in metrics.BLAST_PROVISIONAL
      and ("bat_speed", "freshman") in metrics.BLAST_PROVISIONAL)

# --- a hitter placed on purpose, to check the verdicts mean what they say ---
with ENGINE.begin() as conn:
    conn.execute(insert(db.players).values(
        id=200, slug="bench-guy", first_name="Bench", last_name="Guy"))
    conn.execute(insert(db.player_seasons).values(
        player_id=200, season=2026, level="varsity"))

bench = []
for d in range(2):
    when = date(2027, 3, 1) + timedelta(days=d * 7)
    for _ in range(20):
        # Slow bat (varsity band 60-70) and a quick time to contact.
        r = swing("Bench", "Guy", 810001, when, "tee", RNG.gauss(52, 1.0))
        r["Time to Contact (s)"] = round(RNG.gauss(0.130, 0.004), 3)
        bench.append(r)
bid, _ = ingest.store(ENGINE, "blast", "bench.csv", as_csv(bench),
                      side="hitting", session_type="cage")
ingest.commit(ENGINE, bid)
percentiles.clear_blast_cache()

bm = percentiles.blast_benchmark(ENGINE, 200)
bars = {b["key"]: b for b in (bm["bars"] if bm else [])}
check("the panel reports the level it used", bm and bm["level"] == "varsity"
      and "Varsity" in bm["level_label"], str(bm and bm.get("level_label")))
check("a slow bat against the varsity band reads as SHORT",
      bars.get("bat_speed", {}).get("tone") == "short",
      str(bars.get("bat_speed", {}).get("tone")))
# The one that is easy to get backwards: below the band is GOOD here.
check("under the time-to-contact band is BETTER, not short",
      bars.get("time_to_contact", {}).get("tone") == "good"
      and bars["time_to_contact"]["verdict"] == "below",
      "time to contact is lower-is-better; a fast hitter must not be marked short")
check("a target-band miss is 'watch', never 'short'",
      all(b["tone"] in ("watch", "in") for b in bars.values()
          if metrics.get(b["key"]) and
          metrics.get(b["key"]).polarity == metrics.TARGET_BAND),
      str([(b['key'], b['tone']) for b in bars.values()]))
check("a tee-heavy hitter gets the drill caveat, since these averages include "
      "every drill", bm and bm.get("drill_note"), str(bm and bm.get("drill_note")))
check("a negative band is written out, not rendered as a double minus",
      bars.get("vertical_bat_angle", {}).get("range_txt", "").count("--") == 0
      and " to " in bars.get("vertical_bat_angle", {}).get("range_txt", ""),
      repr(bars.get("vertical_bat_angle", {}).get("range_txt")))
check("the marker position is clamped so a big miss stays on the track",
      all(-0.3 <= b["pos"] <= 1.3 for b in bars.values()),
      str([(b['key'], round(b['pos'], 2)) for b in bars.values()]))

# The two pools must stay separate -- merging them produces a number that
# answers neither question.
strip = percentiles.blast_strip(ENGINE, 200)
check("the teammate ranking and the vendor benchmark are separate objects",
      strip is not None and bm is not None and "bars" in strip and "bars" in bm
      and strip.get("drills") is not None and bm.get("level") is not None)

print()
if FAILS:
    print(f"{len(FAILS)} check(s) FAILED:")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("all blast checks passed\n")
