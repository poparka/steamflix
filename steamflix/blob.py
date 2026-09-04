"""Pure-python reader for Steam2 .blob metadata files.

Mirrors the layout used by the reference C++ extractor (include/blobng.hpp and
include/steam2ng.hpp): a blob is a flat key/value store whose keys are 4-byte
little-endian integers, optionally wrapped in a zlib container. The fields the
downloader cares about are:

    0  -> container format code (3 = 32-bit dat sizes, 4 = 64-bit)
    3  -> compressed blob holding the file manifest
    4  -> checksum/file-id table
    10 -> CRC of this blob (matches the third filename component)
    12 -> CRC of the previous version's blob (0 at version 0)
    13 -> size in bytes of the .dat that pairs with this blob
"""
import struct
import zlib

BLOB_MAGIC = 0x5001
COMPRESSED_MAGIC = 0x4301
# The manifest header is fourteen 32-bit fields, not thirteen. Reading it one
# field short put the node table four bytes out, which still parsed - the counts
# in the header are read correctly either way - but every name offset landed in
# the middle of the previous string. That is why file names used to come back
# empty, and why the key trial had no extensions to check mode-3 chunks against.
MANIFEST_HEADER = struct.Struct("<14I")

# name offset, size, file id, flags, parent, next sibling, first child
DIR_NODE = struct.Struct("<7I")
DIR_NONE = 0xFFFFFFFF


class BlobError(Exception):
    pass


def key(n: int) -> bytes:
    return struct.pack("<I", n)


def parse(data: bytes) -> dict:
    """Decode a (possibly zlib-wrapped) blob into a {key_bytes: value_bytes} dict."""
    if len(data) < 10:
        raise BlobError("blob too small")
    if data[:2] == b"\x01\x43":
        if len(data) < 20:
            raise BlobError("truncated compressed blob header")
        data = zlib.decompress(data[20:])
    magic, total, slack = struct.unpack_from("<HII", data, 0)
    if magic != BLOB_MAGIC:
        raise BlobError(f"bad blob magic 0x{magic:04x}")
    end = min(total - slack, len(data))
    out = {}
    pos = 10
    while pos + 6 <= end:
        ksize, vsize = struct.unpack_from("<HI", data, pos)
        pos += 6
        k = data[pos:pos + ksize]
        pos += ksize
        out[k] = data[pos:pos + vsize]
        pos += vsize
    return out


def u32(value: bytes) -> int:
    return struct.unpack("<I", value)[0]


def field_u32(fields: dict, n: int, default=None):
    v = fields.get(key(n))
    return u32(v) if v is not None and len(v) == 4 else default


def dat_size(fields: dict):
    """Size of the .dat paired with this blob, used to pick the right file
    when a depot reset left several dats sharing one version number."""
    raw = fields.get(key(13))
    if raw is None:
        return None
    if len(raw) == 4:
        return struct.unpack("<I", raw)[0]
    if len(raw) == 8:
        return struct.unpack("<Q", raw)[0]
    return None


def prev_crc(fields: dict):
    """CRC of the parent blob, formatted the way it appears in filenames."""
    v = field_u32(fields, 12)
    return None if v is None else f"{v:08x}"


def own_crc(fields: dict):
    v = field_u32(fields, 10)
    return None if v is None else f"{v:08x}"


def read_manifest(fields: dict):
    """Return summary information from the manifest embedded in a blob."""
    raw = fields.get(key(3))
    if raw is None:
        return None
    try:
        inner = parse(raw)
    except Exception as exc:            # noqa: BLE001 - surfaced to the UI as a warning
        raise BlobError(f"manifest container unreadable: {exc}") from exc
    manifest = inner.get(key(0))
    if not manifest:
        raise BlobError("manifest payload missing")
    return summarize_manifest(manifest)


def _manifest_nodes(manifest: bytes):
    """(header, nodes, name lookup) for a manifest payload.

    One place that knows the layout, so summarising, listing and naming can
    never drift apart again.
    """
    hdr = MANIFEST_HEADER.unpack_from(manifest, 0)
    version, num_nodes, string_table_size = hdr[0], hdr[3], hdr[7]
    if version not in (3, 4):
        raise BlobError(f"unsupported manifest version {version}")

    node_base = MANIFEST_HEADER.size
    strings_at = node_base + DIR_NODE.size * num_nodes
    strings = manifest[strings_at:strings_at + string_table_size]

    def name_at(offset: int) -> str:
        if offset >= len(strings):
            return ""
        end = strings.find(b"\x00", offset)
        chunk = strings[offset:] if end < 0 else strings[offset:end]
        return chunk.decode("latin-1", "replace")

    nodes = [DIR_NODE.unpack_from(manifest, node_base + DIR_NODE.size * i)
             for i in range(num_nodes)]
    return hdr, nodes, name_at


def _full_path(nodes, name_at, index: int) -> str:
    """A node's path, built by walking its parents back to the root."""
    parts = []
    seen = set()
    while index != DIR_NONE and index < len(nodes) and index not in seen:
        seen.add(index)
        name = name_at(nodes[index][0])
        if name:
            parts.append(name)
        parent = nodes[index][4]
        if parent == index:
            break
        index = parent
    return "/".join(reversed(parts))


def summarize_manifest(manifest: bytes) -> dict:
    hdr, nodes, name_at = _manifest_nodes(manifest)
    version, appid, verid, num_nodes, num_files, blk = hdr[0], hdr[1], hdr[2], hdr[3], hdr[4], hdr[5]

    total_bytes = 0
    file_count = 0
    root_dirs = []
    for i, (name_off, size, _fid, flags, parent, _sib, _child) in enumerate(nodes):
        if flags:
            file_count += 1
            total_bytes += size
        elif parent == 0 and i != 0 and len(root_dirs) < 12:
            name = name_at(name_off)
            if name:
                root_dirs.append(name)
    return {
        "manifest_version": version,
        "appid": appid,
        "verid": verid,
        "node_count": num_nodes,
        "file_count": file_count or num_files,
        "total_bytes": total_bytes,
        "block_size": blk,
        "root_dirs": root_dirs,
    }


def manifest_entries(data: bytes) -> dict:
    """{relative path: size} for every file the depot's manifest lists.

    This is what an extracted folder is checked against, so it has to be the
    full path rather than the leaf name.
    """
    fields = parse(data)
    raw = fields.get(key(3))
    if raw is None:
        return {}
    manifest = parse(raw).get(key(0))
    if not manifest:
        return {}
    _hdr, nodes, name_at = _manifest_nodes(manifest)
    out = {}
    for i, node in enumerate(nodes):
        if not node[3]:                     # directories carry no flags
            continue
        path = _full_path(nodes, name_at, i)
        if path:
            out[path] = node[1]
    return out


def manifest_names(data: bytes) -> dict:
    """{file id: file name} from a blob's manifest, used to sanity-check a
    decrypted chunk against the magic bytes its extension implies."""
    fields = parse(data)
    raw = fields.get(key(3))
    if raw is None:
        return {}
    manifest = parse(raw).get(key(0))
    if not manifest:
        return {}
    _hdr, nodes, name_at = _manifest_nodes(manifest)
    return {node[2]: name_at(node[0]) for node in nodes if node[3]}


def describe(data: bytes) -> dict:
    """Everything the app wants to know about one blob file."""
    fields = parse(data)
    info = {
        "format_code": field_u32(fields, 0),
        "crc": own_crc(fields),
        "prev_crc": prev_crc(fields),
        "dat_size": dat_size(fields),
        "manifest": None,
        "manifest_error": None,
    }
    try:
        info["manifest"] = read_manifest(fields)
    except BlobError as exc:
        info["manifest_error"] = str(exc)
    return info
