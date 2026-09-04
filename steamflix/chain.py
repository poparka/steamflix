"""Works out exactly which blobs and dats a given depot version needs.

Steam2 stores content as deltas, so extracting version N requires the blob and
dat for every version from 0 up to N. When Valve reset a depot the same version
number exists more than once, and the only way to know which files belong
together is to start from the wanted blob's CRC and follow the parent-CRC chain
backwards, matching each step's dat by the size recorded inside the blob.
"""
from dataclasses import dataclass, field

from . import blob as blobmod
from . import config, db, net


class ChainError(Exception):
    pass


@dataclass
class Plan:
    depot: int
    version: int
    crc: str = None
    reset: bool = False
    blobs: list = field(default_factory=list)   # dicts: filename, version, crc, size
    dats: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def total_bytes(self):
        known = [f["size"] for f in self.blobs + self.dats if f.get("size")]
        return sum(known)

    @property
    def unknown_sizes(self):
        return sum(1 for f in self.blobs + self.dats if not f.get("size"))

    def to_dict(self):
        return {
            "depot": self.depot,
            "version": self.version,
            "crc": self.crc,
            "reset": self.reset,
            "blobs": self.blobs,
            "dats": self.dats,
            "blob_count": len(self.blobs),
            "dat_count": len(self.dats),
            "total_bytes": self.total_bytes,
            "unknown_sizes": self.unknown_sizes,
            "warnings": self.warnings,
        }


def files_for(depot: int, kind: str):
    return db.query(
        "SELECT filename, version, crc, hash, size, mtime FROM files"
        " WHERE depot = ? AND kind = ? ORDER BY version",
        (depot, kind),
    )


def depot_has_reset(depot: int) -> bool:
    row = db.one("SELECT has_reset FROM depots WHERE depot = ?", (depot,))
    return bool(row and row["has_reset"])


def path_for(kind: str, filename: str) -> str:
    """Mirror-relative path; the actual host is chosen per request by net."""
    return ("blobs/" if kind == "blob" else "dats/") + filename


def remote_size(kind: str, filename: str):
    """Cached Content-Length for a mirror file."""
    row = db.one("SELECT size FROM files WHERE filename = ?", (filename,))
    if row and row["size"]:
        return row["size"]
    size = net.head_size(path_for(kind, filename))
    if size:
        db.execute("UPDATE files SET size = ? WHERE filename = ?", (size, filename))
    return size


def _entry(kind, row, size=None):
    path = path_for(kind, row["filename"])
    return {
        "kind": kind,
        "filename": row["filename"],
        "version": row["version"],
        "crc": row["crc"],
        "size": size if size is not None else row["size"],
        "path": path,
        "url": net.url_for(path),
    }


# --------------------------------------------------------------------------- #
# straightforward depots: one blob and one dat per version
# --------------------------------------------------------------------------- #
def plan_linear(depot: int, version: int) -> Plan:
    plan = Plan(depot=depot, version=version)
    blobs = {r["version"]: r for r in files_for(depot, "blob") if r["version"] <= version}
    dats = {r["version"]: r for r in files_for(depot, "dat") if r["version"] <= version}

    missing = [v for v in range(version + 1) if v not in blobs or v not in dats]
    if missing:
        preview = ", ".join(str(v) for v in missing[:10])
        more = "" if len(missing) <= 10 else f" (+{len(missing) - 10} more)"
        raise ChainError(
            f"depot {depot} is missing files for version(s) {preview}{more}; "
            "the delta chain is incomplete on the mirror"
        )

    for v in range(version + 1):
        plan.blobs.append(_entry("blob", blobs[v]))
        plan.dats.append(_entry("dat", dats[v]))
    return plan


# --------------------------------------------------------------------------- #
# depots Valve reset: follow the parent CRC chain
# --------------------------------------------------------------------------- #
def plan_reset(depot: int, version: int, crc: str, blob_dir, log=None,
               cancelled=None) -> Plan:
    """Resolve a reset depot. Downloads each blob as it walks, since the parent
    CRC and the paired dat size only exist inside the blob itself. Those blobs
    are needed for extraction anyway, so nothing is fetched twice."""
    crc = (crc or "").lower()
    plan = Plan(depot=depot, version=version, crc=crc, reset=True)

    all_blobs = files_for(depot, "blob")
    all_dats = files_for(depot, "dat")
    blobs_by = {}
    for r in all_blobs:
        blobs_by[(r["version"], r["crc"].lower())] = r
    dats_by_version = {}
    for r in all_dats:
        dats_by_version.setdefault(r["version"], []).append(r)

    current = blobs_by.get((version, crc))
    if current is None:
        raise ChainError(f"no blob {depot}_{version}_{crc} on the mirror")

    v = version
    steps = []
    while True:
        if cancelled is not None and cancelled():
            raise ChainError("cancelled")
        if log:
            log(f"chain: reading blob {current['filename'][:40]}... (version {v})")

        path = blob_dir / current["filename"]
        size = remote_size("blob", current["filename"])
        net.download(path_for("blob", current["filename"]), path, expected_size=size,
                     cancelled=cancelled)
        info = blobmod.describe(path.read_bytes())
        steps.append(_entry("blob", current, size or path.stat().st_size))

        wanted_dat = info["dat_size"]
        candidates = dats_by_version.get(v, [])
        chosen = None
        if len(candidates) == 1 and wanted_dat is None:
            chosen = candidates[0]
        else:
            for cand in candidates:
                if remote_size("dat", cand["filename"]) == wanted_dat:
                    chosen = cand
                    break
        if chosen is None:
            raise ChainError(
                f"depot {depot} version {v}: no dat matching the size recorded in the blob "
                f"({wanted_dat}); the mirror copy is incomplete"
            )
        plan.dats.append(_entry("dat", chosen, wanted_dat))

        if v == 0:
            break
        parent = info["prev_crc"]
        if not parent or parent == "00000000":
            plan.warnings.append(
                f"blob at version {v} has no parent CRC but version 0 was not reached"
            )
            break
        nxt = blobs_by.get((v - 1, parent))
        if nxt is None:
            raise ChainError(
                f"depot {depot}: blob {v - 1}_{parent} referenced by version {v} is not on the mirror"
            )
        current = nxt
        v -= 1

    plan.blobs = list(reversed(steps))
    plan.dats.reverse()
    return plan


def size_up(plan: Plan, cancelled=None):
    """Fill in Content-Length for every entry so the UI can show a real total."""
    for entry in plan.blobs + plan.dats:
        if cancelled is not None and cancelled():
            return plan
        if not entry.get("size"):
            entry["size"] = remote_size(entry["kind"], entry["filename"])
    return plan


def variants(depot: int, version: int):
    """Every blob CRC that exists for one version of a depot."""
    rows = db.query(
        "SELECT filename, crc, mtime FROM files WHERE depot = ? AND kind = 'blob' AND version = ?"
        " ORDER BY mtime",
        (depot, version),
    )
    return [dict(r) for r in rows]
