"""
Seed the player identity layer from the school's own roster pages.

    python rapsodo/seed_roster.py            inspect only (default)
    python rapsodo/seed_roster.py --commit   write

This is the key the rest of the system resolves against (spec section 4, roadmap
"one internal Moeller Player ID"). It writes:

  players           one row per human, created once, id never reused
  player_seasons    his level for THIS season -- kept per season so progression survives
  player_vendor_ids the athletics site's own id, so re-runs match on id not name
  player_aliases    spellings other sources use for him

Idempotent: re-running matches existing players on a normalised name and updates
rather than duplicating.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import db  # noqa: E402
from sqlalchemy import func, insert, select, update  # noqa: E402

from scrape_roster import scrape  # noqa: E402

SITE_VENDOR = "roster_site"

# Confirmed by Ian 2026-08-19: same players, different spellings. Rapsodo has the
# nickname in one case and a transposition in the other (the family spelling is
# "Homoelle" -- varsity has a Cooper Homoelle).
RAPSODO_ALIASES = {
    "Jonathan Sommers": "Jon Sommers",
    "Greyson Hoemelle": "Greyson Homoelle",
}

# Threw tracked bullpens but are on no 2026 roster -- cut, transferred, or moved on.
# Created with is_active=False (Ian, 2026-08-19) so their sessions load and stay
# queryable instead of sitting in the review queue forever. They get NO
# player_seasons row, because they hold no level this season -- which is also what
# keeps them out of any roster or team view.
FORMER_PLAYERS = [
    "CJ Gilpan",
    "Cooper Griffith",
    "John Stallo",
    "Nick Hutchinson",
    "William Brenzel",
]

# Never create these. Staff accounts show up in vendor exports looking exactly like
# players -- the Rapsodo coach login 'david cydrus' currently tops the velocity
# board at 91.1 mph. Anything here stays in the review queue permanently.
NOT_PLAYERS = {"david cydrus"}


def name_key(name: str) -> str:
    """Match 'Reggie Watson III' to the roster's 'Reggie Watson'."""
    s = re.sub(r"\s+", " ", str(name or "").strip().lower())
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\.?\b", "", s).strip()
    return s


def split_name(full: str) -> tuple[str, str]:
    parts = [p for p in str(full).split() if p]
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _counts(conn) -> dict:
    out = {}
    for name, tbl in [
        ("players", db.players), ("player_seasons", db.player_seasons),
        ("player_aliases", db.player_aliases), ("player_vendor_ids", db.player_vendor_ids),
        ("sessions", db.sessions), ("pitch_metrics", db.pitch_metrics),
        ("name_review", db.name_review),
    ]:
        out[name] = conn.execute(select(func.count()).select_from(tbl)).scalar()
    return out


def run(commit: bool) -> int:
    season = date.today().year
    engine = db.get_engine()

    with engine.connect() as conn:
        before = _counts(conn)
    print(f"database : {db.database_url().split('@')[-1]}")
    print(f"before   : {before}")

    print(f"\nscraping roster for {season}...")
    roster = scrape(season)
    print(f"  {len(roster)} players")

    ctx = engine.begin() if commit else engine.connect()
    created = updated = seasons = vendor = aliases = former = 0

    with ctx as conn:
        # Existing players, by normalised name.
        existing = {}
        for r in conn.execute(select(db.players.c.id, db.players.c.first_name,
                                     db.players.c.last_name)):
            existing[name_key(f"{r.first_name} {r.last_name}")] = r.id
        taken_slugs = {
            r.slug for r in conn.execute(select(db.players.c.slug))
        }

        by_name: dict[str, int] = {}

        for p in roster:
            first, last = split_name(p["name"])
            key = name_key(p["name"])
            pid = existing.get(key)

            if pid is None:
                slug = db.slugify(first, last)
                base, n = slug, 2
                while slug in taken_slugs:
                    slug, n = f"{base}-{n}", n + 1
                taken_slugs.add(slug)
                if commit:
                    pid = conn.execute(
                        insert(db.players).values(
                            slug=slug, first_name=first, last_name=last,
                            class_year=p["academic_year"], primary_pos=p["position"],
                            is_pitcher=bool(p["is_pitcher"]), is_active=True,
                        ).returning(db.players.c.id)
                    ).scalar()
                else:
                    pid = -(created + 1)  # placeholder for the dry run
                existing[key] = pid
                created += 1
            else:
                # Roster site is authoritative for position/class -- Rapsodo's
                # self-entered demographics are not (it has Bessenbach four years off).
                if commit:
                    conn.execute(
                        update(db.players).where(db.players.c.id == pid).values(
                            class_year=p["academic_year"],
                            primary_pos=p["position"],
                            is_pitcher=bool(p["is_pitcher"]),
                            is_active=True,
                        )
                    )
                updated += 1

            by_name[key] = pid

            if commit:
                already = conn.execute(
                    select(db.player_seasons.c.id)
                    .where(db.player_seasons.c.player_id == pid)
                    .where(db.player_seasons.c.season == season)
                ).first()
                vals = dict(
                    level=p["level"], jersey=p["jersey"], position=p["position"],
                    academic_year=p["academic_year"], height_in=p["height_in"],
                    is_pitcher=bool(p["is_pitcher"]), active=True, source=SITE_VENDOR,
                )
                if already:
                    conn.execute(update(db.player_seasons)
                                 .where(db.player_seasons.c.id == already.id).values(**vals))
                else:
                    conn.execute(insert(db.player_seasons)
                                 .values(player_id=pid, season=season, **vals))

                if p["sidearm_id"]:
                    dup = conn.execute(
                        select(db.player_vendor_ids.c.id)
                        .where(db.player_vendor_ids.c.vendor == SITE_VENDOR)
                        .where(db.player_vendor_ids.c.vendor_id == str(p["sidearm_id"]))
                    ).first()
                    if not dup:
                        conn.execute(insert(db.player_vendor_ids).values(
                            player_id=pid, vendor=SITE_VENDOR,
                            vendor_id=str(p["sidearm_id"])))
                        vendor += 1
            seasons += 1

        # Former players: created inactive, no season row.
        for full in FORMER_PLAYERS:
            key = name_key(full)
            if key in NOT_PLAYERS:
                continue
            if key in existing:
                by_name[key] = existing[key]
                continue
            first, last = split_name(full)
            slug = db.slugify(first, last)
            base, n = slug, 2
            while slug in taken_slugs:
                slug, n = f"{base}-{n}", n + 1
            taken_slugs.add(slug)
            if commit:
                pid = conn.execute(
                    insert(db.players).values(
                        slug=slug, first_name=first, last_name=last,
                        is_pitcher=True, is_active=False,
                    ).returning(db.players.c.id)
                ).scalar()
            else:
                pid = -(created + 1)
            existing[key] = by_name[key] = pid
            former += 1
            print(f"  former (inactive): {full}")

        # Rapsodo spellings that don't match the roster.
        for rapsodo_name, roster_name in RAPSODO_ALIASES.items():
            pid = by_name.get(name_key(roster_name))
            if pid is None:
                print(f"  [warn] alias target not on roster: {roster_name}")
                continue
            if commit:
                dup = conn.execute(
                    select(db.player_aliases.c.id)
                    .where(db.player_aliases.c.source == "rapsodo")
                    .where(db.player_aliases.c.alias == rapsodo_name)
                ).first()
                if not dup:
                    conn.execute(insert(db.player_aliases).values(
                        player_id=pid, source="rapsodo", alias=rapsodo_name))
                    aliases += 1
            else:
                aliases += 1
            print(f"  alias: rapsodo '{rapsodo_name}' -> '{roster_name}'")

    print(f"\n{'COMMITTED' if commit else 'DRY RUN'}")
    print(f"  players created  {created}")
    print(f"  players updated  {updated}")
    print(f"  former inactive  {former}")
    print(f"  season rows      {seasons}")
    print(f"  vendor ids       {vendor}")
    print(f"  aliases          {aliases}")

    with engine.connect() as conn:
        print(f"after    : {_counts(conn)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed players from the school roster site.")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    return run(commit=args.commit or os.environ.get("SEED_COMMIT") == "1")


if __name__ == "__main__":
    raise SystemExit(main())
