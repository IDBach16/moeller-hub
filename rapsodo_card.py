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
             "px": _r(p.get("px")), "pz": _r(p.get("pz")), "date": p["date"]}
            for p in plist
        ],
        "trend": trend,
    }
