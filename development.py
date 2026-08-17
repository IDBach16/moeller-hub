"""
development.py -- creating and updating goals and interventions.
See PLAYER_DEV_SPEC.md sections 5.5 and 8.3.

The roadmap's requirement is that every meaningful development change has a
date, a player, a coach, a goal, the intervention itself, and a review date.
This module is what enforces that: validation lives here rather than in the
route handlers, so the API and any future CLI or import path can't diverge.

Two things worth knowing:

  * Creating a measurable goal SNAPSHOTS the player's current value into
    `start_value`. Without it, "he's 60% of the way there" has no denominator,
    and a progress bar that invents one is worse than no bar at all.

  * Nothing here computes whether an intervention worked. changes.py owns that,
    because the same windowing and the same four gates have to apply.
"""

from datetime import date, datetime

from sqlalchemy import delete, insert, select, update

import db
import metrics
import profiles


class DevError(Exception):
    pass


def _parse_date(v, field, required=False):
    if v in (None, ""):
        if required:
            raise DevError(f"{field} is required")
        return None
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        raise DevError(f"{field} must look like YYYY-MM-DD")


def _player_or_die(conn, player_id):
    row = conn.execute(select(db.players.c.id)
                       .where(db.players.c.id == player_id)).first()
    if not row:
        raise DevError(f"no player #{player_id}")
    return row.id


def _current_value(engine, player_id, metric_key):
    """Where the player is right now on one metric, for the start snapshot."""
    if not metric_key:
        return None
    m = metrics.get(metric_key)
    side = "hitting" if (m and m.side == "hitting") else "pitching"
    with engine.connect() as conn:
        training, _keys = profiles._training(conn, player_id, side)
    latest = profiles.latest_by_metric(training)
    hit = latest.get(metric_key)
    return hit["value"] if hit else None


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------

def create_goal(engine, player_id, title, metric_key=None, direction=None,
                target_value=None, detail=None, set_by=None, set_on=None,
                review_on=None):
    title = (title or "").strip()
    if not title:
        raise DevError("a goal needs a title")

    if metric_key:
        if not metrics.known(metric_key):
            raise DevError(f"'{metric_key}' is not a known metric")
        if direction not in db.GOAL_DIRECTIONS:
            raise DevError("a measurable goal needs a direction: "
                           + ", ".join(db.GOAL_DIRECTIONS))
        if direction in ("increase", "decrease") and target_value is None:
            raise DevError("an increase/decrease goal needs a target value")
        if target_value is not None:
            try:
                target_value = float(target_value)
            except (TypeError, ValueError):
                raise DevError("the target value must be a number")
    else:
        # A narrative goal is legitimate -- not everything worth tracking is a
        # number -- but it must not pretend to be measurable.
        direction, target_value = None, None

    start_value = _current_value(engine, player_id, metric_key)
    set_on = _parse_date(set_on, "set_on") or date.today()
    review_on = _parse_date(review_on, "review_on")
    if review_on and review_on < set_on:
        raise DevError("the review date can't be before the date the goal was set")

    with engine.begin() as conn:
        _player_or_die(conn, player_id)
        return conn.execute(insert(db.goals).values(
            player_id=player_id, title=title, metric_key=metric_key or None,
            direction=direction, target_value=target_value,
            start_value=start_value, detail=(detail or None),
            set_by=(set_by or None), set_on=set_on, review_on=review_on,
            status="active")).inserted_primary_key[0]


def update_goal(engine, goal_id, status=None, review_on=None, detail=None,
                target_value=None):
    values = {}
    if status is not None:
        if status not in ("active", "met", "abandoned", "superseded"):
            raise DevError(f"'{status}' is not a valid goal status")
        values["status"] = status
    if review_on is not None:
        values["review_on"] = _parse_date(review_on, "review_on")
    if detail is not None:
        values["detail"] = detail or None
    if target_value is not None:
        try:
            values["target_value"] = float(target_value)
        except (TypeError, ValueError):
            raise DevError("the target value must be a number")
    if not values:
        raise DevError("nothing to update")
    with engine.begin() as conn:
        row = conn.execute(select(db.goals.c.id)
                           .where(db.goals.c.id == goal_id)).first()
        if not row:
            raise DevError(f"no goal #{goal_id}")
        conn.execute(update(db.goals).where(db.goals.c.id == goal_id).values(**values))
    return values


def delete_goal(engine, goal_id):
    """For a genuine mistake. Interventions that referenced it keep their own
    record and simply lose the link -- deleting a goal must not delete history."""
    with engine.begin() as conn:
        conn.execute(update(db.interventions)
                     .where(db.interventions.c.goal_id == goal_id)
                     .values(goal_id=None))
        conn.execute(delete(db.goals).where(db.goals.c.id == goal_id))


# ---------------------------------------------------------------------------
# Interventions
# ---------------------------------------------------------------------------

def create_intervention(engine, player_id, title, intervention_date=None,
                        category=None, detail=None, coach=None, goal_id=None,
                        review_on=None):
    title = (title or "").strip()
    if not title:
        raise DevError("an intervention needs a title")
    if category and category not in db.INTERVENTION_CATEGORIES:
        raise DevError(f"'{category}' is not a known category")

    when = _parse_date(intervention_date, "intervention_date") or date.today()
    review_on = _parse_date(review_on, "review_on")
    if review_on and review_on < when:
        raise DevError("the review date can't be before the intervention")

    with engine.begin() as conn:
        _player_or_die(conn, player_id)
        if goal_id:
            g = conn.execute(select(db.goals.c.player_id)
                             .where(db.goals.c.id == goal_id)).first()
            if not g:
                raise DevError(f"no goal #{goal_id}")
            if g.player_id != player_id:
                raise DevError("that goal belongs to a different player")
        return conn.execute(insert(db.interventions).values(
            player_id=player_id, intervention_date=when,
            category=(category or None), title=title, detail=(detail or None),
            coach=(coach or None), goal_id=goal_id or None,
            review_on=review_on, outcome="pending")).inserted_primary_key[0]


def update_intervention(engine, intervention_id, outcome=None, review_on=None,
                        detail=None):
    values = {}
    if outcome is not None:
        if outcome not in ("pending", "working", "no_change", "reverted"):
            raise DevError(f"'{outcome}' is not a valid outcome")
        values["outcome"] = outcome
    if review_on is not None:
        values["review_on"] = _parse_date(review_on, "review_on")
    if detail is not None:
        values["detail"] = detail or None
    if not values:
        raise DevError("nothing to update")
    with engine.begin() as conn:
        row = conn.execute(select(db.interventions.c.id)
                           .where(db.interventions.c.id == intervention_id)).first()
        if not row:
            raise DevError(f"no intervention #{intervention_id}")
        conn.execute(update(db.interventions)
                     .where(db.interventions.c.id == intervention_id).values(**values))
    return values


def delete_intervention(engine, intervention_id):
    with engine.begin() as conn:
        conn.execute(delete(db.interventions)
                     .where(db.interventions.c.id == intervention_id))


# ---------------------------------------------------------------------------
# Review queue -- what the home page's "needs review" panel reads
# ---------------------------------------------------------------------------

def due_for_review(engine, on=None):
    """Goals and interventions whose review date has passed."""
    on = on or date.today()
    with engine.connect() as conn:
        goals = conn.execute(
            select(db.goals.c.id, db.goals.c.title, db.goals.c.review_on,
                   db.players.c.slug, db.players.c.first_name, db.players.c.last_name)
            .select_from(db.goals.join(db.players,
                                       db.goals.c.player_id == db.players.c.id))
            .where((db.goals.c.status == "active") &
                   (db.goals.c.review_on.isnot(None)) &
                   (db.goals.c.review_on <= on))
            .order_by(db.goals.c.review_on)).all()
        ivs = conn.execute(
            select(db.interventions.c.id, db.interventions.c.title,
                   db.interventions.c.review_on, db.players.c.slug,
                   db.players.c.first_name, db.players.c.last_name)
            .select_from(db.interventions.join(
                db.players, db.interventions.c.player_id == db.players.c.id))
            .where((db.interventions.c.outcome == "pending") &
                   (db.interventions.c.review_on.isnot(None)) &
                   (db.interventions.c.review_on <= on))
            .order_by(db.interventions.c.review_on)).all()
    return {
        "goals": [{"id": r.id, "title": r.title, "review_on": str(r.review_on),
                   "player": f"{r.first_name} {r.last_name}", "slug": r.slug}
                  for r in goals],
        "interventions": [{"id": r.id, "title": r.title,
                           "review_on": str(r.review_on),
                           "player": f"{r.first_name} {r.last_name}", "slug": r.slug}
                          for r in ivs],
    }


def goal_metric_options(side=None):
    """Metrics a goal can be set on, for the form's dropdown."""
    out = []
    for m in sorted(metrics.REGISTRY.values(), key=lambda m: (m.side, m.label)):
        if side and m.side != side:
            continue
        out.append({"key": m.key, "label": f"{m.label} ({m.unit})",
                    "group": m.side.title(), "polarity": m.polarity,
                    "suggested_direction": (
                        "increase" if m.polarity == metrics.HIGHER_BETTER else
                        "decrease" if m.polarity == metrics.LOWER_BETTER else
                        "target_band" if m.polarity == metrics.TARGET_BAND else
                        "increase")})
    return out
