# -*- coding: utf-8 -*-
"""
test_hittrax.py -- HitTrax auto-ingest.

Guards the phase-1 facts that are easy to "fix" into the wrong answer:

  * JOIN KEY. Plays.SnId matches Session.Id. Plays.SId is the facility. A
    fixture where SId equals a different session's Id must still follow SnId.
  * EXIT VELOCITY. Plays.Velo, converted m/s → mph. EBV1 is a vector
    component; a file that carries both must not store EBV1.
  * UNITS LIVE IN THE JOIN. column_maps.scale stays 1.0 because
    save_mappings cannot persist a factor. Scaling again would double mph.
  * NO NEW PLAYERS. An unknown UserName is a name_review row. The players
    table does not grow.
  * SHA AND SIZE. A committed file pulled again exits 0 (skipped_duplicate).
    A Plays file over HITTRAX_MAX_PLAYS_BYTES is not downloaded and not stored.
  * KEY ENV. The PEM is HITTRAX_SFTP_KEY. PRIVATE_KEY is not read.

Nothing here touches the network or the real database.

    python test_hittrax.py
"""

import csv
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timezone

FAILS = []


def check(label, condition, detail=""):
    mark = "ok  " if condition else "FAIL"
    extra = ""
    if detail and not condition:
        extra = f"  -- {detail}"
    print(f"  [{mark}] {label}{extra}")
    if not condition:
        FAILS.append(label)


def section(title):
    print(f"\n{title}")


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="hittraxtest_")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(TMP, "t.db").replace("\\", "/")
os.environ.pop("RAILWAY_ENVIRONMENT", None)
os.environ.pop("HUB_PASSWORD", None)
os.environ.pop("AUTO_SEED", None)
# A developer shell may already hold droplet settings. Settings tests opt in.
for _k in ("HITTRAX_SFTP_HOST", "HITTRAX_SFTP_USER", "HITTRAX_SFTP_KEY",
           "HITTRAX_SFTP_PASSWORD", "HITTRAX_SFTP_PATH", "HITTRAX_SFTP_PORT",
           "HITTRAX_MAX_PLAYS_BYTES", "PRIVATE_KEY"):
    os.environ.pop(_k, None)

import db                              # noqa: E402
import ingest                          # noqa: E402
import seed                             # noqa: E402
from hittrax import units               # noqa: E402
from hittrax.join import (              # noqa: E402
    MAX_PLAY_ROWS, JoinError, join_exports, ts_to_date)
from hittrax.load_export import JOB_NAME, load   # noqa: E402
from hittrax import pull_export as pull          # noqa: E402
from sqlalchemy import func, insert, select      # noqa: E402

ENGINE = db.get_engine()
db.metadata.create_all(ENGINE)


def n_rows(table, **where):
    q = select(func.count()).select_from(table)
    for key, value in where.items():
        q = q.where(table.c[key] == value)
    with ENGINE.connect() as conn:
        return conn.execute(q).scalar()


def csv_bytes(fieldnames, rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n",
                            extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


PLAY_FIELDS = ["Id", "SId", "SnId", "UsId", "TS", "Velo", "Elv", "Dist",
               "EBV1", "EBV2", "EBV3"]
SESSION_FIELDS = ["Id", "UserName", "UsId", "SId", "CustomID"]


def play(**kwargs):
    base = {"Id": "1", "SId": "111", "SnId": "302346", "UsId": "23010",
            "TS": "2026-09-30 18:00:00", "Velo": "10", "Elv": "15.5",
            "Dist": "10", "EBV1": "999", "EBV2": "1", "EBV3": "1"}
    base.update({k: "" if v is None else str(v) for k, v in kwargs.items()})
    return base


def session_row(**kwargs):
    base = {"Id": "302346", "UserName": "Connor Weckesser", "UsId": "23010",
            "SId": "1", "CustomID": "abc"}
    base.update({k: "" if v is None else str(v) for k, v in kwargs.items()})
    return base


class FakeSFTP:
    """Just enough of paramiko's SFTPClient for pull.run."""

    def __init__(self, files, sizes=None):
        self.files = dict(files)
        self.sizes = dict(sizes or {})
        self.opened = []

    def listdir(self, path):
        return list(self.files)

    def stat(self, path):
        name = os.path.basename(path)

        class _St:
            pass

        st = _St()
        if name in self.sizes:
            st.st_size = self.sizes[name]
        else:
            st.st_size = len(self.files[name])
        return st

    def open(self, path, mode="rb"):
        name = os.path.basename(path)
        self.opened.append(name)
        data = self.files[name]

        class _Handle:
            def __init__(self, payload):
                self._buf = io.BytesIO(payload)

            def read(self, n=-1):
                return self._buf.read(n)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return _Handle(data)

    def close(self):
        pass


def capture(fn):
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = fn()
    return result, buf.getvalue()


# ---------------------------------------------------------------------------
section("1. units — converted before ingest, Elv left alone")
# ---------------------------------------------------------------------------

check("1 m/s → the locked mph factor",
      units.ms_to_mph(1) == units.MS_TO_MPH)
check("10 m/s → 22.369362921 mph",
      abs(units.ms_to_mph(10) - 22.369362921) < 1e-9)
check("1 m → the locked feet factor",
      units.m_to_ft(1) == units.M_TO_FT)
check("Elv is not scaled", units.as_degrees("15.5") == 15.5)
check("negative elevation keeps its sign", units.as_degrees("-3.25") == -3.25)
check("blank and N/A are missing, not zero",
      units.ms_to_mph("") is None and units.ms_to_mph("N/A") is None
      and units.m_to_ft(None) is None)
check("zero is a real measurement",
      units.ms_to_mph(0) == 0.0 and units.m_to_ft("0") == 0.0)
check("four decimal places, phase-1 shape",
      units.format_measure(units.ms_to_mph(10)) == "22.3694")
check("missing measure formats as an empty cell",
      units.format_measure(None) == "")


# ---------------------------------------------------------------------------
section("2. TS → YYYY-MM-DD")
# ---------------------------------------------------------------------------

check("datetime string keeps the written date",
      ts_to_date("2026-09-30 18:04:01") == "2026-09-30")
check("ISO Z keeps the written date",
      ts_to_date("2026-09-30T18:04:01Z") == "2026-09-30")
check("US date with AM/PM",
      ts_to_date("9/30/2026 6:04:01 PM") == "2026-09-30")
_epoch = datetime(2026, 9, 30, tzinfo=timezone.utc).timestamp()
check("epoch seconds, UTC", ts_to_date(str(int(_epoch))) == "2026-09-30")
check("epoch milliseconds, UTC",
      ts_to_date(str(int(_epoch * 1000))) == "2026-09-30")
check("junk TS is None", ts_to_date("not-a-date") is None)


# ---------------------------------------------------------------------------
section("3. join — SnId, Velo, and a low match does not produce a CSV")
# ---------------------------------------------------------------------------

PLAYS = csv_bytes(PLAY_FIELDS, [
    play(Id="1", SId="111", SnId="302346", Velo="10", Elv="15.5", Dist="10",
         EBV1="999"),
    play(Id="2", SId="111", SnId="302346", Velo="20", Elv="-3.25", Dist="0",
         EBV1="999", TS="2026-09-30 18:05:00"),
    play(Id="3", SId="1", SnId="302347", UsId="00000", Velo="5", Elv="12",
         Dist="3"),
    play(Id="4", SId="1", SnId="888", UsId="99999", Velo="8", Elv="10", Dist="4"),
    # SId points at Connor's session. SnId does not exist. Must be dropped,
    # not attributed to Connor.
    play(Id="5", SId="302346", SnId="777", Velo="8", Elv="1", Dist="1", EBV1="5"),
])
SESSIONS = csv_bytes(SESSION_FIELDS, [
    session_row(Id="302346", UserName="Connor Weckesser", UsId="23010"),
    # Decoy: its Id equals the plays' SId. Joining on SId lands here.
    session_row(Id="111", UserName="Wrong Guy", UsId="11111", SId="302346"),
    session_row(Id="302347", UserName="Matt Ponatoski", UsId="00000"),
    session_row(Id="888", UserName="Nobody Special", UsId="99999"),
])

joined, jstats = join_exports(PLAYS, SESSIONS)
text = joined.decode("utf-8")
header = text.splitlines()[0]
check("collect-ready header matches phase-1",
      header == "player,vendor_id,date,session,exit_velocity,launch_angle,distance",
      header)
check("EBV is not a column and Wrong Guy was not joined",
      "EBV" not in header and "Wrong Guy" not in text)
check("unmatched SnId dropped", jstats["unmatched"] == 1 and jstats["written"] == 4,
      str(jstats))
check("join key recorded as SnId → Id",
      jstats["join_key"] == "Plays.SnId->Session.Id" and jstats["ev_column"] == "Velo")
rows = list(csv.DictReader(io.StringIO(text)))
connor = [r for r in rows if r["player"] == "Connor Weckesser"]
check("Connor's rows came from SnId 302346, not SId 111",
      len(connor) == 2 and {r["session"] for r in connor} == {"302346"}
      and {r["vendor_id"] for r in connor} == {"23010"})
check("Velo 10 m/s stored as 22.3694 mph, not EBV 999",
      connor[0]["exit_velocity"] == "22.3694")
check("Velo 20 m/s converted, elevation sign kept, zero distance kept",
      connor[1]["exit_velocity"] == units.format_measure(units.ms_to_mph(20))
      and connor[1]["launch_angle"] == "-3.2500"
      and connor[1]["distance"] == "0.0000")
check("date is YYYY-MM-DD from TS",
      all(r["date"] == "2026-09-30" for r in rows))
check("10 m → feet",
      connor[0]["distance"] == units.format_measure(units.m_to_ft(10)))

low_plays = csv_bytes(PLAY_FIELDS, [
    play(SnId="302346"),
    play(Id="2", SnId="1"),
    play(Id="3", SnId="2"),
])
low_sessions = csv_bytes(SESSION_FIELDS, [session_row()])
low_failed = False
try:
    join_exports(low_plays, low_sessions)
except JoinError as exc:
    low_failed = "SnId" in str(exc) and "join_match_too_low" in str(exc)
check("match rate under 50% raises before any CSV is returned", low_failed)

ebv_only = csv_bytes(
    ["Id", "SId", "SnId", "UsId", "TS", "Elv", "Dist", "EBV1"],
    [{"Id": "1", "SId": "1", "SnId": "302346", "UsId": "23010",
      "TS": "2026-09-30", "Elv": "10", "Dist": "10", "EBV1": "40"}])
missing_velo = False
try:
    join_exports(ebv_only, SESSIONS)
except JoinError as exc:
    missing_velo = "Velo" in str(exc)
check("a file with EBV1 and no Velo is refused", missing_velo)

blank_usid_plays = csv_bytes(PLAY_FIELDS, [play(UsId="23010")])
blank_usid_sessions = csv_bytes(SESSION_FIELDS, [session_row(UsId="")])
fallback, _ = join_exports(blank_usid_plays, blank_usid_sessions)
fallback_rows = list(csv.DictReader(io.StringIO(fallback.decode())))
check("blank session UsId falls back to the play",
      fallback_rows and fallback_rows[0]["vendor_id"] == "23010")


# ---------------------------------------------------------------------------
section("4. pair selection — newest shared UTC stamp, not mtime")
# ---------------------------------------------------------------------------

NAMES = [
    "readme.txt",
    "PlaysExport_notes.CSV",
    "PlaysExport_2026-10-01-09-00-00_UTC.CSV",          # no session partner
    "SessionExport_2026-10-02-01-00-00_UTC.CSV",        # session without plays
    "PlaysExport_2026-09-28-07-30-19_UTC.CSV",
    "SessionExport_2026-09-28-07-30-19_UTC.csv",        # lowercase suffix
    "PlaysExport_2026-09-27-07-30-19_UTC.CSV",
    "SessionExport_2026-09-27-07-30-19_UTC.CSV",
]
pair = pull.select_pair(NAMES)
check("newest paired stamp wins (Sep 28, not Sep 27, not the unpaired Oct 1)",
      pair.stamp == "2026-09-28-07-30-19"
      and pair.plays == "PlaysExport_2026-09-28-07-30-19_UTC.CSV"
      and pair.session == "SessionExport_2026-09-28-07-30-19_UTC.csv",
      str(pair))
check("unpaired newer Plays is reported, not chosen",
      pair.skipped_plays == ["PlaysExport_2026-10-01-09-00-00_UTC.CSV"],
      str(pair.skipped_plays))

no_pair = False
try:
    pull.select_pair(["PlaysExport_2026-10-01-09-00-00_UTC.CSV", "notes.txt"])
except pull.NoMatchingPair as exc:
    no_pair = "no_matching_pair" in str(exc)
check("plays with no session partner is not a pair", no_pair)
check("row cap matches ingest.MAX_ROWS so sniff cannot silently truncate",
      MAX_PLAY_ROWS == ingest.MAX_ROWS)


# ---------------------------------------------------------------------------
section("5. SFTP settings — HITTRAX_SFTP_KEY, never PRIVATE_KEY")
# ---------------------------------------------------------------------------

from cryptography.hazmat.primitives import serialization          # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ed25519    # noqa: E402

_pem_key = ed25519.Ed25519PrivateKey.generate()
PEM = _pem_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.OpenSSH,
    encryption_algorithm=serialization.NoEncryption(),
).decode("ascii")


def _set_min_env():
    os.environ["HITTRAX_SFTP_HOST"] = "192.0.2.10"
    os.environ["HITTRAX_SFTP_USER"] = "moeller_datafeed"


_set_min_env()
os.environ["PRIVATE_KEY"] = "not-a-key-and-not-consulted"
os.environ["HITTRAX_SFTP_KEY"] = PEM
os.environ["HITTRAX_SFTP_PASSWORD"] = "should-not-win"
settings = pull.sftp_settings(load_env=False)
check("PEM contents in HITTRAX_SFTP_KEY parse",
      settings.pkey is not None and settings.host == "192.0.2.10")
check("password is fallback only — ignored when the key is set",
      settings.password is None)
check("PRIVATE_KEY garbage does not block HITTRAX_SFTP_KEY",
      settings.user == "moeller_datafeed")

os.environ["HITTRAX_SFTP_KEY"] = PEM.replace("\n", "\\n")
escaped = pull.sftp_settings(load_env=False)
check("literal \\\\n in HITTRAX_SFTP_KEY still parses (Railway one-line secret)",
      escaped.pkey is not None)

key_path = os.path.join(TMP, "id_ed25519")
with open(key_path, "w", encoding="utf-8") as fh:
    fh.write(PEM)
os.environ["HITTRAX_SFTP_KEY"] = key_path
from_path = pull.sftp_settings(load_env=False)
check("a local path in HITTRAX_SFTP_KEY is a dev convenience",
      from_path.pkey is not None)

os.environ.pop("HITTRAX_SFTP_KEY", None)
os.environ["HITTRAX_SFTP_PASSWORD"] = "sftp-secret"
os.environ["PRIVATE_KEY"] = PEM
password_settings = pull.sftp_settings(load_env=False)
check("password fallback does not read PRIVATE_KEY",
      password_settings.pkey is None and password_settings.password == "sftp-secret")

os.environ.pop("HITTRAX_SFTP_PASSWORD", None)
only_private = False
try:
    pull.sftp_settings(load_env=False)
except pull.ConfigError as exc:
    msg = str(exc)
    only_private = ("HITTRAX_SFTP_KEY" in msg and "PRIVATE_KEY is set and is not read" in msg)
check("PRIVATE_KEY alone is an error that names HITTRAX_SFTP_KEY", only_private)

os.environ["HITTRAX_SFTP_PORT"] = "2222"
os.environ["HITTRAX_SFTP_PATH"] = "/upload"
os.environ["HITTRAX_SFTP_KEY"] = PEM
os.environ.pop("PRIVATE_KEY", None)
ported = pull.sftp_settings(load_env=False)
check("port and path come from HITTRAX_SFTP_PORT / HITTRAX_SFTP_PATH",
      ported.port == 2222 and ported.path == "/upload")

os.environ.pop("HITTRAX_SFTP_PORT", None)
os.environ.pop("HITTRAX_SFTP_PATH", None)
defaults = pull.sftp_settings(load_env=False)
check("port defaults to 22 and path to the droplet upload dir",
      defaults.port == 22 and defaults.path == "/home/moeller_datafeed/upload")

os.environ["HITTRAX_SFTP_PORT"] = "nope"
bad_port = False
try:
    pull.sftp_settings(load_env=False)
except pull.ConfigError as exc:
    bad_port = "HITTRAX_SFTP_PORT" in str(exc)
check("a non-integer port is refused", bad_port)

os.environ.pop("HITTRAX_SFTP_PORT", None)
os.environ.pop("HITTRAX_MAX_PLAYS_BYTES", None)
check("Plays byte cap defaults to 50_000_000",
      pull.max_plays_bytes() == 50_000_000)
os.environ["HITTRAX_MAX_PLAYS_BYTES"] = "1000"
check("HITTRAX_MAX_PLAYS_BYTES overrides the cap", pull.max_plays_bytes() == 1000)
os.environ.pop("HITTRAX_MAX_PLAYS_BYTES", None)

doc = pull.__doc__ or ""
for token in ("HITTRAX_SFTP_HOST", "HITTRAX_SFTP_USER", "HITTRAX_SFTP_KEY",
              "HITTRAX_SFTP_PORT", "HITTRAX_SFTP_PATH", "HITTRAX_SFTP_PASSWORD",
              "HITTRAX_MAX_PLAYS_BYTES", "DATABASE_URL", "15 8 * * *",
              "python hittrax/pull_export.py --commit"):
    check(f"entrypoint docstring lists {token}", token in doc)
check("entrypoint says PRIVATE_KEY is not read", "PRIVATE_KEY is not read" in doc)
src = open(os.path.join(HERE, "hittrax", "pull_export.py"), encoding="utf-8").read()
check("SSH agent and default identity files are disabled",
      "look_for_keys=False" in src and "allow_agent=False" in src)
check("the key is read from HITTRAX_SFTP_KEY",
      'os.environ.get("HITTRAX_SFTP_KEY"' in src)


# ---------------------------------------------------------------------------
section("6. load — cage hitting, no new players, sha skip, job_runs")
# ---------------------------------------------------------------------------

with ENGINE.begin() as conn:
    CONNOR = conn.execute(insert(db.players).values(
        slug=db.slugify("Connor", "Weckesser"),
        first_name="Connor", last_name="Weckesser", is_active=True,
    )).inserted_primary_key[0]
    MATT = conn.execute(insert(db.players).values(
        slug=db.slugify("Matt", "Ponatoski"),
        first_name="Matt", last_name="Ponatoski", is_active=True,
    )).inserted_primary_key[0]
    conn.execute(insert(db.player_vendor_ids).values(
        player_id=CONNOR, vendor="hittrax", vendor_id="23010"))

players_before = n_rows(db.players)
vendors_before = n_rows(db.player_vendor_ids)

dry_rc, dry_out = capture(lambda: load(
    ENGINE, PLAYS, SESSIONS, commit=False,
    plays_name="PlaysExport_2026-09-30-07-30-19_UTC.CSV",
    session_name="SessionExport_2026-09-30-07-30-19_UTC.CSV",
    filename="hittrax_2026-09-30-07-30-19_joined.csv"))
check("dry run exits 0", dry_rc == 0, dry_out)
check("dry run writes no session and no job_runs row",
      n_rows(db.sessions) == 0 and n_rows(db.job_runs) == 0)
check("dry run still stores the joined bytes as pending",
      n_rows(db.raw_imports, status="pending") == 1)
check("dry run does not queue names (commit is the write)",
      n_rows(db.name_review) == 0)

rc, out = capture(lambda: load(
    ENGINE, PLAYS, SESSIONS, commit=True,
    plays_name="PlaysExport_2026-09-30-07-30-19_UTC.CSV",
    session_name="SessionExport_2026-09-30-07-30-19_UTC.CSV",
    filename="hittrax_2026-09-30-07-30-19_joined.csv"))
check("commit exits 0", rc == 0, out)
check("two sessions (two SnIds), not one per day and not the decoy",
      n_rows(db.sessions) == 2)
check("Connor has 6 measurements (2 swings × 3 metrics), not the unmatched row",
      n_rows(db.swings, player_id=CONNOR) == 6,
      str(n_rows(db.swings, player_id=CONNOR)))
check("Matt resolved by name with an unknown vendor id",
      n_rows(db.swings, player_id=MATT) == 3)
check("no player was created", n_rows(db.players) == players_before)
check("vendor ids were not learned or invented",
      n_rows(db.player_vendor_ids) == vendors_before)
check("unknown name queued once",
      n_rows(db.name_review, vendor="hittrax", status="open") == 1)
with ENGINE.connect() as conn:
    review = conn.execute(select(db.name_review.c.raw_name)).all()
    refs = {r.source_ref: (r.source, r.session_type, str(r.session_date))
            for r in conn.execute(select(
                db.sessions.c.source_ref, db.sessions.c.source,
                db.sessions.c.session_type, db.sessions.c.session_date))}
    evs = [r.value for r in conn.execute(
        select(db.swings.c.value).where(
            (db.swings.c.player_id == CONNOR) &
            (db.swings.c.metric_key == "exit_velocity")).order_by(db.swings.c.id))]
    maps = {r.source_column: (r.metric_key, r.scale)
            for r in conn.execute(select(db.column_maps).where(
                db.column_maps.c.vendor == "hittrax"))}
    imp = conn.execute(select(db.raw_imports.c.side, db.raw_imports.c.session_type,
                              db.raw_imports.c.status).where(
        db.raw_imports.c.vendor == "hittrax")).first()
check("queued name is Nobody Special, not Wrong Guy",
      [r.raw_name for r in review] == ["Nobody Special"],
      str([r.raw_name for r in review]))
check("source_ref is {player}|{SnId} — two sessions on one date stay apart",
      refs.get(f"{CONNOR}|302346") == ("hittrax", "cage", "2026-09-30")
      and refs.get(f"{MATT}|302347") == ("hittrax", "cage", "2026-09-30"),
      str(refs))
check("stored exit velocity is the converted Velo, scale already applied",
      abs(evs[0] - 22.3694) < 1e-6 and evs[0] < 30, str(evs))
check("column map scale is 1.0 for every collect column",
      all(scale == 1.0 for _key, scale in maps.values())
      and maps.get("exit_velocity") == ("exit_velocity", 1.0)
      and "EBV1" not in maps,
      str(maps))
check("import is hittrax / hitting side / cage and committed",
      imp is not None and imp.side == "hitting" and imp.session_type == "cage"
      and imp.status == "committed", str(imp))
with ENGINE.connect() as conn:
    job = conn.execute(select(db.job_runs).order_by(db.job_runs.c.id.desc())).first()
summary = job.summary if not isinstance(job.summary, str) else json.loads(job.summary)
check("job_runs records hittrax_daily",
      job is not None and job.job == JOB_NAME and job.ok is True
      and summary.get("plays_file", "").startswith("PlaysExport_")
      and summary.get("skipped_duplicate") is False,
      str(summary)[:300])
check("change detection ran",
      isinstance(summary.get("changes"), dict) and "fired" in summary["changes"],
      str(summary.get("changes")))

sessions_after = n_rows(db.sessions)
swings_after = n_rows(db.swings)
dup_rc, dup_out = capture(lambda: load(
    ENGINE, PLAYS, SESSIONS, commit=True,
    plays_name="PlaysExport_2026-09-30-07-30-19_UTC.CSV",
    session_name="SessionExport_2026-09-30-07-30-19_UTC.CSV",
    filename="hittrax_2026-09-30-07-30-19_joined.csv"))
check("identical joined bytes exit 0", dup_rc == 0, dup_out)
check("log says skipped_duplicate", "skipped_duplicate" in dup_out, dup_out)
check("duplicate does not write sessions or swings again",
      n_rows(db.sessions) == sessions_after and n_rows(db.swings) == swings_after)
with ENGINE.connect() as conn:
    dup_job = conn.execute(
        select(db.job_runs).order_by(db.job_runs.c.id.desc())).first()
dup_summary = (dup_job.summary if not isinstance(dup_job.summary, str)
               else json.loads(dup_job.summary))
check("duplicate job_runs row is ok and flagged skipped_duplicate",
      dup_job.ok is True and dup_summary.get("skipped_duplicate") is True)


# ---------------------------------------------------------------------------
section("7. mocked SFTP — select, size gate, row gate, then a real commit")
# ---------------------------------------------------------------------------

imports_before = n_rows(db.raw_imports)


def _pair_files(stamp, plays_rows, session_rows):
    return {
        f"PlaysExport_{stamp}_UTC.CSV": csv_bytes(PLAY_FIELDS, plays_rows),
        f"SessionExport_{stamp}_UTC.CSV": csv_bytes(SESSION_FIELDS, session_rows),
    }


huge_stamp = "2026-08-01-07-30-19"
huge_files = _pair_files(
    huge_stamp,
    [play(SnId="1", Velo="10")],
    [session_row(Id="1", UserName="Connor Weckesser", UsId="23010")])
huge = FakeSFTP(huge_files, sizes={
    f"PlaysExport_{huge_stamp}_UTC.CSV": 50_000_001})
huge_rc, huge_out = capture(lambda: pull.run(
    commit=True, sftp=huge, remote_dir="/home/moeller_datafeed/upload",
    engine=ENGINE, dest_dir=os.path.join(TMP, "huge")))
check("oversized Plays exits non-zero", huge_rc == 1, huge_out)
check("log says oversized_export_skipped size=",
      "oversized_export_skipped size=50000001" in huge_out, huge_out)
check("oversized Plays is not downloaded", huge.opened == [], str(huge.opened))
check("oversized Plays is not stored", n_rows(db.raw_imports) == imports_before)
with ENGINE.connect() as conn:
    huge_job = conn.execute(
        select(db.job_runs).order_by(db.job_runs.c.id.desc())).first()
huge_summary = (huge_job.summary if not isinstance(huge_job.summary, str)
                else json.loads(huge_job.summary))
check("oversized run records hittrax_daily ok=false",
      huge_job.job == JOB_NAME and huge_job.ok is False
      and "oversized_export_skipped" in (huge_summary.get("error") or ""))

row_stamp = "2026-08-02-07-30-19"
row_files = _pair_files(row_stamp, [
    play(Id="1", SnId="9", Velo="1"),
    play(Id="2", SnId="9", Velo="2"),
], [session_row(Id="9", UserName="Connor Weckesser", UsId="23010")])
row_sftp = FakeSFTP(row_files)
row_rc, row_out = capture(lambda: pull.run(
    commit=True, sftp=row_sftp, remote_dir="/upload", engine=ENGINE,
    dest_dir=os.path.join(TMP, "rows"), max_rows=1))
check("row estimate over the cap exits non-zero and does not store",
      row_rc == 1 and "oversized_export_skipped" in row_out and "rows=" in row_out
      and n_rows(db.raw_imports) == imports_before,
      row_out)

none_sftp = FakeSFTP({
    "PlaysExport_2026-08-03-07-30-19_UTC.CSV": b"Id\n1\n",
})
none_rc, none_out = capture(lambda: pull.run(
    commit=True, sftp=none_sftp, remote_dir="/upload", engine=ENGINE,
    dest_dir=os.path.join(TMP, "none")))
check("no session partner exits non-zero",
      none_rc == 1 and "no_matching_pair" in none_out, none_out)

# Newest Plays has no session. The older complete pair is the one to load.
# The unpaired file's stat is enormous; selecting it would trip the size gate.
new_stamp = "2026-09-28-07-30-19"
old_stamp = "2026-09-27-07-30-19"
unpaired = "PlaysExport_2026-10-01-09-00-00_UTC.CSV"
good = _pair_files(new_stamp, [
    play(Id="9", SId="111", SnId="400100", Velo="12", Elv="8", Dist="20",
         TS="2026-09-28 12:00:00", EBV1="999"),
], [session_row(Id="400100", UserName="Connor Weckesser", UsId="23010")])
older = _pair_files(old_stamp, [
    play(Id="8", SnId="400099", Velo="1", TS="2026-09-27 12:00:00"),
], [session_row(Id="400099", UserName="Connor Weckesser", UsId="23010")])
files = {unpaired: b"not-downloaded"}
files.update(good)
files.update(older)
files["readme.txt"] = b"ignore"
live = FakeSFTP(files, sizes={unpaired: 700_000_000})
live_rc, live_out = capture(lambda: pull.run(
    commit=True, sftp=live, remote_dir="/home/moeller_datafeed/upload",
    engine=ENGINE, dest_dir=os.path.join(TMP, "live")))
check("mocked pull of the newest complete pair exits 0", live_rc == 0, live_out)
check("unpaired newer Plays was skipped and not opened",
      "skip unpaired plays" in live_out and unpaired not in live.opened,
      str(live.opened))
check("opened the Sep 28 pair, not the older Sep 27 pair",
      f"PlaysExport_{new_stamp}_UTC.CSV" in live.opened
      and f"SessionExport_{new_stamp}_UTC.CSV" in live.opened
      and f"PlaysExport_{old_stamp}_UTC.CSV" not in live.opened,
      str(live.opened))
check("still no new players after the pull", n_rows(db.players) == players_before)
with ENGINE.connect() as conn:
    pulled = conn.execute(select(db.sessions.c.source_ref).where(
        db.sessions.c.source_ref == f"{CONNOR}|400100")).first()
    not_older = conn.execute(select(db.sessions.c.source_ref).where(
        db.sessions.c.source_ref == f"{CONNOR}|400099")).first()
    live_job = conn.execute(
        select(db.job_runs).order_by(db.job_runs.c.id.desc())).first()
live_summary = (live_job.summary if not isinstance(live_job.summary, str)
                else json.loads(live_job.summary))
check("pulled session is keyed by SnId 400100",
      pulled is not None and not_older is None)
check("pull job_runs names the remote files",
      live_job.ok is True and live_job.job == JOB_NAME
      and live_summary.get("plays_file") == f"PlaysExport_{new_stamp}_UTC.CSV"
      and live_summary.get("session_file") == f"SessionExport_{new_stamp}_UTC.CSV",
      str(live_summary)[:400])

check("seed is idempotent", seed.seed_hittrax_column_maps(ENGINE) == 0)


def test_hittrax_suite():
    """pytest entry. The checks above already ran at import."""
    assert not FAILS, FAILS


if __name__ == "__main__":
    print()
    if FAILS:
        print(f"{len(FAILS)} check(s) FAILED:")
        for label in FAILS:
            print(f"  - {label}")
        sys.exit(1)
    print("all hittrax checks passed\n")
