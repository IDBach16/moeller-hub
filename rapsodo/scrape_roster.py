"""
Scrape the Moeller athletics roster pages into one roster file.

    python rapsodo/scrape_roster.py            -> out/roster_<year>.json + .csv

Source: letsgobigmoe.com (the program's own public roster pages), one per level.
This is the identity key the whole player-development system hangs off -- see
PLAYER_DEV_SPEC section 4 and the roadmap's "one internal Moeller Player ID".

Levels are scraped separately and kept as a per-player `level`, because a player's
level changes year to year and that progression is exactly what a development
system wants to keep. Nothing here writes to the database; seeding is a separate,
reviewed step.
"""

from __future__ import annotations

import csv
import json
import re
import time
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://letsgobigmoe.com"
LEVELS = {
    "varsity": "/sports/baseball/roster",
    "jv": "/sports/junior-varsity-baseball/roster",
    "freshman": "/sports/freshman-baseball/roster",
}

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = PROJECT_ROOT / "out"

# "Jr." on this site is the academic year, not a name suffix -- don't confuse the two.
YEAR_MAP = {
    "fr.": "Freshman", "so.": "Sophomore", "jr.": "Junior", "sr.": "Senior",
    "freshman": "Freshman", "sophomore": "Sophomore",
    "junior": "Junior", "senior": "Senior",
}

REQUEST_PAUSE_SEC = 1.0  # three requests to someone else's server; be polite


def _text(node, cls: str) -> str | None:
    el = node.select_one(f".sidearm-roster-player-{cls}")
    if not el:
        return None
    val = re.sub(r"\s+", " ", el.get_text(strip=True))
    return val or None


def _height_inches(raw: str | None) -> int | None:
    """5'11\" -> 71. Kept numeric so it can be compared and trended."""
    if not raw:
        return None
    m = re.match(r"(\d+)'\s*(\d+)?", raw)
    if not m:
        return None
    feet = int(m.group(1))
    inches = int(m.group(2) or 0)
    return feet * 12 + inches


def _grad_year(academic_year: str | None, season: int) -> int | None:
    """Senior in the 2026 season graduates 2026, junior 2027, and so on."""
    if not academic_year:
        return None
    offset = {"Senior": 0, "Junior": 1, "Sophomore": 2, "Freshman": 3}
    label = YEAR_MAP.get(academic_year.strip().lower())
    return season + offset[label] if label in offset else None


def scrape(season: int) -> list[dict]:
    players: list[dict] = []
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Moeller player-dev roster sync"})

    for level, path in LEVELS.items():
        resp = sess.get(f"{BASE}{path}", timeout=60)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        cards = soup.select(".sidearm-roster-player")
        if not cards:
            raise RuntimeError(
                f"No players found on {path}. The site's markup probably changed -- "
                "check the .sidearm-roster-player selector before trusting a rerun."
            )

        for card in cards:
            name = _text(card, "name") or ""
            name = re.sub(r"^\s*\d+\s*", "", name).strip()  # strip leading jersey number
            if not name:
                continue
            academic = _text(card, "academic-year")
            players.append(
                {
                    "sidearm_id": card.get("data-player-id"),
                    "level": level,
                    "jersey": _text(card, "jersey-number"),
                    "name": name,
                    "position": _text(card, "position-long-short"),
                    "height_raw": _text(card, "height"),
                    "height_in": _height_inches(_text(card, "height")),
                    "academic_year": YEAR_MAP.get((academic or "").strip().lower(), academic),
                    "grad_year": _grad_year(academic, season),
                    # A "P" anywhere in the position string is what makes him relevant
                    # to the pitching side. Kept as a flag, not a filter.
                    "is_pitcher": bool(re.search(r"\bP\b|^P/|/P$|/P/", _text(card, "position-long-short") or "")),
                    "season": season,
                }
            )
        print(f"  {level:<9} {len(cards):>3} players")
        time.sleep(REQUEST_PAUSE_SEC)

    return players


def main() -> int:
    season = date.today().year
    print(f"Scraping {BASE} rosters for {season}...")
    players = scrape(season)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    js = OUT_DIR / f"roster_{season}.json"
    js.write_text(json.dumps(players, indent=1))

    cs = OUT_DIR / f"roster_{season}.csv"
    with cs.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(players[0].keys()))
        w.writeheader()
        w.writerows(players)

    pitchers = sum(1 for p in players if p["is_pitcher"])
    print(f"\n{len(players)} players ({pitchers} listed as pitchers)")
    print(f"  {js}")
    print(f"  {cs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
