"""Download + extract job manager.

One job = one depot at one version. It plans the delta chain, pulls every blob
and dat it needs into a per-depot folder, then hands that folder to the
reference extractor. Blobs and dats already on disk from an earlier job are
reused, so pulling version 40 after version 38 only fetches the difference.
"""
import json
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from . import blob as blobmod
from . import chain, config, db, keys, logbook, net, settings, storage

_jobs = {}
_lock = threading.RLock()
_queue: "queue.Queue" = queue.Queue()
_worker_started = False

ACTIVE_STATES = {"queued", "planning", "downloading", "extracting"}


class Job:
    def __init__(self, depot, version, crc=None, title=None, do_extract=True,
                 file_filter=None, key_override=None):
        self.id = uuid.uuid4().hex[:12]
        self.depot = int(depot)
        self.version = int(version)
        self.crc = (crc or None)
        self.title = title or f"Depot {depot}"
        self.do_extract = bool(do_extract)
        self.file_filter = file_filter or None
        self.key_override = key_override or None

        self.state = "queued"
        self.error = None
        self.log_lines = []
        self.plan = None
        self.bytes_total = 0
        self.bytes_done = 0
        self.files_total = 0
        self.files_done = 0
        self.current = ""
        self.started = time.time()
        self.finished = None
        self.out_dir = None
        self.extracted_files = 0
        self.key_used = key_override
        self.key_trial = None
        self.partial = False
        # Filled in after extraction: what the manifest listed vs what appeared.
        self.files_expected = 0
        self.files_missing = 0
        self._cancel = threading.Event()
        self._window = []          # (timestamp, bytes) samples for the speed readout

    # -- helpers ---------------------------------------------------------- #
    def log(self, msg):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_lines.append(f"[{stamp}] {msg}")
        del self.log_lines[:-400]

    def cancel(self):
        self._cancel.set()
        self.log("cancel requested")

    def cancelled(self):
        return self._cancel.is_set()

    def add_bytes(self, n):
        self.bytes_done += n
        now = time.time()
        self._window.append((now, n))
        cutoff = now - 5
        while self._window and self._window[0][0] < cutoff:
            self._window.pop(0)

    @property
    def speed(self):
        if len(self._window) < 2:
            return 0.0
        span = self._window[-1][0] - self._window[0][0]
        if span <= 0:
            return 0.0
        return sum(n for _, n in self._window) / span

    @property
    def eta(self):
        s = self.speed
        left = max(0, self.bytes_total - self.bytes_done)
        return int(left / s) if s > 1 else None

    def to_dict(self, with_log=False):
        d = {
            "id": self.id,
            "depot": self.depot,
            "version": self.version,
            "crc": self.crc,
            "title": self.title,
            "state": self.state,
            "error": self.error,
            "bytes_total": self.bytes_total,
            "bytes_done": self.bytes_done,
            "files_total": self.files_total,
            "files_done": self.files_done,
            "current": self.current,
            "speed": round(self.speed, 1),
            "eta": self.eta,
            "started": self.started,
            "finished": self.finished,
            "out_dir": str(self.out_dir) if self.out_dir else None,
            "extracted_files": self.extracted_files,
            "reset": bool(self.plan.reset) if self.plan else None,
            "extract": self.do_extract,
            "key_used": self.key_used,
            "key_trial": self.key_trial,
            "partial": self.partial,
            "files_expected": self.files_expected,
            "files_missing": self.files_missing,
        }
        if with_log:
            d["log"] = self.log_lines[-200:]
        return d


# --------------------------------------------------------------------------- #
def depot_dirs(depot: int):
    base = config.DEPOT_DIR / str(depot)
    return base / "blobs", base / "dats"


def _persist(job: Job):
    db.execute(
        "INSERT INTO jobs(id, depot, version, crc, title, state, payload, created, updated)"
        " VALUES(?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET state=excluded.state, payload=excluded.payload,"
        " updated=excluded.updated",
        (job.id, job.depot, job.version, job.crc, job.title, job.state,
         json.dumps(job.to_dict()), datetime.fromtimestamp(job.started).isoformat(timespec="seconds"),
         datetime.now().isoformat(timespec="seconds")),
    )


def _run_plan(job: Job, blob_dir: Path):
    job.state = "planning"
    if job.crc:
        job.log(f"depot was reset - following CRC chain from {job.crc}")
        return chain.plan_reset(job.depot, job.version, job.crc, blob_dir,
                                log=job.log, cancelled=job.cancelled)
    job.log("building delta chain")
    plan = chain.plan_linear(job.depot, job.version)
    job.log(f"chain: {len(plan.blobs)} blobs + {len(plan.dats)} dats, measuring sizes")
    return chain.size_up(plan, cancelled=job.cancelled)


def _download_all(job: Job, blob_dir: Path, dat_dir: Path):
    entries = []
    for e in job.plan.blobs:
        entries.append((e, blob_dir / e["filename"]))
    for e in job.plan.dats:
        entries.append((e, dat_dir / e["filename"]))

    # Anything already on disk at the right size counts as done up front.
    todo = []
    for entry, path in entries:
        if path.exists() and (not entry.get("size") or path.stat().st_size == entry["size"]):
            job.bytes_done += path.stat().st_size
            job.files_done += 1
        else:
            todo.append((entry, path))

    job.files_total = len(entries)
    if not todo:
        job.log("every file already in the local library")
        return

    job.log(f"downloading {len(todo)} file(s), {_human(sum(e.get('size') or 0 for e, _ in todo))}")

    # With several titles queued, part of the batch is pulled from the swarm
    # instead so two volunteer-run mirrors are not carrying all of it. Only
    # every other file switches: BitTorrent is slower per file here, so the aim
    # is to halve the load, not to move it wholesale.
    conf = settings.all()
    busy = active_count() >= conf["torrent_busy_threshold"]
    spread = (conf["torrent_when_busy"] and busy and settings.use_torrent()
              and settings.use_mirrors())
    if spread:
        job.log(f"{active_count()} downloads running - sending some files to the "
                f"torrent so the mirrors are not carrying the whole queue")

    def fetch(item):
        index, (entry, path) = item
        if job.cancelled():
            raise net.Cancelled()
        job.current = entry["filename"]
        via_swarm = spread and index % 2 == 1
        transfer = net.prefer_torrent if via_swarm else net.download
        transfer(entry["path"], path, expected_size=entry.get("size"),
                 progress=job.add_bytes, cancelled=job.cancelled)
        job.files_done += 1
        return entry["filename"]

    todo = list(enumerate(todo))
    with ThreadPoolExecutor(max_workers=conf["download_threads"]) as pool:
        futures = [pool.submit(fetch, item) for item in todo]
        for fut in as_completed(futures):
            if job.cancelled():
                for f in futures:
                    f.cancel()
                raise net.Cancelled()
            fut.result()


def _offer_to_swarm(job: Job):
    """Hand the files this job downloaded to the seeder.

    They are byte-identical to what the torrent carries, so they can go
    straight back out to whoever else is pulling them.
    """
    try:
        from . import seed
        if seed.running():
            seed.rescan()
            job.log("added to what SteamFlix seeds back to the swarm")
    except Exception as exc:  # noqa: BLE001 - seeding must never fail a job
        logbook.warn("torrent", "could not offer the new files to the swarm",
                     detail=str(exc))


def ensure_extractor():
    """Fetch the mirror's prebuilt extractor on first use."""
    if config.EXTRACTOR_PATH.exists():
        return config.EXTRACTOR_PATH
    config.TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    net.download("extractor/extract.exe", config.EXTRACTOR_PATH)
    return config.EXTRACTOR_PATH


def _slug(text: str) -> str:
    keep = "".join(c if (c.isalnum() or c in " -_.") else "-" for c in text)
    return " ".join(keep.split()).strip(" .-")[:60] or "Untitled"


def output_dir(job: Job) -> Path:
    """Each title gets its own folder, named after the game where known."""
    meta = db.one("SELECT name FROM depot_meta WHERE depot = ?", (job.depot,))
    name = _slug(meta["name"]) if meta and meta["name"] else f"Depot {job.depot}"
    leaf = f"{name} [{job.depot}] v{job.version}"
    if job.crc:
        leaf += f" ({job.crc})"
    return config.EXTRACT_DIR / leaf


def _run_extractor(job: Job, cmd, cwd=None):
    job.log("running: " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1,
                            cwd=str(cwd) if cwd else None)
    output = []
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            output.append(line)
            job.log(line)
        if job.cancelled():
            proc.terminate()
            raise net.Cancelled()
    return proc.wait(), output


def _chain_pairs(job: Job, blob_dir: Path, dat_dir: Path):
    """Every (blob, dat) pair of this job's chain that is on disk, newest first.

    The extractor reads the whole chain, so the key question has to be asked of
    the whole chain too - and the newest version is the likeliest to hold an
    encrypted chunk worth testing against.
    """
    if not job.plan:
        return []
    dats = {d["version"]: d["filename"] for d in job.plan.dats}
    pairs = []
    for blob in sorted(job.plan.blobs, key=lambda b: b["version"], reverse=True):
        blob_path = blob_dir / blob["filename"]
        dat_name = dats.get(blob["version"])
        dat_path = dat_dir / dat_name if dat_name else None
        if blob_path.exists() and dat_path is not None and dat_path.exists():
            pairs.append((blob_path, dat_path))
    return pairs


def _key_attempts(job: Job, blob_dir: Path, dat_dir: Path):
    """Decide which keys to hand the extractor, in order.

    The extractor refuses to run without a key even for depots that hold nothing
    encrypted, so the first job is to find out whether a real key is needed at
    all. When one is, and none is bundled, the trial in keys.py hunts for it.
    """
    if job.key_override:
        return [(job.key_override, "the key you supplied")]

    pairs = _chain_pairs(job, blob_dir, dat_dir)
    known = keys.known_key(job.depot)
    zero = ("0" * 32, "all-zero key (nothing encrypted)")

    if not pairs:
        return ([(known, "known key for this depot")] if known else []) + \
               [(None, "the extractor's built-in key"), zero]

    try:
        info = keys.analyse_chain([b for b, _d in pairs])
    except Exception as exc:  # noqa: BLE001 - fall back to the old behaviour
        job.log(f"could not read the checksum table ({exc}); trying keys blind")
        return ([(known, "known key for this depot")] if known else []) + \
               [(None, "the extractor's built-in key"), zero]

    if not info["needs_key"]:
        job.log(f"nothing in this depot is encrypted across all {info['blobs']} blob(s) "
                f"(file modes {info['modes']}) - no real key is needed")
        return [zero, (None, "the extractor's built-in key")]

    job.log(f"{info['encrypted_files']} of {info['files']} files are encrypted - "
            f"a real key is required")

    names_by_blob = {}
    for blob_path, _dat in pairs:
        try:
            names_by_blob[str(blob_path)] = blobmod.manifest_names(blob_path.read_bytes())
        except Exception:  # noqa: BLE001 - names are only a nicety for mode 3
            continue

    # A key on record is not necessarily a key that works: the bundled table has
    # wrong entries, and an imported list can be for a different dump entirely.
    # Proving it here costs one AES block and saves a whole failed extraction.
    if known:
        verdict = keys.verify_key(known, pairs, names_by_blob)
        if verdict in ("exact", "likely", "unknown"):
            job.log(f"key on record for this depot checks out ({verdict})")
            return [(known, "key already known for this depot"),
                    (None, "the extractor's built-in key")]
        job.log("the key on record for this depot does not decrypt it - "
                "ignoring it and searching for the real one")
        keys.reject_key(job.depot, known)

    job.log("no working key known for this depot - starting a key trial")

    def report(done, total):
        job.current = f"testing keys {done}/{total}"

    top_blob, top_dat = pairs[0]
    result = keys.trial(job.depot, top_blob, top_dat, progress=report,
                        pairs=pairs, names_by_blob=names_by_blob)
    job.key_trial = result
    job.current = ""

    if result.get("key"):
        job.log(f"key trial succeeded: {result['reason']}")
        attempts = [(result["key"], f"recovered key {result['key'][:8]}\u2026")]
        if result.get("confidence") != "exact":
            # Only file magic backed this one up, so keep the usual fallbacks
            # behind it rather than failing the whole job on a near miss.
            attempts += [(None, "the extractor's built-in key"), zero]
        return attempts

    job.log(f"key trial failed: {result['reason']}")
    logbook.add("key", f"depot {job.depot}: no working key found",
                depot=job.depot, job=job.id,
                detail=f"{result.get('tried', 0)} candidates tested against "
                       f"{len(result.get('probes') or [])} chunk(s). Add your own keys in "
                       f"the depot dialog or drop them into data/user_keys.txt.")
    return [zero, (None, "the extractor's built-in key")]


def _check_complete(job: Job, out: Path, blob_dir: Path):
    """Compare what landed on disk with what the manifest said should.

    The extractor exits zero after writing whatever it managed, so without this
    a half-extracted game is indistinguishable from a whole one.
    """
    top = job.plan.blobs[-1] if job.plan and job.plan.blobs else None
    if not top:
        return
    blob_path = blob_dir / top["filename"]
    if not blob_path.exists():
        return
    try:
        expected = blobmod.manifest_entries(blob_path.read_bytes())
    except Exception:  # noqa: BLE001 - the check must never fail the job
        return
    if not expected:
        return

    on_disk = {str(p.relative_to(out)).replace("\\", "/").lower()
               for p in out.rglob("*") if p.is_file()}
    missing = [name for name in expected
               if name.replace("\\", "/").lower() not in on_disk]
    job.files_expected = len(expected)
    job.files_missing = len(missing)
    if not missing:
        job.log(f"all {len(expected)} file(s) in the manifest are present")
        return

    job.partial = True
    job.log(f"{len(missing)} of {len(expected)} manifest file(s) did not appear - "
            f"this extraction is incomplete")
    logbook.warn("extract",
                 f"depot {job.depot} v{job.version}: {len(missing)} of "
                 f"{len(expected)} files missing after extraction",
                 depot=job.depot, job=job.id,
                 detail="First few: " + ", ".join(missing[:5]))


def _extract(job: Job, blob_dir: Path, dat_dir: Path):
    job.state = "extracting"
    job.current = "extract.exe"
    exe = ensure_extractor()

    out = output_dir(job)
    out.mkdir(parents=True, exist_ok=True)
    job.out_dir = out

    # The extractor strips every ':' out of the directory it creates, so an
    # absolute --out like "D:\lib\Game" has its folders made under a relative
    # "D\lib\Game" while the files themselves are still written to the real
    # path. The parent then does not exist and every file inside a subdirectory
    # silently fails - only the ones sitting at the root of the depot appear,
    # which is how a 145-file game came out as 4 files and looked fine.
    # Running from the library folder with a relative --out avoids the colon
    # entirely and the whole tree lands where it should.
    base = [str(exe), str(blob_dir), str(dat_dir), str(job.depot), str(job.version),
            "--out", out.name]
    if job.crc:
        base += ["--blobcrc", job.crc]
    if job.file_filter:
        base += ["--filter", job.file_filter]

    attempts = _key_attempts(job, blob_dir, dat_dir)

    last_output = []
    for i, (key, label) in enumerate(attempts):
        cmd = base + (["--key", key] if key else [])
        job.log(f"attempt {i + 1}/{len(attempts)}: {label}")
        code, last_output = _run_extractor(job, cmd, cwd=out.parent)
        produced = sum(1 for _ in out.rglob("*") if _.is_file())

        if code == 0:
            job.key_used = key
            break

        # A non-zero exit that still produced files means the key was right and
        # the extractor gave up on one chunk it cannot handle. That is worth
        # keeping, and trying more keys would not improve it.
        if produced:
            job.key_used = key
            job.partial = True
            reason = next((ln for ln in reversed(last_output) if "error" in ln.lower()),
                          "the extractor stopped early")
            job.log(f"extractor stopped early after {produced} file(s): {reason}")
            logbook.warn("extract",
                         f"depot {job.depot} v{job.version} extracted partially "
                         f"({produced} files)",
                         depot=job.depot, job=job.id, detail=reason)
            break

        if i + 1 < len(attempts):
            job.log("that key did not work, trying the next one")
    else:
        tail = " | ".join(last_output[-3:]) or "no output"
        category = "key" if any("key" in ln.lower() for ln in last_output) else "extract"
        logbook.add(category, f"depot {job.depot} v{job.version} would not extract",
                    depot=job.depot, job=job.id, detail=tail)
        raise RuntimeError(f"extraction failed for depot {job.depot}: {tail}")

    job.extracted_files = sum(1 for _ in out.rglob("*") if _.is_file())
    _check_complete(job, out, blob_dir)
    job.log(f"extracted {job.extracted_files} file(s) to {out}"
            + (" (partial)" if job.partial else ""))


def _execute(job: Job):
    blob_dir, dat_dir = depot_dirs(job.depot)
    blob_dir.mkdir(parents=True, exist_ok=True)
    dat_dir.mkdir(parents=True, exist_ok=True)
    try:
        job.plan = _run_plan(job, blob_dir)
        job.bytes_total = job.plan.total_bytes
        job.files_total = len(job.plan.blobs) + len(job.plan.dats)
        job.log(f"plan ready: {job.files_total} files, {_human(job.bytes_total)}")
        for w in job.plan.warnings:
            job.log("warning: " + w)

        room = storage.fits(job.bytes_total)
        if not room["fits"]:
            if room["limited_by"] == "budget":
                msg = (f"this needs {_human(job.bytes_total)} but only "
                       f"{_human(room['usable'])} is left in the "
                       f"{_human(room['budget'])} SteamFlix budget "
                       f"({_human(room['budget_used'])} already used). Free space from "
                       f"My Library, or raise STEAMFLIX_BUDGET_GB.")
            else:
                msg = (f"not enough disk space: needs {_human(job.bytes_total)}, "
                       f"only {_human(room['usable'])} usable on {config.LIB_DIR.anchor} "
                       f"(short by {_human(room['short_by'])})")
            logbook.add("storage", msg, depot=job.depot, job=job.id)
            raise RuntimeError(msg)

        job.state = "downloading"
        _download_all(job, blob_dir, dat_dir)
        if job.cancelled():
            raise net.Cancelled()

        if job.do_extract:
            _extract(job, blob_dir, dat_dir)
        else:
            job.log("download only - skipping extraction")

        job.state = "done"
        job.current = ""
        _offer_to_swarm(job)
    except net.Cancelled:
        job.state = "cancelled"
        job.log("cancelled")
    except chain.ChainError as exc:
        job.state = "error"
        job.error = str(exc)
        job.log("error: " + str(exc))
        logbook.add("chain", str(exc), depot=job.depot, job=job.id)
    except net.AllMirrorsFailed as exc:
        job.state = "error"
        job.error = str(exc)
        job.log("error: " + str(exc))
    except Exception as exc:  # noqa: BLE001 - reported verbatim in the UI
        job.state = "error"
        job.error = str(exc)
        job.log("error: " + str(exc))
        # Storage, key and extract failures already logged themselves with the
        # right category; anything else lands here as a generic job failure.
        if not isinstance(exc, RuntimeError):
            logbook.add("extract", str(exc), depot=job.depot, job=job.id)
    finally:
        job.finished = time.time()
        _persist(job)


def _worker():
    while True:
        job = _queue.get()
        try:
            _execute(job)
        finally:
            _queue.task_done()


def start():
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    threading.Thread(target=_worker, daemon=True).start()


def submit(**kwargs) -> Job:
    job = Job(**kwargs)
    with _lock:
        _jobs[job.id] = job
    _persist(job)
    job.log(f"queued {job.title} - depot {job.depot} version {job.version}"
            + (f" crc {job.crc}" if job.crc else ""))
    _queue.put(job)
    start()
    return job


def get(job_id):
    return _jobs.get(job_id)


def all_jobs():
    with _lock:
        return sorted(_jobs.values(), key=lambda j: j.started, reverse=True)


def active_count():
    return sum(1 for j in _jobs.values() if j.state in ACTIVE_STATES)


def cancel_all():
    """Ask every running job to stop. Used when the server is shutting down."""
    n = 0
    with _lock:
        for job in _jobs.values():
            if job.state in ACTIVE_STATES:
                job.cancel()
                n += 1
    return n


def clear_finished():
    with _lock:
        for jid in [j.id for j in _jobs.values() if j.state not in ACTIVE_STATES]:
            _jobs.pop(jid, None)


# --------------------------------------------------------------------------- #
INSTALLED_RE = re.compile(r"^(?P<name>.+?) \[(?P<depot>\d+)\] v(?P<version>\d+)"
                          r"(?: \((?P<crc>[0-9a-f]{8})\))?$")


def installed():
    """Everything already extracted into the library folder."""
    out = []
    if not config.EXTRACT_DIR.exists():
        return out
    for entry in sorted(config.EXTRACT_DIR.iterdir()):
        if not entry.is_dir():
            continue
        m = INSTALLED_RE.match(entry.name)
        if not m:
            continue
        depot = int(m.group("depot"))
        size = 0
        count = 0
        for f in entry.rglob("*"):
            if f.is_file():
                size += f.stat().st_size
                count += 1
        meta = db.one("SELECT name, appid FROM depot_meta WHERE depot = ?", (depot,))
        out.append({
            "depot": depot,
            "version": int(m.group("version")),
            "crc": m.group("crc"),
            "folder": entry.name,
            "path": str(entry),
            "bytes": size,
            "files": count,
            "name": (meta["name"] if meta and meta["name"] else m.group("name")),
            "appid": meta["appid"] if meta else None,
        })
    return out


def cached_size(depot: int):
    """How much of a depot is already sitting in the local library."""
    blob_dir, dat_dir = depot_dirs(depot)
    total = 0
    for d in (blob_dir, dat_dir):
        if d.exists():
            total += sum(f.stat().st_size for f in d.iterdir() if f.is_file())
    return total


def delete_depot_cache(depot: int):
    base = config.DEPOT_DIR / str(depot)
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
        return True
    return False


def _human(n):
    if not n:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"
