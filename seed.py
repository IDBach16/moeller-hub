"""
seed.py -- first-run data for the player-development database.

Two jobs, both idempotent (safe to re-run):

  1. Seed `players` from the bundled AWRE season CSV (season_pitches.csv). Every
     Moeller batter and pitcher who appears in the most recent season becomes a
     Moeller Player ID, with bats/throws read off the pitch rows.

     NOT from the Charting App roster: as of 2026-08-17 that API returns exactly
     two players, both named "TEST TEST". Nobody has entered a real roster there.
     It is still consulted as a secondary source, with TEST rows skipped, so it
     starts contributing the moment a real roster is entered.

  2. Load the Blast Connect vendor IDs recovered from the 2024 R puller
     (2025/Moller Misc/Blast_data_moeller3.0.R). Names that match a seeded player
     get linked. Names that DON'T are written to `name_review` with a fuzzy
     suggestion rather than creating a player -- some are 2024 players who have
     graduated, and some are the same player spelled differently. Guessing either
     one into the roster is exactly what we designed against.

Usage:
    python seed.py                # seed roster + blast ids
    python seed.py --roster-only
    python seed.py --blast-only
    python seed.py --year 2026    # which season to seed the roster from
    python seed.py --status       # what's in the database now
    python seed.py --review       # show the open name-review queue
"""

import difflib
import os
import sys

import requests as rq
from sqlalchemy import func, insert, select, update

import db

CHARTING_BASE = "https://moeller-charting-production.up.railway.app"

# The season the roster is seeded from. 2026 is complete; bump this when 2027
# game data starts landing.
DEFAULT_SEASON = 2026


# ---------------------------------------------------------------------------
# Blast Connect player IDs, recovered from Blast_data_moeller3.0.R (2024).
# These are 2024-era IDs and need verifying against the current Blast roster --
# but they prove the mapping exists and give ingest something real to test on.
#
# NOTE: that R script also carries the Blast login and password in plaintext.
# When the Blast puller is ported to Python the credentials move to Railway env
# vars; they are deliberately not reproduced here.
# ---------------------------------------------------------------------------

BLAST_PLAYER_IDS = {
    "Charlie Valencic": 437961, "Alex Lott": 438737, "Noah Goettke": 438070,
    "Adam Holstein": 408367, "Will Schirmer": 437963, "Luke Pappano": 437968,
    "Logan Rosenberger": 437969, "Adam Maybury": 438071, "Cooper Ridley": 437959,
    "Griffin Booth": 437958, "Tyler Willenbrink": 438075, "Kayde Ridley": 438072,
    "Carter Christenson": 437960, "Connor Scoggins": 360763,
    "Gunnar Voellmecke": 356423, "Jake Bell": 442478, "Athan Bridges": 438147,
    "Teegan Cumberland": 296412, "Connor Cuozzo": 437967, "Matt Ponatoski": 437966,
    "Jackson Porta": 437962, "Donovan Glosser": 322227, "Camden Broadnax": 309062,
    "Brody Foltz": 287042, "Connor Maupin": 460131, "CJ Gilpin": 460519,
    "Zak Wittenauer": 460184, "Will Schlake": 460187, "Kadin Ward": 460186,
    "William Brenzel": 460516, "Thomas Zimmerman": 460188, "John Stallo": 460183,
    "Ronnie Allen": 460520, "Reggie Watson III": 457040, "Ricky Maschinot": 460518,
}


def split_name(full):
    """'Reggie Watson III' -> ('Reggie', 'Watson III'). First token is the first
    name; everything after it is the surname, suffixes included."""
    parts = str(full).strip().split()
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _existing_slugs(conn):
    return {r.slug for r in conn.execute(select(db.players.c.slug))}


def _mode(series):
    """Most common non-null value, or None."""
    s = series.dropna()
    return s.mode().iloc[0] if not s.empty and not s.mode().empty else None


def seed_roster_from_season(engine, year=DEFAULT_SEASON, dry_run=False):
    """Create a player row per Moeller batter/pitcher in the season pitch data.

    Bats and throws come off the pitch rows themselves ('Batter Hand' /
    'PitcherHand'), which is more reliable than any roster we currently hold.
    """
    import agent  # reuses the hub's already-cached season dataframe

    df = agent._season_df()
    d = df[df["Year"] == int(year)]
    if d.empty:
        print(f"  ! no rows for {year} in the season CSV")
        return 0

    bat = d[d["BatterTeam"] == "Moeller"]
    pit = d[d["PitcherTeam"] == "Moeller"]

    people = {}   # name -> dict
    for name, g in bat.groupby("Batter"):
        if not str(name).strip():
            continue
        people.setdefault(name, {})["bats"] = _mode(g["Batter Hand"])
    for name, g in pit.groupby("Pitcher"):
        if not str(name).strip():
            continue
        rec = people.setdefault(name, {})
        rec["throws"] = _mode(g["PitcherHand"])
        rec["is_pitcher"] = True

    added = 0
    with engine.begin() as conn:
        have = _existing_slugs(conn)
        for name, rec in sorted(people.items()):
            first, last = split_name(name)
            if not first:
                continue
            slug = db.slugify(first, last)
            if slug in have:
                continue
            if dry_run:
                print(f"  + would add {name} ({slug})")
                added += 1
                continue
            res = conn.execute(insert(db.players).values(
                slug=slug, first_name=first, last_name=last,
                bats=(rec.get("bats") or None),
                throws=(rec.get("throws") or None),
                is_pitcher=bool(rec.get("is_pitcher")),
                is_active=True,
            ))
            pid = res.inserted_primary_key[0]
            have.add(slug)
            added += 1
            # AWRE's exact spelling, so ingest from that source never matches on
            # a name again. These differ from Blast's spellings for real players
            # -- Conner/Connor Cuozzo, Teagan/Teegan Cumberland, Zac/Zak
            # Wittenauer -- which is the whole reason this table exists.
            conn.execute(insert(db.player_aliases).values(
                player_id=pid, source="awre", alias=name))
    return added


def seed_roster_from_charting(engine, dry_run=False):
    """Secondary source. Contributes nothing today (the roster is two TEST
    rows) but starts working the moment a real roster is entered there."""
    try:
        resp = rq.get(f"{CHARTING_BASE}/api/players", timeout=30)
        resp.raise_for_status()
        roster = resp.json()
    except Exception as e:
        print(f"  ! could not reach the Charting App roster: {e}")
        return 0

    added = 0
    with engine.begin() as conn:
        have = _existing_slugs(conn)
        known_names = {}
        for r in conn.execute(select(db.players.c.id, db.players.c.first_name,
                                     db.players.c.last_name)):
            known_names[f"{r.first_name} {r.last_name}".lower()] = r.id
        for p in roster:
            name = str(p.get("name") or "").strip()
            if not name or name.upper().startswith("TEST"):
                continue
            first, last = split_name(name)
            slug = db.slugify(first, last)
            pid = known_names.get(name.lower())
            if pid is None and slug not in have:
                if dry_run:
                    print(f"  + would add {name} ({slug})")
                    added += 1
                    continue
                res = conn.execute(insert(db.players).values(
                    slug=slug, first_name=first, last_name=last,
                    bats=(p.get("bats") or None), throws=(p.get("throws") or None),
                    is_pitcher=bool(p.get("is_pitcher")), is_active=True))
                pid = res.inserted_primary_key[0]
                have.add(slug)
                added += 1
                conn.execute(insert(db.player_aliases).values(
                    player_id=pid, source="charting", alias=name))
            if pid is not None and p.get("id") is not None:
                exists = conn.execute(select(db.player_vendor_ids.c.id).where(
                    (db.player_vendor_ids.c.vendor == "charting") &
                    (db.player_vendor_ids.c.vendor_id == str(p["id"])))).first()
                if not exists and not dry_run:
                    conn.execute(insert(db.player_vendor_ids).values(
                        player_id=pid, vendor="charting", vendor_id=str(p["id"])))
    return added


def seed_blast_vendor_ids(engine, dry_run=False):
    """Link Blast IDs to players by exact name. Unmatched names go to review."""
    linked = flagged = 0
    with engine.begin() as conn:
        # name -> player_id, over every alias we know plus the canonical spelling
        lookup = {}
        for r in conn.execute(select(db.players.c.id, db.players.c.first_name,
                                     db.players.c.last_name)):
            lookup[f"{r.first_name} {r.last_name}".lower()] = r.id
        for r in conn.execute(select(db.player_aliases.c.player_id,
                                     db.player_aliases.c.alias)):
            lookup.setdefault(str(r.alias).lower(), r.player_id)

        have = {r.vendor_id for r in conn.execute(
            select(db.player_vendor_ids.c.vendor_id).where(
                db.player_vendor_ids.c.vendor == "blast"))}
        open_review = {r.raw_name for r in conn.execute(
            select(db.name_review.c.raw_name).where(
                (db.name_review.c.vendor == "blast") &
                (db.name_review.c.status == "open")))}
        have_alias = {r.alias for r in conn.execute(
            select(db.player_aliases.c.alias).where(
                db.player_aliases.c.source == "blast"))}

        for name, blast_id in BLAST_PLAYER_IDS.items():
            if str(blast_id) in have:
                continue
            pid = lookup.get(name.lower())
            if pid:
                if dry_run:
                    print(f"  + would link {name} -> blast {blast_id}")
                else:
                    conn.execute(insert(db.player_vendor_ids).values(
                        player_id=pid, vendor="blast", vendor_id=str(blast_id)))
                    if name not in have_alias:
                        conn.execute(insert(db.player_aliases).values(
                            player_id=pid, source="blast", alias=name))
                        have_alias.add(name)
                linked += 1
            else:
                # Either a graduated 2024 player, or the same player spelled
                # differently by Blast than by AWRE. We suggest, we don't decide.
                if name in open_review:
                    continue
                suggestion, score = _closest(name, lookup)
                if dry_run:
                    hint = f" -> maybe {suggestion} ({score:.2f})" if suggestion else ""
                    print(f"  ? would flag {name} (blast {blast_id}){hint}")
                else:
                    conn.execute(insert(db.name_review).values(
                        vendor="blast", raw_name=name, status="open",
                        suggested_player_id=(lookup.get(suggestion) if suggestion else None),
                        suggestion_score=score))
                flagged += 1
    return linked, flagged


def _closest(name, lookup):
    """Best fuzzy match for `name` among known player names, with its score.

    This is what catches Blast's 'Teegan Cumberland' against AWRE's 'Teagan
    Cumberland'. The match is only ever a SUGGESTION -- a human accepts it on
    the /collect review queue before any data is attributed.
    """
    candidates = list(lookup.keys())
    hits = difflib.get_close_matches(name.lower(), candidates, n=1, cutoff=0.75)
    if not hits:
        return None, 0.0
    score = difflib.SequenceMatcher(None, name.lower(), hits[0]).ratio()
    return hits[0], round(score, 3)


def show_review(engine):
    """The open name-review queue, worst-to-best suggestion."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(db.name_review.c.vendor, db.name_review.c.raw_name,
                   db.name_review.c.suggestion_score,
                   db.players.c.first_name, db.players.c.last_name)
            .select_from(db.name_review.outerjoin(
                db.players, db.name_review.c.suggested_player_id == db.players.c.id))
            .where(db.name_review.c.status == "open")
            .order_by(db.name_review.c.suggestion_score.desc())).all()
    if not rows:
        print("\n  name-review queue is empty\n")
        return
    print(f"\n  {len(rows)} name(s) awaiting review:\n")
    for r in rows:
        if r.first_name:
            print(f"    {r.vendor:<8} {r.raw_name:<24} -> "
                  f"{r.first_name} {r.last_name}  ({r.suggestion_score:.2f})")
        else:
            print(f"    {r.vendor:<8} {r.raw_name:<24}    (no match -- likely graduated)")
    print()


def seed_blast_column_maps(engine):
    """Pre-seed the Blast header mapping -- the one vendor whose schema we know."""
    import metrics
    added = 0
    with engine.begin() as conn:
        have = {r.source_column for r in conn.execute(
            select(db.column_maps.c.source_column).where(
                db.column_maps.c.vendor == "blast"))}
        for col, (key, unit) in metrics.BLAST_COLUMNS.items():
            if col in have:
                continue
            conn.execute(insert(db.column_maps).values(
                vendor="blast", source_column=col, metric_key=key,
                unit=unit, scale=1.0, confirmed_by="seed",
                confirmed_at=func.now()))
            added += 1
    return added


def status(engine):
    tables = [("players", db.players), ("player_aliases", db.player_aliases),
              ("player_vendor_ids", db.player_vendor_ids),
              ("column_maps", db.column_maps), ("name_review", db.name_review),
              ("sessions", db.sessions), ("swings", db.swings),
              ("pitch_metrics", db.pitch_metrics), ("goals", db.goals),
              ("interventions", db.interventions),
              ("change_events", db.change_events)]
    print(f"\n  database: {db.database_url()}")
    with engine.connect() as conn:
        for name, table in tables:
            n = conn.execute(select(func.count()).select_from(table)).scalar()
            print(f"    {name:<20} {n}")
    print()


# ---------------------------------------------------------------------------
# Seed-on-startup
# ---------------------------------------------------------------------------
#
# Without this, a fresh deploy comes up with an empty roster and every
# player-centred page is a dead end until someone remembers to run this file by
# hand. So the app seeds itself once, on boot, when the database is empty.
#
# Three deliberate constraints:
#
#   * It only runs when `players` is EMPTY. After that it is one cheap COUNT
#     and an immediate return, so restarts cost nothing and nothing a coach
#     later edits gets overwritten.
#
#   * It touches no network. Only the bundled season CSV and the Blast tables
#     built into this file -- so a slow or dead Charting App can never hang
#     startup. The Charting roster stays a manual `python seed.py` (it holds two
#     rows named TEST TEST today, so there is nothing to gain by rushing it).
#
#   * It is OFF outside production by default, so the test suites build their
#     own fixtures without 25 real players appearing underneath them. Set
#     AUTO_SEED=1 to force it locally, AUTO_SEED=0 to disable it in production.

def autoseed_enabled():
    flag = os.environ.get("AUTO_SEED", "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if flag in ("1", "true", "yes", "on"):
        return True
    return bool(os.environ.get("RAILWAY_ENVIRONMENT"))


def maybe_seed(engine, year=DEFAULT_SEASON):
    """Seed the roster if this database has never been seeded. Idempotent.

    Never raises: a hub that starts with an empty roster is a bad day, but a hub
    that refuses to start at all is a worse one.
    """
    if not autoseed_enabled():
        return {"ran": False, "reason": "auto-seed disabled outside production"}
    try:
        with engine.connect() as conn:
            existing = conn.execute(select(func.count())
                                    .select_from(db.players)).scalar()
        if existing:
            return {"ran": False, "reason": f"{existing} players already seeded"}

        players_added = seed_roster_from_season(engine, year=year)
        linked, flagged = seed_blast_vendor_ids(engine)
        columns = seed_blast_column_maps(engine)
        result = {"ran": True, "players": players_added, "blast_linked": linked,
                  "names_queued": flagged, "blast_columns": columns}
        print(f"[seed] first run: {players_added} players from the {year} season, "
              f"{linked} Blast IDs linked, {flagged} names queued for review, "
              f"{columns} Blast columns mapped", flush=True)
        return result
    except Exception as e:
        # Most likely a duplicate-slug race if more than one worker boots at
        # once. The unique constraint is what makes that harmless.
        print(f"[seed] auto-seed skipped: {e}", flush=True)
        return {"ran": False, "error": str(e)}


def main(argv):
    engine = db.get_engine()
    dry = "--dry-run" in argv

    if "--status" in argv:
        status(engine)
        return
    if "--review" in argv:
        show_review(engine)
        return

    year = DEFAULT_SEASON
    if "--year" in argv:
        year = int(argv[argv.index("--year") + 1])

    if "--blast-only" not in argv:
        print(f"Seeding roster from the {year} season pitch data...")
        n = seed_roster_from_season(engine, year=year, dry_run=dry)
        print(f"  {n} player(s) added")
        print("Checking the Charting App roster...")
        n = seed_roster_from_charting(engine, dry_run=dry)
        print(f"  {n} player(s) added")

    if "--roster-only" not in argv:
        print("Loading Blast vendor IDs...")
        linked, flagged = seed_blast_vendor_ids(engine, dry_run=dry)
        print(f"  {linked} linked, {flagged} sent to the name-review queue")
        if not dry:
            print("Seeding Blast column map...")
            print(f"  {seed_blast_column_maps(engine)} column(s) mapped")

    status(engine)
    if "--roster-only" not in argv:
        show_review(engine)


if __name__ == "__main__":
    main(sys.argv[1:])
