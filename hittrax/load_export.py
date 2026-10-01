"""
hittrax/load_export.py -- joined Plays+Session CSV into the database.

    python hittrax/load_export.py --plays Plays.csv --session Session.csv
    python hittrax/load_export.py --plays Plays.csv --session Session.csv --commit

Dry-run by default. ``hittrax/pull_export.py --commit`` is what the cron runs;
it downloads the pair and calls ``load()`` here.

Pipeline: join → seed column map → ingest.store → analyze → commit →
``changes.compute_all`` → one ``job_runs`` row with ``job="hittrax_daily"``.

Players are never inserted. A name that does not resolve is queued in
``name_review`` by ``ingest.commit``, the same rule Blast uses. Accepting the
alias is a coach action on /collect, then ``ingest.recommit``. This cron does
not recommit on its own.

``store()`` dedupes on the sha256 of the *joined* CSV. The same pair pulled
again after a successful commit exits 0 with ``skipped_duplicate`` and does
not write the swings a second time. A pending import of those same bytes (a
dry run, or a crash before commit) is reused, because treating it as a
duplicate would make the real ``--commit`` a silent no-op.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import insert, select

import db
import ingest

from hittrax.join import JoinError, OversizedExport, join_exports

JOB_NAME = "hittrax_daily"


def log(message):
    print(f"[hittrax] {message}", flush=True)


def record_job(engine, ok, summary):
    """One row in job_runs. Written last on the --commit path.

    A missing row is how you tell a cron that died from a cron that never
    started. Dry runs do not write one: a rehearsal must not look like the
    08:15 job.
    """
    payload = json.loads(json.dumps(summary, default=str))
    with engine.begin() as conn:
        conn.execute(insert(db.job_runs).values(
            job=JOB_NAME, ok=bool(ok), summary=payload))


def _existing_import(engine, raw):
    sha = hashlib.sha256(raw).hexdigest()
    with engine.connect() as conn:
        return conn.execute(
            select(db.raw_imports.c.id, db.raw_imports.c.status,
                   db.raw_imports.c.filename)
            .where(db.raw_imports.c.sha256 == sha)).first()


def _finish(engine, commit, ok, summary, exit_code):
    summary = dict(summary)
    summary["ok"] = bool(ok)
    if not commit:
        return exit_code
    try:
        record_job(engine, ok, summary)
    except Exception as exc:                                 # noqa: BLE001
        # The data may already be in. Exiting 0 without a job_runs row is the
        # failure nightly.py exists to stop: the dashboard stays green and
        # nobody can see that this morning did not finish.
        log(f"job_runs write failed: {type(exc).__name__}: {exc}")
        return 1 if exit_code == 0 else exit_code
    return exit_code


def load(engine, plays_bytes, session_bytes, *, commit, plays_name,
         session_name, plays_size=None, session_size=None, filename=None):
    """Join, store, and (if ``commit``) write sessions and a job_runs row.

    Returns a process exit code. 0 is success or ``skipped_duplicate``.
    """
    import changes
    import seed

    summary = {
        "plays_file": plays_name,
        "session_file": session_name,
        "plays_bytes": plays_size if plays_size is not None else len(plays_bytes),
        "session_bytes": (session_size if session_size is not None
                          else len(session_bytes)),
        "skipped_duplicate": False,
        "dry_run": not commit,
    }
    try:
        joined, join_stats = join_exports(plays_bytes, session_bytes)
    except (JoinError, OversizedExport) as exc:
        log(str(exc))
        summary["error"] = str(exc)
        summary["stage"] = "join"
        return _finish(engine, commit, False, summary, 1)

    summary["join"] = join_stats
    log(f"joined {join_stats['written']} rows "
        f"key={join_stats['join_key']} ev={join_stats['ev_column']} "
        f"matched={join_stats['matched']} unmatched={join_stats['unmatched']}")

    seeded = seed.seed_hittrax_column_maps(engine)
    if seeded:
        log(f"seeded {seeded} HitTrax column mapping(s)")

    if not filename:
        filename = "hittrax_joined.csv"
    summary["joined_filename"] = filename

    try:
        import_id, sniffed = ingest.store(
            engine, "hittrax", filename, joined,
            uploaded_by="hittrax/pull_export.py",
            side="hitting", session_type="cage")
        log(f"import #{import_id}: {sniffed['row_count']} rows")
    except ingest.IngestError as exc:
        if "already uploaded" not in str(exc):
            log(str(exc))
            summary["error"] = str(exc)
            summary["stage"] = "store"
            return _finish(engine, commit, False, summary, 1)
        existing = _existing_import(engine, joined)
        if existing is None or existing.status == "committed":
            prior = existing.id if existing is not None else "?"
            log(f"skipped_duplicate import=#{prior} ({exc})")
            summary["skipped_duplicate"] = True
            summary["import_id"] = existing.id if existing is not None else None
            summary["stage"] = "store"
            # The file is already in. Exit 0 so a daily re-pull of an unchanged
            # export is not a red cron. Do not run change detection again.
            return _finish(engine, commit, True, summary, 0)
        import_id = existing.id
        log(f"reusing pending import #{import_id} ({exc})")

    summary["import_id"] = import_id
    info = ingest.analyze(engine, import_id)
    if not info["ready"]:
        message = "not ready to commit -- missing " + "; ".join(info["missing_roles"])
        log(message)
        if info["unmapped"]:
            log("unmapped columns: " + ", ".join(info["unmapped"]))
        summary["error"] = message
        summary["stage"] = "analyze"
        return _finish(engine, commit, False, summary, 1)

    # Unresolved names are queued inside commit. There is no branch here that
    # inserts into players. Phase-1 could be approved by hand; this cron cannot.
    stats = ingest.commit(engine, import_id, dry_run=not commit)
    summary["commit"] = stats
    verb = "wrote" if commit else "would write"
    log(f"{verb}: sessions_new={stats['sessions_new']} "
        f"sessions_existing={stats['sessions_existing']} "
        f"measurements={stats['measurements']} "
        f"unresolved={stats['rows_unresolved_player']} "
        f"names_queued={stats['names_queued']}")

    if not commit:
        log("DRY RUN -- nothing was committed. Re-run with --commit to write.")
        return 0

    try:
        detected = changes.compute_all(engine, write=True)
        detected.pop("events", None)
    except Exception as exc:                                 # noqa: BLE001
        # The import is already committed. Say so, and still fail the job so
        # the missed detection is visible. The next morning's compute_all
        # covers every active player, not only this file.
        log(f"change detection FAILED (data is loaded; detection can be re-run): "
            f"{type(exc).__name__}: {exc}")
        summary["error"] = f"changes: {type(exc).__name__}: {exc}"
        summary["stage"] = "changes"
        return _finish(engine, commit, False, summary, 1)

    summary["changes"] = detected
    log(f"change detection: players={detected.get('players')} "
        f"fired={detected.get('fired')}")
    return _finish(engine, commit, True, summary, 0)


def main(argv):
    commit = "--commit" in argv
    if "--dry-run" in argv and commit:
        log("pass only one of --commit and --dry-run")
        return 1
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0

    def _flag(name):
        if name not in argv:
            return None
        i = argv.index(name)
        if i + 1 >= len(argv):
            return None
        return argv[i + 1]

    plays = _flag("--plays")
    session = _flag("--session")
    if not plays or not session:
        print(__doc__)
        return 1
    if not os.path.exists(plays):
        log(f"no such file: {plays}")
        return 1
    if not os.path.exists(session):
        log(f"no such file: {session}")
        return 1
    with open(plays, "rb") as fh:
        plays_bytes = fh.read()
    with open(session, "rb") as fh:
        session_bytes = fh.read()
    if not commit:
        log("DRY RUN -- add --commit to write.")
    return load(
        db.get_engine(), plays_bytes, session_bytes, commit=commit,
        plays_name=os.path.basename(plays),
        session_name=os.path.basename(session),
        plays_size=len(plays_bytes), session_size=len(session_bytes),
        filename="hittrax_joined.csv")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
