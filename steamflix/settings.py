"""User-editable settings, stored in the database rather than the environment.

Everything here can be changed from the Settings panel while SteamFlix is
running. Environment variables still set the defaults, so a start.bat written by
setup.py keeps working; what the user changes in the UI simply wins.

The settings that matter most are the ones about politeness. The Steam2 mirrors
are volunteer-run and this app can easily open eight connections per file across
several files at once, so the delay, the concurrency caps and the source policy
all live here where they can be turned down.
"""
import json
import threading

from . import config, db

DEFAULTS = {
    # Where blobs and dats come from.
    "mirrors": list(config.MIRRORS),
    "source_mode": "auto",          # auto | mirrors | torrent
    "torrent_fallback": bool(config.TORRENT_FALLBACK),
    # With several titles queued, sending some of them to the swarm keeps the
    # mirrors from carrying the whole batch.
    "torrent_when_busy": True,
    "torrent_busy_threshold": 2,    # queued jobs before the swarm is used in parallel

    # Politeness.
    "request_delay_ms": 120,        # pause between mirror requests, per host
    "download_threads": config.DOWNLOAD_THREADS,
    "segments_per_file": config.SEGMENTS_PER_FILE,
    "resolve_delay_ms": int(config.RESOLVE_DELAY * 1000),
    "resolve_threads": config.RESOLVE_THREADS,
    "max_requests_per_minute": 240,  # hard ceiling across all mirror traffic

    # Optional proxying.
    "proxy_enabled": False,
    "proxy_list": [],               # "http://host:port" entries
    "proxy_mode": "rotate",         # rotate | first
    "proxy_source": "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
}

_cache = None
_lock = threading.Lock()
KEY = "settings"


def _coerce(values):
    """Keep the stored shape sane whatever arrives from the UI."""
    out = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        if key not in values:
            continue
        value = values[key]
        if isinstance(default, bool):
            out[key] = bool(value)
        elif isinstance(default, int):
            try:
                out[key] = max(0, int(value))
            except (TypeError, ValueError):
                pass
        elif isinstance(default, list):
            if isinstance(value, str):
                value = [v.strip() for v in value.replace(",", "\n").splitlines()]
            out[key] = [str(v).strip() for v in value if str(v).strip()]
        else:
            out[key] = str(value).strip() or default

    # A few values would break things outright if taken literally.
    out["mirrors"] = [m.rstrip("/") for m in out["mirrors"]] or list(config.MIRRORS)
    if out["source_mode"] not in ("auto", "mirrors", "torrent"):
        out["source_mode"] = "auto"
    if out["proxy_mode"] not in ("rotate", "first"):
        out["proxy_mode"] = "rotate"
    out["download_threads"] = min(16, max(1, out["download_threads"]))
    out["segments_per_file"] = min(16, max(1, out["segments_per_file"]))
    out["resolve_threads"] = min(8, max(1, out["resolve_threads"]))
    out["torrent_busy_threshold"] = max(1, out["torrent_busy_threshold"])
    return out


def all() -> dict:
    global _cache
    with _lock:
        if _cache is None:
            raw = db.get_kv(KEY)
            try:
                stored = json.loads(raw) if raw else {}
            except ValueError:
                stored = {}
            _cache = _coerce(stored)
        return dict(_cache)


def get(key, default=None):
    return all().get(key, DEFAULTS.get(key, default))


def update(values: dict) -> dict:
    """Merge a partial update and persist it."""
    global _cache
    with _lock:
        current = dict(_cache) if _cache is not None else None
    if current is None:
        current = all()
    merged = _coerce({**current, **(values or {})})
    with _lock:
        _cache = merged
    db.set_kv(KEY, json.dumps(merged))
    return dict(merged)


def reset() -> dict:
    global _cache
    with _lock:
        _cache = dict(DEFAULTS)
    db.set_kv(KEY, json.dumps(DEFAULTS))
    return dict(DEFAULTS)


def mirrors():
    """Mirror list in preference order, always at least the built-in ones."""
    return all()["mirrors"] or list(config.MIRRORS)


def use_mirrors() -> bool:
    return all()["source_mode"] != "torrent"


def use_torrent() -> bool:
    mode = all()["source_mode"]
    if mode == "torrent":
        return True
    return mode == "auto" and all()["torrent_fallback"]
