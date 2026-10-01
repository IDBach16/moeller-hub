"""
hittrax/pull_export.py -- pull the newest HitTrax Plays+Session pair and load it.

    python hittrax/pull_export.py              download and dry-run the load
    python hittrax/pull_export.py --dry-run    same
    python hittrax/pull_export.py --commit     download and write to the database

Railway service ``hittrax-cron`` start command::

    python hittrax/pull_export.py --commit

Cron (set on the service instance, not in a committed railway.json)::

    15 8 * * *

08:15 UTC, about 45 minutes after the droplet's usual ~07:30 UTC drop, and
before the 09:00 UTC Rapsodo nightly. This job is its own service. It is not
a step inside nightly.py.

ENVIRONMENT
-----------
HITTRAX_SFTP_HOST          droplet address
HITTRAX_SFTP_USER          SFTP user
HITTRAX_SFTP_KEY           PEM private key contents. Not a filesystem path
                           required, and not PRIVATE_KEY. Literal ``\\n`` in
                           the value is accepted (Railway's one-line secret).
HITTRAX_SFTP_PORT          default 22
HITTRAX_SFTP_PATH          default /home/moeller_datafeed/upload
HITTRAX_SFTP_PASSWORD      optional fallback, used only when HITTRAX_SFTP_KEY
                           is unset. It is not a passphrase for the key.
HITTRAX_MAX_PLAYS_BYTES    default 50000000. A larger Plays file is not ingested.
DATABASE_URL               same Postgres as the portal

The private key variable is HITTRAX_SFTP_KEY. PRIVATE_KEY is not read.
``look_for_keys`` and the SSH agent are off, so an unrelated key on the box
cannot authenticate this cron in place of HITTRAX_SFTP_KEY.

Selection uses the UTC stamp in the filename, not mtime. Files accumulate on
the droplet and mtime lies. The newest PlaysExport whose stamp has a
SessionExport with that exact stamp is the pair. A newer Plays file with no
session partner is logged and skipped.

The cron is read-only on the droplet: list and get. It does not delete
exports. Joined CSV bytes are what raw_imports stores.
"""

from __future__ import annotations

import io
import os
import posixpath
import re
import sys
import traceback
from dataclasses import dataclass

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hittrax.join import MAX_PLAY_ROWS, OversizedExport
from hittrax.load_export import load, log, record_job

# PlaysExport_YYYY-MM-DD-HH-MM-SS_UTC.CSV — stamp is identical on the pair.
# Lexical order of that stamp is chronological order, which is why selection
# sorts the stamp string and never stats the file for mtime.
_EXPORT_NAME = re.compile(
    r"^(PlaysExport|SessionExport)_(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})_UTC\.CSV$",
    re.IGNORECASE,
)

DEFAULT_REMOTE_PATH = "/home/moeller_datafeed/upload"
DEFAULT_MAX_PLAYS_BYTES = 50_000_000
DEFAULT_PORT = 22
LOCAL_DIR = "/tmp/hittrax"

# The key lives in HITTRAX_SFTP_KEY. This name is refused on purpose so a
# template that says PRIVATE_KEY cannot silently become the credential.
_REJECTED_KEY_ENV = "PRIVATE_KEY"


class ConfigError(Exception):
    """Missing or unreadable SFTP settings. The message names the env var."""


class NoMatchingPair(Exception):
    """No PlaysExport stamp had a SessionExport with the same UTC stamp."""

    def __init__(self, skipped):
        self.skipped = list(skipped)
        if skipped:
            shown = ", ".join(skipped[:5])
            super().__init__(f"no_matching_pair skipped_unpaired={shown}")
        else:
            super().__init__(
                "no_matching_pair (no PlaysExport_/SessionExport_ UTC stamps)")


@dataclass
class Pair:
    stamp: str
    plays: str
    session: str
    skipped_plays: list


@dataclass
class SFTPSettings:
    host: str
    user: str
    port: int
    path: str
    pkey: object
    password: str | None


def load_dotenv():
    """Same reader as blast/pull_export.py. ``setdefault`` so Railway wins."""
    env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def max_plays_bytes():
    raw = os.environ.get("HITTRAX_MAX_PLAYS_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_PLAYS_BYTES
    try:
        limit = int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"HITTRAX_MAX_PLAYS_BYTES must be an integer, got {raw!r}") from exc
    if limit <= 0:
        raise ConfigError("HITTRAX_MAX_PLAYS_BYTES must be positive")
    return limit


def load_private_key(material):
    """Parse HITTRAX_SFTP_KEY. PEM contents, or a local file path for dev.

    Railway stores the PEM in the variable. A one-line value with literal
    backslash-n is what the dashboard produces for a multiline secret. An
    encrypted key is refused: HITTRAX_SFTP_PASSWORD is not a passphrase.
    """
    text = (material or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    if "BEGIN" not in text and os.path.isfile(text):
        # Dev convenience. The cron contract is the PEM text itself.
        with open(text, encoding="utf-8") as fh:
            text = fh.read()
    if "\\n" in text and "BEGIN" in text:
        text = text.replace("\\n", "\n")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "BEGIN" not in text:
        raise ConfigError(
            "HITTRAX_SFTP_KEY must be PEM private key contents "
            "(-----BEGIN ... PRIVATE KEY-----). PRIVATE_KEY is not read.")

    import paramiko
    buffer = io.StringIO(text)
    errors = []
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        buffer.seek(0)
        try:
            return cls.from_private_key(buffer)
        except paramiko.PasswordRequiredException as exc:
            raise ConfigError(
                "HITTRAX_SFTP_KEY is an encrypted PEM. Put an unencrypted "
                "private key in HITTRAX_SFTP_KEY. HITTRAX_SFTP_PASSWORD is "
                "an SFTP password fallback, not a key passphrase.") from exc
        except Exception as exc:                            # noqa: BLE001
            errors.append(f"{cls.__name__}: {type(exc).__name__}")
    raise ConfigError(
        "HITTRAX_SFTP_KEY is not a readable PEM private key "
        f"({'; '.join(errors)}). PRIVATE_KEY is not read.")


def sftp_settings(load_env=True):
    """Read the cron's environment. Does not look at PRIVATE_KEY."""
    if load_env:
        load_dotenv()
    host = os.environ.get("HITTRAX_SFTP_HOST", "").strip()
    user = os.environ.get("HITTRAX_SFTP_USER", "").strip()
    key = os.environ.get("HITTRAX_SFTP_KEY", "").strip()
    password = os.environ.get("HITTRAX_SFTP_PASSWORD", "").strip()
    path = os.environ.get("HITTRAX_SFTP_PATH", "").strip() or DEFAULT_REMOTE_PATH
    port_raw = os.environ.get("HITTRAX_SFTP_PORT", "").strip() or str(DEFAULT_PORT)
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ConfigError(
            f"HITTRAX_SFTP_PORT must be an integer, got {port_raw!r}") from exc
    if not 1 <= port <= 65535:
        raise ConfigError(f"HITTRAX_SFTP_PORT out of range: {port}")

    missing = []
    if not host:
        missing.append("HITTRAX_SFTP_HOST")
    if not user:
        missing.append("HITTRAX_SFTP_USER")
    if not key and not password:
        hint = ""
        if os.environ.get(_REJECTED_KEY_ENV, "").strip():
            hint = (" PRIVATE_KEY is set and is not read; put the PEM in "
                    "HITTRAX_SFTP_KEY.")
        missing.append(
            "HITTRAX_SFTP_KEY (PEM private key contents) or HITTRAX_SFTP_PASSWORD")
        raise ConfigError("missing " + ", ".join(missing) + "." + hint)
    if missing:
        raise ConfigError("missing " + ", ".join(missing) + ".")

    pkey = load_private_key(key) if key else None
    # Key wins. The password is only the fallback the service uses when no
    # HITTRAX_SFTP_KEY is configured.
    return SFTPSettings(
        host=host, user=user, port=port, path=path, pkey=pkey,
        password=None if pkey is not None else password)


def connect_sftp(load_env=True):
    settings = sftp_settings(load_env=load_env)
    import paramiko
    client = paramiko.SSHClient()
    # Host key is not pinned in v1. The cron can only list and download.
    # allow_agent and look_for_keys stay off so PRIVATE_KEY / ssh-agent
    # material cannot satisfy this login.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=settings.host,
        port=settings.port,
        username=settings.user,
        pkey=settings.pkey,
        password=settings.password,
        timeout=30,
        banner_timeout=30,
        auth_timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    return client, client.open_sftp(), settings


def select_pair(filenames):
    """Newest PlaysExport whose UTC stamp also has a SessionExport.

    ``filenames`` may be basenames or paths. mtime is not an input: the stamp
    in the name is the clock, because files accumulate and a copied file's
    mtime is whenever it was copied.
    """
    plays = {}
    sessions = {}
    for raw in filenames:
        base = os.path.basename(str(raw).rstrip("/"))
        match = _EXPORT_NAME.match(base)
        if not match:
            continue
        kind, stamp = match.group(1).lower(), match.group(2)
        bucket = plays if kind == "playsexport" else sessions
        bucket[stamp] = base
    skipped = []
    for stamp in sorted(plays, reverse=True):
        if stamp in sessions:
            return Pair(stamp, plays[stamp], sessions[stamp], skipped)
        skipped.append(plays[stamp])
    raise NoMatchingPair(skipped)


def row_estimate(raw):
    """Newline count minus the header. An estimate: quoted newlines over-count.

    Over-counting fails closed (skip a file that might be under the cap).
    Under-counting is caught again when the CSV is actually parsed.
    """
    if not raw:
        return 0
    return max(raw.count(b"\n") - 1, 0)


def _remote_size(sftp, path):
    try:
        st = sftp.stat(path)
    except Exception:                                        # noqa: BLE001
        return None
    size = getattr(st, "st_size", None)
    if size is None:
        return None
    return int(size)


def _read_remote(sftp, path, limit, name):
    """Download ``path``. Abort once ``limit`` bytes have been exceeded.

    ``limit`` None means no cap (the Session file). The Plays cap is checked
    from ``stat`` first so a 700 MB object is never fully read.
    """
    opener = getattr(sftp, "open", None)
    if opener is None:
        buffer = io.BytesIO()
        sftp.getfo(path, buffer)
        data = buffer.getvalue()
        if limit is not None and len(data) > limit:
            raise OversizedExport(name, size=len(data), limit=limit)
        return data
    chunks = []
    total = 0
    with sftp.open(path, "rb") as remote:
        while True:
            block = remote.read(1024 * 1024)
            if not block:
                break
            total += len(block)
            if limit is not None and total > limit:
                raise OversizedExport(name, size=total, limit=limit)
            chunks.append(block)
    return b"".join(chunks)


def download_pair(sftp, remote_dir, pair, max_bytes, max_rows):
    remote_dir = remote_dir.rstrip("/")
    plays_path = posixpath.join(remote_dir, pair.plays)
    session_path = posixpath.join(remote_dir, pair.session)
    plays_size = _remote_size(sftp, plays_path)
    if plays_size is not None and plays_size > max_bytes:
        raise OversizedExport(pair.plays, size=plays_size, limit=max_bytes)
    plays = _read_remote(sftp, plays_path, max_bytes, pair.plays)
    if len(plays) > max_bytes:
        raise OversizedExport(pair.plays, size=len(plays), limit=max_bytes)
    estimate = row_estimate(plays)
    if estimate > max_rows:
        raise OversizedExport(
            pair.plays, size=len(plays), limit=max_bytes,
            rows=estimate, row_limit=max_rows)
    session = _read_remote(sftp, session_path, None, pair.session)
    reported = plays_size if plays_size is not None else len(plays)
    return plays, session, reported, len(session)


def _record_failure(engine, commit, summary):
    if not commit or engine is None:
        return
    try:
        record_job(engine, False, summary)
    except Exception as exc:                                 # noqa: BLE001
        log(f"job_runs write failed: {type(exc).__name__}: {exc}")


def run(commit=False, sftp=None, remote_dir=None, engine=None, dest_dir=None,
        max_bytes=None, max_rows=None):
    """List, select, download, load. ``sftp`` is injectable for tests.

    Returns a process exit code.
    """
    import db as dbmod
    engine = engine or dbmod.get_engine()
    client = None
    close_sftp = False
    summary = {"stage": "pull", "dry_run": not commit, "skipped_duplicate": False}
    try:
        # Dotenv before the byte cap so a local .env can set
        # HITTRAX_MAX_PLAYS_BYTES. Injected SFTP (tests) skips this and
        # therefore cannot pick up a developer's droplet credentials.
        if sftp is None:
            load_dotenv()
        if max_bytes is None:
            max_bytes = max_plays_bytes()
        if max_rows is None:
            max_rows = MAX_PLAY_ROWS
        if sftp is None:
            client, sftp, settings = connect_sftp()
            close_sftp = True
            remote_dir = remote_dir or settings.path
        else:
            remote_dir = remote_dir or DEFAULT_REMOTE_PATH
        summary["remote_dir"] = remote_dir
        names = list(sftp.listdir(remote_dir))
        pair = select_pair(names)
        for skipped in pair.skipped_plays:
            log(f"skip unpaired plays file={skipped} "
                "(no SessionExport with the same UTC stamp)")
        log(f"selected stamp={pair.stamp} plays={pair.plays} session={pair.session}")
        plays, session, plays_size, session_size = download_pair(
            sftp, remote_dir, pair, max_bytes, max_rows)
        dest = dest_dir or LOCAL_DIR
        os.makedirs(dest, exist_ok=True)
        plays_local = os.path.join(dest, pair.plays)
        session_local = os.path.join(dest, pair.session)
        with open(plays_local, "wb") as fh:
            fh.write(plays)
        with open(session_local, "wb") as fh:
            fh.write(session)
        log(f"downloaded {plays_local} ({plays_size} bytes), "
            f"{session_local} ({session_size} bytes)")
        filename = f"hittrax_{pair.stamp}_joined.csv"
        return load(
            engine, plays, session, commit=commit,
            plays_name=pair.plays, session_name=pair.session,
            plays_size=plays_size, session_size=session_size,
            filename=filename)
    except (OversizedExport, NoMatchingPair, ConfigError) as exc:
        log(str(exc))
        summary["error"] = str(exc)
        _record_failure(engine, commit, summary)
        return 1
    except Exception as exc:                                 # noqa: BLE001
        log(f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        summary["error"] = f"{type(exc).__name__}: {exc}"
        _record_failure(engine, commit, summary)
        return 1
    finally:
        if close_sftp:
            try:
                sftp.close()
            except Exception:                                # noqa: BLE001
                pass
            if client is not None:
                try:
                    client.close()
                except Exception:                            # noqa: BLE001
                    pass


def main(argv):
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0
    commit = "--commit" in argv
    if "--dry-run" in argv and commit:
        log("pass only one of --commit and --dry-run")
        return 1
    if not commit:
        log("DRY RUN -- add --commit to write.")
    try:
        return run(commit=commit)
    except ConfigError as exc:
        log(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
