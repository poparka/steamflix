"""Central error log.

Everything that can fail in SteamFlix fails for one of a handful of reasons, and
telling them apart is the difference between "retry later" and "this depot will
never extract". Each entry carries a category so the UI can say which:

    network   - a mirror timed out, refused a range request or served garbage
    mirror    - a mirror was benched entirely
    key       - the depot has no decryption key, or every key attempt failed
    chain     - the delta chain is broken or incomplete on the mirror
    torrent   - the swarm could not supply a file, or seeding has something to say
    extract   - the extractor ran but exited non-zero
    launch    - an extracted program would not start on this machine
    storage   - not enough free disk space
    metadata  - depot -> app lookup failed
    index     - catalogue build problems
"""
import threading
import time
from collections import deque

MAX_ENTRIES = 600

_entries = deque(maxlen=MAX_ENTRIES)
_lock = threading.Lock()
_counter = 0

HINTS = {
    "network": "Transient. SteamFlix retries on the other mirror automatically; "
               "if both are down the torrent in the About panel has the same files.",
    "mirror": "The mirror is benched for a couple of minutes and traffic moves to "
              "the other one. Nothing to do.",
    "key": "This depot has no decryption key in the bundled table. SteamFlix already "
           "retried with the all-zero keys that work for unencrypted depots. You can "
           "supply a key by hand in the download dialog if you know it.",
    "chain": "Steam2 stores versions as deltas, so a gap anywhere below the version "
             "you picked breaks extraction. Try a lower version, or a different "
             "variant if the depot was reset.",
    "extract": "The files downloaded fine but the extractor rejected them. Usually a "
               "wrong blob variant on a reset depot, or a truncated dat.",
    "launch": "The files extracted correctly - the program itself will not run here. "
              "Steam2-era builds usually want a runtime their installer would have "
              "shipped (an old DirectX or VC++ redistributable), or a compatibility "
              "mode. Nothing is wrong with the download.",
    "torrent": "The swarm is volunteers with the same archive, so it is slower than "
               "the mirrors and only as complete as whoever is online. Seeding "
               "messages are the other direction: what SteamFlix is sharing back, "
               "and whether anyone can reach it to ask for it.",
    "storage": "Free space on the library drive, or point SteamFlix at another drive "
               "with STEAMFLIX_LIBRARY.",
    "metadata": "Only affects the name and artwork on the card. The depot still "
                "downloads and extracts normally.",
    "index": "The catalogue could not be rebuilt from the mirror listings.",
}


def add(category, message, level="error", depot=None, detail=None, job=None):
    global _counter
    with _lock:
        _counter += 1
        entry = {
            "id": _counter,
            "ts": time.time(),
            "level": level,
            "category": category,
            "message": str(message),
            "detail": str(detail) if detail else None,
            "depot": depot,
            "job": job,
            "hint": HINTS.get(category),
        }
        _entries.appendleft(entry)
    return entry


def warn(category, message, **kw):
    return add(category, message, level="warning", **kw)


def info(category, message, **kw):
    return add(category, message, level="info", **kw)


def entries(limit=200, category=None, level=None, since=None):
    with _lock:
        items = list(_entries)
    if category and category != "all":
        items = [e for e in items if e["category"] == category]
    if level and level != "all":
        items = [e for e in items if e["level"] == level]
    if since:
        items = [e for e in items if e["id"] > since]
    return items[:limit]


def summary():
    with _lock:
        items = list(_entries)
    by_category = {}
    by_level = {"error": 0, "warning": 0, "info": 0}
    for e in items:
        by_category[e["category"]] = by_category.get(e["category"], 0) + 1
        by_level[e["level"]] = by_level.get(e["level"], 0) + 1
    return {
        "total": len(items),
        "errors": by_level.get("error", 0),
        "warnings": by_level.get("warning", 0),
        "by_category": by_category,
        "latest": items[0] if items else None,
    }


def clear():
    with _lock:
        _entries.clear()
