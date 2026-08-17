"""
ingest.py -- getting Blast / HitTrax / Rapsodo exports into the database.

See PLAYER_DEV_SPEC.md sections 5.3, 5.4 and 8.4.

The design constraint this file exists to satisfy: we do not know what HitTrax's
or Rapsodo's export columns are called. So nothing here hard-codes a vendor's
schema. A file moves through four stages, and a human is in the loop at the two
places where guessing would corrupt the data:

    1. sniff    -- find the header row, read the columns          (automatic)
    2. store    -- keep the file whole in raw_imports             (automatic)
    3. map      -- source column -> canonical metric key          (HUMAN, once per vendor)
    4. commit   -- resolve players, build sessions, write metrics (automatic;
                   unresolved names go to review rather than being guessed)

Stage 3 is remembered in `column_maps`, so it happens once per vendor and never
again -- which is what lets Ian map his first HitTrax export himself, with no
code change and no redeploy.
"""

import csv
import hashlib
import io
import json
import re
from datetime import date, datetime

from sqlalchemy import delete, func, insert, select, update

import db
import metrics

# Which measurement table a vendor's rows land in. Overridable per upload,
# because Rapsodo sells a hitting unit too.
VENDOR_SIDE = {
    "blast": "hitting",
    "hittrax": "hitting",
    "rapsodo": "pitching",
    "charting": "pitching",
    "awre": "pitching",
}

VENDOR_DEFAULT_SESSION = {
    "blast": "cage",
    "hittrax": "cage",
    "rapsodo": "bullpen",
    "charting": "bullpen",
    "awre": "game",
}

# Rows we refuse to keep in memory. Real exports are far below this; the cap is
# a guard against someone dropping a 2 GB file into the box.
MAX_ROWS = 200_000


class IngestError(Exception):
    pass


# ---------------------------------------------------------------------------
# Stage 1 -- sniff
# ---------------------------------------------------------------------------

def _decode(raw):
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _looks_like_header(cells):
    """A header row is mostly non-empty, mostly non-numeric, mostly distinct."""
    vals = [str(c).strip() for c in cells]
    filled = [v for v in vals if v]
    if len(filled) < 2:
        return 0.0
    numeric = sum(1 for v in filled if _to_float(v) is not None)
    distinct = len(set(v.lower() for v in filled))
    return (len(filled) / max(len(vals), 1)) * \
           (1 - numeric / len(filled)) * \
           (distinct / len(filled))


def sniff(raw_bytes, filename=""):
    """Find the header row and read the table.

    Rapsodo (and some HitTrax exports) put metadata lines above the real header,
    so we score the first 20 lines and take the best rather than assuming line 1.

    Returns dict: header_row, columns, rows (list of dicts), row_count.
    """
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xls")):
        return _sniff_excel(raw_bytes)

    text = _decode(raw_bytes)
    if not text.strip():
        raise IngestError("that file is empty")

    sample = text[:8000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delim = dialect.delimiter
    except Exception:
        delim = "\t" if sample.count("\t") > sample.count(",") else ","

    all_rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    if not all_rows:
        raise IngestError("no rows found in that file")

    best_i, best_score = 0, -1.0
    for i, row in enumerate(all_rows[:20]):
        score = _looks_like_header(row)
        if score > best_score:
            best_i, best_score = i, score

    header = [str(c).strip() for c in all_rows[best_i]]
    header = _dedupe_header(header)
    body = all_rows[best_i + 1:]

    rows = []
    for r in body:
        if not any(str(c).strip() for c in r):
            continue                       # blank separator line
        rows.append({header[j]: (r[j] if j < len(r) else "")
                     for j in range(len(header))})
        if len(rows) >= MAX_ROWS:
            break

    return {"header_row": best_i, "columns": header, "rows": rows,
            "row_count": len(rows), "delimiter": delim}


def _sniff_excel(raw_bytes):
    try:
        import pandas as pd
    except ImportError:                                  # pragma: no cover
        raise IngestError("reading .xlsx needs pandas + openpyxl on the server")
    try:
        df = pd.read_excel(io.BytesIO(raw_bytes), header=None, dtype=str)
    except Exception as e:
        raise IngestError(f"could not read that spreadsheet: {e}")
    rows_raw = df.fillna("").values.tolist()
    if not rows_raw:
        raise IngestError("that spreadsheet is empty")
    best_i, best_score = 0, -1.0
    for i, row in enumerate(rows_raw[:20]):
        score = _looks_like_header(row)
        if score > best_score:
            best_i, best_score = i, score
    header = _dedupe_header([str(c).strip() for c in rows_raw[best_i]])
    rows = []
    for r in rows_raw[best_i + 1:]:
        if not any(str(c).strip() for c in r):
            continue
        rows.append({header[j]: (str(r[j]) if j < len(r) else "")
                     for j in range(len(header))})
        if len(rows) >= MAX_ROWS:
            break
    return {"header_row": best_i, "columns": header, "rows": rows,
            "row_count": len(rows), "delimiter": "xlsx"}


def _dedupe_header(header):
    """Exports do ship duplicate and blank column names. Make them addressable."""
    seen, out = {}, []
    for i, h in enumerate(header):
        h = h or f"column_{i + 1}"
        if h in seen:
            seen[h] += 1
            h = f"{h}.{seen[h]}"
        else:
            seen[h] = 0
        out.append(h)
    return out


# ---------------------------------------------------------------------------
# Stage 2 -- store
# ---------------------------------------------------------------------------

def store(engine, vendor, filename, raw_bytes, uploaded_by=None,
          side=None, session_type=None, purpose=None):
    """Keep the file whole, before any parsing. Returns (import_id, sniffed)."""
    if vendor not in db.SOURCES:
        raise IngestError(f"unknown vendor '{vendor}'")

    sha = hashlib.sha256(raw_bytes).hexdigest()
    with engine.connect() as conn:
        dupe = conn.execute(select(db.raw_imports.c.id, db.raw_imports.c.filename,
                                   db.raw_imports.c.status)
                            .where(db.raw_imports.c.sha256 == sha)).first()
    if dupe:
        raise IngestError(
            f"this exact file was already uploaded (import #{dupe.id}, "
            f"'{dupe.filename}', status {dupe.status})")

    sniffed = sniff(raw_bytes, filename)
    side = side or VENDOR_SIDE.get(vendor, "hitting")
    session_type = session_type or VENDOR_DEFAULT_SESSION.get(vendor, "cage")

    with engine.begin() as conn:
        import_id = conn.execute(insert(db.raw_imports).values(
            vendor=vendor, filename=filename, sha256=sha,
            uploaded_by=uploaded_by, header=sniffed["columns"],
            header_row=sniffed["header_row"], row_count=sniffed["row_count"],
            payload=sniffed["rows"], status="pending",
            side=side, session_type=session_type, purpose=purpose,
        )).inserted_primary_key[0]
    return import_id, sniffed


# ---------------------------------------------------------------------------
# Stage 3 -- map
# ---------------------------------------------------------------------------

# Hints used only to PRE-SELECT a dropdown for a human. Never applied silently:
# a suggestion with no confirmation leaves the column unmapped.
_HINTS = {
    "player": ["player", "batter", "hitter", "pitcher", "name", "user", "athlete"],
    "vendor_id": ["player id", "playerid", "user id", "athlete id"],
    "date": ["date", "session date", "timestamp", "datetime", "created", "time"],
    "session": ["session", "session id", "round", "group", "bucket"],
    "pitch_type": ["pitch type", "pitchtype", "pitch"],
    "seq": ["no", "number", "index", "swing no", "pitch no", "seq"],
    "exit_velocity": ["exit velocity", "exit velo", "ev", "velo mph", "exitvelo"],
    "launch_angle": ["launch angle", "launchangle", "la", "elevation"],
    "distance": ["distance", "dist", "carry"],
    "bat_speed": ["bat speed", "batspeed", "swing speed"],
    "attack_angle": ["attack angle", "bat path angle", "attackangle"],
    "on_plane_efficiency": ["on plane efficiency", "planar efficiency", "on plane"],
    "velocity": ["velocity", "speed", "pitch speed", "release speed", "mph"],
    "spin_rate": ["spin rate", "spin", "rpm", "total spin"],
    "induced_vertical_break": ["induced vertical break", "ivb", "vertical break",
                               "vb", "induced vert break"],
    "horizontal_break": ["horizontal break", "hb", "horz break", "h break"],
    "spin_efficiency": ["spin efficiency", "efficiency", "active spin"],
    "release_height": ["release height", "rel height", "release z", "vertical release"],
    "release_side": ["release side", "rel side", "release x", "horizontal release"],
    "extension": ["extension", "release extension"],
}


def suggest_role(column):
    """Best guess for one source column, or None. A guess, not a decision."""
    c = re.sub(r"[_\-.]+", " ", str(column).lower()).strip()
    c = re.sub(r"\s+", " ", c)
    for key, hints in _HINTS.items():
        if c in hints:
            return key
    for key, hints in _HINTS.items():
        if any(h in c for h in hints):
            return key
    return None


def analyze(engine, import_id):
    """What's mapped, what isn't, and what we'd suggest for the rest."""
    with engine.connect() as conn:
        imp = conn.execute(select(db.raw_imports)
                           .where(db.raw_imports.c.id == import_id)).first()
        if not imp:
            raise IngestError(f"no import #{import_id}")
        known = {r.source_column: r for r in conn.execute(
            select(db.column_maps).where(db.column_maps.c.vendor == imp.vendor))}

    rows = imp.payload or []
    columns = []
    for col in (imp.header or []):
        samples = [str(r.get(col, "")).strip() for r in rows[:200]]
        samples = [s for s in samples if s][:3]
        existing = known.get(col)
        columns.append({
            "column": col,
            "mapped_to": existing.metric_key if existing else None,
            "unit": existing.unit if existing else None,
            "scale": existing.scale if existing else 1.0,
            "suggestion": None if existing else suggest_role(col),
            "samples": samples,
        })

    roles = {c["mapped_to"] for c in columns if c["mapped_to"]}
    missing = []
    if "player" not in roles and "vendor_id" not in roles:
        missing.append("a player column (or a vendor id column)")
    if "date" not in roles:
        missing.append("a date column")
    if not (roles & set(metrics.REGISTRY)):
        missing.append("at least one metric column")

    return {
        "import_id": import_id,
        "vendor": imp.vendor,
        "filename": imp.filename,
        "status": imp.status,
        "side": imp.side,
        "session_type": imp.session_type,
        "purpose": imp.purpose,
        "row_count": imp.row_count,
        "header_row": imp.header_row,
        "columns": columns,
        "unmapped": [c["column"] for c in columns if not c["mapped_to"]],
        "missing_roles": missing,
        "ready": not missing,
    }


def save_mappings(engine, vendor, mappings, confirmed_by=None):
    """mappings: {source_column: metric_key or role or 'ignore'}.

    Confirmed once per vendor and remembered forever -- the whole point of
    column_maps being a table rather than a dict in the source.
    """
    valid = set(metrics.REGISTRY) | set(db.COLUMN_ROLES)
    saved = 0
    with engine.begin() as conn:
        for col, key in (mappings or {}).items():
            if not key:
                continue
            if key not in valid:
                raise IngestError(f"'{key}' is not a known metric or role")
            m = metrics.REGISTRY.get(key)
            existing = conn.execute(select(db.column_maps.c.id).where(
                (db.column_maps.c.vendor == vendor) &
                (db.column_maps.c.source_column == col))).first()
            values = {"metric_key": key, "unit": (m.unit if m else None),
                      "confirmed_by": confirmed_by or "coach",
                      "confirmed_at": datetime.now()}
            if existing:
                conn.execute(update(db.column_maps)
                             .where(db.column_maps.c.id == existing.id).values(**values))
            else:
                conn.execute(insert(db.column_maps).values(
                    vendor=vendor, source_column=col, scale=1.0, **values))
            saved += 1
    return saved


# ---------------------------------------------------------------------------
# Value + name parsing
# ---------------------------------------------------------------------------

def _to_float(v):
    """'86.4 mph' -> 86.4, '1,204' -> 1204.0, '', '-', 'N/A' -> None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if not s or s.lower() in {"-", "--", "n/a", "na", "null", "none", "nan"}:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


_DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y",
                 "%Y/%m/%d", "%b %d, %Y", "%d-%b-%Y", "%Y%m%d"]


def _to_date(v):
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.date() if isinstance(v, datetime) else v
    s = str(v).strip()
    if not s:
        return None
    s = s.split("T")[0]
    # Trailing time component, e.g. '2026-04-15 00:00:00'
    if " " in s and re.match(r"^\d{4}-\d{2}-\d{2} ", s):
        s = s.split(" ")[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:                                    # last resort
        import pandas as pd
        parsed = pd.to_datetime(s, errors="coerce")
        return None if parsed is None or parsed is pd.NaT else parsed.date()
    except Exception:
        return None


def _name_key(name):
    """'Ponatoski, Matt' and 'Matt  Ponatoski' collapse to the same key."""
    s = str(name or "").strip()
    if "," in s:
        parts = [p.strip() for p in s.split(",", 1)]
        if len(parts) == 2 and parts[1]:
            s = f"{parts[1]} {parts[0]}"
    s = re.sub(r"\s+", " ", s)
    return s.lower()


def _player_lookup(conn):
    """Every way we know to reach a player id: canonical name and every alias."""
    lookup = {}
    for r in conn.execute(select(db.players.c.id, db.players.c.first_name,
                                 db.players.c.last_name)):
        lookup[_name_key(f"{r.first_name} {r.last_name}")] = r.id
    for r in conn.execute(select(db.player_aliases.c.player_id,
                                 db.player_aliases.c.alias)):
        lookup.setdefault(_name_key(r.alias), r.player_id)
    return lookup


def _vendor_lookup(conn, vendor):
    return {str(r.vendor_id): r.player_id for r in conn.execute(
        select(db.player_vendor_ids.c.vendor_id, db.player_vendor_ids.c.player_id)
        .where(db.player_vendor_ids.c.vendor == vendor))}


# ---------------------------------------------------------------------------
# Stage 4 -- commit
# ---------------------------------------------------------------------------

def commit(engine, import_id, dry_run=False):
    """Resolve players, group rows into sessions, write measurements.

    Nothing is guessed: a row whose player can't be resolved is counted and its
    name queued for review, never attributed to a best-guess player.
    """
    with engine.connect() as conn:
        imp = conn.execute(select(db.raw_imports)
                           .where(db.raw_imports.c.id == import_id)).first()
        if not imp:
            raise IngestError(f"no import #{import_id}")
        if imp.status == "committed":
            raise IngestError(f"import #{import_id} is already committed")
        colmaps = {r.source_column: r for r in conn.execute(
            select(db.column_maps).where(db.column_maps.c.vendor == imp.vendor))}
        names = _player_lookup(conn)
        vendor_ids = _vendor_lookup(conn, imp.vendor)

    info = analyze(engine, import_id)
    if not info["ready"]:
        raise IngestError("not ready to commit -- missing " +
                          ", ".join(info["missing_roles"]))

    # Which source column plays which structural role
    role_col = {}
    for col, cm in colmaps.items():
        if cm.metric_key in db.COLUMN_ROLES:
            role_col.setdefault(cm.metric_key, col)
    metric_cols = {col: cm for col, cm in colmaps.items()
                   if cm.metric_key in metrics.REGISTRY}

    side = imp.side or VENDOR_SIDE.get(imp.vendor, "hitting")
    table = db.swings if side == "hitting" else db.pitch_metrics

    groups = {}          # session_ref -> {player_id, date, rows:[(seq, ptype, {k:v})]}
    unresolved = {}      # raw name -> count
    no_date = 0

    for i, row in enumerate(imp.payload or []):
        # --- who
        pid = None
        if "vendor_id" in role_col:
            pid = vendor_ids.get(str(row.get(role_col["vendor_id"], "")).strip())
        raw_name = str(row.get(role_col.get("player", ""), "")).strip()
        if pid is None and raw_name:
            pid = names.get(_name_key(raw_name))
        if pid is None:
            label = raw_name or str(row.get(role_col.get("vendor_id", ""), "")).strip()
            if label:
                unresolved[label] = unresolved.get(label, 0) + 1
            continue

        # --- when
        d = _to_date(row.get(role_col.get("date", "")))
        if d is None:
            no_date += 1
            continue

        # --- which session. If the export carries no session id we fall back to
        # (player, date) -- which cannot tell two bullpens on one day apart. That
        # is a known limitation, flagged back to the caller, not hidden.
        if "session" in role_col and str(row.get(role_col["session"], "")).strip():
            ref = f"{pid}|{str(row.get(role_col['session'])).strip()}"
            synthesized = False
        else:
            ref = f"{pid}|{d.isoformat()}"
            synthesized = True

        g = groups.setdefault(ref, {"player_id": pid, "date": d, "rows": [],
                                    "synthesized": synthesized})
        seq = _to_float(row.get(role_col.get("seq", ""))) if "seq" in role_col else None
        ptype = None
        if side == "pitching" and "pitch_type" in role_col:
            ptype = metrics.normalize_pitch_type(row.get(role_col["pitch_type"]))

        values = {}
        for col, cm in metric_cols.items():
            v = _to_float(row.get(col))
            if v is not None:
                values[cm.metric_key] = v * (cm.scale or 1.0)
        if values:
            g["rows"].append((int(seq) if seq is not None else i + 1, ptype, values))

    # --- unresolved names go to the review queue with a fuzzy suggestion
    queued = _queue_names(engine, import_id, imp.vendor, unresolved, names,
                          dry_run=dry_run)

    stats = {
        "import_id": import_id, "side": side,
        "rows_read": len(imp.payload or []),
        "sessions_new": 0, "sessions_existing": 0,
        "measurements": 0,
        "rows_unresolved_player": sum(unresolved.values()),
        "names_queued": queued,
        "rows_no_date": no_date,
        "unknown_pitch_types": 0,
        "synthesized_session_refs": sum(1 for g in groups.values() if g["synthesized"]),
    }
    if side == "pitching":
        stats["unknown_pitch_types"] = sum(
            1 for g in groups.values() for (_s, pt, _v) in g["rows"]
            if "pitch_type" in role_col and pt is None)

    if dry_run:
        stats["sessions_new"] = len(groups)
        stats["measurements"] = sum(len(v) for g in groups.values()
                                    for (_s, _p, v) in g["rows"])
        return stats

    with engine.begin() as conn:
        existing = {r.source_ref for r in conn.execute(
            select(db.sessions.c.source_ref)
            .where(db.sessions.c.source == imp.vendor))}
        for ref, g in groups.items():
            # A cumulative weekly export re-sends every past session. Skipping
            # ones we already hold is what makes cumulative and incremental
            # exports both work without the coach having to know which he has.
            if ref in existing:
                stats["sessions_existing"] += 1
                continue
            sid = conn.execute(insert(db.sessions).values(
                player_id=g["player_id"], session_date=g["date"],
                session_type=imp.session_type, source=imp.vendor,
                source_ref=ref, purpose=imp.purpose, import_id=import_id,
            )).inserted_primary_key[0]
            stats["sessions_new"] += 1
            payload = []
            for seq, ptype, values in g["rows"]:
                for key, val in values.items():
                    rec = {"session_id": sid, "player_id": g["player_id"],
                           "seq": seq, "metric_key": key, "value": val}
                    if side == "pitching":
                        rec["pitch_type"] = ptype
                    payload.append(rec)
            if payload:
                conn.execute(insert(table), payload)
                stats["measurements"] += len(payload)

        conn.execute(update(db.raw_imports)
                     .where(db.raw_imports.c.id == import_id)
                     .values(status="committed", note=json.dumps(stats)))
    return stats


def _queue_names(engine, import_id, vendor, unresolved, names, dry_run=False):
    """Unresolved export names -> name_review, each with a fuzzy suggestion."""
    if not unresolved:
        return 0
    import difflib
    candidates = list(names.keys())
    queued = 0
    with engine.begin() as conn:
        open_now = {r.raw_name for r in conn.execute(
            select(db.name_review.c.raw_name).where(
                (db.name_review.c.vendor == vendor) &
                (db.name_review.c.status == "open")))}
        for raw in unresolved:
            if raw in open_now:
                continue
            hit = difflib.get_close_matches(_name_key(raw), candidates, n=1, cutoff=0.75)
            score = (round(difflib.SequenceMatcher(None, _name_key(raw), hit[0]).ratio(), 3)
                     if hit else 0.0)
            if not dry_run:
                conn.execute(insert(db.name_review).values(
                    import_id=import_id, vendor=vendor, raw_name=raw,
                    suggested_player_id=(names.get(hit[0]) if hit else None),
                    suggestion_score=score, status="open"))
            queued += 1
    return queued


# ---------------------------------------------------------------------------
# The name-review queue
# ---------------------------------------------------------------------------

def open_reviews(engine):
    with engine.connect() as conn:
        rows = conn.execute(
            select(db.name_review.c.id, db.name_review.c.vendor,
                   db.name_review.c.raw_name, db.name_review.c.suggestion_score,
                   db.name_review.c.suggested_player_id,
                   db.players.c.first_name, db.players.c.last_name)
            .select_from(db.name_review.outerjoin(
                db.players, db.name_review.c.suggested_player_id == db.players.c.id))
            .where(db.name_review.c.status == "open")
            .order_by(db.name_review.c.suggestion_score.desc())).all()
    return [{"id": r.id, "vendor": r.vendor, "raw_name": r.raw_name,
             "score": r.suggestion_score,
             "suggested_player_id": r.suggested_player_id,
             "suggested_name": (f"{r.first_name} {r.last_name}"
                                if r.first_name else None)} for r in rows]


def accept_review(engine, review_id, player_id=None, resolved_by=None):
    """Accepting writes an ALIAS -- so this name resolves automatically forever
    after, and the same export never has to be reviewed twice."""
    with engine.begin() as conn:
        r = conn.execute(select(db.name_review)
                         .where(db.name_review.c.id == review_id)).first()
        if not r:
            raise IngestError(f"no review #{review_id}")
        pid = player_id or r.suggested_player_id
        if not pid:
            raise IngestError("no player chosen for this name")
        exists = conn.execute(select(db.player_aliases.c.id).where(
            (db.player_aliases.c.source == r.vendor) &
            (db.player_aliases.c.alias == r.raw_name))).first()
        if not exists:
            conn.execute(insert(db.player_aliases).values(
                player_id=pid, source=r.vendor, alias=r.raw_name))
        conn.execute(update(db.name_review)
                     .where(db.name_review.c.id == review_id)
                     .values(status="accepted", suggested_player_id=pid,
                             resolved_by=resolved_by or "coach",
                             resolved_at=datetime.now()))
    return pid


def reject_review(engine, review_id, resolved_by=None):
    with engine.begin() as conn:
        conn.execute(update(db.name_review)
                     .where(db.name_review.c.id == review_id)
                     .values(status="rejected", resolved_by=resolved_by or "coach",
                             resolved_at=datetime.now()))


def recommit(engine, import_id):
    """Re-run a committed import after names were resolved in review.

    Wipes only what this import wrote, then commits again -- so accepting three
    names doesn't mean re-uploading the file.
    """
    with engine.begin() as conn:
        sids = [r.id for r in conn.execute(
            select(db.sessions.c.id).where(db.sessions.c.import_id == import_id))]
        if sids:
            conn.execute(delete(db.swings).where(db.swings.c.session_id.in_(sids)))
            conn.execute(delete(db.pitch_metrics)
                         .where(db.pitch_metrics.c.session_id.in_(sids)))
            conn.execute(delete(db.sessions).where(db.sessions.c.id.in_(sids)))
        conn.execute(update(db.raw_imports)
                     .where(db.raw_imports.c.id == import_id)
                     .values(status="pending"))
    return commit(engine, import_id)


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

def recent_imports(engine, limit=25):
    with engine.connect() as conn:
        rows = conn.execute(
            select(db.raw_imports.c.id, db.raw_imports.c.vendor,
                   db.raw_imports.c.filename, db.raw_imports.c.uploaded_at,
                   db.raw_imports.c.row_count, db.raw_imports.c.status,
                   db.raw_imports.c.side, db.raw_imports.c.note)
            .order_by(db.raw_imports.c.id.desc()).limit(limit)).all()
    out = []
    for r in rows:
        stats = {}
        if r.note:
            try:
                stats = json.loads(r.note)
            except Exception:
                pass
        out.append({"id": r.id, "vendor": r.vendor, "filename": r.filename,
                    "uploaded_at": str(r.uploaded_at or ""), "rows": r.row_count,
                    "status": r.status, "side": r.side, "stats": stats})
    return out
