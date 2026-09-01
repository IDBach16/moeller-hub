"""
Moeller Baseball Analytics Hub.

Central landing page for the Moeller analytics tools, and -- from here on -- the
front end of the player-development system described in PLAYER_DEV_SPEC.md.

Structure (spec section 1.1):
    app.py       this file: the Flask app factory, auth gate, routes
    db.py        schema + engine for the player-development database
    metrics.py   the metric registry (polarity, thresholds, pitch types)
    seed.py      first-run roster / vendor-id seeding
    tools.py     the existing analytics tools, as data
    agent.py     the Coach Assistant
    templates/   base.html, home.html, login.html
    static/      images and the PWA manifest

Asset URLs are deliberately unchanged (/shield.png, not /static/shield.png):
the PWA manifest and cached clients reference the old paths.
"""

import os
import subprocess
from datetime import timedelta

from flask import (Flask, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)

import db
import metrics
import tools

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")


def _load_dotenv():
    """Read a local .env so `python app.py` behaves like the deployed service.

    setdefault, never overwrite: on Railway the platform supplies the real
    variables and there is no .env, so this is a no-op there. Locally it picks up
    ANTHROPIC_API_KEY, DATABASE_URL and the Rapsodo credentials from the same
    gitignored file the pipeline already uses -- otherwise the AI summary reports
    "no ANTHROPIC_API_KEY on the server" on a dev machine that has one.
    """
    path = os.path.join(APP_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()

# Empty = no gate. To require a password again, set HUB_PASSWORD on Railway.
#
# NOTE (spec section 11): the gate has been off since 2026-08-14. That is fine
# for a page of links. It is NOT fine once this app holds player-development
# records and coach notes -- turn it back on before any write endpoint ships.
HUB_PASSWORD = os.environ.get("HUB_PASSWORD", "")

# Routes that never require login (assets the login page itself needs).
PUBLIC_PATHS = {"/login", "/bg-field.jpg", "/shield.png", "/moeller-logo.png",
                "/favicon.ico", "/manifest.json"}

# Served at the repo root historically; they live in static/ now.
ROOT_ASSETS = {"bg-field.jpg", "shield.png", "moeller-logo.png",
               "field2.jpg", "manifest.json"}

WRITES_OFF = ("Uploads are disabled because the hub is public. Set HUB_PASSWORD "
              "on the Railway service to turn the coaches' login back on.")


def writes_enabled():
    """Spec section 11: endpoints that write player-development records must not
    be reachable on an open URL. The password gate is what makes them private.

    Local development is exempt -- there is no public URL to protect, and Ian
    needs to be able to map his first HitTrax export before deciding anything
    about the gate. RAILWAY_ENVIRONMENT is only set on Railway.
    """
    if HUB_PASSWORD:
        return True
    return not os.environ.get("RAILWAY_ENVIRONMENT")


def _engine():
    return db.get_engine()


def _metric_options():
    """Dropdown contents for the column-mapping UI: the structural roles first,
    then every registered metric grouped by side."""
    roles = [{"key": r, "label": r.replace("_", " ").title(), "group": "Column role"}
             for r in db.COLUMN_ROLES]
    mets = [{"key": m.key, "label": f"{m.label} ({m.unit})",
             "group": m.side.title()}
            for m in sorted(metrics.REGISTRY.values(),
                            key=lambda m: (m.side, m.label))]
    return roles + mets


def create_app():
    app = Flask(__name__, static_folder=STATIC_DIR, template_folder="templates")
    app.secret_key = os.environ.get("SECRET_KEY", "moeller-hub-2027-secret")
    app.permanent_session_lifetime = timedelta(days=30)

    # Build the schema and, on a genuinely empty database, seed the roster --
    # otherwise a fresh deploy comes up with no players and every player page is
    # a dead end. Guarded and idempotent; see seed.maybe_seed. Failure here must
    # never stop the hub serving, so the tool cards still work regardless.
    try:
        # Migrations first: create_all makes missing tables but never adds
        # columns to existing ones, so a schema change would otherwise never
        # reach a database that already exists. See migrate.py.
        import migrate
        ok, msg = migrate.upgrade_to_head()
        print(f"[startup] migrate: {msg}", flush=True)

        import seed
        seed.maybe_seed(db.get_engine())
    except Exception as e:                                   # pragma: no cover
        print(f"[startup] database not ready: {e}", flush=True)

    # Name -> slug for every player, so an agent reply can turn "Seth Maybury"
    # into a link to his profile. Small and slow-changing; cached in-process.
    _roster_cache = {"at": 0.0, "rows": []}

    @app.context_processor
    def inject_chat_roster():
        import time as _t
        from sqlalchemy import select as _select
        if _t.time() - _roster_cache["at"] > 300:
            try:
                with _engine().connect() as conn:
                    _roster_cache["rows"] = [
                        {"name": f"{r.first_name} {r.last_name}", "slug": r.slug}
                        for r in conn.execute(_select(
                            db.players.c.first_name, db.players.c.last_name,
                            db.players.c.slug))]
                _roster_cache["at"] = _t.time()
            except Exception:                               # noqa: BLE001
                pass                                        # links are a nicety
        return {"chat_roster": _roster_cache["rows"]}

    # -----------------------------------------------------------------------
    # Password gate
    # -----------------------------------------------------------------------

    @app.before_request
    def require_login():
        if not HUB_PASSWORD:
            return None
        if request.path in PUBLIC_PATHS:
            return None
        # A player's link carries its own credential in the token, and the kid
        # does not have the staff password. Gating it would make the whole
        # feature unusable the moment the gate goes back on.
        if request.path.startswith("/me/"):
            return None
        if not session.get("authed"):
            return redirect(url_for("login"))
        return None

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not HUB_PASSWORD:
            return redirect(url_for("index"))
        error = None
        if request.method == "POST":
            if request.form.get("password") == HUB_PASSWORD:
                session.permanent = True
                session["authed"] = True
                return redirect(url_for("index"))
            error = "Incorrect password"
        return render_template("login.html", error=error)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # -----------------------------------------------------------------------
    # Static assets, at their original root URLs
    # -----------------------------------------------------------------------

    # One explicit rule per asset rather than a /<catch-all>, which would
    # shadow the page routes Phase C adds (/players, /team, /tools...).
    def _make_asset_route(filename):
        def _serve():
            return send_from_directory(STATIC_DIR, filename)
        _serve.__name__ = "asset_" + filename.replace(".", "_").replace("-", "_")
        return _serve

    for _asset in ROOT_ASSETS:
        app.add_url_rule("/" + _asset, view_func=_make_asset_route(_asset))

    @app.route("/favicon.ico")
    def favicon():
        return send_from_directory(STATIC_DIR, "moeller-logo.png")

    # -----------------------------------------------------------------------
    # Coach Assistant
    # -----------------------------------------------------------------------

    @app.route("/api/agent", methods=["POST"])
    def api_agent():
        from agent import RateLimited, answer
        ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
              or request.remote_addr or "?")
        data = request.get_json(silent=True) or {}
        history = data.get("messages") or []
        if not isinstance(history, list) or len(history) > 40:
            return jsonify({"error": "bad request"}), 400
        try:
            return jsonify({"reply": answer(history, ip)})
        except RateLimited as e:
            return jsonify({"error": str(e)}), 429
        except Exception as e:
            return jsonify({"error": f"The assistant hit a snag: {e}"}), 500

    @app.route("/api/report-options")
    def api_report_options():
        """Rosters and seasons for the Quick Reports pickers. Loaded on first use --
        it parses the season CSV, so page loads don't pay for it."""
        try:
            import reports
            return jsonify(reports.report_options())
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # -----------------------------------------------------------------------
    # Git push endpoint
    # -----------------------------------------------------------------------

    @app.route("/api/git-push", methods=["POST"])
    def git_push():
        # The password gate is what kept this endpoint private. With the gate
        # off, it must not be reachable at all.
        if not HUB_PASSWORD:
            return jsonify({"ok": False,
                            "error": "disabled while the password gate is off"}), 403
        try:
            subprocess.run(["git", "add", "-A"], cwd=APP_DIR,
                           capture_output=True, text=True)
            msg = request.json.get("message", "auto-push") if request.is_json else "auto-push"
            subprocess.run(["git", "commit", "-m", msg], cwd=APP_DIR,
                           capture_output=True, text=True)
            result = subprocess.run(["git", "push"], cwd=APP_DIR,
                                    capture_output=True, text=True)
            return jsonify({"ok": True, "output": result.stdout or result.stderr})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    # -----------------------------------------------------------------------
    # Data Collection -- upload, column mapping, name review (spec section 8.4)
    # -----------------------------------------------------------------------

    @app.route("/collect")
    def collect():
        import ingest
        engine = _engine()
        return render_template(
            "collect.html",
            imports=ingest.recent_imports(engine),
            reviews=ingest.open_reviews(engine),
            vendors=[v for v in db.SOURCES if v not in ("awre", "manual")],
            session_types=db.SESSION_TYPES,
            purposes=db.SESSION_PURPOSES,
            metric_options=_metric_options(),
            writes_enabled=writes_enabled(),
        )

    @app.route("/api/import", methods=["POST"])
    def api_import():
        import ingest
        if not writes_enabled():
            return jsonify({"error": WRITES_OFF}), 403
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify({"error": "no file"}), 400
        raw = f.read()
        if len(raw) > 60 * 1024 * 1024:
            return jsonify({"error": "that file is larger than 60 MB"}), 413
        try:
            import_id, sniffed = ingest.store(
                _engine(), request.form.get("vendor", ""), f.filename, raw,
                uploaded_by=request.form.get("uploaded_by") or None,
                side=request.form.get("side") or None,
                session_type=request.form.get("session_type") or None,
                purpose=request.form.get("purpose") or None)
        except ingest.IngestError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"import_id": import_id,
                        "analysis": ingest.analyze(_engine(), import_id)})

    @app.route("/api/import/<int:import_id>")
    def api_import_get(import_id):
        import ingest
        try:
            return jsonify(ingest.analyze(_engine(), import_id))
        except ingest.IngestError as e:
            return jsonify({"error": str(e)}), 404

    @app.route("/api/import/<int:import_id>/map", methods=["POST"])
    def api_import_map(import_id):
        import ingest
        if not writes_enabled():
            return jsonify({"error": WRITES_OFF}), 403
        data = request.get_json(silent=True) or {}
        try:
            info = ingest.analyze(_engine(), import_id)
            n = ingest.save_mappings(_engine(), info["vendor"],
                                     data.get("mappings") or {},
                                     confirmed_by=data.get("confirmed_by"))
        except ingest.IngestError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"saved": n, "analysis": ingest.analyze(_engine(), import_id)})

    @app.route("/api/import/<int:import_id>/commit", methods=["POST"])
    def api_import_commit(import_id):
        import ingest
        if not writes_enabled():
            return jsonify({"error": WRITES_OFF}), 403
        dry = bool((request.get_json(silent=True) or {}).get("dry_run"))
        try:
            stats = ingest.commit(_engine(), import_id, dry_run=dry)
        except ingest.IngestError as e:
            return jsonify({"error": str(e)}), 400
        # New data means the baselines may have moved -- detect straight away so
        # the coach sees the consequence of the upload, not next time a job runs.
        if not dry and stats.get("measurements"):
            import changes
            try:
                stats["changes_detected"] = changes.compute_all(
                    _engine(), write=True)["fired"]
            except Exception as e:
                stats["change_detection_error"] = str(e)
        return jsonify(stats)

    @app.route("/api/import/<int:import_id>/recommit", methods=["POST"])
    def api_import_recommit(import_id):
        import ingest
        if not writes_enabled():
            return jsonify({"error": WRITES_OFF}), 403
        try:
            return jsonify(ingest.recommit(_engine(), import_id))
        except ingest.IngestError as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/reviews")
    def api_reviews():
        import ingest
        return jsonify(ingest.open_reviews(_engine()))

    @app.route("/api/reviews/<int:review_id>/<action>", methods=["POST"])
    def api_review_action(review_id, action):
        import ingest
        if not writes_enabled():
            return jsonify({"error": WRITES_OFF}), 403
        data = request.get_json(silent=True) or {}
        try:
            if action == "accept":
                pid = ingest.accept_review(_engine(), review_id,
                                           player_id=data.get("player_id"),
                                           resolved_by=data.get("resolved_by"))
                return jsonify({"ok": True, "player_id": pid})
            if action == "reject":
                ingest.reject_review(_engine(), review_id,
                                     resolved_by=data.get("resolved_by"))
                return jsonify({"ok": True})
        except ingest.IngestError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"error": "unknown action"}), 400

    @app.route("/api/players")
    def api_players():
        from sqlalchemy import select
        engine = _engine()
        with engine.connect() as conn:
            rows = conn.execute(
                select(db.players.c.id, db.players.c.slug, db.players.c.first_name,
                       db.players.c.last_name, db.players.c.is_pitcher,
                       db.players.c.bats, db.players.c.throws)
                .where(db.players.c.is_active == True)  # noqa: E712
                .order_by(db.players.c.last_name, db.players.c.first_name)).all()
        return jsonify([{"id": r.id, "slug": r.slug,
                         "name": f"{r.first_name} {r.last_name}",
                         "is_pitcher": bool(r.is_pitcher),
                         "bats": r.bats, "throws": r.throws} for r in rows])

    # -----------------------------------------------------------------------
    # Health -- confirms whether the player-development database is reachable.
    # Useful the moment Postgres is added on Railway (spec section 12, phase A).
    # -----------------------------------------------------------------------

    @app.route("/api/health")
    def health():
        """First thing to check after a deploy. `backend` must say postgres --
        sqlite in production means Railway will wipe the data on redeploy."""
        import seed
        from sqlalchemy import func, select
        out = {"ok": True, "gate": bool(HUB_PASSWORD),
               "writes_enabled": writes_enabled(),
               "auto_seed": seed.autoseed_enabled()}
        try:
            engine = _engine()
            with engine.connect() as conn:
                for label, table in (("players", db.players),
                                     ("sessions", db.sessions),
                                     ("change_events", db.change_events)):
                    out[label] = conn.execute(
                        select(func.count()).select_from(table)).scalar()
            out["backend"] = "postgres" if db.is_postgres() else "sqlite"
            if not out["players"]:
                out["warning"] = ("no players seeded -- run `python seed.py`, "
                                  "or set AUTO_SEED=1 and restart")
            elif out["backend"] == "sqlite" and os.environ.get("RAILWAY_ENVIRONMENT"):
                out["warning"] = ("running on SQLite in production -- Railway wipes "
                                  "this on every redeploy. Add the Postgres plugin "
                                  "so DATABASE_URL is set.")
        except Exception as e:
            out["ok"] = False
            out["db_error"] = str(e)
        return jsonify(out)

    # -----------------------------------------------------------------------
    # Pages
    # -----------------------------------------------------------------------

    @app.route("/")
    def index():
        import development
        import profiles
        import season as season_mod
        # The same week the Season tab would land on (current week in season,
        # latest game week otherwise) drives the home page's "this week" strip.
        view = season_mod.week_view()
        latest_game = next((g for g in reversed(view["games"])
                            if g["official"] or g["tracked"]), None)
        return render_template("home.html", nav="home", chips=tools.CHIPS,
                               overview=profiles.team_overview(_engine()),
                               due=development.due_for_review(_engine()),
                               week_start=view["start"],
                               week_label=view["label"],
                               latest_game=latest_game,
                               notable=season_mod.notable_trends(view["start"],
                                                                 limit=3))

    @app.route("/players")
    def players_page():
        import profiles
        import rapsodo_card
        # Two tabs, one per side; each led by its own coordinator chat.
        side = request.args.get("side")
        if side not in ("pitching", "hitting"):
            side = "pitching"
        engine = _engine()
        return render_template("players.html", nav="players", side=side,
                               roster=profiles.roster(engine),
                               # Per-pitcher visuals (velo, mix, movement, slot)
                               # so the grid reads like a wall of cards, not a
                               # list of names.
                               viz=rapsodo_card.roster_cards(_engine()),
                               # Hitter visuals -- empty until the first Blast /
                               # HitTrax export lands, then the grid lights up
                               # on its own.
                               hviz=profiles.hitter_cards(_engine()))

    @app.route("/players/<slug>")
    def player_page(slug):
        import development
        import profiles
        p = profiles.profile(_engine(), slug)
        if not p:
            return render_template("notfound.html", nav="players", slug=slug), 404
        # Pitchers get the Rapsodo report card in place of the training-history
        # table -- a coach reads shapes, not a run-on list of metric averages.
        card = None
        if p["player"]["is_pitcher"]:
            import rapsodo_card
            card = rapsodo_card.card(_engine(), p["player"]["id"])
        slog = {e["date"]: e for e in rapsodo_card.session_log(card["pitches"])} \
            if card and card.get("has_data") else {}
        # One strip per pitch type -- "is his slider any good" is a different
        # question from "is his fastball any good".
        import percentiles
        strips = []
        if p["player"]["is_pitcher"]:
            strips = percentiles.by_pitch(_engine(), p["player"]["id"])
        # In-season, from the charted games. The only percentiles a hitter can
        # have until there is bat data, and the game layer for a pitcher.
        awre_name = p["aliases"].get("awre") or p["player"]["name"]
        gyear = request.args.get("gyear")
        side = "pitching" if p["player"]["is_pitcher"] else "hitting"
        game_strip = percentiles.game_strip(awre_name, side, gyear)
        # A two-way player deserves both; his bat is not a footnote.
        game_bat = None
        if p["player"]["is_pitcher"]:
            game_bat = percentiles.game_strip(awre_name, "hitting", gyear)
        return render_template(
            "player.html", nav="players", p=p, card=card, slog=slog,
            strips=strips, game_strip=game_strip, game_bat=game_bat,
            writes_enabled=writes_enabled(),
            metric_options=development.goal_metric_options(),
            directions=db.GOAL_DIRECTIONS,
            categories=db.INTERVENTION_CATEGORIES)

    # -----------------------------------------------------------------------
    # A player's own link. Read-only, one player, no way out to the rest of
    # the hub -- see playerlink.py for why the token lives in a table.
    # -----------------------------------------------------------------------

    @app.route("/me/<token>")
    def my_card(token):
        import playerlink
        c = playerlink.card(_engine(), token)
        if not c:
            # Deliberately the same answer for revoked, mistyped and never-
            # issued: a probe learns nothing about which tokens exist.
            return render_template("mycard_gone.html"), 404
        return render_template("mycard.html", c=c)

    @app.route("/api/players/<int:player_id>/link", methods=["POST", "DELETE"])
    def player_link_api(player_id):
        if not writes_enabled():
            return jsonify({"error": "Sharing links are disabled while the hub "
                                     "is public without a password."}), 403
        import playerlink
        if request.method == "DELETE":
            n = playerlink.revoke(_engine(), player_id)
            return jsonify({"revoked": n})
        token = playerlink.issue(_engine(), player_id)
        return jsonify({"token": token, "url": url_for(
            "my_card", token=token, _external=True)})

    @app.route("/season")
    def season_page():
        import season as season_mod
        view = season_mod.week_view(request.args.get("week"))
        return render_template(
            "season.html", nav="season", view=view,
            notable=season_mod.notable_trends(view["start"]))

    @app.route("/season/game/<date>")
    def season_game_page(date):
        import season as season_mod
        box = season_mod.game_box(date)
        if not box:
            return render_template("notfound.html"), 404
        return render_template("season_game.html", nav="season", box=box)

    @app.route("/team")
    def team_page():
        import profiles
        import season as season_mod
        return render_template("team.html", nav="team",
                               o=profiles.team_overview(_engine()),
                               prog=season_mod.program_development(),
                               writes_enabled=writes_enabled())

    @app.route("/prep")
    def prep_page():
        return render_template(
            "toolpage.html", nav="prep", heading="Game Prep",
            blurb="Opponent scouting, matchup analysis, umpire information and the "
                  "scouting AI.",
            tools=tools.by_category("prep"))

    @app.route("/video")
    def video_page():
        return render_template(
            "toolpage.html", nav="video", heading="Video",
            blurb="Game video search and delivery overlay comparisons.",
            tools=tools.by_category("video"))

    @app.route("/tools")
    def tools_page():
        return render_template(
            "toolpage.html", nav="tools", heading="Applications",
            blurb="Every analytics application. These stay exactly as they are — "
                  "they're the infrastructure the player pages are built on.",
            tools=tools.TOOLS)

    # -----------------------------------------------------------------------
    # Player APIs (spec section 8.5)
    # -----------------------------------------------------------------------

    @app.route("/api/players/<int:player_id>/timeline")
    def api_timeline(player_id):
        import profiles
        from sqlalchemy import select
        with _engine().connect() as conn:
            row = conn.execute(select(db.players.c.slug)
                               .where(db.players.c.id == player_id)).first()
        if not row:
            return jsonify({"error": "no such player"}), 404
        p = profiles.profile(_engine(), row.slug)
        return jsonify({"sessions": p["training"], "changes": p["changes"]})

    @app.route("/api/players/<int:player_id>/metric/<metric_key>")
    def api_metric(player_id, metric_key):
        import profiles
        if not metrics.known(metric_key):
            return jsonify({"error": f"unknown metric '{metric_key}'"}), 404
        return jsonify(profiles.metric_series(_engine(), player_id, metric_key))

    @app.route("/api/changes")
    def api_changes():
        import profiles
        return jsonify(profiles.team_overview(_engine())["changes"])

    @app.route("/api/changes/<int:event_id>/acknowledge", methods=["POST"])
    def api_acknowledge(event_id):
        import changes
        if not writes_enabled():
            return jsonify({"error": WRITES_OFF}), 403
        undo = bool((request.get_json(silent=True) or {}).get("undo"))
        changes.acknowledge(_engine(), event_id, acknowledged=not undo)
        return jsonify({"ok": True, "acknowledged": not undo})

    @app.route("/api/changes/detect", methods=["POST"])
    def api_detect():
        """Run change detection now. Also called after every ingest commit, and
        by the nightly job."""
        import changes
        if not writes_enabled():
            return jsonify({"error": WRITES_OFF}), 403
        data = request.get_json(silent=True) or {}
        result = changes.compute_all(
            _engine(), write=not data.get("dry_run"),
            player_id=data.get("player_id"))
        result.pop("events", None)      # keep the response small
        return jsonify(result)

    @app.route("/api/interventions/<int:intervention_id>/evaluate", methods=["POST"])
    def api_evaluate_intervention(intervention_id):
        import changes
        if not writes_enabled():
            return jsonify({"error": WRITES_OFF}), 403
        res = changes.evaluate_intervention(_engine(), intervention_id)
        if res is None:
            return jsonify({"error": "no such intervention"}), 404
        return jsonify(res)

    # -----------------------------------------------------------------------
    # Goals and interventions (spec section 5.5) -- validation lives in
    # development.py, so these handlers stay thin.
    # -----------------------------------------------------------------------

    def _dev_call(fn, *a, **kw):
        import development
        if not writes_enabled():
            return jsonify({"error": WRITES_OFF}), 403
        try:
            return jsonify({"ok": True, "result": fn(*a, **kw)})
        except development.DevError as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/goals", methods=["POST"])
    def api_create_goal():
        import development
        d = request.get_json(silent=True) or request.form.to_dict() or {}
        return _dev_call(
            development.create_goal, _engine(),
            player_id=int(d.get("player_id") or 0),
            title=d.get("title"), metric_key=d.get("metric_key") or None,
            direction=d.get("direction") or None,
            target_value=d.get("target_value") if d.get("target_value") not in ("", None) else None,
            detail=d.get("detail"), set_by=d.get("set_by"),
            set_on=d.get("set_on"), review_on=d.get("review_on"))

    @app.route("/api/goals/<int:goal_id>", methods=["POST", "DELETE"])
    def api_update_goal(goal_id):
        import development
        if request.method == "DELETE":
            return _dev_call(development.delete_goal, _engine(), goal_id)
        d = request.get_json(silent=True) or {}
        return _dev_call(development.update_goal, _engine(), goal_id,
                         status=d.get("status"), review_on=d.get("review_on"),
                         detail=d.get("detail"), target_value=d.get("target_value"))

    @app.route("/api/interventions", methods=["POST"])
    def api_create_intervention():
        import development
        d = request.get_json(silent=True) or request.form.to_dict() or {}
        return _dev_call(
            development.create_intervention, _engine(),
            player_id=int(d.get("player_id") or 0),
            title=d.get("title"),
            intervention_date=d.get("intervention_date"),
            category=d.get("category") or None, detail=d.get("detail"),
            coach=d.get("coach"),
            goal_id=int(d["goal_id"]) if d.get("goal_id") else None,
            review_on=d.get("review_on"))

    @app.route("/api/interventions/<int:intervention_id>", methods=["POST", "DELETE"])
    def api_update_intervention(intervention_id):
        import development
        if request.method == "DELETE":
            return _dev_call(development.delete_intervention, _engine(),
                             intervention_id)
        d = request.get_json(silent=True) or {}
        return _dev_call(development.update_intervention, _engine(), intervention_id,
                         outcome=d.get("outcome"), review_on=d.get("review_on"),
                         detail=d.get("detail"))

    @app.route("/api/reviews-due")
    def api_reviews_due():
        import development
        return jsonify(development.due_for_review(_engine()))

    # -----------------------------------------------------------------------
    # AI summaries (spec section 9.2/9.3). Generating costs an API call, so it
    # is an explicit action -- never something a page view triggers.
    # -----------------------------------------------------------------------

    @app.route("/api/players/<int:player_id>/summary", methods=["GET", "POST"])
    def api_summary(player_id):
        import summaries
        engine = _engine()
        if request.method == "GET":
            hit = summaries.cached(engine, player_id)
            return jsonify(hit or {"summary": None, "note": "no current summary"})
        if not writes_enabled():
            return jsonify({"error": WRITES_OFF}), 403
        force = bool((request.get_json(silent=True) or {}).get("force"))
        try:
            res = summaries.generate(engine, player_id, force=force)
            res["parsed"] = summaries.parse_note(res.get("summary"))
            return jsonify(res)
        except Exception as e:
            return jsonify({"error": f"Could not write a summary: {e}"}), 500

    @app.route("/api/groups/<side>/chat", methods=["POST"])
    def api_group_chat(side):
        """The pitching / hitting coordinator chat on the Players tabs."""
        import agent
        if side not in ("pitching", "hitting"):
            return jsonify({"error": "side must be pitching or hitting"}), 400
        d = request.get_json(silent=True) or {}
        msgs = d.get("messages") or []
        if not isinstance(msgs, list) or not msgs:
            return jsonify({"error": "no question asked"}), 400
        try:
            reply, drafts = agent.answer_with_proposals(
                msgs, request.remote_addr or "?", focus=side)
            return jsonify({"reply": reply, "proposals": drafts,
                            "can_write": writes_enabled()})
        except agent.RateLimited as e:
            return jsonify({"error": str(e)}), 429
        except Exception as e:                              # noqa: BLE001
            return jsonify({"error": f"Could not answer: {e}"}), 500

    @app.route("/api/development/apply", methods=["POST"])
    def api_development_apply():
        """Commit a draft the coach confirmed in the chat.

        The agent never reaches this path -- it only drafts. Everything is
        re-validated here against development.py, so a tampered payload is
        rejected exactly as a bad form submission would be.
        """
        import development
        if not writes_enabled():
            return jsonify({"error": WRITES_OFF}), 403
        d = request.get_json(silent=True) or {}
        kind = d.get("kind")
        engine = _engine()
        try:
            pid = int(d.get("player_id"))
            if kind == "goal":
                new_id = development.create_goal(
                    engine, pid, title=d.get("title"),
                    metric_key=d.get("metric_key") or None,
                    direction=d.get("direction") or None,
                    target_value=d.get("target_value"),
                    detail=d.get("detail") or None,
                    review_on=d.get("review_on") or None,
                    set_by=d.get("set_by") or "Coach (via assistant)")
                return jsonify({"ok": True, "kind": kind, "id": new_id})
            if kind == "intervention":
                new_id = development.create_intervention(
                    engine, pid, title=d.get("title"),
                    category=d.get("category") or None,
                    detail=d.get("detail") or None,
                    intervention_date=d.get("intervention_date") or None,
                    review_on=d.get("review_on") or None,
                    coach=d.get("coach") or "Coach (via assistant)")
                return jsonify({"ok": True, "kind": kind, "id": new_id})
            return jsonify({"error": "kind must be goal or intervention"}), 400
        except development.DevError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:                              # noqa: BLE001
            return jsonify({"error": f"Could not save: {e}"}), 500

    @app.route("/api/summaries/weekly", methods=["POST"])
    def api_weekly_summaries():
        import summaries
        if not writes_enabled():
            return jsonify({"error": WRITES_OFF}), 403
        d = request.get_json(silent=True) or {}
        res = summaries.run_weekly(_engine(), everyone=bool(d.get("all")),
                                   dry_run=bool(d.get("dry_run")))
        res.pop("results", None)
        return jsonify(res)

    return app


# Module-level, because the Procfile runs `gunicorn app:app`.
app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(debug=True, host="0.0.0.0", port=port)
