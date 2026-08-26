"""Where a pitcher's stuff sits against the arms he is actually measured with.

Savant's percentile strip is the one thing on that page a coach reads without
being taught it -- "83rd" lands instantly where "2048 rpm" does not. This is
that idea, honestly scoped: the population is Moeller pitchers, and every
surface that shows these numbers says so.

Shared by two surfaces with different needs. A player sees one strip for the
pitch he throws most (his card is not a place to bury him in tabs); a coach
sees one per pitch type, because the question "is his slider any good" is a
different question from "is his fastball any good".
"""
from sqlalchemy import select

import db
import rapsodo_card

# Tracked pitches of a type before it is ranked, or counted in anyone else's
# ranking. Below this the average still moves with every rep, so a percentile
# reports attendance rather than stuff.
MIN_N = 10

# Enough same-level peers for "vs varsity" to mean something. Under it the
# comparison falls back to the whole staff and says which population it used.
MIN_PEERS = 6

# (key, label, unit, higher_is_better). Shape and arm strength only -- no
# command metric appears here, because bullpen location does not predict game
# command in this data (measured, r = -0.73) and a percentile built on it would
# tell a pitcher something untrue about himself.
STRIP = [
    ("velo", "Velocity", "mph", True),
    ("max", "Top velo", "mph", True),
    ("spin", "Spin rate", "rpm", True),
    ("eff", "Spin efficiency", "%", True),
    ("ivb", "Ride (IVB)", "in", True),
    ("run", "Run", "in", True),
]


def _pct(values, mine):
    """Percentile of `mine` within `values`, which includes him."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    below = sum(1 for v in vals if v < mine)
    ties = sum(1 for v in vals if v == mine) - 1          # not against himself
    return int(round(100.0 * (below + 0.5 * max(ties, 0)) / (len(vals) - 1)))


def _run(mix_row):
    """Arm-side run as a magnitude.

    A left-hander's horizontal break is negative by definition, so ranking the
    raw number sorts every lefty to the bottom of a column that has nothing to
    do with how good the pitch is.
    """
    hb = mix_row.get("hb")
    return None if hb is None else abs(hb)


def _one(cards, player_id, pitch, level):
    """Bars for a single pitch type, or None if he can't be ranked on it."""
    mine = next((m for m in cards[player_id].get("mix", [])
                 if m["pt"] == pitch), None)
    if not mine or mine["n"] < MIN_N:
        return None

    peers = []
    for pid, card in cards.items():
        row = next((m for m in card.get("mix", []) if m["pt"] == pitch), None)
        if row and row["n"] >= MIN_N:
            peers.append((card, row))

    same = [p for p in peers if level and p[0].get("level") == level]
    pool, pool_label = ((same, level) if len(same) >= MIN_PEERS
                        else (peers, "the staff"))

    bars = []
    for key, label, unit, higher_better in STRIP:
        mine_v = _run(mine) if key == "run" else mine.get(key)
        if mine_v is None:
            continue
        vals = [(_run(r) if key == "run" else r.get(key)) for _c, r in pool]
        pct = _pct(vals, mine_v)
        if pct is None:
            continue
        bars.append({"label": label, "value": mine_v, "unit": unit,
                     "pct": pct if higher_better else 100 - pct})
    if not bars:
        return None
    return {"pitch": pitch, "n": mine["n"], "bars": bars,
            "pool": pool_label, "pool_n": len(pool), "level": level}


def best_pitch(engine, player_id):
    """One strip, for the pitch he throws most.

    Not automatically the fastball: a reliever throwing 60% sliders should be
    measured on the slider.
    """
    cards = rapsodo_card.roster_cards(engine)
    mine = cards.get(player_id)
    if not mine or not mine.get("mix"):
        return None
    top = max(mine["mix"], key=lambda m: m["n"])
    got = _one(cards, player_id, top["pt"], mine.get("level"))
    if got:
        return dict(got, enough=True)
    # Say why it is empty rather than rendering nothing.
    return {"pitch": top["pt"], "n": top["n"], "enough": False, "min_n": MIN_N}


def by_pitch(engine, player_id):
    """One strip per pitch type he throws enough of -- the coach's view.

    'Is his slider any good' is a different question from 'is his fastball any
    good', and pooling them would answer neither.
    """
    cards = rapsodo_card.roster_cards(engine)
    mine = cards.get(player_id)
    if not mine or not mine.get("mix"):
        return []
    out = []
    for row in mine["mix"]:
        if row["pt"] == "UNK":      # unlabelled pitches rank nothing
            continue
        got = _one(cards, player_id, row["pt"], mine.get("level"))
        if got:
            out.append(got)
    # Most-thrown first: that is the pitch the conversation starts with.
    out.sort(key=lambda s: -s["n"])
    return out
