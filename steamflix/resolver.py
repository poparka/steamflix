"""Turns bare depot ids into game names and artwork.

SteamDB is the natural source for this, but steamdb.info sits behind Cloudflare
and answers plain HTTP clients with 403, so it cannot be scraped from a local
app. Instead the same underlying PICS data is read from the public mirror at
api.steamcmd.net, and the UI deep-links to the matching SteamDB pages.

Resolution walks backwards from the depot id looking for an app that lists it
(depot ids are nearly always the owning app id plus a small offset). Because one
app answer resolves every depot it owns, the whole 10k-depot catalogue costs far
fewer requests than it looks.
"""
import json
import queue
import threading
import time
from datetime import datetime

from . import config, db, logbook, net

# Old Steam2 platform depots that predate the modern app catalogue entirely.
STATIC_NAMES = {
    0: "Steam Client (Win32)",
    1: "Steam Client (Win32 Beta)",
    2: "Steam Client Content",
    3: "Steam Client Base",
    4: "Steam Client Common",
    5: "Steam Platform Content",
    6: "Steam Media Content",
    7: "Steam Client Support",
}

# Steam's genre ids are stable and small, so the mapping lives here rather than
# costing a request per app.
GENRES = {
    "1": "Action", "2": "Strategy", "3": "RPG", "4": "Casual", "9": "Racing",
    "18": "Sports", "23": "Indie", "25": "Adventure", "28": "Simulation",
    "29": "Massively Multiplayer", "37": "Free to Play", "50": "Accounting",
    "51": "Animation & Modeling", "52": "Audio Production",
    "53": "Design & Illustration", "54": "Education", "55": "Photo Editing",
    "56": "Software Training", "57": "Utilities", "58": "Video Production",
    "59": "Web Publishing", "60": "Game Development", "70": "Early Access",
    "71": "Sexual Content", "72": "Nudity", "73": "Violent", "74": "Gore",
    "81": "Documentary", "84": "Tutorial",
}


def genre_names(ids):
    """'2,23' -> ['Strategy', 'Indie'], skipping ids Steam has retired."""
    if not ids:
        return []
    out = []
    for gid in str(ids).split(","):
        gid = gid.strip()
        if not gid:
            continue
        name = GENRES.get(gid)
        if name and name not in out:
            out.append(name)
    return out


def _associations(common):
    """Pull developer / publisher / franchise out of PICS.

    ``common.associations`` is a numbered map of {name, type}. Ports get their
    own entries ("Aspyr (Mac)"), so the first of each type is the one that
    actually made the build these Steam2 depots hold.
    """
    devs, pubs, franchises = [], [], []
    assoc = common.get("associations") or {}
    values = assoc.values() if isinstance(assoc, dict) else assoc
    for item in values:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        kind = (item.get("type") or "").strip().lower()
        if not name:
            continue
        bucket = {"developer": devs, "publisher": pubs, "franchise": franchises}.get(kind)
        if bucket is not None and name not in bucket:
            bucket.append(name)
    return devs, pubs, franchises


def _details(app):
    """Studio, publisher, franchise, genres and release date for one PICS app."""
    common = app.get("common") or {}
    extended = app.get("extended") or {}
    devs, pubs, franchises = _associations(common)

    developer = devs[0] if devs else (extended.get("developer") or "").strip() or None
    publisher = pubs[0] if pubs else (extended.get("publisher") or "").strip() or None
    franchise = franchises[0] if franchises else None

    genres = common.get("genres") or {}
    ids = list(genres.values()) if isinstance(genres, dict) else list(genres)
    primary = common.get("primary_genre")
    if primary and str(primary) not in [str(i) for i in ids]:
        ids.insert(0, primary)
    genre_ids = ",".join(str(i).strip() for i in ids if str(i).strip())

    released = None
    stamp = common.get("steam_release_date")
    if stamp:
        try:
            released = datetime.utcfromtimestamp(int(stamp)).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            released = None

    return developer, publisher, franchise, genre_ids or None, released


_queue: "queue.PriorityQueue" = queue.PriorityQueue()
_seen = set()
_seen_lock = threading.Lock()
_workers = []
_stats = {"resolved": 0, "failed": 0, "queued": 0, "running": False}


# --------------------------------------------------------------------------- #
# app lookups
# --------------------------------------------------------------------------- #
def _cached_app(appid: int):
    return db.one("SELECT * FROM app_cache WHERE appid = ?", (appid,))


def _row_to_app(appid, row):
    keys_ = row.keys()
    get = lambda k: (row[k] if k in keys_ else None)
    return {
        "appid": appid,
        "name": row["name"],
        "type": row["app_type"],
        "depots": [int(x) for x in (row["depots"] or "").split(",") if x],
        "developer": get("developer"),
        "publisher": get("publisher"),
        "franchise": get("franchise"),
        "genres": get("genres"),
        "released": get("released"),
    }


def fetch_app(appid: int, force=False):
    """PICS record for an app id, cached forever (including misses).

    ``force`` re-fetches an app that was cached before SteamFlix started
    recording studio and genre, which is how the backfill fills those in
    without throwing the rest of the cache away.
    """
    row = _cached_app(appid)
    if row is not None and not force:
        if not row["found"]:
            return None
        if row["detailed"]:
            return _row_to_app(appid, row)
        # Cached by an older build that had no studio/genre columns: fall
        # through, re-fetch once, and never pay for it again.

    data = net.get_json(config.STEAMCMD_API.format(appid=appid))
    app = ((data or {}).get("data") or {}).get(str(appid)) or {}
    common = app.get("common") or {}
    name = common.get("name")
    if not name:
        if data is None and row is not None and row["found"]:
            return _row_to_app(appid, row)      # transient failure: keep what we had
        db.execute(
            "INSERT OR REPLACE INTO app_cache"
            "(appid, name, app_type, depots, found, fetched, detailed)"
            " VALUES(?,NULL,NULL,NULL,0,?,1)",
            (appid, datetime.now().isoformat(timespec="seconds")),
        )
        return None

    depots = [int(k) for k in (app.get("depots") or {}) if k.isdigit()]
    developer, publisher, franchise, genres, released = _details(app)
    db.execute(
        "INSERT OR REPLACE INTO app_cache"
        "(appid, name, app_type, depots, found, fetched,"
        " developer, publisher, franchise, genres, released, detailed)"
        " VALUES(?,?,?,?,1,?,?,?,?,?,?,1)",
        (appid, name, common.get("type"), ",".join(str(d) for d in depots),
         datetime.now().isoformat(timespec="seconds"),
         developer, publisher, franchise, genres, released),
    )
    return {"appid": appid, "name": name, "type": common.get("type"), "depots": depots,
            "developer": developer, "publisher": publisher, "franchise": franchise,
            "genres": genres, "released": released}


def _record(depot: int, appid, name, app_type, confidence, info=None):
    info = info or {}
    db.execute(
        "INSERT INTO depot_meta(depot, appid, name, app_type, confidence, state,"
        " resolved_at, developer, publisher, franchise, genres, released)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(depot) DO UPDATE SET appid=excluded.appid, name=excluded.name,"
        " app_type=excluded.app_type, confidence=excluded.confidence,"
        " state=excluded.state, resolved_at=excluded.resolved_at,"
        " developer=excluded.developer, publisher=excluded.publisher,"
        " franchise=excluded.franchise, genres=excluded.genres,"
        " released=excluded.released",
        (depot, appid, name, app_type, confidence,
         "done" if name else "failed", datetime.now().isoformat(timespec="seconds"),
         info.get("developer"), info.get("publisher"), info.get("franchise"),
         info.get("genres"), info.get("released")),
    )


def _known_depots() -> set:
    rows = db.query("SELECT depot FROM depots")
    return {r["depot"] for r in rows}


def resolve_depot(depot: int, known: set = None) -> dict:
    """Resolve one depot, writing results for every sibling depot we learn about."""
    if depot in STATIC_NAMES:
        _record(depot, None, STATIC_NAMES[depot], "platform", "static")
        return {"depot": depot, "name": STATIC_NAMES[depot], "confidence": "static"}

    known = known if known is not None else _known_depots()
    same_id_app = None

    for cand in range(depot, max(-1, depot - config.DEPOT_APP_SPAN), -1):
        info = fetch_app(cand)
        if config.RESOLVE_DELAY:
            time.sleep(config.RESOLVE_DELAY)
        if not info:
            continue
        if cand == depot:
            same_id_app = info
        if depot in info["depots"]:
            # One app answer resolves every depot it owns that we have files for.
            for sib in info["depots"]:
                if sib in known:
                    _record(sib, info["appid"], info["name"], info["type"], "exact", info)
                    with _seen_lock:
                        _seen.add(sib)
            return {"depot": depot, "appid": info["appid"], "name": info["name"],
                    "confidence": "exact"}

    if same_id_app:
        # The app exists under the depot's own id but no longer lists the depot,
        # which is what repacked pre-2010 titles look like today.
        _record(depot, same_id_app["appid"], same_id_app["name"],
                same_id_app["type"], "likely", same_id_app)
        return {"depot": depot, "appid": same_id_app["appid"], "name": same_id_app["name"],
                "confidence": "likely"}

    _record(depot, None, None, None, "none")
    return {"depot": depot, "name": None, "confidence": "none"}


# --------------------------------------------------------------------------- #
# background worker pool
# --------------------------------------------------------------------------- #
def _worker():
    known = _known_depots()
    while True:
        try:
            _, depot = _queue.get(timeout=2)
        except queue.Empty:
            if not _stats["running"]:
                return
            continue
        try:
            row = db.one("SELECT state FROM depot_meta WHERE depot = ?", (depot,))
            if row and row["state"] == "done":
                continue
            result = resolve_depot(depot, known)
            if result.get("name"):
                _stats["resolved"] += 1
            else:
                _stats["failed"] += 1
        except Exception as exc:  # noqa: BLE001 - a bad lookup must not kill the pool
            _stats["failed"] += 1
            logbook.warn("metadata", f"could not resolve depot {depot}",
                         depot=depot, detail=str(exc))
        finally:
            _stats["queued"] = _queue.qsize()
            _queue.task_done()


def enqueue(depots, priority=5):
    added = 0
    with _seen_lock:
        for d in depots:
            if d in _seen:
                continue
            _seen.add(d)
            _queue.put((priority, int(d)))
            added += 1
    _stats["queued"] = _queue.qsize()
    return added


def prioritise(depots):
    """Push depots currently on screen to the front of the queue."""
    pending = []
    for d in depots:
        row = db.one("SELECT state FROM depot_meta WHERE depot = ?", (int(d),))
        if row is None or row["state"] != "done":
            pending.append(int(d))
    with _seen_lock:
        for d in pending:
            _seen.discard(d)
    return enqueue(pending, priority=0)


def start(backfill=True):
    if _stats["running"]:
        return
    _stats["running"] = True
    for _ in range(config.RESOLVE_THREADS):
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        _workers.append(t)
    if backfill:
        rows = db.query(
            "SELECT d.depot FROM depots d LEFT JOIN depot_meta m ON m.depot = d.depot"
            " WHERE m.state IS NULL OR m.state = 'pending' ORDER BY d.depot"
        )
        enqueue([r["depot"] for r in rows], priority=9)


def stats():
    total = db.one("SELECT COUNT(*) c FROM depots")
    done = db.one("SELECT COUNT(*) c FROM depot_meta WHERE state = 'done'")
    failed = db.one("SELECT COUNT(*) c FROM depot_meta WHERE state = 'failed'")
    return {
        "total": total["c"] if total else 0,
        "named": done["c"] if done else 0,
        "unidentified": failed["c"] if failed else 0,
        "queued": _queue.qsize(),
        "running": _stats["running"],
    }


# --------------------------------------------------------------------------- #
# studio / genre backfill
# --------------------------------------------------------------------------- #
# Depots resolved before SteamFlix recorded studio and genre still have a name,
# so re-resolving them would be pure waste. This pass instead walks the app ids
# they already point at, fetches the missing half of the PICS record once each,
# and copies it onto every depot that app owns.
_detail = {"running": False, "done": 0, "total": 0, "step": "idle"}


def detail_stats():
    row = db.one(
        "SELECT COUNT(DISTINCT appid) c FROM depot_meta"
        " WHERE appid IS NOT NULL AND (developer IS NOT NULL OR genres IS NOT NULL)"
    )
    total = db.one("SELECT COUNT(DISTINCT appid) c FROM depot_meta WHERE appid IS NOT NULL")
    out = dict(_detail)
    out["apps_with_details"] = row["c"] if row else 0
    out["apps_total"] = total["c"] if total else 0
    return out


def _apply_details(appid, info):
    """Copy one app's studio/genre onto every depot already tied to it."""
    db.execute(
        "UPDATE depot_meta SET developer=?, publisher=?, franchise=?, genres=?, released=?"
        " WHERE appid = ?",
        (info.get("developer"), info.get("publisher"), info.get("franchise"),
         info.get("genres"), info.get("released"), appid),
    )


def _backfill_one(appid):
    try:
        info = fetch_app(appid)
        if info:
            _apply_details(appid, info)
    except Exception as exc:  # noqa: BLE001 - one bad app must not stop the pass
        logbook.warn("metadata", f"no studio/genre for app {appid}", detail=str(exc))
    _detail["done"] += 1
    if config.RESOLVE_DELAY:
        time.sleep(config.RESOLVE_DELAY)


def _backfill_worker(appids):
    _detail.update(running=True, done=0, total=len(appids), step="fetching")
    try:
        # One request per app over a few thousand apps, so it runs on the same
        # small pool the name resolver uses rather than one at a time.
        threads = max(1, config.RESOLVE_THREADS)
        pending = list(appids)
        lock = threading.Lock()

        def pump():
            while True:
                with lock:
                    if not pending:
                        return
                    appid = pending.pop()
                _backfill_one(appid)

        workers = [threading.Thread(target=pump, daemon=True) for _ in range(threads)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        _detail["step"] = "idle"
        logbook.info("metadata",
                     f"studio and genre filled in for {len(appids)} app(s)")
    finally:
        _detail["running"] = False


def backfill_details(limit=None):
    """Kick off the studio/genre pass. Returns how many apps it will fetch."""
    if _detail["running"]:
        return 0
    rows = db.query(
        "SELECT DISTINCT m.appid FROM depot_meta m"
        " LEFT JOIN app_cache a ON a.appid = m.appid"
        " WHERE m.appid IS NOT NULL"
        "   AND (m.developer IS NULL AND m.genres IS NULL)"
        "   AND (a.detailed IS NULL OR a.detailed = 0"
        "        OR a.developer IS NOT NULL OR a.genres IS NOT NULL)"
        " ORDER BY m.appid"
    )
    appids = [r["appid"] for r in rows]
    if limit:
        appids = appids[:limit]
    if not appids:
        # Everything is already fetched; just make sure the depots have a copy.
        for row in db.query(
            "SELECT appid, developer, publisher, franchise, genres, released"
            " FROM app_cache WHERE found = 1 AND detailed = 1"
            "   AND (developer IS NOT NULL OR genres IS NOT NULL)"
        ):
            _apply_details(row["appid"], dict(row))
        return 0
    t = threading.Thread(target=_backfill_worker, args=(appids,), daemon=True)
    t.start()
    return len(appids)
