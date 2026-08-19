"""
Rapsodo Cloud API client.

Three-call chain (see RECON.md for the full field reference):
    1. /v3/reports                      -> players active in a date window
    2. /v2/session/byPlayerId/<id>      -> that player's sessions  (param: beginDate!)
    3. /v2/shots/<type>/bySessionId/... -> the actual per-shot rows

Token handling: the Rapsodo JWT lives 30 days, so we cache it rather than logging in
every run. Resolution order: RAPSODO_TOKEN env var, then token.json in the project root.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_URL = "https://cloud.rapsodo.com"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILE = PROJECT_ROOT / "token.json"

# The UI sends 10; the parameter is caller-controlled and larger values work.
PAGE_SIZE = 100

# Rapsodo splits pitching and hitting into separate result sets. Pulling only one
# silently loses half the data, so every sweep iterates both.
SHOT_TYPES = ("pitch", "hit")

# Be polite to an API we don't own.
REQUEST_PAUSE_SEC = 0.25


class RapsodoAuthError(RuntimeError):
    pass


def to_epoch(d: datetime) -> int:
    """Rapsodo takes epoch SECONDS (not milliseconds)."""
    return int(d.replace(tzinfo=timezone.utc).timestamp())


def _decode_jwt_claims(token: str) -> dict:
    """Decode the JWT payload without verifying -- we only want `exp`."""
    payload = token.replace("JWT ", "", 1).split(".")[1]
    payload += "=" * (-len(payload) % 4)  # restore stripped base64 padding
    return json.loads(base64.urlsafe_b64decode(payload))


def load_dotenv() -> None:
    """Minimal .env reader -- avoids a dependency for four keys."""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _days_left(token: str) -> float:
    exp = _decode_jwt_claims(token).get("exp")
    return float("inf") if not exp else (exp - time.time()) / 86400


def _read_cached_token() -> str:
    """Whatever is on disk, in either shape we might have written it."""
    raw = os.environ.get("RAPSODO_TOKEN", "")
    if not raw and TOKEN_FILE.exists():
        text = TOKEN_FILE.read_text(encoding="utf-8-sig").strip()
        # Accept the whole localStorage value -- {"created":..,"data":"JWT .."} --
        # or a bare token pasted on its own.
        try:
            raw = json.loads(text).get("data", "")
        except json.JSONDecodeError:
            raw = text.strip('"')
    if not raw:
        return ""
    # localStorage stores the value already prefixed with 'JWT '. Don't double it.
    return raw if raw.startswith("JWT ") else f"JWT {raw}"


def login() -> str:
    """
    Authenticate with email/password and cache the resulting JWT.

    This is what makes the pipeline unattended: the token Rapsodo issues lasts 30
    days, so without this every run would eventually fail and need a human to paste
    a fresh one out of the browser.
    """
    email = os.environ.get("RAPSODO_EMAIL", "").strip()
    password = os.environ.get("RAPSODO_PASSWORD", "")
    if not email or not password:
        raise RapsodoAuthError(
            "No usable Rapsodo token and no credentials to get one.\n"
            f"Put RAPSODO_EMAIL and RAPSODO_PASSWORD in {PROJECT_ROOT / '.env'} "
            "(gitignored), or set them as environment variables."
        )

    resp = requests.post(
        f"{BASE_URL}/v3/auth/login",
        json={"email": email, "password": password},
        headers={"Accept": "application/json", "Referer": f"{BASE_URL}/login"},
        timeout=60,
    )
    if resp.status_code >= 400:
        # Rapsodo answers a bad login with 400 {"success":false,
        # "message":"invalid_email_or_password"} rather than a 401.
        try:
            msg = resp.json().get("message", resp.text[:200])
        except ValueError:
            msg = resp.text[:200]
        raise RapsodoAuthError(f"Login failed ({resp.status_code}): {msg}")

    body = resp.json()
    token = _find_token(body)
    if not token:
        raise RapsodoAuthError(
            f"Login succeeded but no token found in the response. Keys: {list(body)}"
        )

    token = token if token.startswith("JWT ") else f"JWT {token}"
    TOKEN_FILE.write_text(
        json.dumps({"created": int(time.time() * 1000), "data": token})
    )
    print(f"[auth] logged in as {email}; token good for {_days_left(token):.0f} days")
    return token


def _find_token(body) -> str:
    """
    Pull the JWT out of the login response without assuming its exact shape --
    the field has been seen nested under `data` on this API.
    """
    if isinstance(body, str):
        return body if body.count(".") == 2 else ""
    if isinstance(body, dict):
        for key in ("token", "accessToken", "access_token", "jwt", "idToken"):
            val = body.get(key)
            if isinstance(val, str) and val.count(".") == 2:
                return val
        for val in body.values():
            if isinstance(val, (dict, list, str)):
                found = _find_token(val)
                if found:
                    return found
    if isinstance(body, list):
        for item in body:
            found = _find_token(item)
            if found:
                return found
    return ""


def load_token(min_days: float = 2.0) -> str:
    """
    Return a usable Authorization header value, logging in if needed.

    Refreshes early rather than on expiry -- a token that dies mid-backfill is a
    worse failure than one re-issued a couple of days sooner than strictly needed.
    """
    load_dotenv()

    cached = _read_cached_token()
    if cached:
        left = _days_left(cached)
        if left > min_days:
            return cached
        print(f"[auth] cached token has {max(left, 0):.1f} days left -- refreshing")

    return login()


@dataclass
class RapsodoClient:
    token: str

    @classmethod
    def from_env(cls) -> "RapsodoClient":
        return cls(token=load_token())

    def __post_init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": self.token,
                "Accept": "application/json",
                "Referer": f"{BASE_URL}/data",
            }
        )

    def _get(self, path: str, params: dict | None = None) -> dict:
        resp = self.session.get(f"{BASE_URL}{path}", params=params or {}, timeout=60)
        if resp.status_code == 401:
            raise RapsodoAuthError(f"401 from {path} -- token rejected or expired.")
        resp.raise_for_status()
        time.sleep(REQUEST_PAUSE_SEC)

        body = resp.json()
        if body.get("success") is False:
            raise RuntimeError(f"Rapsodo returned success=false for {path}: {body}")
        return body

    # -- 1. players ------------------------------------------------------------
    def fetch_players(self, start: datetime, end: datetime) -> list[dict]:
        """Every player with activity in the window, following pagination."""
        base = {
            "startDate": to_epoch(start),
            "endDate": to_epoch(end),
            "orderBy": "lastSessionDate",
            "orderType": "desc",
            "pageSize": PAGE_SIZE,
        }

        rows: list[dict] = []
        page = 1
        while True:
            body = self._get("/v3/reports", {**base, "currentPage": page})
            batch = body.get("data", [])
            rows.extend(batch)

            total = body.get("totalCount")
            if not batch or total is None or len(rows) >= total:
                break
            page += 1

        return rows

    # -- 2. sessions -----------------------------------------------------------
    def fetch_sessions(
        self, player_id: int, shot_type: str, start: datetime, end: datetime
    ) -> list[dict]:
        """
        Sessions for one player and one shot type.

        NOTE the parameter is `beginDate` at this level, not `startDate` -- using the
        wrong name returns a bad window silently instead of erroring.
        """
        body = self._get(
            f"/v2/session/byPlayerId/{player_id}",
            {
                "shotType": shot_type,
                "beginDate": to_epoch(start),
                "endDate": to_epoch(end),
                "sessionTypes": "",
                "hitPlacements": "",
                "deviceName": "",
            },
        )
        # VERIFIED 2026-08-19: this endpoint's envelope is {success, sessions,
        # dataCount} -- the list is under `sessions`, NOT `data` like /v3/reports.
        # Keep the `data` fallback in case the shape ever converges.
        return body.get("sessions") or body.get("data") or []

    # -- 3. shots --------------------------------------------------------------
    def fetch_shots(self, session_id: str, player_id: int, shot_type: str) -> list[dict]:
        """Per-shot rows. Top-level key is `shots`, not `data`."""
        body = self._get(
            f"/v2/shots/{shot_type}/bySessionId/{session_id}",
            {"playerId": player_id},
        )
        return body.get("shots", [])

    def fetch_session_types(self) -> dict:
        """Session-type taxonomy lookup."""
        return self._get("/v2/session/types")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Probe the Rapsodo API chain.")
    ap.add_argument("--start", default="2025-08-19", help="YYYY-MM-DD")
    ap.add_argument("--end", default="2026-08-19", help="YYYY-MM-DD")
    args = ap.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d")
    end = datetime.strptime(args.end, "%Y-%m-%d")

    client = RapsodoClient.from_env()
    players = client.fetch_players(start, end)
    print(f"{len(players)} players active between {args.start} and {args.end}")

    for row in players[:3]:
        p = row.get("player", {})
        pid = p.get("_id")
        name = f"{p.get('firstName')} {p.get('lastName')}"
        for st in SHOT_TYPES:
            sessions = client.fetch_sessions(pid, st, start, end)
            print(f"  {name:<22} {st:<6} {len(sessions)} sessions")
