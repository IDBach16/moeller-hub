"""Player links: does the token do what a coach thinks it does?

Every check here is a promise made to a parent, not a nice-to-have: the link
opens one kid's data, it opens nobody else's, and it stops working when a coach
turns it off.
"""
import os
import sys

os.environ.setdefault("DATABASE_URL", "")
os.environ.pop("RAILWAY_ENVIRONMENT", None)

import db                                                   # noqa: E402
import playerlink                                           # noqa: E402
from sqlalchemy import select                               # noqa: E402

FAILED = []


def ok(label, cond, detail=""):
    print("  [%s] %s%s" % ("ok  " if cond else "FAIL", label,
                           "" if cond else "  -- " + str(detail)))
    if not cond:
        FAILED.append(label)


def main():
    engine = db.get_engine()
    with engine.connect() as conn:
        ids = [r.id for r in conn.execute(
            select(db.players.c.id).order_by(db.players.c.id).limit(2))]
    if len(ids) < 2:
        print("need two players seeded; run seed first")
        return 1
    a, b = ids

    print("tokens")
    t = playerlink.issue(engine, a)
    ok("issuing returns a token", bool(t) and len(t) >= 20, t)
    ok("issuing twice keeps the SAME link (his phone keeps working)",
       playerlink.issue(engine, a) == t)
    # Not "contains no digit of the id" -- a random string hits that by chance.
    # What matters is that it is long, random, and carries nothing derivable.
    tb = playerlink.issue(engine, b)
    ok("a token is long enough to be unguessable", len(t) >= 30, len(t))
    ok("two players get unrelated tokens", tb != t and tb[:8] != t[:8])
    ok("the token is not derived from the player", t != str(a) and t != str(b))
    playerlink.revoke(engine, b)

    print("resolution")
    ok("token resolves to its own player", playerlink.resolve(engine, t) == a)
    ok("token does NOT resolve to anyone else",
       playerlink.resolve(engine, t) != b)
    ok("garbage resolves to nobody", playerlink.resolve(engine, "nope") is None)
    ok("empty resolves to nobody", playerlink.resolve(engine, "") is None)
    ok("an over-long token is refused before touching the db",
       playerlink.resolve(engine, "x" * 200) is None)

    print("visits")
    st = playerlink.link_status(engine, a)
    ok("a coach can see it was opened", st and st["views"] >= 1, st)

    print("revocation")
    playerlink.revoke(engine, a)
    ok("a revoked token stops resolving", playerlink.resolve(engine, t) is None)
    ok("a revoked link is gone from the coach view",
       playerlink.link_status(engine, a) is None)
    t2 = playerlink.issue(engine, a)
    ok("re-issuing after revoke mints a NEW token", t2 != t)
    ok("the old one stays dead", playerlink.resolve(engine, t) is None)

    print("the card")
    c = playerlink.card(engine, t2)
    ok("card resolves for a live token", c is not None)
    ok("card is for the right player", c and c["name"], c)
    ok("dead token yields no card", playerlink.card(engine, t) is None)
    if c:
        ok("card carries no roster and no other players",
           not any(k in c for k in ("roster", "players", "team")), list(c))
        ok("baseline gate matches session count",
           c["baseline_ready"] == (c["sessions_n"] >= 3))

    print("the http surface")
    import app as app_mod
    client = app_mod.create_app().test_client()
    r = client.get("/me/" + t2)
    ok("GET /me/<token> renders", r.status_code == 200, r.status_code)
    body = r.get_data(as_text=True)
    ok("the page names him", c and c["name"].split()[0] in body)
    ok("the page has no link back into the hub",
       'href="/players' not in body and 'href="/team' not in body)
    # Any percentile on this page must name the population it came from. A
    # bare "88th" reads as 88th in high school baseball, which it is not.
    # Checked structurally rather than by heading text, because an empty-state
    # block can carry the same heading with no percentile under it.
    def labelled(html):
        if 'class="track"' not in html:              # no bars drawn at all
            return True
        return "Moeller" in html and (
            "not against high school" in html or "college, or pro" in html)

    ok("every percentile shown names its population", labelled(body))

    # The check above is vacuous on a player with no bars, so run it again on
    # someone who actually has some.
    with engine.connect() as conn:
        everyone = [r.id for r in conn.execute(select(db.players.c.id))]
    for pid in everyone:
        tok = playerlink.issue(engine, pid)
        page = client.get("/me/" + tok).get_data(as_text=True)
        playerlink.revoke(engine, pid)
        if 'class="track"' in page:
            ok("a player WITH percentiles names the population too",
               labelled(page))
            ok("his bars carry an ordinal, not a bare number",
               "th</span>" in page or "st</span>" in page
               or "nd</span>" in page or "rd</span>" in page)
            break
    else:
        print("  [skip] nobody in this database renders a percentile bar")
    r2 = client.get("/me/" + t)
    ok("a revoked token 404s", r2.status_code == 404, r2.status_code)
    ok("the 404 does not say whether the token ever existed",
       "isn&#39;t active" in r2.get_data(as_text=True)
       or "isn't active" in r2.get_data(as_text=True))

    # A player WITH a goal renders. The first cut of this page treated
    # goal.progress as a bare percentage when it is a dict, and every card
    # belonging to a player who had a goal returned 500 -- invisible until a
    # goal-bearing player was opened, because the fixtures above have none.
    with engine.connect() as conn:
        g = conn.execute(select(db.goals.c.player_id, db.goals.c.title)
                         .where(db.goals.c.status == "active")).first()
    if g:
        gt = playerlink.issue(engine, g.player_id)
        rg = client.get("/me/" + gt)
        ok("a player with an active goal renders", rg.status_code == 200,
           rg.status_code)
        ok("his goal is on the page", g.title[:18] in rg.get_data(as_text=True))
        playerlink.revoke(engine, g.player_id)
    else:
        print("  [skip] no active goal in this database to render")

    # A neutral-polarity change must not be painted as bad news. favorable is
    # None for release side and horizontal break -- the system knows the number
    # moved, not whether that is good -- and the first cut rendered every
    # not-True value red, which tells a kid his delivery broke.
    with engine.connect() as conn:
        neu = conn.execute(select(db.change_events.c.player_id)
                           .where(db.change_events.c.favorable.is_(None))).first()
    if neu:
        nt = playerlink.issue(engine, neu.player_id)
        bn = client.get("/me/" + nt).get_data(as_text=True)
        ok("a neutral change is not painted red", 'class="h down"' not in bn)
        ok("a neutral change says it is not a verdict",
           "not a verdict" in bn)
        playerlink.revoke(engine, neu.player_id)
    else:
        print("  [skip] no neutral-polarity change in this database")

    playerlink.revoke(engine, a)                            # leave it clean

    print()
    if FAILED:
        print("FAILED: " + ", ".join(FAILED))
        return 1
    print("all player-link checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
