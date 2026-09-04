"""Builds the local catalogue from the mirror's file listings.

The mirror publishes ``blobs_dates.txt`` and ``dats_dates.txt``: one line per
file, ``<filename>\\t<timestamp>``. Every filename is
``<depot>_<version>_<crc>_<sha256>.<ext>`` so the listings alone are enough to
know which depots exist, how many versions each has, and which of them were
reset by Valve (a reset shows up as two different CRCs sharing a version).

Only depots that actually appear in those listings are ever shown in the UI.
"""
import re
import threading
import time
from datetime import datetime

from . import config, db, logbook, net

FILENAME_RE = re.compile(r"^(\d+)_(\d+)_([0-9a-fA-F]{8})_([0-9a-fA-F]{64})\.(blob|dat)$")
KEY_RE = re.compile(r"^\s*\{\s*(\d+)\s*,\s*\{")

_lock = threading.Lock()
_state = {
    "running": False, "step": "idle", "detail": "", "progress": 0.0,
    "recent": [], "seen": 0, "depots": 0, "bytes": 0, "total_bytes": 0,
}


def state():
    s = dict(_state)
    s["recent"] = list(_state["recent"])
    return s


def _set(step, detail="", progress=None):
    _state["step"] = step
    _state["detail"] = detail
    if progress is not None:
        _state["progress"] = progress


def _saw(filename, depot):
    """Feed the boot screen: the newest names scrolling past, and a running count."""
    _state["seen"] += 1
    if _state["seen"] % 97 == 0:
        recent = _state["recent"]
        recent.append(filename)
        del recent[:-14]


def parse_line(line: str):
    line = line.strip()
    if not line:
        return None
    parts = line.split("\t")
    filename = parts[0].strip()
    mtime = parts[1].strip() if len(parts) > 1 else ""
    m = FILENAME_RE.match(filename)
    if not m:
        return None
    depot, version, crc, sha, ext = m.groups()
    # "2003-09-10+13:11:59.0156250000" -> "2003-09-10 13:11:59"
    stamp = mtime.replace("+", " ").split(".")[0]
    return (filename, ext, int(depot), int(version), crc.lower(), sha.lower(), stamp)


def _known_key_depots() -> set:
    """Depot ids the bundled extractor already has decryption keys for."""
    for path in config.KEYS_CPP_CANDIDATES:
        if not path.exists():
            continue
        found = set()
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = KEY_RE.match(line)
                if m:
                    found.add(int(m.group(1)))
        if found:
            return found
    return set()


def _load_listing(remote_name: str, refresh: bool):
    path = config.CACHE_DIR / remote_name
    if refresh or not path.exists():
        _set("download", f"fetching {remote_name}")
        _state["bytes"] = 0
        _state["total_bytes"] = net.head_size(remote_name) or 0

        def tick(n):
            _state["bytes"] += n

        path.parent.mkdir(parents=True, exist_ok=True)
        net.download(remote_name, path, expected_size=_state["total_bytes"] or None,
                     progress=tick, segments=1)
    return path


def sync(refresh=False, force=False):
    """Download the mirror listings and (re)build the catalogue tables."""
    with _lock:
        if _state["running"]:
            return state()
        _state["running"] = True
    try:
        db.init()
        if not force and db.get_kv("index_built") and not refresh:
            _set("idle", "catalogue already built", 1.0)
            return state()

        rows = []
        _state["seen"] = 0
        _state["recent"] = []
        for name, kind in (("blobs_dates.txt", "blob"), ("dats_dates.txt", "dat")):
            path = _load_listing(name, refresh)
            _set("parse", f"reading {name}")
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    parsed = parse_line(line)
                    if parsed and parsed[1] == kind:
                        rows.append(parsed)
                        _saw(parsed[0], parsed[2])
            _state["depots"] = len({r[2] for r in rows})

        _set("store", f"writing {len(rows)} entries")
        with db.transaction() as conn:
            conn.execute("DELETE FROM files")
            conn.executemany(
                "INSERT OR REPLACE INTO files"
                "(filename, kind, depot, version, crc, hash, mtime) VALUES(?,?,?,?,?,?,?)",
                rows,
            )

        _set("aggregate", "summarising depots")
        keyed = _known_key_depots()
        conn = db.connect()
        conn.execute("BEGIN")
        conn.execute("DELETE FROM depots")
        conn.execute(
            """
            INSERT INTO depots(depot, blob_count, dat_count, max_version,
                               has_reset, first_date, last_date, has_key)
            SELECT depot,
                   SUM(kind = 'blob'),
                   SUM(kind = 'dat'),
                   MAX(version),
                   0,
                   MIN(NULLIF(mtime, '')),
                   MAX(NULLIF(mtime, '')),
                   0
            FROM files GROUP BY depot
            """
        )
        # A reset shows up as two blobs sharing one version number.
        conn.execute(
            """
            UPDATE depots SET has_reset = 1 WHERE depot IN (
                SELECT depot FROM files WHERE kind = 'blob'
                GROUP BY depot, version HAVING COUNT(*) > 1
            )
            """
        )
        if keyed:
            conn.executemany(
                "UPDATE depots SET has_key = 1 WHERE depot = ?",
                [(d,) for d in keyed],
            )
        conn.execute(
            "INSERT OR IGNORE INTO depot_meta(depot, state) SELECT depot, 'pending' FROM depots"
        )
        conn.execute("COMMIT")

        db.set_kv("index_built", datetime.now().isoformat(timespec="seconds"))
        db.set_kv("index_files", len(rows))
        _set("idle", f"{len(rows)} files indexed", 1.0)
        logbook.info("index", f"catalogue built: {len(rows)} files")
        return state()
    except Exception as exc:  # noqa: BLE001 - surfaced in the logs panel
        _set("error", str(exc))
        logbook.add("index", "catalogue build failed", detail=str(exc))
        raise
    finally:
        _state["running"] = False


def sync_async(refresh=False, force=False):
    t = threading.Thread(target=sync, kwargs={"refresh": refresh, "force": force}, daemon=True)
    t.start()
    return t


def is_built() -> bool:
    try:
        return bool(db.get_kv("index_built"))
    except Exception:  # noqa: BLE001 - database not created yet
        return False


def wait_until_built(timeout=900):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_built():
            return True
        time.sleep(0.5)
    return False
