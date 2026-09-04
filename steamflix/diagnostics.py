"""Self-checks: is this install healthy, and did that game come out complete?

Two separate questions, both answered here.

``run()`` checks the installation - Python, packages, disk, extractor, database,
mirrors, the torrent and the key table - and returns a list of results the UI
renders as a report. Each result says what was checked, what happened, and what
to do about it if it is wrong.

``verify_install()`` answers the more interesting one: the download finished and
the extractor said nothing, but is the game actually all there? It compares the
depot's own manifest against what ended up on disk and reports what is missing,
what is the wrong size, and whether there is anything to run.
"""
import os
import shutil
import sys
import time
from pathlib import Path

from . import blob as blobmod
from . import chain, config, db, index, jobs, keys, media, net, proxies, settings

OK, WARN, BAD = "ok", "warning", "bad"


def _result(name, state, detail, fix=None, extra=None):
    out = {"check": name, "state": state, "detail": detail}
    if fix:
        out["fix"] = fix
    if extra:
        out["extra"] = extra
    return out


# --------------------------------------------------------------------------- #
# installation checks
# --------------------------------------------------------------------------- #
def check_python():
    v = sys.version_info
    version = f"{v.major}.{v.minor}.{v.micro}"
    if v < (3, 9):
        return _result("Python", BAD, f"{version} is too old",
                       "SteamFlix needs Python 3.9 or newer.")
    return _result("Python", OK, f"{version} at {sys.executable}")


def check_packages():
    missing = []
    for module, package in (("flask", "flask"), ("requests", "requests"),
                            ("cryptography", "cryptography")):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        return _result("Packages", BAD, f"missing: {', '.join(missing)}",
                       f"Run: {sys.executable} -m pip install {' '.join(missing)}")
    return _result("Packages", OK, "flask, requests and cryptography are present")


def check_catalogue():
    if not index.is_built():
        return _result("Catalogue", BAD, "not built yet",
                       "Start SteamFlix and let the first run finish, or run "
                       "server.py --rebuild.")
    files = db.one("SELECT COUNT(*) c FROM files")["c"]
    depots = db.one("SELECT COUNT(*) c FROM depots")["c"]
    named = db.one("SELECT COUNT(*) c FROM depot_meta WHERE name IS NOT NULL")["c"]
    if not files:
        return _result("Catalogue", BAD, "the file table is empty",
                       "Rebuild it: server.py --rebuild")
    state = OK if named else WARN
    return _result("Catalogue", state,
                   f"{files:,} files, {depots:,} depots, {named:,} named",
                   None if named else "Names arrive in the background from "
                                      "api.steamcmd.net; give it a few minutes.")


def check_disk():
    u = __import__("steamflix.storage", fromlist=["storage"]).usage()
    free_gb = u["free"] / 2 ** 30
    if u["usable"] <= 0:
        return _result("Disk", BAD,
                       f"{free_gb:.1f} GB free on {u['drive']} - nothing usable "
                       f"after the {u['reserve'] / 2 ** 30:.0f} GB reserve",
                       "Free some space, or point STEAMFLIX_LIBRARY at another drive.")
    state = WARN if u["usable"] < 5 * 2 ** 30 else OK
    return _result("Disk", state,
                   f"{u['usable'] / 2 ** 30:.1f} GB usable on {u['drive']} "
                   f"({free_gb:.1f} GB free)",
                   "Most single versions are well under a gigabyte, but a deep "
                   "chain can run to tens." if state == WARN else None)


def check_extractor():
    exe = config.EXTRACTOR_PATH
    if exe.exists():
        return _result("Extractor", OK,
                       f"{exe} ({exe.stat().st_size:,} bytes)")
    return _result("Extractor", WARN, "not downloaded yet",
                   "SteamFlix fetches it from the mirror the first time you "
                   "extract something. Nothing to do.")


def check_keys():
    total = db.one("SELECT COUNT(*) c FROM depot_keys")["c"]
    by_source = {r["source"]: r["c"] for r in
                 db.query("SELECT source, COUNT(*) c FROM depot_keys GROUP BY source")}
    keyed = db.one("SELECT SUM(has_key) k FROM depots")["k"] or 0
    if not total:
        where = ", ".join(str(p) for p in config.KEYS_CPP_CANDIDATES[-3:])
        return _result("Depot keys", WARN, "no keys loaded",
                       "Most depots need a key. Drop the reference extractor's "
                       f"keys.cpp into any of: {where} - or import a key list "
                       "from a depot's dialog. Depots that hold nothing "
                       "encrypted still extract without one.")
    return _result("Depot keys", OK,
                   f"{total:,} keys ({', '.join(f'{v:,} {k}' for k, v in by_source.items())}) "
                   f"covering {keyed:,} depots")


def check_mirrors(deep=True):
    """Ask each mirror for a byte, so a benched or dead host is obvious."""
    rows = []
    worst = OK
    for mirror in net.all_mirrors():
        if not deep:
            rows.append({"mirror": mirror, "state": "unchecked"})
            continue
        started = time.time()
        try:
            r = net.session().get(
                net.mirror_url("blobs_dates.txt", mirror),
                headers={"Range": "bytes=0-63"}, timeout=10, stream=True)
            r.close()
            ms = int((time.time() - started) * 1000)
            if r.status_code in (200, 206):
                rows.append({"mirror": mirror, "state": "up", "ms": ms,
                             "ranges": r.status_code == 206})
            else:
                worst = WARN
                rows.append({"mirror": mirror, "state": f"HTTP {r.status_code}",
                             "ms": ms})
        except Exception as exc:  # noqa: BLE001
            worst = WARN
            rows.append({"mirror": mirror, "state": "unreachable",
                         "error": str(exc)[:120]})

    up = [r for r in rows if r.get("state") == "up"]
    if deep and not up:
        worst = BAD
    detail = (f"{len(up)} of {len(rows)} reachable" if deep
              else f"{len(rows)} configured")
    fix = None
    if deep and not up:
        fix = ("Both mirrors are refusing traffic. SteamFlix can still download "
               "through the torrent if steam2.torrent is present - check the "
               "swarm below.")
    elif any(r.get("state") == "up" and not r.get("ranges") for r in rows):
        fix = ("A mirror ignored a Range request, so segmented downloads will "
               "fall back to a single stream on it.")
    return _result("Mirrors", worst, detail, fix, {"mirrors": rows})


def check_torrent(deep=False):
    from . import torrent as torrentmod
    if not torrentmod.available():
        return _result("Torrent fallback", WARN, "no steam2.torrent found",
                       "Without it SteamFlix is HTTP-only. Put steam2.torrent "
                       "beside the project folder, or set STEAMFLIX_TORRENT.")
    info = torrentmod.status()
    if info.get("error"):
        return _result("Torrent fallback", BAD, info["error"],
                       "The file may be truncated - download it again.")
    detail = (f"{info['files']:,} files, {info['trackers']} trackers"
              + (f", index built ({info['indexed']:,} rows)" if info["indexed"]
                 else ", not indexed yet"))
    if not deep:
        return _result("Torrent fallback", OK, detail)
    try:
        found = torrentmod.peers(limit=60, deadline=15)
    except Exception as exc:  # noqa: BLE001
        return _result("Torrent fallback", WARN, f"{detail}; trackers failed",
                       str(exc))
    if not found:
        return _result("Torrent fallback", WARN, f"{detail}; no peers answered",
                       "The swarm may be quiet right now. The mirrors are then "
                       "the only route.")
    return _result("Torrent fallback", OK, f"{detail}; {len(found)} peers online")


def check_settings():
    conf = settings.all()
    notes = []
    state = OK
    if conf["request_delay_ms"] < 50:
        state = WARN
        notes.append("the delay between requests is very short")
    if conf["download_threads"] * conf["segments_per_file"] > 48:
        state = WARN
        notes.append(f"{conf['download_threads']} files x "
                     f"{conf['segments_per_file']} segments is a lot of "
                     f"simultaneous connections")
    detail = (f"{conf['source_mode']} source, {conf['request_delay_ms']} ms between "
              f"requests, {conf['download_threads']} files at a time")
    return _result("Politeness", state, detail,
                   ("These mirrors are volunteer-run: " + ", ".join(notes) + ".")
                   if notes else None,
                   {"throttle": net.throttle_status()})


def check_proxies():
    st = proxies.status()
    if not st["enabled"]:
        return _result("Proxies", OK, "off - connecting directly")
    if not st["working"]:
        return _result("Proxies", BAD, "enabled but no working proxy in the pool",
                       "Refresh the pool in Settings, or turn proxies off.")
    return _result("Proxies", OK,
                   f"{st['working']} working proxies, {st['mode']} order")


def check_library():
    installed = jobs.installed()
    if not installed:
        return _result("Library", OK, "nothing downloaded yet")
    broken = [i for i in installed if not i["files"]]
    state = WARN if broken else OK
    return _result("Library", state,
                   f"{len(installed)} title(s) extracted",
                   f"{len(broken)} folder(s) are empty - re-download them."
                   if broken else None)


def run(deep=True):
    """The whole report. ``deep`` allows network checks."""
    db.init()
    checks = [
        check_python(), check_packages(), check_catalogue(), check_disk(),
        check_extractor(), check_keys(), check_settings(), check_proxies(),
        check_mirrors(deep), check_torrent(deep), check_library(),
    ]
    worst = BAD if any(c["state"] == BAD for c in checks) else (
        WARN if any(c["state"] == WARN for c in checks) else OK)
    return {
        "state": worst,
        "checked_at": time.time(),
        "deep": deep,
        "checks": checks,
        "summary": {
            "ok": sum(1 for c in checks if c["state"] == OK),
            "warning": sum(1 for c in checks if c["state"] == WARN),
            "bad": sum(1 for c in checks if c["state"] == BAD),
        },
    }


# --------------------------------------------------------------------------- #
# "is this game actually complete?"
# --------------------------------------------------------------------------- #
def _manifest_files(depot, version, crc=None):
    """File list and sizes from the depot's own manifest.

    The manifest is inside the blob, which is small, so this reads the local
    copy if the download left one behind and downloads it otherwise.
    """
    blob_dir, _ = jobs.depot_dirs(depot)
    sql = "SELECT * FROM files WHERE depot = ? AND kind = 'blob' AND version = ?"
    args = [depot, version]
    if crc:
        sql += " AND crc = ?"
        args.append(crc)
    row = db.one(sql + " ORDER BY version DESC LIMIT 1", args)
    if row is None:
        return None
    path = blob_dir / row["filename"]
    if not path.exists():
        size = chain.remote_size("blob", row["filename"])
        net.download(chain.path_for("blob", row["filename"]), path, expected_size=size)
    return blobmod.manifest_entries(path.read_bytes())


def verify_install(folder: str):
    """Check an extracted game against the manifest it came from.

    Answers the question the extractor cannot: it exits zero after writing what
    it managed, so a chain with one bad delta produces a folder that looks fine
    and is missing half the game.
    """
    entry = next((i for i in jobs.installed() if i["folder"] == folder), None)
    if entry is None:
        return {"error": "no such installed title"}

    root = Path(entry["path"])
    on_disk = {}
    for path in root.rglob("*"):
        if path.is_file():
            rel = str(path.relative_to(root)).replace("\\", "/").lower()
            try:
                on_disk[rel] = path.stat().st_size
            except OSError:
                on_disk[rel] = -1

    result = {
        "folder": folder,
        "name": entry["name"],
        "depot": entry["depot"],
        "version": entry["version"],
        "path": str(root),
        "files_on_disk": len(on_disk),
        "bytes_on_disk": sum(v for v in on_disk.values() if v > 0),
    }

    try:
        expected = _manifest_files(entry["depot"], entry["version"], entry.get("crc"))
    except Exception as exc:  # noqa: BLE001 - the check itself must not blow up
        expected = None
        result["manifest_error"] = str(exc)

    if expected:
        missing, short = [], []
        for name, size in expected.items():
            key = name.replace("\\", "/").lower()
            if key not in on_disk:
                missing.append({"file": name, "size": size})
            elif size and on_disk[key] < size:
                short.append({"file": name, "expected": size, "got": on_disk[key]})
        result.update({
            "files_expected": len(expected),
            "missing": missing[:200],
            "missing_count": len(missing),
            "short": short[:200],
            "short_count": len(short),
            "complete": not missing and not short,
        })
    else:
        result["complete"] = None

    # Even a complete extraction is not much use without something to run.
    scan = media.scan(root)
    result["launcher"] = scan.get("launcher")
    result["mode"] = scan.get("mode")
    result["counts"] = scan.get("counts")

    if result.get("complete") is False:
        result["verdict"] = (
            f"{result['missing_count']} file(s) missing and "
            f"{result['short_count']} truncated. Usually a broken delta in the "
            f"chain: re-download this version, and if the depot was reset try "
            f"the other blob variant.")
    elif result.get("complete") is None:
        result["verdict"] = ("Could not read the manifest, so completeness is "
                             "unknown. What is on disk looks like "
                             f"{scan.get('mode')}.")
    elif scan.get("mode") == "play":
        result["verdict"] = (f"Complete, and ready to run "
                             f"({scan['launcher']['name']}).")
    elif scan.get("mode") == "watch":
        result["verdict"] = ("Complete. This depot holds media rather than an "
                             "executable - it is a trailer or a content pack.")
    else:
        result["verdict"] = ("Complete. There is no executable in this depot; "
                             "big games keep their binaries in a separate one, "
                             "so check the other depots for this title.")
    return result
