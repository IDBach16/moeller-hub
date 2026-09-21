"""
nightly.py -- the one job that runs every night. Replaces `python rapsodo/daily.py`
as the start command of the `rapsodo-cron` Railway service (09:00 UTC daily).

    python nightly.py              the real thing
    python nightly.py --dry-run    every step in read-only mode, no API calls, no email

In order:

  1. rapsodo    pull the last few days from Rapsodo Cloud and load them
                (rapsodo/daily.py, run as a subprocess so its exit code is kept)
  2. detect     change detection across every player -- the step that was never
                scheduled. Data landed nightly for months while the "What changed"
                feed only moved when someone clicked "Run detection now".
  3. notes      Mondays only: refresh the weekly analyst note for every player
                with new data in the last 7 days. Cached text is reused when
                nothing moved, so this costs one model call per player who
                actually trained.
  4. freshness  newest data per source, and whether each vendor job has landed a
                file recently enough
  5. record     one row in job_runs -- the durable proof the job finished
  6. email      a short daily heartbeat to ALERT_TO. Subject line is enough to
                read. Absence of the email is itself the signal that the job
                did not run, which is the failure mode that hid two dead jobs.

WHY EVERY STEP RUNS EVEN IF AN EARLIER ONE FAILED
-------------------------------------------------
A Rapsodo auth failure must not stop change detection on the Blast data that
landed on Monday, and neither failure must stop the freshness report that would
tell you about it. Each step is isolated; the report lists what broke; the exit
code is non-zero if anything did, so Railway shows the run as failed instead of
green.

EMAIL
-----
Sends through Gmail with an App Password -- the same credential the pipeline's
email_draft.py already uses for --gmail, so there is one kind of mail secret
here, not two. Set on the service:

    GMAIL_APP_PASSWORD   the 16-char app password (myaccount.google.com -> App passwords)
    GMAIL_USER           sending account          (default idbach16@gmail.com)
    ALERT_TO             recipient                (default: GMAIL_USER)
    ALERT_ONLY_ON_PROBLEM=1   to silence the daily OK heartbeat and mail only on trouble

With no password set, the job still runs everything and prints the report;
it just cannot mail it.
"""
from __future__ import annotations

import json
import os
import smtplib
import subprocess
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage

from sqlalchemy import func, insert, select

import db

HERE = os.path.dirname(os.path.abspath(__file__))

# How old a vendor's newest landed file may be before it counts as stale.
# Blast runs Mondays, so > 8 days means a Monday was missed. HitTrax is on the
# same weekly cadence once the vendor starts delivering. Rapsodo is pulled by
# THIS job, so its staleness is the pull step's own exit code, not a date.
STALE_AFTER_DAYS = {"blast": 8, "hittrax": 8}


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------
def step(report: dict, name: str, fn):
    """Run one step in isolation. Records the outcome; never raises."""
    print(f"\n[nightly] {name}", flush=True)
    try:
        out = fn() or {}
        report["steps"][name] = {"ok": True, **out}
        for k, v in out.items():
            print(f"  {k}: {v}")
    except Exception as e:                                   # noqa: BLE001
        msg = f"{type(e).__name__}: {str(e)[:300]}"
        report["steps"][name] = {"ok": False, "error": msg}
        report["problems"].append(f"{name} -- {msg}")
        traceback.print_exc()


def pull_rapsodo(dry: bool):
    if dry:
        return {"skipped": "dry-run"}
    r = subprocess.run([sys.executable, os.path.join(HERE, "rapsodo", "daily.py")],
                       capture_output=True, text=True, cwd=HERE)
    lines = (r.stdout + "\n" + r.stderr).strip().splitlines()
    for ln in lines[-8:]:
        print("   | " + ln)
    if r.returncode != 0:
        # 2 = auth (documented in daily.py), 1 = anything else
        raise RuntimeError(f"rapsodo/daily.py exited {r.returncode}: "
                           f"{lines[-1] if lines else '(no output)'}")
    loaded = next((ln for ln in lines if "[rapsodo] loaded" in ln), "")
    return {"exit": 0, "loaded": loaded.replace("[rapsodo] ", "") or "nothing new"}


def detect_changes(engine, dry: bool):
    import changes
    res = changes.compute_all(engine, write=not dry)
    res.pop("events", None)
    return res


def weekly_notes(engine, dry: bool, today: date):
    if today.weekday() != 0:                                 # Monday, in the job's UTC clock
        return {"skipped": "not Monday"}
    if not dry and not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set on this service -- "
                           "weekly notes cannot run")
    import summaries
    res = summaries.run_weekly(engine, days=7, dry_run=dry)
    res.pop("results", None)
    return res


def freshness(engine, today: date):
    """Newest data per source, plus whether each vendor job landed recently."""
    out = {}
    with engine.connect() as conn:
        newest = dict(conn.execute(
            select(db.sessions.c.source, func.max(db.sessions.c.session_date))
            .group_by(db.sessions.c.source)).all())
        landed = dict(conn.execute(
            select(db.raw_imports.c.vendor, func.max(db.raw_imports.c.uploaded_at))
            .group_by(db.raw_imports.c.vendor)).all())
        recent = dict(conn.execute(
            select(db.sessions.c.source, func.count())
            .where(db.sessions.c.session_date >= today - timedelta(days=30))
            .group_by(db.sessions.c.source)).all())

    problems = []
    for src in ("rapsodo", "blast", "hittrax"):
        nd = newest.get(src)
        lf = landed.get(src)
        row = {"newest_session": str(nd) if nd else None,
               "last_file_landed": lf.strftime("%Y-%m-%d") if lf else None,
               "sessions_last_30d": int(recent.get(src, 0))}
        limit = STALE_AFTER_DAYS.get(src)
        if limit:
            if lf is None:
                # HitTrax has never delivered; Blast should have. Say which.
                row["status"] = "not live yet" if src == "hittrax" else "NEVER LANDED"
                if src != "hittrax":
                    problems.append(f"{src}: no file has ever landed")
            else:
                age = (datetime.now(timezone.utc).date() - lf.date()).days
                row["file_age_days"] = age
                if age > limit:
                    row["status"] = f"STALE ({age}d, limit {limit})"
                    problems.append(f"{src}: last file landed {age} days ago (limit {limit})")
                else:
                    row["status"] = "ok"
        else:
            row["status"] = "pulled by this job"
        out[src] = row
    return {"sources": out, "stale": problems}


def record_run(engine, report: dict, dry: bool):
    if dry:
        return {"skipped": "dry-run"}
    with engine.begin() as conn:
        conn.execute(insert(db.job_runs).values(
            job="nightly", ok=not report["problems"],
            summary=json.loads(json.dumps(report, default=str))))
    return {"written": True}


# --------------------------------------------------------------------------
# the email
# --------------------------------------------------------------------------
def compose(report: dict) -> tuple[str, str]:
    ok = not report["problems"]
    fr = report["steps"].get("freshness", {}).get("sources", {})

    def d(src, key="newest_session"):
        return (fr.get(src) or {}).get(key) or "-"

    det = report["steps"].get("detect", {})
    notes = report["steps"].get("notes", {})
    fired = det.get("fired", det.get("events_written", det.get("new", "?")))
    # run_weekly returns its own numeric "skipped" COUNT; the Monday gate stores a
    # string reason under the same key. Only the string means "did not run".
    written = "-" if isinstance(notes.get("skipped"), str) else notes.get("written", 0)

    subject = (f"Moeller nightly {'OK' if ok else 'ATTENTION'} -- "
               f"Rapsodo thru {d('rapsodo')}, Blast thru {d('blast')}, "
               f"{fired} changes, {written} notes")

    lines = [f"Nightly job -- {report['ran_at']}", ""]
    if report["problems"]:
        lines += ["PROBLEMS:"] + [f"  - {p}" for p in report["problems"]] + [""]
    lines.append("Data freshness:")
    for src, row in fr.items():
        lines.append(f"  {src:<8} newest session {row.get('newest_session') or '-':<11} "
                     f"last file {row.get('last_file_landed') or '-':<11} "
                     f"{row.get('sessions_last_30d', 0):>3} sessions/30d   {row.get('status')}")
    lines.append("")
    lines.append("Steps:")
    for name, res in report["steps"].items():
        flag = "ok " if res.get("ok") else "FAIL"
        detail = {k: v for k, v in res.items() if k not in ("ok",)}
        lines.append(f"  [{flag}] {name:<10} {json.dumps(detail, default=str)[:160]}")
    return subject, "\n".join(lines)


def send_email(subject: str, body: str) -> bool:
    pw = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    user = os.environ.get("GMAIL_USER", "idbach16@gmail.com").strip()
    to = os.environ.get("ALERT_TO", user).strip()
    if not pw:
        print("[nightly] GMAIL_APP_PASSWORD not set -- report printed above, not emailed")
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pw)
        s.send_message(msg)
    print(f"[nightly] emailed {to}: {subject}")
    return True


# --------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    today = datetime.now(timezone.utc).date()
    report = {"ran_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
              "dry_run": dry, "steps": {}, "problems": []}
    engine = db.get_engine()

    step(report, "rapsodo",   lambda: pull_rapsodo(dry))
    step(report, "detect",    lambda: detect_changes(engine, dry))
    step(report, "notes",     lambda: weekly_notes(engine, dry, today))
    step(report, "freshness", lambda: freshness(engine, today))
    for p in report["steps"].get("freshness", {}).get("stale", []):
        report["problems"].append(f"freshness -- {p}")
    step(report, "record",    lambda: record_run(engine, report, dry))

    subject, body = compose(report)
    print("\n" + "=" * 70 + f"\n{subject}\n" + "=" * 70 + f"\n{body}\n")

    only_problems = os.environ.get("ALERT_ONLY_ON_PROBLEM", "").strip() in ("1", "true", "yes")
    if not dry and (report["problems"] or not only_problems):
        try:
            send_email(subject, body)
        except Exception as e:                               # noqa: BLE001
            print(f"[nightly] email failed: {type(e).__name__}: {e}")
            report["problems"].append(f"email -- {type(e).__name__}")

    return 1 if report["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
