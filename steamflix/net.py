"""HTTP helpers with mirror failover.

Every mirror carries the same tree, so requests are addressed by path
(``blobs/<file>``) and tried against each live mirror in turn. A mirror that
errors or times out is benched for a cooldown period instead of being retried
on every single file.
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from . import config, logbook, proxies, settings

_local = threading.local()
_benched = {}
_bench_lock = threading.Lock()

# --------------------------------------------------------------------------- #
# politeness
# --------------------------------------------------------------------------- #
# The Steam2 mirrors are volunteer-run and SteamFlix can open eight connections
# per file across several files at once. Two limits keep that civil: a minimum
# gap between requests to the same host, and a ceiling on requests per minute
# across all of them. Both are settings, so the user can loosen or tighten them.
_gap_lock = threading.Lock()
_last_request = {}
_recent = []


def _throttle(host: str):
    conf = settings.all()
    delay = conf["request_delay_ms"] / 1000.0
    ceiling = conf["max_requests_per_minute"]

    while True:
        with _gap_lock:
            now = time.time()

            # Per-minute ceiling across every mirror.
            cutoff = now - 60
            while _recent and _recent[0] < cutoff:
                _recent.pop(0)
            if ceiling and len(_recent) >= ceiling:
                wait = 60 - (now - _recent[0])
            else:
                # Minimum gap to this particular host.
                wait = (_last_request.get(host, 0) + delay) - now
                if wait <= 0:
                    _last_request[host] = now
                    _recent.append(now)
                    return
        time.sleep(min(max(wait, 0.005), 2.0))


def throttle_status():
    with _gap_lock:
        now = time.time()
        recent = [t for t in _recent if t > now - 60]
    conf = settings.all()
    return {
        "requests_last_minute": len(recent),
        "ceiling": conf["max_requests_per_minute"],
        "delay_ms": conf["request_delay_ms"],
    }


def session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = config.USER_AGENT
        adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        _local.session = s
    s.proxies = proxies.for_request()
    return s


# --------------------------------------------------------------------------- #
# mirror selection
# --------------------------------------------------------------------------- #
def all_mirrors():
    """The mirror list the user is actually using, not the built-in default."""
    return settings.mirrors()


def live_mirrors():
    if not settings.use_mirrors():
        return []                      # torrent-only: skip HTTP entirely
    now = time.time()
    known = all_mirrors()
    with _bench_lock:
        live = [m for m in known if _benched.get(m, 0) <= now]
    return live or list(known)


def bench(mirror, reason=""):
    with _bench_lock:
        already = _benched.get(mirror, 0) > time.time()
        _benched[mirror] = time.time() + config.MIRROR_COOLDOWN
    if not already:
        others = [m for m in all_mirrors() if m != mirror]
        logbook.warn(
            "mirror",
            f"{mirror} benched for {config.MIRROR_COOLDOWN}s"
            + (f", falling back to {others[0]}" if others else " and no mirror is left"),
            detail=reason,
        )


def mirror_status():
    now = time.time()
    config_mirrors = all_mirrors()
    with _bench_lock:
        return [
            {"url": m, "healthy": _benched.get(m, 0) <= now,
             "retry_in": max(0, int(_benched.get(m, 0) - now))}
            for m in config_mirrors
        ]


def url_for(path: str, mirror: str = None) -> str:
    base = mirror or (all_mirrors() or config.MIRRORS)[0]
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _host(url: str) -> str:
    """Host of a URL, used as the throttle bucket."""
    rest = url.split("://", 1)[-1]
    return rest.split("/", 1)[0]


def mirror_url(path: str, mirror: str = None) -> str:
    """A mirror URL, rate-limited before it is handed back.

    Every call site that talks to a mirror goes through here, so there is one
    place where the pacing is enforced and no way to accidentally bypass it.
    """
    url = url_for(path, mirror)
    _throttle(_host(url))
    return url


# --------------------------------------------------------------------------- #
# requests
# --------------------------------------------------------------------------- #
def head_size(path: str):
    """Content-Length for a mirror path, or None if nobody will say."""
    for mirror in live_mirrors():
        try:
            r = session().head(mirror_url(path, mirror), timeout=config.HTTP_TIMEOUT,
                               allow_redirects=True)
            if r.status_code >= 500:
                bench(mirror, f"HTTP {r.status_code}")
                continue
            if r.status_code >= 400:
                return None
            length = r.headers.get("content-length")
            if length is not None:
                return int(length)
        except requests.RequestException as exc:
            bench(mirror, str(exc))
    return None


def get_bytes(path: str, timeout=None) -> bytes:
    last = None
    for mirror in live_mirrors():
        try:
            r = session().get(mirror_url(path, mirror), timeout=timeout or config.HTTP_TIMEOUT)
            r.raise_for_status()
            return r.content
        except requests.RequestException as exc:
            last = exc
            bench(mirror, str(exc))
    raise last or IOError(f"no mirror served {path}")


def get_json(url: str, timeout=None):
    """Absolute-URL GET for third-party metadata APIs (not mirror traffic)."""
    _throttle(_host(url))
    try:
        r = session().get(url, timeout=timeout or config.HTTP_TIMEOUT)
    except requests.RequestException:
        return None
    if r.status_code >= 400:
        return None
    try:
        return r.json()
    except ValueError:
        return None


class Cancelled(Exception):
    """Raised inside a download when the owning job was cancelled."""


class AllMirrorsFailed(IOError):
    def __init__(self, path, errors):
        self.path = path
        self.errors = errors
        detail = "; ".join(f"{m}: {e}" for m, e in errors)
        logbook.add("network", f"could not fetch {path.split('/')[-1]} from any mirror",
                    detail=detail)
        super().__init__(f"every mirror failed for {path} ({detail})")


def _plan_segments(size: int, count: int):
    span = size // count
    bounds = []
    for i in range(count):
        start = i * span
        end = size - 1 if i == count - 1 else (start + span - 1)
        bounds.append([start, end, 0])          # start, end, bytes already written
    return bounds


def _seg_state_path(part):
    return part.with_suffix(part.suffix + ".segs")


def _load_segments(part, size, count):
    state = _seg_state_path(part)
    if state.exists() and part.exists():
        try:
            saved = json.loads(state.read_text())
            if saved.get("size") == size and len(saved.get("segments", [])) == count:
                return saved["segments"]
        except (ValueError, OSError):
            pass
    return _plan_segments(size, count)


def _save_segments(part, size, segments):
    try:
        _seg_state_path(part).write_text(json.dumps({"size": size, "segments": segments}))
    except OSError:
        pass


def download_segmented(path: str, dest, size: int, progress=None, cancelled=None,
                       segments=None):
    """Fetch one file as several parallel byte ranges, the way a download
    manager does.

    Each segment gets its own connection, and segments are spread across the
    mirrors round-robin so both hosts contribute bandwidth. Progress is written
    to a sidecar file, so an interrupted download resumes per segment rather
    than starting the whole file again.
    """
    count = segments or config.SEGMENTS_PER_FILE
    mirrors = live_mirrors()
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    segs = _load_segments(part, size, count)
    with open(part, "r+b" if part.exists() else "wb") as fh:
        fh.truncate(size)

    already = sum(s[2] for s in segs)
    if progress and already:
        progress(already)

    lock = threading.Lock()
    failures = []

    def pull(index):
        start, end, done = segs[index]
        mirror = mirrors[index % len(mirrors)]
        attempts = 0
        while True:
            if done > (end - start):
                return
            first = start + done
            if first > end:
                return
            try:
                r = session().get(mirror_url(path, mirror),
                                  headers={"Range": f"bytes={first}-{end}"},
                                  stream=True, timeout=config.HTTP_TIMEOUT)
                if r.status_code != 206:
                    raise IOError(f"expected 206, got {r.status_code}")
                with open(part, "r+b") as fh:
                    fh.seek(first)
                    for chunk in r.iter_content(chunk_size=config.CHUNK_SIZE):
                        if cancelled is not None and cancelled():
                            raise Cancelled()
                        if not chunk:
                            continue
                        fh.write(chunk)
                        done += len(chunk)
                        with lock:
                            segs[index][2] = done
                        if progress:
                            progress(len(chunk))
                r.close()
                return
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - retried on the next mirror
                attempts += 1
                bench(mirror, str(exc))
                with lock:
                    _save_segments(part, size, segs)
                if attempts >= len(config.MIRRORS) + 2:
                    failures.append((mirror, str(exc)))
                    raise
                mirror = live_mirrors()[(index + attempts) % len(live_mirrors())]

    try:
        with ThreadPoolExecutor(max_workers=count) as pool:
            list(pool.map(pull, range(count)))
    except Cancelled:
        _save_segments(part, size, segs)
        raise
    except Exception:
        _save_segments(part, size, segs)
        raise AllMirrorsFailed(path, failures or [("all", "segmented transfer failed")])

    final = part.stat().st_size
    if final != size:
        raise IOError(f"size mismatch: got {final}, expected {size}")
    _seg_state_path(part).unlink(missing_ok=True)
    part.replace(dest)
    return final


def _torrent_fallback(path: str, dest, expected_size=None, progress=None,
                      cancelled=None, swarm=None, reason=None):
    """Last resort when every mirror refused: pull the file from the swarm.

    The torrent holds exactly the same blobs and dats under ``blobs/`` and
    ``dats/``, so a file the mirrors cannot serve is usually still reachable -
    slower, but reachable.
    """
    from . import torrent as torrentmod           # imported late: optional feature

    if not settings.use_torrent() or not torrentmod.available():
        return None
    name = path.rsplit("/", 1)[-1]
    kind = "dat" if name.endswith(".dat") else "blob"

    # net.download reports deltas; the torrent reports totals. Convert once here
    # so both paths look the same to a job's progress bar.
    seen = {"n": 0}

    def relay(done, _total):
        if progress and done > seen["n"]:
            progress(done - seen["n"])
            seen["n"] = done

    logbook.warn("mirror",
                 reason or f"every mirror refused {name} - trying the torrent",
                 detail="BitTorrent is slower than HTTP here and depends on the "
                        "swarm having seeders for the pieces this file sits in.")
    try:
        return torrentmod.fetch(name, kind, dest, progress=relay,
                                should_stop=cancelled, swarm=swarm)
    except Exception as exc:  # noqa: BLE001 - the caller still raises its own error
        logbook.add("torrent", f"the swarm could not supply {name}", detail=str(exc))
        return None


def download(path: str, dest, expected_size=None, progress=None, cancelled=None,
             segments=None):
    """Stream a mirror path to disk, resuming a partial file and falling over
    between mirrors. Large files are split into parallel ranges automatically.
    Returns the final size on disk."""
    if dest.exists():
        size = dest.stat().st_size
        if expected_size is None or size == expected_size:
            return size

    use = segments if segments is not None else config.SEGMENTS_PER_FILE
    if (expected_size and use > 1 and expected_size >= config.SEGMENT_THRESHOLD):
        try:
            return download_segmented(path, dest, expected_size, progress=progress,
                                      cancelled=cancelled, segments=use)
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - fall back to one stream
            logbook.warn("network",
                         f"segmented download of {path.split('/')[-1]} failed, "
                         f"retrying as a single stream", detail=str(exc))
    # Torrent-only, or the mirrors are switched off: go straight to the swarm.
    if not settings.use_mirrors():
        got = _torrent_fallback(path, dest, expected_size, progress, cancelled,
                                reason="the swarm is the selected source")
        if got is not None:
            return got
        raise AllMirrorsFailed(path, [("torrent", "the swarm could not supply this file")])

    try:
        return _download_single(path, dest, expected_size, progress, cancelled)
    except Cancelled:
        raise
    except AllMirrorsFailed as exc:
        got = _torrent_fallback(path, dest, expected_size, progress, cancelled)
        if got is not None:
            return got
        raise exc


def prefer_torrent(path: str, dest, expected_size=None, progress=None, cancelled=None):
    """Try the swarm first, and fall back to the mirrors.

    Used when several titles are downloading at once: pushing part of the batch
    to BitTorrent keeps a queue of jobs from hammering two volunteer-run hosts.
    """
    got = _torrent_fallback(path, dest, expected_size, progress, cancelled,
                            reason="spreading a busy queue across both sources")
    if got is not None:
        return got
    return download(path, dest, expected_size, progress, cancelled)


def _download_single(path: str, dest, expected_size=None, progress=None, cancelled=None):
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    if dest.exists():
        size = dest.stat().st_size
        if expected_size is None or size == expected_size:
            return size
        dest.unlink()

    errors = []
    for mirror in live_mirrors():
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with session().get(mirror_url(path, mirror), headers=headers, stream=True,
                               timeout=config.HTTP_TIMEOUT) as r:
                if have and r.status_code == 200:
                    have = 0                     # mirror ignored Range; restart
                    part.unlink(missing_ok=True)
                elif r.status_code not in (200, 206):
                    r.raise_for_status()
                with open(part, "ab" if have else "wb") as fh:
                    for chunk in r.iter_content(chunk_size=config.CHUNK_SIZE):
                        if cancelled is not None and cancelled():
                            raise Cancelled()
                        if not chunk:
                            continue
                        fh.write(chunk)
                        if progress:
                            progress(len(chunk))

            final = part.stat().st_size
            if expected_size is not None and final != expected_size:
                raise IOError(f"size mismatch: got {final}, expected {expected_size}")
            part.replace(dest)
            return final
        except Cancelled:
            raise
        except (requests.RequestException, IOError) as exc:
            errors.append((mirror, str(exc)))
            bench(mirror, str(exc))
            # A wrong-size result means the partial file is suspect; start over
            # on the next mirror rather than resuming onto bad bytes.
            if "size mismatch" in str(exc):
                part.unlink(missing_ok=True)

    raise AllMirrorsFailed(path, errors)
