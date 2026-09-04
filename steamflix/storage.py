"""Disk reporting and the SteamFlix storage budget.

Two separate limits matter. The drive has whatever free space it has, and on top
of that SteamFlix keeps its own budget so an archive this large cannot quietly
eat a disk. Both are shown in the UI and both are enforced before a job starts.
"""
import shutil
import string
from pathlib import Path

from . import config

_size_cache = {}


def dir_size(path: Path, cache_key=None):
    if not path.exists():
        return 0
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return total


def disks():
    """Every fixed drive with its usage, so the UI can show where space is."""
    out = []
    lib_drive = str(Path(config.LIB_DIR).anchor).upper()
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if not Path(root).exists():
            continue
        try:
            total, used, free = shutil.disk_usage(root)
        except OSError:
            continue
        if not total:
            continue
        out.append({
            "drive": root,
            "total": total,
            "used": used,
            "free": free,
            "percent_used": round(used / total * 100, 1),
            "is_library": root.upper() == lib_drive,
        })
    return out


def usage():
    config.ensure_dirs()
    total, used, free = shutil.disk_usage(config.LIB_DIR)
    cache = dir_size(config.DEPOT_DIR)
    extracted = dir_size(config.EXTRACT_DIR)
    mine = cache + extracted

    disk_left = max(0, free - config.STORAGE_RESERVE)

    # With no budget set the "budget" is whatever the drive can still take, so
    # the header pill reports this machine's real headroom instead of a made-up
    # hundred gigabytes.
    capped = config.STORAGE_BUDGET is not None
    budget = config.STORAGE_BUDGET if capped else (mine + disk_left)
    budget_left = max(0, budget - mine)

    return {
        "path": str(config.LIB_DIR),
        "drive": str(Path(config.LIB_DIR).anchor),
        "total": total,
        "used": used,
        "free": free,
        "reserve": config.STORAGE_RESERVE,
        "budget": budget,
        "budget_capped": capped,
        "budget_used": mine,
        "budget_left": budget_left,
        "budget_percent": round(mine / budget * 100, 1) if budget else 0,
        "usable": min(budget_left, disk_left),
        "steamflix": mine,
        "cache_bytes": cache,
        "extracted_bytes": extracted,
        "percent_used": round(used / total * 100, 1) if total else 0,
        "percent_steamflix": round(mine / total * 100, 2) if total else 0,
        "disks": disks(),
    }


def fits(nbytes: int):
    """Whether a download fits inside both the disk headroom and the budget."""
    u = usage()
    limit = "budget" if u["budget_left"] < (u["free"] - u["reserve"]) else "disk"
    return {
        "fits": nbytes <= u["usable"],
        "needed": nbytes,
        "usable": u["usable"],
        "short_by": max(0, nbytes - u["usable"]),
        "limited_by": limit,
        "budget": u["budget"],
        "budget_used": u["budget_used"],
        "free": u["free"],
    }
