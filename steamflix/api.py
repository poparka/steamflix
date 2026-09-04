"""Flask application: JSON API plus the static SteamFlix front end."""
import mimetypes
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

from . import chain, config, db, diagnostics, index, jobs, keys, logbook, media
from . import proxies, resolver, seed, settings, storage
from . import torrent as torrentmod
from . import blob as blobmod
from . import net

app = Flask(__name__, static_folder=None)


def art(appid):
    if not appid:
        return {}
    base = config.CDN_BASE.format(appid=appid)
    return {
        "header": base + "header.jpg",
        "portrait": base + "library_600x900.jpg",
        "hero": base + "library_hero.jpg",
        "capsule": base + "capsule_616x353.jpg",
    }


def depot_card(row):
    cols = row.keys()

    def col(name, default=None):
        return row[name] if name in cols else default

    appid = col("appid")
    name = col("name")
    depot_count = col("depot_count", 1) or 1
    return {
        "depot": row["depot"],
        "name": name or f"Depot {row['depot']}",
        "identified": bool(name),
        "appid": appid,
        "confidence": col("confidence"),
        # `versions` is this depot's own history; `total_versions` counts the
        # whole game, which is what a grouped card is actually standing for.
        "versions": row["blob_count"],
        "total_versions": col("total_versions", row["blob_count"]),
        "depot_count": depot_count,
        "grouped": depot_count > 1,
        "max_version": row["max_version"],
        "reset": bool(col("grp_has_reset", row["has_reset"])),
        "has_key": bool(col("grp_has_key", row["has_key"])),
        "favourite": bool(col("grp_favourite", col("favourite", 0))),
        "first_date": col("grp_first", row["first_date"]),
        "last_date": col("grp_last", row["last_date"]),
        "developer": col("developer"),
        "publisher": col("publisher"),
        "franchise": col("franchise"),
        "released": col("released"),
        "year": col("year"),
        "genres": resolver.genre_names(col("genres")),
        "art": art(appid),
        "steamdb": config.STEAMDB_DEPOT_URL.format(depot=row["depot"]),
        "steamdb_app": config.STEAMDB_APP_URL.format(appid=appid) if appid else None,
    }


# One Steam app owns many depots, so listing depots means listing the same game
# a dozen times over. Cards are grouped by app id instead; a depot with no app
# behind it is its own group, keyed negative so it can never collide with one.
GROUP_EXPR = "COALESCE(m.appid, -d.depot - 1)"

# The year a title is filed under: its Steam release date when PICS knows one,
# otherwise the year the mirror first saw the depot.
YEAR_EXPR = ("COALESCE(NULLIF(substr(m.released, 1, 4), ''),"
             " NULLIF(substr(d.first_date, 1, 4), ''))")

BASE_SELECT = f"""
    SELECT d.depot, d.blob_count, d.dat_count, d.max_version, d.has_reset,
           d.first_date, d.last_date, d.has_key,
           m.appid, m.name, m.confidence, m.state,
           m.developer, m.publisher, m.franchise, m.genres, m.released,
           {YEAR_EXPR} AS year,
           (fav.depot IS NOT NULL) AS favourite,
           {GROUP_EXPR} AS grp
    FROM depots d
    LEFT JOIN depot_meta m ON m.depot = d.depot
    LEFT JOIN favourites fav ON fav.depot = d.depot
"""

# Kept for the single-depot lookups, which never group.
SELECT_CARD = BASE_SELECT

# SQLite sorts NULL first on ASC, so "name IS NULL" has to lead every ordering
# that mentions a name - otherwise a year's worth of nameless depots is the
# first thing anyone sees.
ORDERS = {
    # Newest first is the default: a 2012 title is far more interesting to most
    # people than a 2003 platform depot, and the old A-Z default buried it.
    "newest": "year IS NULL, year DESC, name IS NULL, name COLLATE NOCASE ASC",
    "oldest": "year IS NULL, year ASC, name IS NULL, name COLLATE NOCASE ASC",
    "name": "name IS NULL, name COLLATE NOCASE ASC, depot ASC",
    "studio": ("developer IS NULL, developer COLLATE NOCASE ASC,"
               " name IS NULL, name COLLATE NOCASE ASC"),
    "versions": "total_versions DESC, depot ASC",
    "depot": "depot ASC",
    "updated": "grp_last DESC",
}
DEFAULT_SORT = "newest"


def filter_clauses(args):
    """Turn the browse query string into SQL. Shared by every catalogue view."""
    where, params = [], []

    q = (args.get("q") or "").strip()
    if q:
        if q.isdigit():
            where.append("(m.name LIKE ? OR d.depot = ? OR m.appid = ?"
                         " OR m.developer LIKE ? OR m.publisher LIKE ?)")
            params += [f"%{q}%", int(q), int(q), f"%{q}%", f"%{q}%"]
        else:
            where.append("(m.name LIKE ? OR m.developer LIKE ? OR m.publisher LIKE ?"
                         " OR m.franchise LIKE ?)")
            params += [f"%{q}%"] * 4

    scope = args.get("scope", "all")
    if scope == "named":
        where.append("m.name IS NOT NULL")
    elif scope == "unidentified":
        where.append("m.name IS NULL")
    elif scope == "reset":
        where.append("d.has_reset = 1")
    elif scope == "keyed":
        where.append("d.has_key = 1")
    elif scope == "nokey":
        where.append("d.has_key = 0")
    elif scope == "favourite":
        where.append("fav.depot IS NOT NULL")

    developer = (args.get("developer") or "").strip()
    if developer:
        where.append("m.developer = ?")
        params.append(developer)

    publisher = (args.get("publisher") or "").strip()
    if publisher:
        where.append("m.publisher = ?")
        params.append(publisher)

    # Genres are stored as a comma separated id list, so the match has to be on
    # a whole element - ",4," would otherwise also match id 41 or 14.
    genre = (args.get("genre") or "").strip()
    if genre and genre.isdigit():
        where.append("(',' || m.genres || ',') LIKE ?")
        params.append(f"%,{genre},%")

    year = (args.get("year") or "").strip()
    if year.isdigit():
        where.append(f"{YEAR_EXPR} = ?")
        params.append(year)

    return where, params


def grouped_sql(where, order, extra=""):
    """One row per game rather than per depot.

    The representative depot is the one with the deepest history, and the
    group's totals ride along beside it so a card can say "6 depots, 812
    versions" instead of appearing six times.
    """
    base = BASE_SELECT + (" WHERE " + " AND ".join(where) if where else "")
    return f"""
    WITH base AS ({base}),
    ranked AS (
        SELECT base.*,
               ROW_NUMBER() OVER (PARTITION BY grp ORDER BY blob_count DESC, depot ASC) rn,
               COUNT(*)        OVER (PARTITION BY grp) depot_count,
               SUM(blob_count) OVER (PARTITION BY grp) total_versions,
               MAX(has_key)    OVER (PARTITION BY grp) grp_has_key,
               MAX(has_reset)  OVER (PARTITION BY grp) grp_has_reset,
               MAX(favourite)  OVER (PARTITION BY grp) grp_favourite,
               MIN(first_date) OVER (PARTITION BY grp) grp_first,
               MAX(last_date)  OVER (PARTITION BY grp) grp_last
        FROM base
    )
    SELECT * FROM ranked WHERE rn = 1 ORDER BY {order} {extra}
    """


def grouped_count(where, params):
    base = BASE_SELECT + (" WHERE " + " AND ".join(where) if where else "")
    row = db.one(f"SELECT COUNT(DISTINCT grp) c FROM ({base})", params)
    return row["c"] if row else 0


def fetch_cards(where, params, order, limit, offset=0, probe_more=False):
    """A page of grouped cards, plus whether another page exists."""
    sql = grouped_sql(where, order, "LIMIT ? OFFSET ?")
    rows = db.query(sql, list(params) + [limit + (1 if probe_more else 0), offset])
    more = probe_more and len(rows) > limit
    items = [depot_card(r) for r in rows[:limit]]
    resolver.prioritise([i["depot"] for i in items if not i["identified"]])
    return items, more


# --------------------------------------------------------------------------- #
# static front end
# --------------------------------------------------------------------------- #
@app.route("/")
def home():
    return send_from_directory(config.WEB_DIR, "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(config.WEB_DIR, filename)


# --------------------------------------------------------------------------- #
# catalogue
# --------------------------------------------------------------------------- #
@app.get("/api/status")
def api_status():
    built = index.is_built()
    counts = {}
    if built:
        row = db.one("SELECT COUNT(*) c, SUM(has_reset) r, SUM(has_key) k FROM depots")
        files = db.one("SELECT COUNT(*) c FROM files")
        # Depots and titles are different numbers and the UI means the second
        # one whenever it says "titles" - one Steam app owns many depots.
        titles = db.one(
            "SELECT COUNT(DISTINCT COALESCE(m.appid, -d.depot - 1)) c"
            " FROM depots d LEFT JOIN depot_meta m ON m.depot = d.depot")
        counts = {
            "depots": row["c"], "reset_depots": row["r"] or 0,
            "keyed_depots": row["k"] or 0, "files": files["c"],
            "titles": titles["c"] if titles else 0,
        }
    return jsonify({
        "app": config.APP_NAME,
        "mirrors": net.mirror_status(),
        "index_built": built,
        "index": index.state(),
        "counts": counts,
        "resolver": resolver.stats() if built else {},
        "library": str(config.LIB_DIR),
        "active_jobs": jobs.active_count(),
        "extractor_ready": config.EXTRACTOR_PATH.exists(),
        "torrent": str(config.TORRENT_PATH) if config.TORRENT_PATH.exists() else None,
        "torrent_fallback": config.TORRENT_FALLBACK,
    })


@app.get("/api/storage")
def api_storage():
    return jsonify(storage.usage())


@app.get("/api/hardware")
def api_hardware():
    """What SteamFlix can throw at a key search, and what that actually buys."""
    info = keys.gpu_status()
    info["download_segments"] = config.SEGMENTS_PER_FILE
    info["segment_threshold"] = config.SEGMENT_THRESHOLD
    info["parallel_files"] = config.DOWNLOAD_THREADS
    return jsonify(info)


@app.post("/api/sync")
def api_sync():
    body = request.get_json(silent=True) or {}
    index.sync_async(refresh=bool(body.get("refresh")), force=True)
    return jsonify({"started": True})


@app.get("/api/library")
def api_library():
    sort = request.args.get("sort", DEFAULT_SORT)
    limit = min(int(request.args.get("limit", 60)), 500)
    offset = int(request.args.get("offset", 0))
    where, args = filter_clauses(request.args)
    order = ORDERS.get(sort, ORDERS[DEFAULT_SORT])

    items, _ = fetch_cards(where, args, order, limit, offset)
    return jsonify({
        "total": grouped_count(where, args),
        "offset": offset, "limit": limit, "items": items,
    })


@app.get("/api/facets")
def api_facets():
    """Studios, publishers, genres and years to filter by, with live counts.

    Counts respect every filter except the one being drawn, so picking a genre
    narrows the studio list rather than emptying it. They count games, not
    depots, for the same reason the grid does.
    """
    def counts(column, drop):
        args_without = {k: v for k, v in request.args.items() if k != drop}
        where, params = filter_clauses(args_without)
        where = [f"m.{column} IS NOT NULL AND m.{column} != ''"] + where
        base = BASE_SELECT + " WHERE " + " AND ".join(where)
        sql = (f"SELECT {column} AS v, COUNT(DISTINCT grp) c FROM ({base})"
               f" GROUP BY {column} ORDER BY c DESC, v COLLATE NOCASE ASC LIMIT 400")
        return [{"name": r["v"], "count": r["c"]} for r in db.query(sql, params)]

    args_without_year = {k: v for k, v in request.args.items() if k != "year"}
    where, params = filter_clauses(args_without_year)
    where = [f"{YEAR_EXPR} IS NOT NULL"] + where
    base = BASE_SELECT + " WHERE " + " AND ".join(where)
    years = [{"year": r["year"], "count": r["c"]} for r in db.query(
        f"SELECT year, COUNT(DISTINCT grp) c FROM ({base})"
        " GROUP BY year ORDER BY year DESC", params)]

    args_without_genre = {k: v for k, v in request.args.items() if k != "genre"}
    where, params = filter_clauses(args_without_genre)
    where = ["m.genres IS NOT NULL AND m.genres != ''"] + where
    base = BASE_SELECT + " WHERE " + " AND ".join(where)
    rows = db.query(f"SELECT genres, grp FROM ({base}) GROUP BY grp", params)

    tally = {}
    for row in rows:
        for gid in str(row["genres"]).split(","):
            gid = gid.strip()
            if gid in resolver.GENRES:
                tally[gid] = tally.get(gid, 0) + 1
    genres = sorted(
        ({"id": gid, "name": resolver.GENRES[gid], "count": n} for gid, n in tally.items()),
        key=lambda x: (-x["count"], x["name"]))

    return jsonify({
        "developers": counts("developer", "developer"),
        "publishers": counts("publisher", "publisher"),
        "genres": genres,
        "years": years,
        "details": resolver.detail_stats(),
        "favourites": db.one("SELECT COUNT(*) c FROM favourites")["c"],
    })


@app.post("/api/facets/backfill")
def api_facets_backfill():
    """Fill in studio and genre for depots resolved before those were recorded."""
    started = resolver.backfill_details()
    return jsonify({"apps": started, "details": resolver.detail_stats()})


# --------------------------------------------------------------------------- #
# favourites
# --------------------------------------------------------------------------- #
@app.get("/api/favourites")
def api_favourites():
    rows = db.query("SELECT depot FROM favourites ORDER BY added DESC")
    return jsonify({"depots": [r["depot"] for r in rows]})


@app.post("/api/favourites/<int:depot>")
def api_favourite_add(depot):
    """Favourite a game.

    Favouriting is done on whichever depot the card is standing for, and every
    depot of the same app goes with it - otherwise the heart would come back
    empty as soon as the grouping picked a different representative.
    """
    row = db.one("SELECT appid FROM depot_meta WHERE depot = ?", (depot,))
    appid = row["appid"] if row else None
    if appid:
        depots = [r["depot"] for r in db.query(
            "SELECT d.depot FROM depots d JOIN depot_meta m ON m.depot = d.depot"
            " WHERE m.appid = ?", (appid,))]
    else:
        depots = [depot]
    now = datetime.now().isoformat(timespec="seconds")
    with db.transaction() as conn:
        conn.executemany("INSERT OR REPLACE INTO favourites(depot, added) VALUES(?,?)",
                         [(d, now) for d in depots])
    return jsonify({"favourite": True, "depots": depots})


@app.delete("/api/favourites/<int:depot>")
def api_favourite_remove(depot):
    row = db.one("SELECT appid FROM depot_meta WHERE depot = ?", (depot,))
    appid = row["appid"] if row else None
    if appid:
        db.execute(
            "DELETE FROM favourites WHERE depot IN"
            " (SELECT depot FROM depot_meta WHERE appid = ?)", (appid,))
    else:
        db.execute("DELETE FROM favourites WHERE depot = ?", (depot,))
    return jsonify({"favourite": False})


# The home screen's fixed shelves. Genre and year shelves are generated from
# what the catalogue actually holds and appended to these.
SHELVES = [
    {"key": "favourites", "title": "Your Favourites",
     "subtitle": "Everything you hearted",
     "where": "fav.depot IS NOT NULL", "order": ORDERS["newest"]},
    {"key": "identified", "title": "Newest Arrivals",
     "subtitle": "The most recent games preserved on the mirror",
     "where": "m.name IS NOT NULL AND m.appid IS NOT NULL", "order": ORDERS["newest"]},
    {"key": "deepest", "title": "Deepest Histories",
     "subtitle": "Most versions preserved on the mirror",
     "where": "m.name IS NOT NULL", "order": "total_versions DESC"},
    {"key": "early", "title": "The Early Days",
     "subtitle": "Oldest content on the server",
     "where": "d.first_date IS NOT NULL", "order": "grp_first ASC"},
    {"key": "resets", "title": "Depot Resets",
     "subtitle": "Valve wiped these and started over - pick a variant",
     "where": "d.has_reset = 1", "order": "total_versions DESC"},
    {"key": "keyed", "title": "Ready to Extract",
     "subtitle": "Decryption key already available",
     "where": "d.has_key = 1 AND m.name IS NOT NULL", "order": ORDERS["newest"]},
    {"key": "unidentified", "title": "Unidentified Depots",
     "subtitle": "On the mirror, not in Steam's catalogue any more",
     "where": "m.name IS NULL AND m.state = 'failed'", "order": "total_versions DESC"},
]


def genre_shelves(limit=6):
    """A shelf per popular genre, biggest first."""
    rows = db.query(
        "SELECT genres, grp FROM (" + BASE_SELECT +
        " WHERE m.genres IS NOT NULL AND m.genres != '') GROUP BY grp")
    tally = {}
    for row in rows:
        for gid in str(row["genres"]).split(","):
            gid = gid.strip()
            if gid in resolver.GENRES:
                tally[gid] = tally.get(gid, 0) + 1
    top = sorted(tally.items(), key=lambda kv: -kv[1])[:limit]
    return [{
        "key": f"genre-{gid}",
        "title": resolver.GENRES[gid],
        "subtitle": f"{n:,} {resolver.GENRES[gid].lower()} titles on the mirror",
        "where": f"(',' || m.genres || ',') LIKE '%,{gid},%'",
        "order": ORDERS["newest"],
    } for gid, n in top]


def year_shelves(limit=5):
    """A shelf per year, newest first, so the home screen reads chronologically.

    Which years get a shelf is decided by how much the archive actually holds
    from them - picking the highest years instead gives a "From 2026" shelf with
    a single title in it, which looks broken rather than curated.
    """
    rows = db.query(
        f"SELECT year, COUNT(DISTINCT grp) c FROM ({BASE_SELECT}"
        f" WHERE {YEAR_EXPR} IS NOT NULL AND m.name IS NOT NULL)"
        " GROUP BY year ORDER BY c DESC LIMIT ?", (limit,))
    rows = sorted(rows, key=lambda r: str(r["year"]), reverse=True)
    return [{
        "key": f"year-{r['year']}",
        "title": f"From {r['year']}",
        "subtitle": f"{r['c']:,} titles dated {r['year']}",
        "where": f"{YEAR_EXPR} = '{r['year']}' AND m.name IS NOT NULL",
        "order": "total_versions DESC",
    } for r in rows if str(r["year"]).isdigit()]


def all_shelves(arrangement="mixed"):
    """The home screen's shelf order.

    "genre" and "year" put those shelves straight after the favourites, which is
    what the arrangement control on the home screen switches between.
    """
    fixed = list(SHELVES)
    if arrangement == "genre":
        return fixed[:2] + genre_shelves() + fixed[2:]
    if arrangement == "year":
        return fixed[:2] + year_shelves() + fixed[2:]
    return fixed[:2] + genre_shelves(3) + year_shelves(2) + fixed[2:]


def _shelf_page(shelf, offset, limit):
    where = [shelf["where"]] if shelf.get("where") else []
    return fetch_cards(where, (), shelf["order"], limit, offset, probe_more=True)


@app.get("/api/rows")
def api_rows():
    """Curated shelves for the home screen.

    ``per`` and ``shelves`` keep the first paint small: the default light start
    asks for a couple of shelves of a dozen cards instead of the whole wall, and
    the rest arrives only when someone actually asks for it.
    """
    per = max(1, min(int(request.args.get("per", 24)), 60))
    arrangement = request.args.get("arrange", "mixed")
    shelves = all_shelves(arrangement)
    count = max(1, min(int(request.args.get("shelves", len(shelves))), len(shelves)))
    offset = max(0, int(request.args.get("offset", 0)))

    out = []
    for shelf in shelves[offset:offset + count]:
        items, more = _shelf_page(shelf, 0, per)
        if not items:
            continue
        out.append({"key": shelf["key"], "title": shelf["title"],
                    "subtitle": shelf["subtitle"], "items": items,
                    "offset": len(items), "more": more})

    heroes = []
    if offset == 0:
        # A rotating hero rather than a single fixed one: the home screen is a
        # shop window, and one frozen cover makes the whole archive look empty.
        rows = db.query(grouped_sql(
            ["m.appid IS NOT NULL", "d.has_key = 1", "m.name IS NOT NULL"],
            "total_versions DESC", "LIMIT 40"))
        cards = [depot_card(r) for r in rows]
        import random
        random.shuffle(cards)
        heroes = cards[:8]

    return jsonify({
        "hero": heroes[0] if heroes else None,   # kept for older callers
        "heroes": heroes,
        "shelves": out,
        "shelf_offset": offset,
        "shelf_total": len(shelves),
        "arrange": arrangement,
        "more_shelves": offset + count < len(shelves),
    })


@app.get("/api/shelf/<key>")
def api_shelf(key):
    """One more page of a single home shelf."""
    shelf = next((sh for sh in all_shelves(request.args.get("arrange", "mixed"))
                  if sh["key"] == key), None)
    if shelf is None:
        return jsonify({"error": "unknown shelf"}), 404
    offset = max(0, int(request.args.get("offset", 0)))
    limit = max(1, min(int(request.args.get("limit", 24)), 60))
    items, more = _shelf_page(shelf, offset, limit)
    return jsonify({"key": key, "items": items, "offset": offset + len(items),
                    "more": more})


@app.get("/api/depot/<int:depot>")
def api_depot(depot):
    row = db.one(SELECT_CARD + " WHERE d.depot = ?", (depot,))
    if row is None:
        return jsonify({"error": "unknown depot"}), 404
    card = depot_card(row)

    blobs = db.query(
        "SELECT version, crc, filename, mtime, size FROM files"
        " WHERE depot = ? AND kind = 'blob' ORDER BY version DESC, mtime DESC",
        (depot,),
    )
    by_version = {}
    for b in blobs:
        by_version.setdefault(b["version"], []).append(dict(b))
    versions = [
        {"version": v, "variants": vs, "reset": len(vs) > 1,
         "date": vs[0]["mtime"], "crc": vs[0]["crc"]}
        for v, vs in sorted(by_version.items(), reverse=True)
    ]

    card["version_list"] = versions
    card["cached_bytes"] = jobs.cached_size(depot)
    card["state"] = row["state"]
    manifest = db.one(
        "SELECT * FROM manifest_cache WHERE depot = ? ORDER BY version DESC LIMIT 1", (depot,)
    )
    card["manifest"] = dict(manifest) if manifest else None

    # Cards are one per game, so the dialog has to be able to reach the game's
    # other depots - a big title keeps its content, its language packs and its
    # tools in separate ones.
    siblings = []
    if row["appid"]:
        siblings = [{
            "depot": r["depot"], "versions": r["blob_count"],
            "max_version": r["max_version"], "reset": bool(r["has_reset"]),
            "has_key": bool(r["has_key"]), "first_date": r["first_date"],
            "last_date": r["last_date"], "current": r["depot"] == depot,
        } for r in db.query(
            "SELECT d.* FROM depots d JOIN depot_meta m ON m.depot = d.depot"
            " WHERE m.appid = ? ORDER BY d.blob_count DESC, d.depot ASC", (row["appid"],))]
    card["siblings"] = siblings
    card["depot_count"] = len(siblings) or 1
    return jsonify(card)


@app.post("/api/depot/<int:depot>/peek")
def api_peek(depot):
    """Download just the blob for one version and read its manifest, which
    gives the real file count and installed size without pulling any dats."""
    body = request.get_json(silent=True) or {}
    version = int(body.get("version", 0))
    crc = (body.get("crc") or "").lower() or None

    sql = "SELECT * FROM files WHERE depot = ? AND kind = 'blob' AND version = ?"
    args = [depot, version]
    if crc:
        sql += " AND crc = ?"
        args.append(crc)
    row = db.one(sql + " LIMIT 1", args)
    if row is None:
        return jsonify({"error": "no such blob"}), 404

    blob_dir, _ = jobs.depot_dirs(depot)
    path = blob_dir / row["filename"]
    try:
        size = chain.remote_size("blob", row["filename"])
        net.download(chain.path_for("blob", row["filename"]), path, expected_size=size)
        info = blobmod.describe(path.read_bytes())
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502

    man = info.get("manifest") or {}
    db.execute(
        "INSERT OR REPLACE INTO manifest_cache"
        "(depot, version, crc, appid, verid, file_count, total_bytes, dat_size, prev_crc, root_dirs)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (depot, version, row["crc"], man.get("appid"), man.get("verid"),
         man.get("file_count"), man.get("total_bytes"), info.get("dat_size"),
         info.get("prev_crc"), ",".join(man.get("root_dirs") or [])),
    )
    return jsonify({
        "depot": depot, "version": version, "crc": row["crc"],
        "dat_size": info.get("dat_size"), "prev_crc": info.get("prev_crc"),
        "manifest": man, "manifest_error": info.get("manifest_error"),
    })


@app.post("/api/depot/<int:depot>/plan")
def api_plan(depot):
    """Size up a download without starting it. Only works without network
    walking for linear depots; reset depots need the chain walk a job does."""
    body = request.get_json(silent=True) or {}
    version = int(body.get("version", 0))
    try:
        plan = chain.plan_linear(depot, version)
    except chain.ChainError as exc:
        return jsonify({"error": str(exc)}), 400
    if body.get("sizes", True):
        chain.size_up(plan)
    return jsonify(plan.to_dict())


@app.delete("/api/depot/<int:depot>/cache")
def api_drop_cache(depot):
    return jsonify({"removed": jobs.delete_depot_cache(depot)})


# --------------------------------------------------------------------------- #
# depot keys
# --------------------------------------------------------------------------- #
@app.get("/api/depot/<int:depot>/key")
def api_key_status(depot):
    """Whether this depot needs a real key, and whether we have one.

    The check only needs the blob, which is small, so it downloads that and
    reads the per-file compression modes out of the checksum table.
    """
    version = int(request.args.get("version", 0))
    crc = (request.args.get("crc") or "").lower() or None

    sql = "SELECT * FROM files WHERE depot = ? AND kind = 'blob' AND version = ?"
    args = [depot, version]
    if crc:
        sql += " AND crc = ?"
        args.append(crc)
    row = db.one(sql + " ORDER BY version DESC LIMIT 1", args)
    if row is None:
        return jsonify({"error": "no such blob"}), 404

    blob_dir, _ = jobs.depot_dirs(depot)
    path = blob_dir / row["filename"]
    try:
        size = chain.remote_size("blob", row["filename"])
        net.download(chain.path_for("blob", row["filename"]), path, expected_size=size)
        info = keys.analyse(path)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502

    depot_row = db.one("SELECT has_key FROM depots WHERE depot = ?", (depot,))
    key_row = keys.known_key_row(depot)
    known = key_row["key"] if key_row else None
    key_source = key_row["source"] if key_row else None
    mode_names = {0: "stored", 1: "compressed", 2: "compressed + encrypted", 3: "encrypted"}

    # A key can only be tested against real encrypted bytes, so say plainly
    # whether the dat that makes that possible is already here.
    pairs = _local_pairs(depot, prefer_version=version)
    dat_ready = bool(pairs)

    # "We have a key" is only worth saying when the key actually decrypts the
    # depot - the bundled table has wrong entries, and imported lists can be for
    # a different dump entirely.
    verified = None
    if known and pairs:
        names_by_blob = {}
        for bp, _dp in pairs:
            try:
                names_by_blob[str(bp)] = blobmod.manifest_names(bp.read_bytes())
            except Exception:  # noqa: BLE001 - names only sharpen mode 3 probes
                continue
        try:
            verified = keys.verify_key(known, pairs, names_by_blob)
            if verified is None and info["needs_key"]:
                # Proved wrong against this depot's own data: drop the claim so
                # the card, the shelves and this dialog stop disagreeing.
                keys.reject_key(depot, known)
                depot_row = db.one("SELECT has_key FROM depots WHERE depot = ?", (depot,))
        except Exception:  # noqa: BLE001 - verification must never break the panel
            verified = None

    return jsonify({
        "depot": depot,
        "version": version,
        "needs_key": info["needs_key"],
        "files": info["files"],
        "encrypted_files": info["encrypted_files"],
        "modes": {mode_names.get(k, str(k)): v for k, v in info["modes"].items()},
        "bundled": key_source == "bundled",
        "has_key": bool(depot_row and depot_row["has_key"]),
        "known_key": known,
        "key_source": key_source,
        "key_verified": verified,      # exact | likely | weak | unknown | None
        "dat_ready": dat_ready,
        "candidate_keys": db.one("SELECT COUNT(*) c FROM depot_keys")["c"],
        "verdict": (
            "No key needed - nothing in this depot is encrypted. The extractor only "
            "refuses to start because it always demands a key."
            if not info["needs_key"] else
            "A real key is required for this depot."
        ),
    })


@app.post("/api/depot/<int:depot>/key/trial")
def api_key_trial(depot):
    """Hunt for a working key by decrypting one chunk with each candidate.

    Needs the dat for the chosen version, so it is only offered once that file
    is already in the local cache.
    """
    body = request.get_json(silent=True) or {}
    version = int(body.get("version", 0))
    crc = (body.get("crc") or "").lower() or None

    blob_dir, dat_dir = jobs.depot_dirs(depot)
    sql = "SELECT * FROM files WHERE depot = ? AND kind = 'blob' AND version = ?"
    args = [depot, version]
    if crc:
        sql += " AND crc = ?"
        args.append(crc)
    blob_row = db.one(sql + " LIMIT 1", args)
    if blob_row is None:
        return jsonify({"error": "no such blob"}), 404
    blob_path = blob_dir / blob_row["filename"]

    # Any version already on disk can supply a test chunk, so the trial is not
    # limited to the one that happens to be selected in the dialog.
    pairs = _local_pairs(depot, prefer_version=version)
    if not pairs:
        return jsonify({
            "error": "no dat for this depot is downloaded yet - a key can only be tested "
                     "against real encrypted data. Download the depot first; the trial "
                     "runs automatically as part of the job."
        }), 409

    try:
        if not blob_path.exists():
            net.download(chain.path_for("blob", blob_row["filename"]), blob_path,
                         expected_size=chain.remote_size("blob", blob_row["filename"]))
        names_by_blob = {}
        for bp, _dp in pairs:
            try:
                names_by_blob[str(bp)] = blobmod.manifest_names(bp.read_bytes())
            except Exception:  # noqa: BLE001 - names only sharpen mode 3 probes
                continue
        top_blob, top_dat = pairs[0]
        result = keys.trial(depot, top_blob, top_dat, pairs=pairs,
                            names_by_blob=names_by_blob,
                            extra_keys=body.get("keys") or ())
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500
    return jsonify(result)


def _local_pairs(depot, prefer_version=None):
    """Every (blob, dat) pair for this depot already in the cache, newest first.

    Pairing is by version number and confirmed by both files being present, so a
    half-finished download cannot feed the key trial a dat with no table.
    """
    blob_dir, dat_dir = jobs.depot_dirs(depot)
    if not (blob_dir.exists() and dat_dir.exists()):
        return []

    def version_of(path):
        parts = path.name.split("_")
        try:
            return int(parts[1])
        except (IndexError, ValueError):
            return None

    dats = {}
    for cand in dat_dir.iterdir():
        v = version_of(cand)
        if v is not None:
            dats.setdefault(v, cand)

    pairs = []
    for cand in blob_dir.iterdir():
        v = version_of(cand)
        if v is not None and v in dats:
            pairs.append((v, cand, dats[v]))

    def rank(entry):
        v = entry[0]
        return (0 if v == prefer_version else 1, -v)

    return [(b, d) for _v, b, d in sorted(pairs, key=rank)]


@app.post("/api/keys/sweep")
def api_key_sweep():
    """Run the trial against every depot whose files are already cached.

    Nothing is downloaded: this only revisits depots you have already pulled,
    which is where a newly imported key list pays off.
    """
    results, recovered = [], 0
    for row in db.query(
        "SELECT d.depot, m.name FROM depots d LEFT JOIN depot_meta m ON m.depot = d.depot"
        " WHERE d.has_key = 0"
    ):
        depot = row["depot"]
        pairs = _local_pairs(depot)
        if not pairs:
            continue
        try:
            names_by_blob = {}
            for bp, _dp in pairs:
                try:
                    names_by_blob[str(bp)] = blobmod.manifest_names(bp.read_bytes())
                except Exception:  # noqa: BLE001
                    continue
            res = keys.trial(depot, pairs[0][0], pairs[0][1], pairs=pairs,
                             names_by_blob=names_by_blob)
        except Exception as exc:  # noqa: BLE001 - one bad depot must not stop the sweep
            results.append({"depot": depot, "name": row["name"], "error": str(exc)})
            continue
        if res.get("key"):
            recovered += 1
        results.append({"depot": depot, "name": row["name"], "key": res.get("key"),
                        "needs_key": res.get("needs_key"), "reason": res.get("reason"),
                        "confidence": res.get("confidence")})
    keys.refresh_has_key()
    return jsonify({"checked": len(results), "recovered": recovered, "results": results})


@app.post("/api/keys/import")
def api_import_keys():
    """Take a pasted key list or a path to a key file from an old backup."""
    body = request.get_json(silent=True) or {}
    text = body.get("text") or ""
    if body.get("path"):
        p = Path(body["path"])
        if not p.exists():
            return jsonify({"error": f"{p} does not exist"}), 404
        text += "\n" + p.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return jsonify({"error": "nothing to import"}), 400
    added = keys.import_user_keys(text, source=body.get("source", "user"))
    total = db.one("SELECT COUNT(*) c FROM depot_keys")["c"]
    return jsonify({"imported": added, "total_keys": total})


@app.get("/api/keys")
@app.get("/api/keys/summary")
def api_keys_summary():
    keys.bundled_keys()
    keys.refresh_has_key()
    rows = db.query("SELECT source, COUNT(*) c FROM depot_keys GROUP BY source")
    depots = db.one("SELECT COUNT(*) c, SUM(has_key) k FROM depots")
    return jsonify({
        "by_source": {r["source"]: r["c"] for r in rows},
        "total": db.one("SELECT COUNT(*) c FROM depot_keys")["c"],
        "depots_with_key": depots["k"] or 0,
        "depots_without_key": (depots["c"] or 0) - (depots["k"] or 0),
        "key_file": str(keys.user_key_file()),
        "found_file": str(config.DATA_DIR / "found_keys.txt"),
    })


@app.post("/api/resolve")
def api_resolve():
    body = request.get_json(silent=True) or {}
    depots = [int(d) for d in body.get("depots", [])]
    if body.get("sync") and len(depots) == 1:
        return jsonify(resolver.resolve_depot(depots[0]))
    return jsonify({"queued": resolver.prioritise(depots)})


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #
@app.post("/api/jobs")
def api_create_job():
    body = request.get_json(silent=True) or {}
    depot = int(body["depot"])
    version = int(body.get("version", 0))
    crc = (body.get("crc") or "").lower() or None

    row = db.one("SELECT has_reset FROM depots WHERE depot = ?", (depot,))
    if row is None:
        return jsonify({"error": "unknown depot"}), 404
    if row["has_reset"] and not crc:
        return jsonify({
            "error": "this depot was reset - choose which blob variant you want",
            "variants": chain.variants(depot, version),
        }), 400

    job = jobs.submit(
        depot=depot, version=version, crc=crc,
        title=body.get("title") or f"Depot {depot}",
        do_extract=body.get("extract", True),
        file_filter=body.get("filter"),
        key_override=body.get("key"),
    )
    return jsonify(job.to_dict())


@app.get("/api/jobs")
def api_jobs():
    return jsonify({"jobs": [j.to_dict() for j in jobs.all_jobs()]})


@app.get("/api/jobs/<job_id>")
def api_job(job_id):
    job = jobs.get(job_id)
    if job is None:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(job.to_dict(with_log=True))


@app.post("/api/jobs/<job_id>/cancel")
def api_cancel(job_id):
    job = jobs.get(job_id)
    if job is None:
        return jsonify({"error": "unknown job"}), 404
    job.cancel()
    return jsonify(job.to_dict())


@app.post("/api/jobs/clear")
def api_clear_jobs():
    jobs.clear_finished()
    return jsonify({"ok": True})


@app.get("/api/installed")
def api_installed():
    return jsonify({"items": jobs.installed()})


# --------------------------------------------------------------------------- #
# play / watch
# --------------------------------------------------------------------------- #
@app.get("/api/content/<path:folder>")
def api_content(folder):
    """Inventory of one extracted depot, plus what SteamFlix would open."""
    try:
        target = media.resolve(folder)
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    if not target.is_dir():
        return jsonify({"error": "not extracted yet"}), 404
    return jsonify(media.scan(target))


@app.get("/api/file/<path:rel>")
def api_file(rel):
    """Serve a file out of the extracted library with byte-range support so the
    browser's video player can seek."""
    try:
        target = media.resolve(rel)
    except PermissionError as exc:
        return Response(str(exc), status=403)
    if not target.is_file():
        return Response("not found", status=404)

    size = target.stat().st_size
    mime = media.guess_type(target)
    range_header = request.headers.get("Range")
    if not range_header:
        resp = send_file(target, mimetype=mime, conditional=True)
        resp.headers["Accept-Ranges"] = "bytes"
        return resp

    m = re.match(r"bytes=(\d*)-(\d*)", range_header)
    start = int(m.group(1)) if m and m.group(1) else 0
    end = int(m.group(2)) if m and m.group(2) else size - 1
    start = max(0, min(start, size - 1))
    end = max(start, min(end, size - 1))
    length = end - start + 1

    def stream():
        with open(target, "rb") as fh:
            fh.seek(start)
            left = length
            while left > 0:
                chunk = fh.read(min(1 << 20, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk

    resp = Response(stream(), status=206, mimetype=mime, direct_passthrough=True)
    resp.headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Length"] = str(length)
    return resp


@app.post("/api/launch")
def api_launch():
    """Start a game executable, or hand a file to its default Windows app."""
    body = request.get_json(silent=True) or {}
    rel = body.get("rel")
    if not rel:
        return jsonify({"error": "no file given"}), 400
    try:
        target = media.resolve(rel)
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    if not target.exists():
        return jsonify({"error": "file is gone"}), 404
    try:
        if body.get("shell") or target.suffix.lower() not in media.RUN_EXT:
            media.open_with_default(target)
            return jsonify({"ok": True, "launched": target.name, "running": True})
        result = media.launch(target)
    except Exception as exc:  # noqa: BLE001
        logbook.add("launch", f"could not launch {target.name}", detail=str(exc))
        return jsonify({"error": str(exc)}), 400

    # Started and still alive after the settle window: as good as it gets from
    # out here. Gone already means it failed, and the exit code usually says how.
    if result["running"]:
        return jsonify({"ok": True, "launched": target.name, **result})

    code = result["exit_code"]
    if code == 0:
        return jsonify({"ok": True, "launched": target.name, "note":
                        "the program ran and exited straight away", **result})
    hint = media.LAUNCH_HINTS.get(code)
    detail = f"exit code {code}" + (f" - {hint}" if hint else "")
    logbook.warn("launch", f"{target.name} exited immediately", detail=detail)
    return jsonify({
        "ok": False, "launched": target.name, **result,
        "error": f"{target.name} started and closed immediately ({detail}). "
                 f"Old Steam2 builds often need their original installer's runtimes, "
                 f"or a compatibility mode.",
    }), 200


@app.post("/api/open")
def api_open():
    """Reveal a folder in Explorer."""
    import os
    body = request.get_json(silent=True) or {}
    path = body.get("path")
    if not path:
        return jsonify({"error": "no path"}), 400
    root = config.LIB_DIR.resolve()
    if not media.safe_under(root, Path(path)):
        return jsonify({"error": "path is outside the library"}), 403
    try:
        os.startfile(path)  # noqa: S606 - local desktop convenience
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True})


@app.delete("/api/installed/<path:folder>")
def api_delete_installed(folder):
    import shutil
    try:
        target = media.resolve(folder)
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    if not target.is_dir():
        return jsonify({"error": "not found"}), 404
    shutil.rmtree(target, ignore_errors=True)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# logs
# --------------------------------------------------------------------------- #
@app.get("/api/logs")
def api_logs():
    return jsonify({
        "summary": logbook.summary(),
        "entries": logbook.entries(
            limit=min(int(request.args.get("limit", 200)), 600),
            category=request.args.get("category"),
            level=request.args.get("level"),
            since=int(request.args["since"]) if request.args.get("since") else None,
        ),
        "categories": sorted(logbook.HINTS.keys()),
    })


@app.post("/api/logs/clear")
def api_clear_logs():
    logbook.clear()
    return jsonify({"ok": True})


@app.get("/api/torrent")
def api_torrent_status():
    """What the built-in torrent fallback can do right now."""
    info = torrentmod.status()
    info["enabled"] = config.TORRENT_FALLBACK
    path = config.TORRENT_PATH
    info["size"] = path.stat().st_size if path.exists() else 0
    info["note"] = ("SteamFlix fetches missing blobs and dats from the swarm itself "
                    "when every mirror refuses them, downloading only the pieces "
                    "covering the file it needs - and seeds back the files it "
                    "already holds.")
    info["seed"] = seed.status()
    return jsonify(info)


@app.post("/api/torrent/index")
def api_torrent_index():
    """Build (or rebuild) the torrent's file table."""
    try:
        n = torrentmod.build_index(force=bool((request.get_json(silent=True) or {})
                                              .get("force")))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    return jsonify({"files": n, **torrentmod.status()})


@app.post("/api/torrent/peers")
def api_torrent_peers():
    """Ask the trackers who is sharing the archive at the moment.

    Worth having as its own button: it answers "is the swarm alive?" without
    committing to a download.
    """
    if not torrentmod.available():
        return jsonify({"error": "no steam2.torrent alongside SteamFlix"}), 404
    try:
        found = torrentmod.peers(limit=200, deadline=20)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502
    return jsonify({"peers": len(found),
                    "sample": [f"{ip}:{port}" for ip, port in found[:12]]})


@app.get("/api/torrent/seed")
def api_seed_status():
    """What SteamFlix is currently giving back to the swarm."""
    return jsonify(seed.status())


@app.post("/api/torrent/seed")
def api_seed_control():
    """Start or stop seeding by hand.

    Turning it on here also writes the setting, so it survives a restart - a
    button that quietly forgot itself would be worse than no button at all.
    """
    if not torrentmod.available():
        return jsonify({"error": "no steam2.torrent alongside SteamFlix"}), 404
    body = request.get_json(silent=True) or {}
    if "enabled" in body:
        settings.update({"seed_enabled": bool(body["enabled"])})
    return jsonify(seed.apply_settings())


@app.post("/api/torrent/seed/verify")
def api_seed_verify():
    """Re-read the library for anything new that can be seeded."""
    if not torrentmod.available():
        return jsonify({"error": "no steam2.torrent alongside SteamFlix"}), 404
    seed.rescan()
    return jsonify(seed.status())


# --------------------------------------------------------------------------- #
# shutting down
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
@app.get("/api/settings")
def api_settings():
    conf = settings.all()
    return jsonify({
        "settings": conf,
        "defaults": settings.DEFAULTS,
        "mirror_status": net.mirror_status(),
        "throttle": net.throttle_status(),
        "proxies": proxies.status(),
    })


@app.put("/api/settings")
def api_settings_save():
    body = request.get_json(silent=True) or {}
    conf = settings.update(body)
    logbook.info("index", "settings updated",
                 detail=f"source={conf['source_mode']}, "
                        f"delay={conf['request_delay_ms']}ms, "
                        f"mirrors={len(conf['mirrors'])}")
    # Seeding settings are live: a changed port, cap or switch takes effect now
    # rather than on the next restart.
    threading.Thread(target=seed.apply_settings, daemon=True).start()
    return jsonify({"settings": conf, "mirror_status": net.mirror_status()})


@app.post("/api/settings/reset")
def api_settings_reset():
    return jsonify({"settings": settings.reset()})


@app.post("/api/mirrors/test")
def api_mirror_test():
    """Check one mirror before it is added to the list.

    Adding a dead host would otherwise just slow every download down until it
    gets benched.
    """
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip().rstrip("/")
    if not url:
        return jsonify({"error": "no URL given"}), 400
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    started = time.time()
    try:
        r = net.session().get(url.rstrip("/") + "/blobs_dates.txt",
                              headers={"Range": "bytes=0-63"}, timeout=12, stream=True)
        body_head = r.raw.read(64) if r.status_code in (200, 206) else b""
        r.close()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"url": url, "ok": False, "error": str(exc)[:200]})

    ms = int((time.time() - started) * 1000)
    # A Steam2 listing starts "<depot>_<version>_<crc8>_<sha256>.blob<TAB><date>",
    # and only 64 bytes come back, so the check has to recognise the shape of
    # that first line rather than wait for the extension or the tab.
    looks_right = bool(re.match(rb"\d+_\d+_[0-9a-fA-F]{8}_[0-9a-fA-F]{8}", body_head))
    return jsonify({
        "url": url,
        "ok": r.status_code in (200, 206) and looks_right,
        "status": r.status_code,
        "ms": ms,
        "ranges": r.status_code == 206,
        "sample": body_head.decode("utf-8", "replace")[:80],
        "note": None if looks_right else
                "That URL answered, but not with a Steam2 file listing - check "
                "it points at the root of a mirror.",
    })


# --------------------------------------------------------------------------- #
# proxies
# --------------------------------------------------------------------------- #
@app.get("/api/proxies")
def api_proxies():
    return jsonify({**proxies.status(), "pool": proxies.pool()})


@app.post("/api/proxies/refresh")
def api_proxies_refresh():
    """Check the configured proxies, optionally topping up from a public list."""
    body = request.get_json(silent=True) or {}
    try:
        result = proxies.refresh(include_public=bool(body.get("public")),
                                 want=int(body.get("want", 25)))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502
    return jsonify({**result, **proxies.status()})


# --------------------------------------------------------------------------- #
# diagnostics
# --------------------------------------------------------------------------- #
@app.get("/api/diagnostics")
def api_diagnostics():
    """Whole-install health check."""
    deep = request.args.get("deep", "1") not in ("0", "false", "no")
    return jsonify(diagnostics.run(deep=deep))


@app.post("/api/installed/<path:folder>/verify")
def api_verify_install(folder):
    """Is this extracted game actually complete, and is there anything to run?"""
    result = diagnostics.verify_install(folder)
    if "error" in result:
        return jsonify(result), 404
    return jsonify(result)


@app.post("/api/shutdown")
def api_shutdown():
    """Stop the server from the UI.

    SteamFlix is a local app people start from a .bat file, so the honest way to
    close it is a button rather than telling someone to hunt for the console
    window. Running jobs are cancelled first so nothing is left half-written.
    """
    body = request.get_json(silent=True) or {}
    cancelled = jobs.cancel_all() if body.get("cancel_jobs", True) else 0

    def stop():
        time.sleep(0.4)                 # let this response reach the browser
        # Leave the swarm properly: tell the trackers we are gone and hand the
        # router its port back, rather than lingering as a peer nobody can
        # reach for the next half hour.
        try:
            if seed.running():
                seed.stop()
                time.sleep(1.2)
        except Exception:  # noqa: BLE001 - never block the shutdown itself
            pass
        os._exit(0)

    threading.Thread(target=stop, daemon=True).start()
    return jsonify({"stopping": True, "cancelled_jobs": cancelled})


def bootstrap():
    """Prepare the database, catalogue and background workers."""
    config.ensure_dirs()
    db.init()
    jobs.start()

    def seeding():
        # Whatever is already in the library can go straight back out, so this
        # waits for neither the catalogue nor a download.
        try:
            seed.start()
        except Exception as exc:  # noqa: BLE001 - never block startup on it
            logbook.warn("torrent", "seeding did not start", detail=str(exc))

    if torrentmod.available():
        threading.Thread(target=seeding, daemon=True).start()

    def warm():
        if not index.is_built():
            index.sync()
        else:
            index.sync_async(refresh=False)
        index.wait_until_built()
        # Keys recovered on an earlier run live in found_keys.txt as well as in
        # the database, so a rebuilt catalogue does not lose them.
        try:
            keys.bundled_keys()
            keys.load_found_keys()
            keys.refresh_has_key()
        except Exception as exc:  # noqa: BLE001 - never block the catalogue on keys
            logbook.warn("key", "could not reload saved keys", detail=str(exc))
        resolver.start()
        try:
            resolver.backfill_details()
        except Exception as exc:  # noqa: BLE001
            logbook.warn("metadata", "studio/genre backfill did not start",
                         detail=str(exc))

    threading.Thread(target=warm, daemon=True).start()
    return app
