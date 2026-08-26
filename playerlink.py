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
presentation, not of arithmetic. The maths lives in percentiles.py, shared with
the coach profile so the two surfaces can never drift apart.
"""
import secrets
from datetime import datetime

from sqlalchemy import select, update

import db
import percentiles


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

def strip(engine, player_id):
    """Percentile bars for this pitcher's most-thrown pitch. The maths is in
    percentiles.py so the coach profile and this card cannot disagree."""
    return percentiles.best_pitch(engine, player_id)


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
    is_p = p["player"]["is_pitcher"]

    # What he did in games. For a hitter this is his ONLY percentile -- there is
    # no bat-tracking data yet -- so without it half the roster opens this page
    # and finds nothing about themselves.
    awre_name = (p.get("aliases") or {}).get("awre") or p["player"]["name"]
    side = "pitching" if is_p else "hitting"
    game = percentiles.game_strip(awre_name, side)
    game_bat = percentiles.game_strip(awre_name, "hitting") if is_p else None
    # Only the pitches he has a real read on -- a player's card is not the
    # place for four "not enough sample" notes in a row.
    by_pitch = [s for s in percentiles.game_by_pitch(awre_name, side)
                if s["bars"]]
    by_pitch_bat = ([s for s in percentiles.game_by_pitch(awre_name, "hitting")
                     if s["bars"]] if is_p else [])

    # His own words-level answers, in the order he cares about them.
    return {
        "name": p["player"]["name"],
        "class_year": p["player"]["class_year"],
        "pos": p["player"]["pos"],
        "is_pitcher": is_p,
        "sessions_n": len(sessions),
        "last_session": sessions[0] if sessions else None,
        "strip": strip(engine, player_id) if is_p else None,
        "game": game,
        "game_bat": game_bat,
        "by_pitch": by_pitch,
        "by_pitch_bat": by_pitch_bat,
        # Only what cleared detection, and only against his OWN baseline.
        "changes": p.get("changes") or [],
        "goals": p.get("goals") or [],
        # Three sessions is the floor for a baseline; below it the card says so
        # instead of showing him a comparison that cannot mean anything yet.
        "baseline_ready": len(sessions) >= 3,
    }
