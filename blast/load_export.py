"""
blast/load_export.py -- load a Blast Connect "All swings by player" CSV.

Drop the export in `blast/exports/` (gitignored -- ingest keeps the whole file in
`raw_imports`, which is the copy that survives a redeploy), then:

    python blast/load_export.py blast/exports/All_swings_by_player_*.csv
    python blast/load_export.py blast/exports/All_swings_by_player_*.csv --commit

Dry-run by default, like every other loader here. The dry run resolves every
player, groups the sessions and reports exactly what a commit would write,
without touching the database.

WHY THIS IS A THIN SCRIPT AND NOT A PIPELINE
--------------------------------------------
Everything that matters already exists in ingest.py: the file is kept whole and
sha256-deduped in raw_imports, the column map is confirmed once per vendor and
remembered, unresolved names queue in name_review instead of being guessed, and
re-running a cumulative export only adds sessions it doesn't already hold. This
script's whole job is to hand Blast's CSV to that pipeline with the two things
the generic path can't infer:

  1. `air Swing` rows are dropped. A sensor reading with no ball; Blast leaves
     them out of a player's averages and so do we. 22 of 7,198 in the first
     export -- small, but they are not swings at a pitch and would sit in the
     same baselines as ones that are.

  2. Blast sends no session id. Sessions fall back to (player, date), which
     cannot tell a morning tee session from an evening machine session on the
     same day. That limitation is reported, not hidden -- and it matters less
     here than it looks, because drill context lives on the SWING, so a mixed
     day still compares tee-to-tee. 18 of 126 player-days in the first export
     mixed two or more drills.

WHAT THIS DOES NOT DO
---------------------
It does not pull from Blast. There is a 2024 R script that logs into
blastconnect.com with a plaintext password and hits the v3 API; porting it is a
separate job and the credentials move to env vars when it happens. Until then a
coach exports the CSV from Blast Connect and runs this.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import ingest
import metrics
import seed


def is_real_swing(row):
    """drops Blast 'air Swing' rows (a sensor reading with no ball)"""
    action = str(row.get("actiontype", "") or "").strip().lower()
    if not action:
        return True                      # untyped rows predate the column; keep them
    return action in metrics.BLAST_ACTION_TYPES


def _pending_import_for(engine, raw):
    """The id of an uncommitted import holding exactly these bytes, or None."""
    import hashlib
    from sqlalchemy import select
    sha = hashlib.sha256(raw).hexdigest()
    with engine.connect() as conn:
        row = conn.execute(
            select(db.raw_imports.c.id, db.raw_imports.c.status)
            .where(db.raw_imports.c.sha256 == sha)).first()
    return row.id if row and row.status != "committed" else None


def load(engine, path, commit=False, uploaded_by="blast/load_export.py",
         purpose=None, session_type="cage"):
    with open(path, "rb") as fh:
        raw = fh.read()

    seeded = seed.seed_blast_column_maps(engine)
    if seeded:
        print(f"seeded {seeded} Blast column mapping(s)")

    # store() refuses a file it already holds, by sha256. That is the right
    # behaviour for a coach uploading twice, but it makes a dry run un-repeatable:
    # the first dry run stores the file (storing is not the part being rehearsed --
    # committing is), so the second says "already uploaded" and stops. Reuse a
    # PENDING import of the same bytes instead. A committed one still refuses,
    # because that genuinely is a double-upload.
    try:
        import_id, sniffed = ingest.store(
            engine, "blast", os.path.basename(path), raw,
            uploaded_by=uploaded_by, side="hitting",
            session_type=session_type, purpose=purpose,
            row_filter=is_real_swing)
        print(f"import #{import_id}: {sniffed['row_count']} swings, "
              f"header on line {sniffed['header_row'] + 1}")
        if sniffed.get("rows_filtered"):
            print(f"  dropped {sniffed['rows_filtered']} row(s): "
                  f"{sniffed['filtered_by']}")
    except ingest.IngestError as exc:
        import_id = _pending_import_for(engine, raw)
        if import_id is None:
            raise
        print(f"reusing pending import #{import_id} ({exc})")

    info = ingest.analyze(engine, import_id)
    if info["unmapped"]:
        print(f"  UNMAPPED COLUMNS: {', '.join(info['unmapped'])}")
    if not info["ready"]:
        print("  NOT READY: " + "; ".join(info["missing_roles"]))
        return import_id, None

    stats = ingest.commit(engine, import_id, dry_run=not commit)
    verb = "wrote" if commit else "would write"
    print(f"\n{verb}:")
    print(f"  sessions new       {stats['sessions_new']}")
    print(f"  sessions existing  {stats['sessions_existing']}")
    print(f"  measurements       {stats['measurements']}")
    if stats.get("untagged_swings"):
        n = stats["untagged_swings"]
        print(f"  untagged swings    {n}  <- stored and shown, but these drive no")
        print( "                         change detection: an untagged swing can't")
        print( "                         be compared drill-to-drill. Fixed by")
        print( "                         tagging in the Blast app, not in code.")
    if stats["rows_unresolved_player"]:
        print(f"  unresolved rows    {stats['rows_unresolved_player']} "
              f"({stats['names_queued']} name(s) queued for review)")
    if stats["rows_no_date"]:
        print(f"  rows with no date  {stats['rows_no_date']}")
    if stats["synthesized_session_refs"]:
        print(f"  NOTE: {stats['synthesized_session_refs']} session(s) keyed on "
              f"(player, date) -- Blast sends no session id.")

    # Learn the vendor id for everyone this export resolved BY NAME. Names are the
    # fragile path -- three players are spelled differently in AWRE than in Blast,
    # and 'Caleb Williams28' only resolves at all because a human accepted it in
    # review. Once the user_id is stored the spelling stops mattering for good.
    learned, skipped = _learn_ids(engine, import_id, dry_run=not commit)
    if learned:
        print(f"  {'learned' if commit else 'would learn'} {learned} Blast user id(s) "
              f"-- future exports resolve these players by id, not by name")
    if skipped:
        print(f"  SKIPPED {len(skipped)} id(s) that collide with the 2024 namespace: "
              f"{', '.join(skipped)}")
    return import_id, stats


def _learn_ids(engine, import_id, dry_run=False):
    """(player_id, blast user_id) for every row this import actually resolved."""
    from sqlalchemy import select
    import ingest as ing
    with engine.connect() as conn:
        imp = conn.execute(select(db.raw_imports)
                           .where(db.raw_imports.c.id == import_id)).first()
        names = ing._player_lookup(conn)
        vids = ing._vendor_lookup(conn, "blast")
    pairs = {}
    for row in (imp.payload or []):
        uid = str(row.get("user_id", "") or "").strip()
        if not uid or uid in vids:
            continue
        full = f"{row.get('first_name','')} {row.get('last_name','')}".strip()
        pid = names.get(ing._name_key(full))
        if pid is not None:
            pairs[uid] = pid                 # last writer wins; ids are 1:1 here
    return seed.learn_blast_user_ids(
        engine, [(pid, uid) for uid, pid in pairs.items()], dry_run=dry_run)


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    commit = "--commit" in argv
    if not args:
        print(__doc__)
        return 1
    path = args[0]
    if not os.path.exists(path):
        print(f"no such file: {path}")
        return 1

    engine = db.get_engine()
    if not commit:
        print("DRY RUN -- nothing will be written. Add --commit to write.\n")
    try:
        load(engine, path, commit=commit)
    except ingest.IngestError as exc:
        # Refusing a file already committed is the dedupe working, not a crash. A
        # coach re-running this on last week's export should read one sentence,
        # not a traceback.
        print(f"Nothing to do: {exc}")
        print("\nIf this export really does contain new swings, re-export from "
              "Blast Connect -- an identical file has already been loaded.")
        return 0
    if not commit:
        print("\nDRY RUN -- nothing was written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
