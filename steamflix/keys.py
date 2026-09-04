"""Depot key handling and the automatic key trial.

Two separate problems get conflated by the extractor's error message:

    error!! -- extract: missing depot key!, pass --key if you know it though

It refuses to start without a key even when the depot contains nothing
encrypted, which is why people report that ``--key 0`` "works" on some depots -
the key is never used. So the first thing SteamFlix does is read the checksum
table inside the blob and look at the per-file compression mode:

    0 uncompressed          no key needed
    1 compressed            no key needed
    2 compressed+encrypted  key required
    3 encrypted             key required

If nothing is encrypted, any key satisfies the argument check and extraction is
guaranteed. If something *is* encrypted and no key is bundled, SteamFlix runs a
trial: it decrypts one small chunk with each candidate key and keeps the one
that produces valid data. Candidates come from the extractor's own table, from
any key file you point SteamFlix at, and from the all-zero keys.

Validation is exact for mode 2 chunks, whose plaintext must inflate with zlib to
a length the chunk header states up front. Mode 3 chunks are checked against the
magic bytes of a file whose extension is known from the manifest.
"""
import json
import os
import re
import struct
import zlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import blob as blobmod
from . import config, db, logbook

KEY_RE = re.compile(r"^\s*\{\s*(\d+)\s*,\s*\{([^}]*)\}", re.MULTILINE)
HEX_RE = re.compile(r"\b([0-9a-fA-F]{32})\b")
ZERO_IV = b"\x00" * 16

# Extension -> bytes every file of that type starts with.
MAGICS = {
    ".exe": [b"MZ"], ".dll": [b"MZ"], ".ocx": [b"MZ"], ".sys": [b"MZ"],
    ".wav": [b"RIFF"], ".avi": [b"RIFF"], ".bmp": [b"BM"],
    ".png": [b"\x89PNG"], ".gif": [b"GIF8"], ".jpg": [b"\xff\xd8\xff"],
    ".pdf": [b"%PDF"], ".zip": [b"PK\x03\x04"], ".cab": [b"MSCF"],
    ".mp3": [b"ID3", b"\xff\xfb"], ".ogg": [b"OggS"], ".gcf": [b"\x01\x00\x00\x00"],
    ".bik": [b"BIK"], ".wad": [b"WAD2", b"WAD3", b"IWAD", b"PWAD"],
    ".mdl": [b"IDST", b"IDSQ"], ".bsp": [b"\x1e\x00\x00\x00", b"VBSP"],
    ".vtf": [b"VTF\x00"], ".vpk": [b"\x34\x12\xaa\x55"],
}
TEXT_EXT = {".txt", ".ini", ".cfg", ".log", ".res", ".vdf", ".gam", ".rc"}


class KeyError_(Exception):
    pass


# --------------------------------------------------------------------------- #
# candidate sources
# --------------------------------------------------------------------------- #
def _parse_keys_cpp(path: Path):
    """Read the extractor's built-in table: { depot, { 0xaa,0xbb,... } }."""
    out = {}
    text = path.read_text(encoding="utf-8", errors="replace")
    for depot, body in KEY_RE.findall(text):
        parts = [p.strip() for p in body.split(",") if p.strip()]
        try:
            raw = bytes(int(p, 16) for p in parts)
        except ValueError:
            continue
        if len(raw) == 16:
            out[int(depot)] = raw
    return out


def bundled_keys():
    """Every key shipped with the extractor, cached in the database."""
    cached = db.get_kv("bundled_keys_loaded")
    # A cached zero means the last attempt found nothing - keys.cpp may have
    # arrived since, so that case is retried rather than remembered forever.
    if not cached or cached in ("0", 0):
        for path in config.KEYS_CPP_CANDIDATES:
            if not path.exists():
                continue
            found = _parse_keys_cpp(path)
            if found:
                with db.transaction() as conn:
                    conn.executemany(
                        "INSERT OR REPLACE INTO depot_keys(depot, key, source)"
                        " VALUES(?,?,'bundled')",
                        [(d, k.hex(), ) for d, k in found.items()],
                    )
                db.set_kv("bundled_keys_loaded", len(found))
                logbook.info("key", f"loaded {len(found)} keys from the extractor's table")
                break
        else:
            db.set_kv("bundled_keys_loaded", 0)
    rows = db.query("SELECT depot, key FROM depot_keys")
    return {r["depot"]: bytes.fromhex(r["key"]) for r in rows}


def load_found_keys():
    """Re-import data/found_keys.txt.

    Keys recovered by a trial are written there as well as to the database, so
    the file survives a wiped catalogue - and if you copy one in from another
    machine, this is what picks it up.
    """
    path = config.DATA_DIR / "found_keys.txt"
    if not path.exists():
        return 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return import_user_keys(text, source="trial")


USABLE_KEY = "source != 'rejected'"


def refresh_has_key():
    """Point depots.has_key at reality, not just at the extractor's table.

    The catalogue build only knows about the built-in keys, so a depot whose key
    was recovered by a trial or imported from a backup showed up as "no key" in
    the UI. The reverse matters just as much: a key that has been proved not to
    decrypt its depot must stop the card claiming the depot is ready.
    """
    with db.transaction() as conn:
        changed = conn.execute(
            "UPDATE depots SET has_key = 1 WHERE has_key = 0 AND depot IN"
            f" (SELECT depot FROM depot_keys WHERE depot >= 0 AND {USABLE_KEY})"
        ).rowcount or 0
        changed += conn.execute(
            "UPDATE depots SET has_key = 0 WHERE has_key = 1 AND depot NOT IN"
            f" (SELECT depot FROM depot_keys WHERE depot >= 0 AND {USABLE_KEY})"
        ).rowcount or 0
    return changed


def reject_key(depot: int, hexkey: str):
    """Remember that a key does not decrypt its depot.

    The key is kept rather than deleted - it is still worth trying against other
    depots - but it stops counting as this depot's key, so the card, the shelves
    and the dialog all stop claiming the depot is ready to extract.
    """
    if not hexkey:
        return False
    db.execute("UPDATE depot_keys SET source = 'rejected' WHERE depot = ? AND key = ?",
               (depot, str(hexkey).lower()))
    refresh_has_key()
    logbook.warn("key", f"depot {depot}: stored key does not decrypt this depot",
                 depot=depot, detail=str(hexkey))
    return True


def user_key_file():
    return config.DATA_DIR / "user_keys.txt"


def _json_pairs(text: str):
    """Depot->key maps as shipped by key dumps: {"441": "a1d2..."}.

    Also understands the nested shape Steam's own config.vdf uses once it has
    been converted to JSON, i.e. {"441": {"DecryptionKey": "a1d2..."}}.
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    # Unwrap one or two levels of wrapper object ({"depots": {...}}).
    for wrapper in ("depots", "Depots", "data", "keys"):
        if wrapper in data and isinstance(data[wrapper], dict):
            data = data[wrapper]
            break
    pairs = []
    for depot, value in data.items():
        if not str(depot).strip().isdigit():
            continue
        if isinstance(value, dict):
            value = (value.get("DecryptionKey") or value.get("decryptionkey")
                     or value.get("key") or "")
        if isinstance(value, str) and HEX_RE.fullmatch(value.strip()):
            pairs.append((int(depot), value.strip().lower()))
    return pairs or None


VDF_DEPOT_RE = re.compile(r'"(\d+)"\s*\{[^{}]*?"DecryptionKey"\s*"([0-9a-fA-F]{32})"',
                          re.IGNORECASE | re.DOTALL)


def _vdf_pairs(text: str):
    """Steam's config.vdf, where each depot block carries a DecryptionKey."""
    return [(int(d), k.lower()) for d, k in VDF_DEPOT_RE.findall(text)] or None


def import_user_keys(text: str, source="user"):
    """Accept a pasted or uploaded key list.

    Understands ``depot hexkey`` pairs, bare 32-character hex keys, lines copied
    straight out of a keys.cpp-style table, depot->key JSON dumps, and Steam's
    own config.vdf.
    """
    added, pairs = 0, []

    structured = _json_pairs(text) or _vdf_pairs(text)
    if structured:
        pairs.extend(structured)
        text = ""                      # a structured file needs no line scan

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "//")):
            continue
        m = KEY_RE.match(line)
        if m:
            parts = [p.strip() for p in m.group(2).split(",") if p.strip()]
            try:
                raw = bytes(int(p, 16) for p in parts)
            except ValueError:
                continue
            if len(raw) == 16:
                pairs.append((int(m.group(1)), raw.hex()))
                continue
        tokens = re.split(r"[\s,;:=]+", line)
        depot = None
        if tokens and tokens[0].isdigit():
            depot = int(tokens[0])
        hexes = HEX_RE.findall(line)
        for h in hexes:
            pairs.append((depot, h.lower()))

    seen = set()
    for depot, hexkey in pairs:
        row = (depot if depot is not None else -1, hexkey.lower(), source)
        if row[:2] in seen:
            continue
        seen.add(row[:2])
        db.execute(
            "INSERT OR REPLACE INTO depot_keys(depot, key, source) VALUES(?,?,?)", row)
        added += 1
    if added:
        refresh_has_key()
        logbook.info("key", f"imported {added} key(s) from {source}")
    return added


def candidates_for(depot: int):
    """Every key worth trying, best guess first, de-duplicated."""
    seen, out = set(), []

    def push(raw, label):
        if raw and len(raw) == 16 and raw not in seen:
            seen.add(raw)
            out.append((raw, label))

    table = bundled_keys()
    push(table.get(depot), f"bundled key for depot {depot}")

    if user_key_file().exists():
        import_user_keys(user_key_file().read_text(encoding="utf-8", errors="replace"),
                         source="keyfile")

    for row in db.query("SELECT key, source FROM depot_keys WHERE depot = ?", (depot,)):
        push(bytes.fromhex(row["key"]), f"{row['source']} key for this depot")

    push(b"\x00" * 16, "all-zero key")

    # Depots of the same game share a key surprisingly often, so nearby depots
    # come before the rest of the table.
    for row in db.query(
        "SELECT depot, key, source FROM depot_keys WHERE depot BETWEEN ? AND ? AND depot != ?",
        (depot - 24, depot + 24, depot),
    ):
        push(bytes.fromhex(row["key"]), f"key from neighbouring depot {row['depot']}")

    for row in db.query("SELECT depot, key, source FROM depot_keys WHERE depot != -1"):
        push(bytes.fromhex(row["key"]), f"key from depot {row['depot']}")
    for row in db.query("SELECT key, source FROM depot_keys WHERE depot = -1"):
        push(bytes.fromhex(row["key"]), f"{row['source']} key")

    return out


# --------------------------------------------------------------------------- #
# reading the checksum table
# --------------------------------------------------------------------------- #
def read_file_table(blob_path: Path):
    """Per-file compression mode, dat offset and block list, from a blob."""
    fields = blobmod.parse(Path(blob_path).read_bytes())
    table = fields.get(blobmod.key(4))
    if not table:
        raise KeyError_("blob has no checksum table")

    magic, version, num_blocks, num_items = struct.unpack_from("<4I", table, 0)
    if magic != 0x34457234:
        raise KeyError_("bad checksum table magic")

    entries = [struct.unpack_from("<4I", table, 0x20 + 16 * i) for i in range(num_blocks)]
    pos = 0x20 + 16 * num_blocks
    files = []
    for start, count, _off, _dummy in entries:
        for fid in range(start, start + count):
            if version == 0:
                size, offset, packed = struct.unpack_from("<3I", table, pos)
                pos += 12
            else:
                size, offset = struct.unpack_from("<2Q", table, pos)
                pos += 16
                packed, = struct.unpack_from("<I", table, pos)
                pos += 4
            mode = packed >> 24
            nblocks = packed & 0x00FFFFFF
            blocks = [struct.unpack_from("<2I", table, pos + 8 * j) for j in range(nblocks)]
            pos += 8 * nblocks
            files.append({"fileid": fid, "mode": mode, "offset": offset,
                          "size": size, "blocks": blocks})
    return files


def analyse(blob_path: Path):
    """Does this depot actually need a key?"""
    files = read_file_table(blob_path)
    modes = {}
    for f in files:
        modes[f["mode"]] = modes.get(f["mode"], 0) + 1
    encrypted = modes.get(2, 0) + modes.get(3, 0)
    return {
        "modes": modes,
        "files": len(files),
        "encrypted_files": encrypted,
        "needs_key": encrypted > 0,
        "table": files,
    }


def analyse_chain(blob_paths):
    """Same question, asked of a whole version chain.

    A depot can be plain in its newest blob and encrypted in an older one, and
    the extractor reads every blob in the chain, so looking only at the top blob
    can report "no key needed" for a depot that will stop dead halfway through.
    """
    modes, files, unreadable = {}, 0, []
    for path in blob_paths:
        try:
            info = analyse(Path(path))
        except Exception as exc:  # noqa: BLE001 - a torn blob must not hide the rest
            unreadable.append((str(path), str(exc)))
            continue
        files += info["files"]
        for mode, count in info["modes"].items():
            modes[mode] = modes.get(mode, 0) + count
    encrypted = modes.get(2, 0) + modes.get(3, 0)
    return {
        "modes": modes,
        "files": files,
        "encrypted_files": encrypted,
        "needs_key": encrypted > 0,
        "unreadable": unreadable,
        "blobs": len(blob_paths),
    }


# --------------------------------------------------------------------------- #
# the trial itself
# --------------------------------------------------------------------------- #
def _decrypt(key: bytes, data: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CFB(ZERO_IV))
    dec = cipher.decryptor()
    return dec.update(data) + dec.finalize()


def _keystream_block(key: bytes) -> bytes:
    """First CFB keystream block.

    The extractor decrypts with AES-128 CFB and an all-zero IV, so the first
    16 plaintext bytes are just ciphertext XOR AES-ECB(key, 0). That makes a
    candidate key testable with a single block encryption instead of a full
    decrypt-and-inflate, which is what makes searching large key lists cheap.
    """
    return Cipher(algorithms.AES(key), modes.ECB()).encryptor().update(ZERO_IV)


# zlib streams start 0x78 followed by one of these four compression levels.
ZLIB_FIRST = 0x78
ZLIB_SECOND = (0x01, 0x5E, 0x9C, 0xDA)


def _prefilter(probe):
    """A cheap accept/reject on the first two plaintext bytes.

    Only ~1 key in 16,000 survives it, so the expensive full check runs almost
    never. Returns None when the probe kind gives nothing to filter on.
    """
    head = probe["data"][8:10] if probe["kind"] == "zlib" else probe["data"][:2]
    if len(head) < 2:
        return None
    if probe["kind"] == "zlib":
        return (head, ZLIB_FIRST, ZLIB_SECOND)
    if probe["kind"] == "magic" and probe.get("ext") in MAGICS:
        firsts = {m[0] for m in MAGICS[probe["ext"]] if m}
        seconds = tuple({m[1] for m in MAGICS[probe["ext"]] if len(m) > 1})
        if len(firsts) == 1 and seconds:
            return (head, next(iter(firsts)), seconds)
    return None


def _survives(pre, key: bytes) -> bool:
    head, first, seconds = pre
    ks = _keystream_block(key)
    if (head[0] ^ ks[0]) != first:
        return False
    return (head[1] ^ ks[1]) in seconds


def _scan_chunk(args):
    """Worker body: return the keys in this slice that pass the prefilter."""
    pre, blob = args
    head, first, seconds = pre
    hits = []
    for i in range(0, len(blob), 16):
        key = blob[i:i + 16]
        try:
            ks = _keystream_block(key)
        except Exception:  # noqa: BLE001 - malformed key material
            continue
        if (head[0] ^ ks[0]) == first and (head[1] ^ ks[1]) in seconds:
            hits.append(key)
    return hits


def gpu_status():
    """Report whether a GPU backend is usable, and be honest about what it buys.

    Testing a key is one AES block plus two comparisons, so a key list of any
    realistic size finishes on the CPU in well under a second. A GPU only
    matters for lists in the hundreds of millions, and no hardware makes an
    exhaustive AES-128 search possible - that is 2^128 keys.
    """
    backends = []
    for mod, label in (("pyopencl", "OpenCL"), ("pycuda", "CUDA"),
                       ("numba.cuda", "Numba CUDA"), ("cupy", "CuPy")):
        try:
            __import__(mod)
            backends.append(label)
        except Exception:  # noqa: BLE001 - absence is the normal case
            continue
    return {
        "available": bool(backends),
        "backends": backends,
        "workers": max(1, (os.cpu_count() or 2)),
        "note": "Key testing is one AES block per candidate, so the CPU clears any "
                "realistic key list instantly and SteamFlix spreads big lists over "
                "every core. A GPU would only help with lists of hundreds of millions "
                "of keys. Exhaustively searching an unknown AES-128 key is 2^128 "
                "attempts - that is not possible on any hardware, so SteamFlix "
                "searches key lists rather than the key space.",
    }


def _probe_exact(probe):
    """Can this probe prove a key wrong on its own, or only hint?"""
    if probe["kind"] == "zlib":
        return True
    return probe["kind"] == "magic" and probe.get("ext") in MAGICS


def _read_probe(dat_path: Path, offset, length):
    with open(dat_path, "rb") as fh:
        fh.seek(offset)
        return fh.read(length)


def pick_probes(files, dat_path: Path, names=None, want=4):
    """Every chunk in this dat that can test a key, cheapest and surest first.

    One probe is enough to find the key; more than one is what stops a wrong key
    being reported as right. The mode 2 test is exact - the plaintext has to
    inflate to a length the chunk states up front - so a single one of those
    settles it. The mode 3 tests are weaker, and are only trusted in agreement.
    """
    try:
        dat_size = dat_path.stat().st_size
    except OSError:
        return []
    out = []

    mode2 = [f for f in files if f["mode"] == 2 and f["blocks"]
             and f["blocks"][0][0] > 8 and f["offset"] + f["blocks"][0][0] <= dat_size]
    for f in sorted(mode2, key=lambda x: x["blocks"][0][0])[:want]:
        out.append({"kind": "zlib", "data": _read_probe(dat_path, f["offset"], f["blocks"][0][0]),
                    "file": f, "dat": str(dat_path)})
    if len(out) >= want:
        return out

    mode3 = [f for f in files if f["mode"] == 3 and f["blocks"] and f["blocks"][0][0]
             and f["offset"] + f["blocks"][0][0] <= dat_size]
    typed, untyped = [], []
    for f in sorted(mode3, key=lambda x: x["blocks"][0][0]):
        name = (names or {}).get(f["fileid"], "")
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        (typed if (ext in MAGICS or ext in TEXT_EXT) else untyped).append((f, ext, name))

    for f, ext, name in typed:
        if len(out) >= want:
            break
        out.append({"kind": "magic", "data": _read_probe(dat_path, f["offset"], f["blocks"][0][0]),
                    "ext": ext, "file": f, "name": name, "dat": str(dat_path)})
    for f, _ext, _name in untyped:
        if len(out) >= want:
            break
        out.append({"kind": "entropy",
                    "data": _read_probe(dat_path, f["offset"], f["blocks"][0][0]),
                    "file": f, "dat": str(dat_path)})
    return out


def _pick_probe(files, dat_path: Path, names=None):
    """Backwards-compatible single-probe wrapper."""
    probes = pick_probes(files, dat_path, names, want=1)
    return probes[0] if probes else None


def _valid(probe, plain: bytes) -> bool:
    if probe["kind"] == "zlib":
        expected = struct.unpack_from("<I", probe["data"], 4)[0]
        if expected == 0 or expected > 0x8000:
            return False
        try:
            # decompressobj rather than decompress: the extractor's zlib stops at
            # the end of the stream and ignores whatever padding follows, and a
            # strict decompress would reject a correct key over those spare bytes.
            out = zlib.decompressobj().decompress(plain, expected + 1)
        except zlib.error:
            return False
        return len(out) == expected

    if probe["kind"] == "magic":
        ext = probe["ext"]
        if ext in MAGICS:
            return any(plain.startswith(m) for m in MAGICS[ext])
        printable = sum(1 for b in plain[:512] if 9 <= b <= 13 or 32 <= b < 127)
        return printable / max(1, len(plain[:512])) > 0.92

    # Nothing better to go on: real plaintext is far less uniform than AES output.
    sample = plain[:1024]
    if not sample:
        return False
    hist = {}
    for b in sample:
        hist[b] = hist.get(b, 0) + 1
    return max(hist.values()) / len(sample) > 0.08


def _confirm(probes, raw: bytes):
    """Does this key decrypt every probe we have?

    An exact probe (mode 2, or mode 3 with a known file magic) settles it on its
    own. With only weak probes to go on, at least two have to agree before the
    key is believed, because the entropy test alone accepts roughly one wrong
    key in a hundred.
    """
    exact_hits = weak_hits = 0
    for probe in probes:
        payload = probe["data"][8:] if probe["kind"] == "zlib" else probe["data"]
        try:
            ok = _valid(probe, _decrypt(raw, payload))
        except Exception:  # noqa: BLE001 - a bad key can throw anywhere
            return None
        if not ok:
            return None
        if _probe_exact(probe):
            exact_hits += 1
        else:
            weak_hits += 1
    if exact_hits:
        return "exact"
    if weak_hits >= 2:
        return "likely"
    return "weak" if weak_hits else None


def collect_probes(pairs, names_by_blob=None, want=4):
    """Test chunks drawn from every downloaded (blob, dat) pair in the chain."""
    probes = []
    for blob_path, dat_path in pairs:
        if not (blob_path and dat_path):
            continue
        blob_path, dat_path = Path(blob_path), Path(dat_path)
        if not (blob_path.exists() and dat_path.exists()):
            continue
        try:
            table = read_file_table(blob_path)
        except Exception:  # noqa: BLE001 - skip a blob we cannot parse
            continue
        names = (names_by_blob or {}).get(str(blob_path)) or {}
        probes.extend(pick_probes(table, dat_path, names, want=want))
        if sum(1 for p in probes if _probe_exact(p)) >= 2:
            break
    # Exact probes first, then the smallest chunks: cheapest confirmation wins.
    probes.sort(key=lambda p: (not _probe_exact(p), len(p["data"])))
    return probes[:max(want, 3)]


def trial(depot: int, blob_path: Path, dat_path: Path, names=None, progress=None,
          extra_keys=(), pairs=None, names_by_blob=None):
    """Find a working key for an encrypted depot.

    Returns a dict describing the outcome. ``key`` is None when the depot needs
    no key at all (``needs_key`` False) or when every candidate failed.

    ``pairs`` is the whole chain as [(blob, dat), ...]; passing it lets the trial
    draw test chunks from any version that is already on disk, which matters for
    depots whose newest blob happens to hold nothing encrypted.
    """
    if pairs is None:
        pairs = [(blob_path, dat_path)]
    if names is not None and blob_path is not None:
        names_by_blob = dict(names_by_blob or {})
        names_by_blob.setdefault(str(Path(blob_path)), names)

    blobs = [b for b, _d in pairs if b and Path(b).exists()]
    info = analyse_chain(blobs) if blobs else analyse(Path(blob_path))
    if not info["needs_key"]:
        return {
            "needs_key": False,
            "key": None,
            "reason": "nothing in this depot is encrypted - the extractor only wants a "
                      "key because it always asks for one",
            "modes": info["modes"],
            "tried": 0,
        }

    probes = collect_probes(pairs, names_by_blob)
    if not probes:
        return {"needs_key": True, "key": None, "tried": 0, "modes": info["modes"],
                "reason": "no chunk in the downloaded dats can be used to test a key - "
                          "download at least one version that contains encrypted files"}

    primary = probes[0]
    payload = primary["data"][8:] if primary["kind"] == "zlib" else primary["data"]
    exact_probes = sum(1 for p in probes if _probe_exact(p))

    cands = []
    for raw in extra_keys:
        if isinstance(raw, str):
            try:
                raw = bytes.fromhex(raw.strip())
            except ValueError:
                continue
        if len(raw) == 16:
            cands.append((raw, "key you supplied"))
    cands.extend(candidates_for(depot))
    total = len(cands)
    if not total:
        return {"needs_key": True, "key": None, "tried": 0, "total": 0,
                "modes": info["modes"],
                "reason": "SteamFlix has no candidate keys at all - import a key list first"}

    probe_summary = [{"kind": p["kind"], "exact": _probe_exact(p),
                      "bytes": len(p["data"]), "ext": p.get("ext")} for p in probes]

    def hit(raw, label, tried, strength, workers=None):
        remember(depot, raw, source="trial")
        out = {
            "needs_key": True, "key": raw.hex(), "label": label,
            "tried": tried, "total": total, "modes": info["modes"],
            "probe": primary["kind"], "probes": probe_summary,
            "confidence": strength,
            "reason": f"found a working key after {tried} attempt(s) ({label})"
                      + ("" if strength == "exact" else
                         " - confirmed against file magic only, so verify the extraction"),
        }
        if workers:
            out["workers"] = workers
        return out

    def miss(tried, workers=None):
        out = {
            "needs_key": True, "key": None, "tried": tried, "total": total,
            "modes": info["modes"], "probe": primary["kind"], "probes": probe_summary,
            "reason": f"none of the {total} known keys decrypt this depot. Import more "
                      f"keys, or paste one in the depot dialog.",
        }
        if workers:
            out["workers"] = workers
        return out

    pre = _prefilter(primary)
    workers = max(1, (os.cpu_count() or 2) - 1)

    # Big lists get the single-AES-block prefilter spread over every core; only
    # the handful of survivors pay for a full decrypt and inflate.
    if pre and total > 20000 and workers > 1:
        labels = {raw: label for raw, label in cands}
        blob = b"".join(raw for raw, _ in cands)
        span = max(16, ((len(blob) // 16) // (workers * 4) + 1) * 16)
        slices = [(pre, blob[i:i + span]) for i in range(0, len(blob), span)]
        done = 0
        try:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                for hits in pool.map(_scan_chunk, slices):
                    done += span // 16
                    if progress:
                        progress(min(done, total), total)
                    for raw in hits:
                        strength = _confirm(probes, raw)
                        if strength:
                            return hit(raw, labels.get(raw, "recovered key"),
                                       total, strength, workers)
        except Exception:  # noqa: BLE001 - fall back to the serial path
            pass
        else:
            return miss(total, workers)

    for i, (raw, label) in enumerate(cands):
        if progress and i % 500 == 0:
            progress(i, total)
        if pre and not _survives(pre, raw):
            continue
        strength = _confirm(probes, raw)
        if strength:
            return hit(raw, label, i + 1, strength)

    result = miss(total)
    result["exact_probes"] = exact_probes
    return result


def remember(depot: int, raw: bytes, source="trial"):
    # INSERT OR REPLACE, so a key previously marked rejected for this depot is
    # restored the moment it is proved to work after all.
    db.execute("INSERT OR REPLACE INTO depot_keys(depot, key, source) VALUES(?,?,?)",
               (depot, raw.hex(), source))
    # Without this the card and the modal keep saying "no key" for a depot we
    # have just cracked.
    db.execute("UPDATE depots SET has_key = 1 WHERE depot = ?", (depot,))
    found = config.DATA_DIR / "found_keys.txt"
    line = f"{depot} {raw.hex()}\n"
    try:
        if not found.exists() or line not in found.read_text(encoding="utf-8"):
            with open(found, "a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError:
        pass
    logbook.info("key", f"depot {depot}: key recovered and saved", depot=depot,
                 detail=raw.hex())


def known_key(depot: int):
    row = known_key_row(depot)
    return row["key"] if row else None


def known_key_row(depot: int):
    """Best key on record for a depot, and where it came from.

    A key found by a trial has been proved against real ciphertext. One that was
    typed in or imported has not, and a wrong one there is worse than none: it
    would otherwise stop the trial from ever running.
    """
    return db.one("SELECT key, source FROM depot_keys WHERE depot = ?"
                  " AND source != 'rejected' ORDER BY "
                  "CASE source WHEN 'trial' THEN 0 WHEN 'bundled' THEN 1 "
                  "WHEN 'user' THEN 2 WHEN 'keyfile' THEN 3 ELSE 4 END LIMIT 1", (depot,))


def verify_key(hexkey, pairs, names_by_blob=None):
    """Check a key against real encrypted bytes.

    Returns "exact" / "likely" / "weak" when the key decrypts every test chunk,
    None when it does not, and "unknown" when there is nothing to test it
    against (no encrypted chunk downloaded yet).
    """
    if not hexkey:
        return None
    try:
        raw = bytes.fromhex(str(hexkey).strip())
    except ValueError:
        return None
    if len(raw) != 16:
        return None
    probes = collect_probes(pairs, names_by_blob)
    if not probes:
        return "unknown"
    return _confirm(probes, raw)
