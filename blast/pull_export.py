"""
blast/pull_export.py -- fetch the Blast "All swings by player" CSV from WIN Reality.

    python blast/pull_export.py --discover     log in and report what the page offers
    python blast/pull_export.py                download the CSV, dry-run the load
    python blast/pull_export.py --commit       download and write to the database

This is the port load_export.py's docstring said was coming. Blast Connect was
absorbed into WIN Reality: blastconnect.com and moeller-high-school.blastconnect.com
both 301 to teams.winreality.com, and the v3 API the 2024 R script used
(`/api/v3/insights/{player_id}`) went with them. /insights now redirects to a
login form, so this drives a real browser session instead.

CREDENTIALS NEVER LIVE IN THIS FILE.
Put them in `Player_Dev_Hub/.env` (gitignored), same file and reader the Rapsodo
job uses:

    BLAST_EMAIL=...
    BLAST_PASSWORD=...

WHY A BROWSER AND NOT REQUESTS
------------------------------
The login is a JS app; there is no documented auth endpoint and no API key. If
--discover turns up a JSON endpoint that returns swings directly, prefer it and
retire the clicking -- an intercepted API is far less brittle than a UI. That is
exactly what --discover exists to find out, and why it logs every XHR.

WHY IT IS ADAPTIVE ABOUT THE EXPORT CONTROL
-------------------------------------------
The export UI was never seen while this was written, so rather than pin one CSS
selector that may not exist, it tries several plain-language strategies and, when
none match, writes a screenshot and an HTML dump and tells you what it DID see.
A failure here should hand you the information to fix it in one pass, not a
stack trace.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
EXPORTS = HERE / "exports"
DIAG = HERE / "diagnostics"

LOGIN = "https://teams.winreality.com/login"
INSIGHTS = "https://teams.winreality.com/insights"

# Blast's own export starts at the beginning of the training year. The loader is
# idempotent -- raw_imports is sha256-deduped and sessions dedupe on source_ref --
# so pulling the full cumulative file weekly only adds what is new.
SEASON_START = date(2026, 1, 1)


def load_dotenv() -> None:
    """Same minimal reader rapsodo_client uses -- one .env for the whole repo."""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def credentials() -> tuple[str, str]:
    load_dotenv()
    email = (os.environ.get("BLAST_EMAIL") or "").strip()
    password = (os.environ.get("BLAST_PASSWORD") or "").strip()
    if not email or not password:
        sys.exit(
            "No Blast credentials.\n"
            f"Add these to {PROJECT_ROOT / '.env'} (gitignored):\n"
            "    BLAST_EMAIL=you@example.com\n"
            "    BLAST_PASSWORD=...\n"
        )
    return email, password


def _dump(page, tag: str) -> None:
    """Everything a human needs to fix a selector, without re-running blind."""
    DIAG.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    shot = DIAG / f"{tag}_{stamp}.png"
    html = DIAG / f"{tag}_{stamp}.html"
    try:
        page.screenshot(path=str(shot), full_page=True)
        html.write_text(page.content(), encoding="utf-8")
        print(f"  [diag] screenshot -> {shot}")
        print(f"  [diag] html       -> {html}")
    except Exception as e:                                  # noqa: BLE001
        print(f"  [diag] could not capture: {e}")


def sign_in(page, email: str, password: str) -> None:
    page.goto(LOGIN, wait_until="networkidle", timeout=60000)
    # The form labels its fields Email* / Password* but the email input is not
    # type=email, so match on placeholder/label rather than type.
    for sel in ("input[type=email]", "input[name*=mail i]",
                "input[placeholder*=mail i]", "input[type=text]"):
        if page.locator(sel).count():
            page.locator(sel).first.fill(email)
            break
    else:
        _dump(page, "login_no_email_field")
        sys.exit("Could not find the email field on the login page.")

    page.locator("input[type=password]").first.fill(password)
    page.locator("button[type=submit]").first.click()

    # The submit button spins while the request is in flight and the SPA does a
    # client-side route change rather than a document navigation, so
    # wait_for_load_state returns long before the outcome is known and a URL
    # check here reports a false "wrong password". Wait for the URL to actually
    # leave /login, and only treat a timeout as a real failure.
    try:
        page.wait_for_url(lambda u: "/login" not in u, timeout=90000)
    except Exception:                                       # noqa: BLE001
        page.wait_for_timeout(3000)
        if "/login" in page.url:
            body = ""
            try:
                body = page.inner_text("body")
            except Exception:                               # noqa: BLE001
                pass
            for msg in ("invalid", "incorrect", "not found", "try again",
                        "locked", "verify", "code"):
                if msg in body.lower():
                    _dump(page, "login_rejected")
                    sys.exit(f"Login rejected by the site: '...{msg}...' -- "
                             "check BLAST_EMAIL / BLAST_PASSWORD, and whether the "
                             "account needs a verification code.")
            _dump(page, "login_timeout")
            sys.exit("Sign-in did not complete within 90s and the page showed no "
                     "error. See the screenshot -- if the button is still "
                     "spinning the site is just slow; re-run.")
    page.wait_for_load_state("networkidle", timeout=60000)
    print(f"  signed in, landed on {page.url}")


def discover(page, api_log: list[str]) -> None:
    page.goto(INSIGHTS, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(4000)          # let the SPA settle
    print(f"\n=== /insights as a signed-in user ===")
    print(f"  url   : {page.url}")
    print(f"  title : {page.title()}")

    print("\n=== controls that look like an export ===")
    words = ["export", "download", "csv", "report", "all swings"]
    seen = []
    for w in words:
        for role in ("button", "link", "menuitem"):
            try:
                loc = page.get_by_role(role, name=__import__("re").compile(w, 2))
                for i in range(min(loc.count(), 5)):
                    t = (loc.nth(i).inner_text() or "").strip().replace("\n", " ")
                    if t and t not in seen:
                        seen.append(t)
                        print(f"  <{role}> {t[:80]}")
            except Exception:                               # noqa: BLE001
                pass
    if not seen:
        print("  (none found by name -- see the screenshot and html dump)")
        _dump(page, "insights_no_export_control")

    print("\n=== XHR/API calls the page made ===")
    for a in sorted(set(api_log))[:40]:
        print("  " + a)
    if not api_log:
        print("  (none captured)")
    print("\nIf one of those returns swing rows, say so and the browser step can go away.")


def download_csv(page) -> Path | None:
    """Export the raw-swing CSV from the embedded QuickSight dashboard.

    The Insights page is an AWS QuickSight embed. The swing-level data lives on a
    sheet tab called "All Swings (non events)", whose visual is titled "All swings
    by player" -- and that visual's CSV export is exactly the schema load_export.py
    expects (captureid, Swing Timestamp, ... Bat Speed (MPH), ...). Everything --
    tabs, visual, export menu -- is INSIDE the cross-origin QuickSight iframe, so
    each step is frame-scoped, and QuickSight renders on a canvas so its tab needs
    a force click (it fails Playwright's actionability check) and its per-visual
    menu only appears on hover.
    """
    import re
    page.goto(INSIGHTS, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(7000)

    qs = next((f for f in page.frames if "quicksight" in f.url), None)
    if not qs:
        _dump(page, "no_quicksight_frame")
        print("  Could not find the QuickSight frame on /insights.")
        return None

    # 1. Switch to the swing-level sheet. force=True: QuickSight tabs are canvas
    #    controls that never satisfy the default actionability wait.
    try:
        qs.get_by_role("tab", name="All Swings (non events)").click(force=True, timeout=30000)
    except Exception as e:                                  # noqa: BLE001
        _dump(page, "swing_tab_not_found")
        print(f"  Could not select the swing tab: {type(e).__name__}")
        return None

    # 2. Wait for that sheet's visual to render, then hover it to reveal the
    #    per-visual toolbar (the ... "Menu options" button).
    try:
        title = qs.locator("[aria-label*='visual title' i]").filter(
            has_text=re.compile("all swings by player", re.I))
        title.first.wait_for(timeout=45000)
        title.first.hover()
    except Exception:                                       # noqa: BLE001
        # Fall back to hovering the visual title by text; some builds label it
        # slightly differently.
        try:
            qs.get_by_text(re.compile("all swings by player", re.I)).first.hover()
        except Exception as e:                              # noqa: BLE001
            _dump(page, "swing_visual_not_found")
            print(f"  Swing visual never rendered: {type(e).__name__}")
            return None
    page.wait_for_timeout(2500)

    # 3. Open Menu options and Export to CSV, catching the download. .last is the
    #    swing table's menu when more than one visual carries a toolbar.
    menu = qs.locator("[aria-label*='Menu options' i]")
    try:
        menu.last.wait_for(timeout=20000)
        menu.last.click()
    except Exception as e:                                  # noqa: BLE001
        _dump(page, "menu_options_not_found")
        print(f"  The visual's export menu did not appear: {type(e).__name__}")
        return None

    page.wait_for_timeout(1200)
    try:
        with page.expect_download(timeout=120000) as dl:
            qs.get_by_role("menuitem", name=re.compile("export to csv", re.I)).first.click()
        got = dl.value
    except Exception as e:                                  # noqa: BLE001
        _dump(page, "export_click_failed")
        print(f"  'Export to CSV' did not produce a download: {type(e).__name__}")
        return None

    EXPORTS.mkdir(exist_ok=True)
    # Normalise to Blast's own naming so load_export.py's glob still matches, and
    # stamp it so successive weekly pulls do not overwrite each other.
    dest = EXPORTS / f"All_swings_by_player_{int(time.time()*1000)}.csv"
    got.save_as(str(dest))
    size = dest.stat().st_size
    print(f"  downloaded -> {dest.name} ({size:,} bytes)")
    if size < 2000:
        print("  ⚠ that file is suspiciously small -- it may be the aggregated view,"
              " not raw swings. Check the header before trusting a --commit.")
    return dest


def load(csv_path: Path, commit: bool) -> int:
    cmd = [sys.executable, str(HERE / "load_export.py"), str(csv_path)]
    if commit:
        cmd.append("--commit")
    print(f"\n=== {' '.join(cmd[1:])} ===")
    rc = subprocess.call(cmd, cwd=str(PROJECT_ROOT))
    if rc == 0 and commit:
        detect_after_load()
    return rc


def detect_after_load() -> None:
    """Run change detection on what just landed.

    Without this, Monday's swings sit in the database until the nightly job
    reaches them, and the "What changed" feed lags a day. Isolated so a
    detection error cannot turn a successful load into a failed run -- the
    data is in; detection can be re-run.
    """
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        import changes
        import db
        res = changes.compute_all(db.get_engine(), write=True)
        res.pop("events", None)
        print(f"\n=== change detection after load: {res} ===")
    except Exception as e:                                  # noqa: BLE001
        print(f"\n=== change detection after load FAILED (data is loaded; "
              f"re-run detection): {type(e).__name__}: {e} ===")


def main(argv: list[str]) -> int:
    want_discover = "--discover" in argv
    commit = "--commit" in argv
    headed = "--headed" in argv          # watch it work, for debugging

    email, password = credentials()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright is not installed.\n"
                 "    pip install playwright && playwright install chromium")

    api_log: list[str] = []

    def note(resp):
        u = resp.url
        if any(k in u for k in ("/api/", "graphql", ".json", "export", "csv")):
            if "weglot" not in u and "settings" not in u:
                api_log.append(f"{resp.status}  {resp.request.method:<5} {u[:140]}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        page.on("response", note)
        try:
            sign_in(page, email, password)
            if want_discover:
                discover(page, api_log)
                return 0
            csv_path = download_csv(page)
        finally:
            browser.close()

    if not csv_path:
        return 1
    if not commit:
        print("\nDownloaded only. Re-run with --commit to write it to the database.")
    return load(csv_path, commit)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
