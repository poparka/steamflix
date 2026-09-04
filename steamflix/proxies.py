"""An optional proxy pool for mirror traffic.

Off by default, and deliberately so. Routing archive downloads through random
public proxies is slower, less reliable and puts your traffic through machines
run by strangers, so SteamFlix only does it when asked. What it is genuinely
useful for is the case where a mirror blocks or rate-limits a whole ISP.

Two ways to fill the pool:

    * paste a list into Settings ("host:port" or "http://host:port" per line)
    * fetch one from a public list URL, which is a plain text file of the same

Fetched proxies are checked before use - a public list is mostly dead entries -
and anything that fails during a real download is dropped from the pool.
"""
import threading
import time

import requests

from . import config, db, logbook, settings

_lock = threading.Lock()
_pool = []                 # working proxies, in rotation order
_cursor = 0
_dead = set()
_checked_at = 0

CHECK_URL = "http://httpbin.org/ip"
CHECK_TIMEOUT = 6


def _normalise(entry: str):
    entry = entry.strip()
    if not entry or entry.startswith("#"):
        return None
    if "://" not in entry:
        entry = "http://" + entry
    if not entry.startswith(("http://", "https://", "socks5://", "socks4://")):
        return None
    return entry.rstrip("/")


def configured():
    """Whatever the user has pasted into Settings."""
    return [p for p in (_normalise(x) for x in settings.get("proxy_list", [])) if p]


def enabled() -> bool:
    return bool(settings.get("proxy_enabled")) and bool(_pool or configured())


def pool():
    with _lock:
        return list(_pool)


def status():
    conf = settings.all()
    with _lock:
        return {
            "enabled": bool(conf["proxy_enabled"]),
            "mode": conf["proxy_mode"],
            "configured": len(configured()),
            "working": len(_pool),
            "dead": len(_dead),
            "source": conf["proxy_source"],
            "checked_at": _checked_at,
        }


def for_request():
    """The proxies dict to hand to requests, or {} for a direct connection."""
    if not settings.get("proxy_enabled"):
        return {}
    global _cursor
    with _lock:
        if not _pool:
            return {}
        if settings.get("proxy_mode") == "first":
            chosen = _pool[0]
        else:
            chosen = _pool[_cursor % len(_pool)]
            _cursor += 1
    return {"http": chosen, "https": chosen}


def drop(proxy: str, reason=""):
    """Take a proxy out of rotation after it fails a real request."""
    with _lock:
        if proxy in _pool:
            _pool.remove(proxy)
            _dead.add(proxy)
            logbook.warn("network", f"dropped proxy {proxy}", detail=reason)


def check(proxy: str) -> bool:
    try:
        r = requests.get(CHECK_URL, proxies={"http": proxy, "https": proxy},
                         timeout=CHECK_TIMEOUT,
                         headers={"User-Agent": config.USER_AGENT})
        return r.status_code < 400
    except Exception:  # noqa: BLE001 - a dead proxy is the normal case
        return False


def _check_many(candidates, want, workers=40):
    """Check candidates in parallel and keep the first ``want`` that answer."""
    from concurrent.futures import ThreadPoolExecutor

    good = []
    good_lock = threading.Lock()

    def probe(proxy):
        if len(good) >= want:
            return
        if check(proxy):
            with good_lock:
                if len(good) < want:
                    good.append(proxy)

    with ThreadPoolExecutor(max_workers=workers) as pool_exec:
        list(pool_exec.map(probe, candidates))
    return good


def fetch_public(limit=400):
    """Download the configured public proxy list. Returns raw candidates."""
    url = settings.get("proxy_source")
    if not url:
        raise ValueError("no proxy list URL is configured")
    r = requests.get(url, timeout=20, headers={"User-Agent": config.USER_AGENT})
    r.raise_for_status()
    out = []
    for line in r.text.splitlines():
        entry = _normalise(line)
        if entry and entry not in out:
            out.append(entry)
        if len(out) >= limit:
            break
    return out


def refresh(include_public=False, want=25, candidates=None):
    """Rebuild the working pool.

    The user's own list is always checked first; the public list is only pulled
    in when explicitly asked for, because that is a request to a third party
    nobody asked SteamFlix to talk to.
    """
    global _pool, _checked_at, _cursor
    pending = list(candidates or configured())
    fetched = 0
    if include_public and len(pending) < want:
        try:
            public = fetch_public()
            fetched = len(public)
            pending += [p for p in public if p not in pending]
        except Exception as exc:  # noqa: BLE001
            logbook.warn("network", "could not fetch the public proxy list",
                         detail=str(exc))

    good = _check_many(pending, want) if pending else []
    with _lock:
        _pool = good
        _cursor = 0
        _checked_at = time.time()
    db.set_kv("proxy_pool", "\\n".join(good))
    logbook.info("network",
                 f"proxy pool: {len(good)} working of {len(pending)} checked"
                 + (f" ({fetched} from the public list)" if fetched else ""))
    return {"checked": len(pending), "working": len(good), "fetched": fetched,
            "pool": good}


def load_saved():
    """Bring back the pool a previous run proved working."""
    global _pool
    raw = db.get_kv("proxy_pool")
    if not raw:
        return 0
    with _lock:
        _pool = [p for p in (x.strip() for x in raw.splitlines()) if p]
    return len(_pool)
