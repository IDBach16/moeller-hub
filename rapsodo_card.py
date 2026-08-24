"""
rapsodo_card.py -- the pitcher's Rapsodo report card.

Replaces the wall-of-text training history for pitchers. A coach between bullpens
reads shapes, not a semicolon-separated list of eleven metrics, so this returns
plot-ready per-pitch rows alongside the arsenal table.

Everything here reads the long-format pitch_metrics table and pivots back to one
row per pitch. The database does the aggregation; the template only draws.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select

import db
import metrics

# Long-format metric keys -> the short names the card template uses.
PLOT_KEYS = {
    "velocity": "velo",
    "spin_rate": "spin",
    "induced_vertical_break": "ivb",
    "horizontal_break": "hb",
    "spin_efficiency": "eff",
    "release_height": "rel_h",
    "release_side": "rel_s",
    "plate_side": "px",      # inches, catcher's view, 0 = middle of the plate
    "plate_height": "pz",    # inches above the ground
    "is_strike": "strike",   # the unit's own zone call, 1/0
}

# Usage order on the card: the fastball first, then whatever he throws most.
PITCH_ORDER = ["FB", "SI", "CT", "SL", "CB", "CH", "SP"]


def _r(v, digits=1):
    if v is None:
        return None
    return round(v, digits) if digits else int(round(v))


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def _circular_mean(degs):
    """Mean of angles in degrees -- a naive average of 355 and 5 is 180."""
    import math
    if not degs:
        return None
    x = sum(math.cos(math.radians(d)) for d in degs)
    y = sum(math.sin(math.radians(d)) for d in degs)
    if x == 0 and y == 0:
        return None
    return math.degrees(math.atan2(y, x)) % 360


def _slot_from_axis(axis_mean):
    """Arm angle above horizontal, inferred from fastball spin axis (0 = 12:00).
    Same method as the Rapsodo dashboard: tilt tracks the arm and, unlike
    release position, doesn't care where the kid stands on the rubber."""
    if axis_mean is None:
        return None
    dev = min(axis_mean, 360.0 - axis_mean)
    return round(90.0 - dev, 1)


def _slot_name(angle):
    if angle is None:
        return None
    if angle >= 70:
        return "over the top"
    if angle >= 45:
        return "high three-quarters"
    if angle >= 20:
        return "three-quarters"
    if angle >= 0:
        return "low three-quarters"
    return "sidearm"


def roster_cards(engine):
    """Per-pitcher visual summary for the players grid: fastball velo, pitch-mix
    segments, per-pitch movement means for a thumbnail, and arm slot.

    One pass over pitch_metrics for the whole roster -- the grid shows 69 cards
    and must not run 69 queries.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            select(db.pitch_metrics.c.player_id, db.pitch_metrics.c.pitch_type,
                   db.pitch_metrics.c.metric_key, db.pitch_metrics.c.value)
            .select_from(db.pitch_metrics.join(
                db.sessions, db.sessions.c.id == db.pitch_metrics.c.session_id))
            .where(db.sessions.c.source == "rapsodo")
            .where(db.pitch_metrics.c.metric_key.in_(
                ["velocity", "induced_vertical_break",
                 "horizontal_break", "spin_axis", "is_strike"]))).all()
        levels = {r.player_id: r.level for r in conn.execute(
            select(db.player_seasons.c.player_id, db.player_seasons.c.level))}

    acc: dict[tuple, dict] = {}
    for r in rows:
        if not r.pitch_type:
            continue
        acc.setdefault((r.player_id, r.pitch_type), {}) \
           .setdefault(r.metric_key, []).append(r.value)

    out: dict[int, dict] = {}
    for (pid, pt), d in acc.items():
        velos = d.get("velocity", [])
        if len(velos) < 3:          # one mis-tagged pitch shouldn't paint a card
            continue
        card = out.setdefault(pid, {"level": levels.get(pid), "total": 0,
                                    "mix": [], "fb": None, "fb_max": None,
                                    "slot": None, "slot_name": None,
                                    "_strikes": 0, "_zone_n": 0})
        card["total"] += len(velos)
        # The device's own zone call, stored 1/0 -- a location check, not a
        # game strike rate, and the card labels it "zone" for that reason.
        zone = d.get("is_strike", [])
        card["_strikes"] += int(sum(zone))
        card["_zone_n"] += len(zone)
        card["mix"].append({
            "pt": pt, "n": len(velos),
            "velo": round(sum(velos) / len(velos), 1),
            "ivb": round(sum(d["induced_vertical_break"]) / len(d["induced_vertical_break"]), 1)
                   if d.get("induced_vertical_break") else None,
            "hb": round(sum(d["horizontal_break"]) / len(d["horizontal_break"]), 1)
                  if d.get("horizontal_break") else None,
        })
        if pt == "FB":
            card["fb"] = round(sum(velos) / len(velos), 1)
            card["fb_max"] = round(max(velos), 1)
            axes = d.get("spin_axis", [])
            if len(axes) >= 10:
                card["slot"] = _slot_from_axis(_circular_mean(axes))
                card["slot_name"] = _slot_name(card["slot"])

    order = {p: i for i, p in enumerate(PITCH_ORDER)}
    for card in out.values():
        card["mix"].sort(key=lambda m: order.get(m["pt"], 99))
        for m in card["mix"]:
            m["pct"] = round(100.0 * m["n"] / card["total"], 1)
        if card["_zone_n"]:
            card["strike_pct"] = int(round(100.0 * card["_strikes"] / card["_zone_n"]))
            card["ball_pct"] = 100 - card["strike_pct"]
        del card["_strikes"], card["_zone_n"]
    return out


def card(engine, player_id: int) -> dict:
    """Plot-ready Rapsodo summary for one pitcher, or {'has_data': False}."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                db.pitch_metrics.c.session_id,
                db.pitch_metrics.c.seq,
                db.pitch_metrics.c.pitch_type,
                db.pitch_metrics.c.metric_key,
                db.pitch_metrics.c.value,
                db.sessions.c.session_date,
            )
            .select_from(
                db.pitch_metrics.join(
                    db.sessions, db.sessions.c.id == db.pitch_metrics.c.session_id
                )
            )
            .where(db.pitch_metrics.c.player_id == player_id)
            .where(db.sessions.c.source == "rapsodo")
        ).fetchall()

    if not rows:
        return {"has_data": False}

    # Long rows -> one dict per pitch.
    pitches: dict[tuple, dict] = defaultdict(dict)
    for r in rows:
        key = (r.session_id, r.seq)
        p = pitches[key]
        p["pt"] = r.pitch_type or "UNK"
        p["date"] = str(r.session_date)
        p["session_id"] = r.session_id
        short = PLOT_KEYS.get(r.metric_key)
        if short:
            p[short] = r.value

    plist = [p for p in pitches.values() if p.get("velo") is not None]
    if not plist:
        return {"has_data": False}

    # -- arsenal table ------------------------------------------------------
    by_pt: dict[str, list] = defaultdict(list)
    for p in plist:
        by_pt[p["pt"]].append(p)

    total = len(plist)
    arsenal = []
    for pt, group in by_pt.items():
        velos = [g["velo"] for g in group if g.get("velo") is not None]
        arsenal.append({
            "pt": pt,
            "label": metrics.PITCH_TYPE_LABELS.get(pt, pt),
            "n": len(group),
            "usage": round(100.0 * len(group) / total, 1),
            "velo": _mean(velos),
            "max": round(max(velos), 1) if velos else None,
            "spin": _mean([g.get("spin") for g in group]),
            "ivb": _mean([g.get("ivb") for g in group]),
            "hb": _mean([g.get("hb") for g in group]),
            "eff": _mean([g.get("eff") for g in group]),
            "rel_h": _mean([g.get("rel_h") for g in group]),
            "rel_s": _mean([g.get("rel_s") for g in group]),
        })
    arsenal.sort(key=lambda a: (PITCH_ORDER.index(a["pt"]) if a["pt"] in PITCH_ORDER else 99,
                                -a["n"]))

    # -- velocity by session, per pitch type --------------------------------
    per_session: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for p in plist:
        per_session[p["date"]][p["pt"]].append(p["velo"])

    trend = []
    for dt in sorted(per_session):
        entry = {"date": dt, "n": sum(len(v) for v in per_session[dt].values()), "by_pt": {}}
        for pt, velos in per_session[dt].items():
            # A pitch type only trends if there are enough of it that session --
            # a single mis-tagged pitch shouldn't draw a line.
            if len(velos) >= 3:
                entry["by_pt"][pt] = {"velo": _mean(velos), "n": len(velos)}
        trend.append(entry)

    dates = sorted({p["date"] for p in plist})
    return {
        "has_data": True,
        "n_pitches": total,
        "n_sessions": len({p["session_id"] for p in plist}),
        "span": [dates[0], dates[-1]],
        "arsenal": arsenal,
        # Only what the plots need, so the page doesn't ship the whole table.
        "pitches": [
            # Rounded here, not in the template: these land in tooltips a coach
            # reads, and raw floats ("75.18448396704001 mph") are unreadable. It
            # also keeps the embedded JSON small.
            {"pt": p["pt"], "velo": _r(p.get("velo")), "spin": _r(p.get("spin"), 0),
             "ivb": _r(p.get("ivb")), "hb": _r(p.get("hb")),
             "rel_h": _r(p.get("rel_h"), 2), "rel_s": _r(p.get("rel_s"), 2),
             "px": _r(p.get("px")), "pz": _r(p.get("pz")),
             "strike": _r(p.get("strike"), 0), "eff": _r(p.get("eff"), 0),
             "date": p["date"]}
            for p in plist
        ],
        "trend": trend,
    }


def session_log(pitches):
    """The session log's rows: one entry per bullpen, one line per pitch type.

    Per pitch because pooled session averages are the mix-shift trap all over
    again -- a night of extra sliders drags the pooled IVB down with no pitch
    having changed. A type needs 3+ reps to get a line (one mis-tagged pitch
    is noise, not an offering); UNK never earns one.
    """
    by_date = defaultdict(list)
    for p in pitches:
        by_date[p["date"]].append(p)
    out = []
    for dt in sorted(by_date, reverse=True):
        g = by_date[dt]
        strikes = [x.get("strike") for x in g if x.get("strike") is not None]
        by_pt = defaultdict(list)
        for x in g:
            by_pt[x["pt"]].append(x)
        rows = []
        for pt, gg in by_pt.items():
            if pt == "UNK" or len(gg) < 3:
                continue
            velos = [x["velo"] for x in gg if x.get("velo") is not None]
            rows.append({
                "pt": pt, "label": metrics.PITCH_TYPE_LABELS.get(pt, pt),
                "n": len(gg),
                "velo": _mean(velos),
                "max": round(max(velos), 1) if velos else None,
                "ivb": _mean([x.get("ivb") for x in gg]),
                "hb": _mean([x.get("hb") for x in gg]),
                "spin": _r(_mean([x.get("spin") for x in gg]), 0),
                "eff": _r(_mean([x.get("eff") for x in gg]), 0),
            })
        rows.sort(key=lambda r: (PITCH_ORDER.index(r["pt"])
                                 if r["pt"] in PITCH_ORDER else 99, -r["n"]))
        out.append({
            "date": dt, "n": len(g),
            "zone_pct": _r(100.0 * sum(strikes) / len(strikes), 0) if strikes else None,
            "rows": rows,
        })
    return out
