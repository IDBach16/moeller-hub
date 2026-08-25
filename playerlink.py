"""A player's personal link: his own data, on his own phone, and nothing else.

Everything else in this hub is built for a coach looking across the roster.
This is the one view built for one kid looking at himself, so it answers the
three questions he actually has -- where do I rank, what has changed, what am I
working on -- and deliberately cannot reach anyone else's page.

The token is a bearer credential. Whoever holds the link can read that player's
card, so it is issued per player, revocable, and never guessable. It is stored
rather than signed into the URL for exactly that reason: a signed token cannot
be taken back, and a coach cannot see whether it was ever opened.

Percentiles here are against MOELLER ARMS ONLY. That is the honest population
we have, and the card says so in as many words -- a percentile that reads like
Savant's but is computed off forty high-school pitchers would be a lie of
presentation, not of arithmetic.
"""
import secrets
from datetime import datetime

from sqlalchemy import select, update

import db
import rapsodo_card

# A pitcher needs this many tracked pitches of a type before he is ranked on it,
# or put into anyone else's ranking. Below it the average is still moving around
# with each rep and a percentile says more about attendance than about stuff.
MIN_N = 10

# Enough same-level peers to make "vs varsity" mean something; under it the card
# falls back to the whole staff and says which population it used.
MIN_PEERS = 6

# What a pitcher sees on his strip, in this order. Each is (key, label, unit,
# higher_is_better). Every one of these is shape or arm strength -- nothing here
# is a command metric, because the bullpen unit's location data does not predict
# game command (measured, r = -0.73) and putting it on a kid's card would tell
# him something untrue about himself.
STRIP = [
    ("velo", "Velocity", "mph", True),
    ("max", "Top velo", "mph", True),
    ("spin", "Spin rate", "rpm", True),
    ("eff", "Spin efficiency", "%", True),
    ("ivb", "Ride (IVB)", "in", True),
    ("run", "Run", "in", True),
]


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def issue(engine, player_id):
    """Return this player's active link, creating one only if he has none.

    Reissuing on every call would silently break the link already saved on a
    kid's phone, so an existing token is handed back unchanged.
    """
    existing = for_player(engine, player_id)
    if existing:
        return existing
    token = secrets.token_urlsafe(24)
    with engine.begin() as conn:
        conn.execute(db.player_links.insert().values(
            token=token, player_id=int(player_id)))
    return token


def for_player(engine, player_id):
    with engine.connect() as conn:
        row = conn.execute(
            select(db.player_links.c.token)
            .where(db.player_links.c.player_id == int(player_id))
            .where(db.player_links.c.revoked_at.is_(None))
            .order_by(db.player_links.c.created_at.desc())).first()
    return row.token if row else None


def revoke(engine, player_id):
    """Kill every live link for this player. Used when a phone is lost, a kid
    leaves the program, or a link ends up somewhere it shouldn't."""
    with engine.begin() as conn:
        res = conn.execute(
            update(db.player_links)
            .where(db.player_links.c.player_id == int(player_id))
            .where(db.player_links.c.revoked_at.is_(None))
            .values(revoked_at=datetime.utcnow()))
    return res.rowcount


def resolve(engine, token):
    """Token -> player_id, or None. Records the visit so a coach can see
    whether the kid ever opened it."""
    if not token or len(token) > 48:
        return None
    with engine.connect() as conn:
        row = conn.execute(
            select(db.player_links.c.player_id, db.player_links.c.views)
            .where(db.player_links.c.token == token)
            .where(db.player_links.c.revoked_at.is_(None))).first()
    if not row:
        return None
    try:
        with engine.begin() as conn:
            conn.execute(update(db.player_links)
                         .where(db.player_links.c.token == token)
                         .values(last_seen_at=datetime.utcnow(),
                                 views=(row.views or 0) + 1))
    except Exception:                                       # noqa: BLE001
        pass                        # a stats bump must never block the page
    return row.player_id


def link_status(engine, player_id):
    """What the coach sees next to the link on the profile page."""
    with engine.connect() as conn:
        row = conn.execute(
            select(db.player_links)
            .where(db.player_links.c.player_id == int(player_id))
            .where(db.player_links.c.revoked_at.is_(None))
            .order_by(db.player_links.c.created_at.desc())).first()
    if not row:
        return None
    return {"token": row.token, "views": row.views or 0,
            "last_seen": row.last_seen_at.date().isoformat()
                         if row.last_seen_at else None}


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------

def _pct(values, mine):
    """Percentile of `mine` within `values` (which includes him)."""
    others = [v for v in values if v is not None]
    if len(others) < 2:
        return None
    below = sum(1 for v in others if v < mine)
    ties = sum(1 for v in others if v == mine) - 1        # exclude himself
    return int(round(100.0 * (below + 0.5 * max(ties, 0)) / (len(others) - 1)))


def _run(mix_row, throws):
    """Arm-side run as a magnitude.

    A lefty's horizontal break is negative by definition, so ranking on the raw
    number would sort every left-hander to the bottom of a column that has
    nothing to do with how good the pitch is.
    """
    hb = mix_row.get("hb")
    if hb is None:
        return None
    return abs(hb)


def strip(engine, player_id, throws_by_id=None):
    """Percentile bars for this pitcher's most-thrown pitch."""
    cards = rapsodo_card.roster_cards(engine)
    mine = cards.get(player_id)
    if not mine or not mine.get("mix"):
        return None

    with engine.connect() as conn:
        throws = {r.id: r.throws for r in conn.execute(
            select(db.players.c.id, db.players.c.throws))}

    # His bread and butter, not necessarily his fastball -- a reliever who
    # throws 60% sliders should be ranked on the slider.
    best = max(mine["mix"], key=lambda m: m["n"])
    if best["n"] < MIN_N:
        return {"pitch": best["pt"], "n": best["n"], "enough": False,
                "min_n": MIN_N}

    level = mine.get("level")
    peers = []
    for pid, card in cards.items():
        row = next((m for m in card.get("mix", []) if m["pt"] == best["pt"]), None)
        if row and row["n"] >= MIN_N:
            peers.append((pid, card, row))

    same = [p for p in peers if p[1].get("level") == level and level]
    pool, pool_label = ((same, level) if len(same) >= MIN_PEERS
                        else (peers, "the staff"))

    bars = []
    for key, label, unit, higher_better in STRIP:
        if key == "run":
            mine_v = _run(best, throws.get(player_id))
            vals = [_run(r, throws.get(pid)) for pid, _c, r in pool]
        else:
            mine_v = best.get(key)
            vals = [r.get(key) for _pid, _c, r in pool]
        if mine_v is None:
            continue
        vals = [v for v in vals if v is not None]
        pct = _pct(vals, mine_v)
        if pct is None:
            continue
        bars.append({"label": label, "value": mine_v, "unit": unit,
                     "pct": pct if higher_better else 100 - pct})

    return {"pitch": best["pt"], "n": best["n"], "enough": True,
            "bars": bars, "pool": pool_label, "pool_n": len(pool),
            "level": level}


def card(engine, token):
    """Everything the player-facing page needs, or None for a dead token."""
    import profiles

    player_id = resolve(engine, token)
    if player_id is None:
        return None

    with engine.connect() as conn:
        row = conn.execute(select(db.players)
                           .where(db.players.c.id == player_id)).first()
    if row is None:
        return None

    p = profiles.profile(engine, row.slug)
    if not p:
        return None

    sessions = p.get("training") or []
    # His own words-level answers, in the order he cares about them.
    return {
        "name": p["player"]["name"],
        "class_year": p["player"]["class_year"],
        "pos": p["player"]["pos"],
        "is_pitcher": p["player"]["is_pitcher"],
        "sessions_n": len(sessions),
        "last_session": sessions[0] if sessions else None,
        "strip": strip(engine, player_id) if p["player"]["is_pitcher"] else None,
        # Only what cleared detection, and only against his OWN baseline.
        "changes": p.get("changes") or [],
        "goals": p.get("goals") or [],
        # Three sessions is the floor for a baseline; below it the card says so
        # instead of showing him a comparison that cannot mean anything yet.
        "baseline_ready": len(sessions) >= 3,
    }
