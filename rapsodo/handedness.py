"""
rapsodo/handedness.py -- fill bats/throws from the Rapsodo player profile.

Rapsodo carries handedness on the nested profile objects, not at the top level:

    player.pitcherProfile.handedness   0 = right, 1 = left
    player.hitterProfile.handedness    same enum

The enum was decoded on 2026-09-21 against the 11 roster players whose throws
was already known, then validated independently by each pitcher's fastball
spin-axis cluster (right-handers near 1:16 on the clock, left-handers near
10:40): 22 of 23 agree. The one exception is a vendor-side data-entry error
(roster and physics both say R; only the Rapsodo profile says L), which is why
this module NEVER overwrites a value that is already set -- the roster is the
source of truth, this only fills what the roster left blank.

Before this existed `throws` was null for 63 of 74 players, and the pitching
analyst note had to refuse to name a side for spin axis on most of the staff.

    python rapsodo/handedness.py            dry run over every stored payload
    python rapsodo/handedness.py --commit   fill the nulls
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from sqlalchemy import select, update  # noqa: E402

ENUM = {0: "R", 1: "L", "0": "R", "1": "L"}


def decode(player_obj: dict) -> tuple[str | None, str | None]:
    """-> (throws, bats) from a Rapsodo player object, None where absent."""
    p = player_obj or {}
    t = ENUM.get((p.get("pitcherProfile") or {}).get("handedness"))
    b = ENUM.get((p.get("hitterProfile") or {}).get("handedness"))
    return t, b


def apply(conn, player_id: int, player_obj: dict) -> dict:
    """Fill NULL bats/throws for one player from his profile. Never overwrites.

    Returns what it set, e.g. {"throws": "L"}. Safe to call on every load.
    """
    t, b = decode(player_obj)
    if not t and not b:
        return {}
    row = conn.execute(select(db.players.c.throws, db.players.c.bats)
                       .where(db.players.c.id == player_id)).first()
    if not row:
        return {}
    vals = {}
    if t and row.throws is None:
        vals["throws"] = t
    if b and row.bats is None:
        vals["bats"] = b
    if vals:
        conn.execute(update(db.players).where(db.players.c.id == player_id).values(**vals))
    return vals


def backfill(engine, commit: bool = False) -> dict:
    """Walk every stored Rapsodo payload and fill nulls for resolved players."""
    filled, conflicts = {}, []
    with engine.begin() as conn:
        vid = {str(r.vendor_id): r.player_id for r in conn.execute(
            select(db.player_vendor_ids.c.vendor_id, db.player_vendor_ids.c.player_id)
            .where(db.player_vendor_ids.c.vendor == "rapsodo")).all()}
        names = {r.id: f"{r.first_name} {r.last_name}" for r in conn.execute(
            select(db.players.c.id, db.players.c.first_name, db.players.c.last_name)).all()}
        seen = set()
        for (payload,) in conn.execute(select(db.raw_imports.c.payload)
                                       .where(db.raw_imports.c.vendor == "rapsodo")).all():
            p = payload if isinstance(payload, dict) else json.loads(payload)
            pl = p.get("player") or {}
            pid = vid.get(str(pl.get("_id") or ""))
            if not pid or pid in seen:
                continue
            seen.add(pid)
            t, b = decode(pl)
            row = conn.execute(select(db.players.c.throws, db.players.c.bats)
                               .where(db.players.c.id == pid)).first()
            # Report, do not touch, where the roster already disagrees.
            if t and row.throws and row.throws != t:
                conflicts.append(f"{names.get(pid, pid)}: roster throws={row.throws}, Rapsodo profile={t} (roster kept)")
            if b and row.bats and row.bats != b:
                conflicts.append(f"{names.get(pid, pid)}: roster bats={row.bats}, Rapsodo profile={b} (roster kept)")
            vals = apply(conn, pid, pl) if commit else {
                k: v for k, v in (("throws", t), ("bats", b))
                if v and getattr(row, k) is None}
            if vals:
                filled[names.get(pid, pid)] = vals
        if not commit:
            conn.rollback()
    return {"players_seen": len(seen), "filled": filled, "conflicts": conflicts,
            "committed": commit}


if __name__ == "__main__":
    res = backfill(db.get_engine(), commit="--commit" in sys.argv)
    print(f"players with a profile : {res['players_seen']}")
    print(f"{'filled' if res['committed'] else 'would fill'}  : {len(res['filled'])} player(s)")
    for name, vals in sorted(res["filled"].items()):
        print(f"   {name:<24} {vals}")
    if res["conflicts"]:
        print("conflicts (roster kept):")
        for c in res["conflicts"]:
            print("   " + c)
    if not res["committed"]:
        print("DRY RUN -- nothing written. Add --commit to write.")
