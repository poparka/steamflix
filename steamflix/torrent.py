"""A small BitTorrent client, used only as a last resort.

SteamFlix normally pulls blobs and dats over HTTP from the Steam2 mirrors. When
every mirror is down the same files are still reachable through the public
steam2 torrent, so this module fetches them from the swarm instead.

It is deliberately not a general torrent client. The torrent is 12 TiB across
116,346 files and SteamFlix only ever wants a handful of them, so the whole
design is built around one job:

    given "blobs/441_0_7f92e6ea_....blob", produce that one file

Which means: no piece picking strategy, no DHT, no resume files. Just work out
which byte range of the torrent the wanted file occupies, ask peers for exactly
the 16 KiB blocks covering it, and stop.

Correctness does not suffer from skipping piece hashes on the way in. Every blob
and dat carries its own SHA-256 in its filename, so a finished file is verified
against a stronger hash than the torrent's own SHA-1 pieces would give.

Giving back is the other half, and it lives in seed.py: which pieces this
machine holds in full, and a listening socket to serve them from. This module
takes part in that too. A peer we dialled to download from can ask us for
pieces over the very same socket, which is the one route out of a household NAT
that needs no router setup at all, so ``seed`` registers its piece store here
and every connection we open advertises what we have.
"""
import hashlib
import os
import random
import socket
import struct
import threading
import time
from urllib.parse import urlencode, urlparse

from . import config, db, logbook

BLOCK = 1 << 14                     # 16 KiB: the request size every client accepts
HANDSHAKE_PSTR = b"BitTorrent protocol"
PEER_ID = b"-SF0001-" + bytes(random.getrandbits(8) for _ in range(12))

CONNECT_TIMEOUT = 6
PEER_TIMEOUT = 12
TRACKER_TIMEOUT = 8


class TorrentError(Exception):
    pass


# seed.py drops its verified piece store here when seeding starts. Every peer
# we dial then advertises what we hold and answers requests for it, so uploads
# happen even when nothing outside can reach us.
_upload_source = None
_listen_port = 6881
_traffic = {"downloaded": 0, "uploaded": 0}
_traffic_lock = threading.Lock()


def set_upload_source(store, port=None):
    """Let downloads serve pieces back. ``store`` is None to stop."""
    global _upload_source, _listen_port
    _upload_source = store
    if port:
        _listen_port = port


def upload_source():
    return _upload_source


def listen_port() -> int:
    """The port trackers should hand out for us, so peers can connect back."""
    return _listen_port


def note_downloaded(n):
    with _traffic_lock:
        _traffic["downloaded"] += n


def note_uploaded(n):
    with _traffic_lock:
        _traffic["uploaded"] += n


def traffic():
    """Bytes moved since this run started, both ways."""
    with _traffic_lock:
        return dict(_traffic)


# --------------------------------------------------------------------------- #
# bencode
# --------------------------------------------------------------------------- #
def bdecode(data: bytes, i: int = 0):
    """Decode one bencoded value, returning (value, index after it)."""
    c = data[i:i + 1]
    if c == b"i":
        j = data.index(b"e", i)
        return int(data[i + 1:j]), j + 1
    if c == b"l":
        i += 1
        out = []
        while data[i:i + 1] != b"e":
            value, i = bdecode(data, i)
            out.append(value)
        return out, i + 1
    if c == b"d":
        i += 1
        out = {}
        while data[i:i + 1] != b"e":
            key, i = bdecode(data, i)
            value, i = bdecode(data, i)
            out[key] = value
        return out, i + 1
    if not c.isdigit():
        raise TorrentError(f"bad bencode at byte {i}")
    j = data.index(b":", i)
    n = int(data[i:j])
    return data[j + 1:j + 1 + n], j + 1 + n


# --------------------------------------------------------------------------- #
# metainfo
# --------------------------------------------------------------------------- #
class Meta:
    """Everything we need out of the .torrent, and nothing we do not."""

    def __init__(self, path):
        raw = open(path, "rb").read()
        meta, _ = bdecode(raw)
        info = meta[b"info"]

        # The info hash has to be the SHA-1 of the original bytes, so it is
        # sliced straight out of the file rather than re-encoded - re-encoding
        # a dict can reorder keys and silently produce the wrong hash.
        start = raw.find(b"4:infod")
        if start < 0:
            raise TorrentError("no info dictionary in this torrent")
        start += len(b"4:info")
        _, end = bdecode(raw, start)
        self.info_hash = hashlib.sha1(raw[start:end]).digest()

        self.name = info[b"name"].decode("utf-8", "replace")
        self.piece_length = info[b"piece length"]
        # Kept, not discarded: seeding may only advertise a piece whose SHA-1
        # actually matches, and this is where that hash comes from.
        self.pieces = info[b"pieces"]
        self.piece_count = len(self.pieces) // 20
        self.total = 0
        self.files = []                       # (path, offset, length)
        offset = 0
        for entry in info.get(b"files") or []:
            parts = [p.decode("utf-8", "replace") for p in entry[b"path"]]
            length = entry[b"length"]
            self.files.append(("/".join(parts), offset, length))
            offset += length
        if not self.files:                    # single-file torrent
            self.files.append((self.name, 0, info[b"length"]))
            offset = info[b"length"]
        self.total = offset

        self.trackers = []
        for tier in meta.get(b"announce-list") or []:
            for url in tier:
                self.trackers.append(url.decode("utf-8", "replace"))
        if meta.get(b"announce"):
            self.trackers.insert(0, meta[b"announce"].decode("utf-8", "replace"))
        # Some public lists carry a typo'd scheme ("dp://"), which would only
        # waste a connection attempt.
        self.trackers = [t for t in dict.fromkeys(self.trackers)
                         if t.startswith(("udp://", "http://", "https://"))]

    def piece_hash(self, index: int) -> bytes:
        return self.pieces[index * 20:index * 20 + 20]

    def piece_size(self, index: int) -> int:
        """The last piece is short; every other one is a full piece length."""
        start = index * self.piece_length
        return max(0, min(self.piece_length, self.total - start))


_meta = None
_meta_lock = threading.Lock()


def available() -> bool:
    return config.TORRENT_PATH.exists()


def meta() -> Meta:
    """Parsed torrent, read once per run."""
    global _meta
    with _meta_lock:
        if _meta is None:
            if not available():
                raise TorrentError(f"{config.TORRENT_PATH} is not here")
            _meta = Meta(config.TORRENT_PATH)
        return _meta


def build_index(force=False):
    """Copy the file table into SQLite so lookups do not re-parse 30 MB.

    116,346 rows go in once and are then read by exact path, which keeps both
    startup and every later lookup effectively free.
    """
    db.init()
    if not force and db.get_kv("torrent_index"):
        return int(db.get_kv("torrent_files") or 0)
    m = meta()
    with db.transaction() as conn:
        conn.execute("DELETE FROM torrent_files")
        conn.executemany(
            "INSERT OR REPLACE INTO torrent_files(path, offset, length) VALUES(?,?,?)",
            m.files)
    db.set_kv("torrent_index", m.info_hash.hex())
    db.set_kv("torrent_files", len(m.files))
    logbook.info("torrent", f"indexed {len(m.files):,} files from the torrent")
    return len(m.files)


def locate(path: str):
    """(offset, length) of one file inside the torrent, or None."""
    build_index()
    row = db.one("SELECT offset, length FROM torrent_files WHERE path = ?", (path,))
    return (row["offset"], row["length"]) if row else None


def find_file(name: str, kind: str):
    """Map a mirror path onto the torrent's own layout.

    The torrent stores exactly what the mirrors serve, under ``blobs/`` and
    ``dats/``, so the mapping is a straight prefix.
    """
    candidates = [f"{kind}s/{name}", name]
    for cand in candidates:
        hit = locate(cand)
        if hit:
            return cand, hit
    return None, None


# --------------------------------------------------------------------------- #
# trackers
# --------------------------------------------------------------------------- #
def _udp_announce(url, info_hash, left, want=200, port=6881, uploaded=0,
                  downloaded=0, event=2):
    """BEP 15 announce. Two round trips: connect, then announce.

    Returns (peers, interval). ``port`` has to be the port we really listen on:
    it is the address the tracker hands to everyone else, and getting it wrong
    is the difference between being seen as a seeder and being invisible.
    """
    parsed = urlparse(url)
    addr = (parsed.hostname, parsed.port or 80)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(TRACKER_TIMEOUT)
    try:
        txn = random.getrandbits(31)
        sock.sendto(struct.pack(">QII", 0x41727101980, 0, txn), addr)
        data, _ = sock.recvfrom(2048)
        if len(data) < 16:
            raise TorrentError("short connect reply")
        action, rtxn, conn_id = struct.unpack(">IIQ", data[:16])
        if action != 0 or rtxn != txn:
            raise TorrentError("bad connect reply")

        txn = random.getrandbits(31)
        # Thirteen fields, in BEP 15 order. There used to be fourteen arguments
        # against a thirteen-field format, so struct.pack raised on every UDP
        # tracker and peers() quietly wrote the whole UDP half of the tracker
        # list off as "down" - most of the swarm, in other words.
        req = struct.pack(">QII20s20sQQQIIIiH", conn_id, 1, txn, info_hash, PEER_ID,
                          downloaded, left, uploaded, event, 0,
                          random.getrandbits(31), want, port)
        sock.sendto(req, addr)
        data, _ = sock.recvfrom(8192)
        if len(data) < 20:
            raise TorrentError("short announce reply")
        action, rtxn = struct.unpack(">II", data[:8])
        if action != 1 or rtxn != txn:
            raise TorrentError("bad announce reply")
        interval, = struct.unpack(">I", data[8:12])
        return _peers_from_compact(data[20:]), interval
    finally:
        sock.close()


def _http_announce(url, info_hash, left, want=200, port=6881, uploaded=0,
                   downloaded=0, event="started"):
    import requests
    fields = {
        "info_hash": info_hash, "peer_id": PEER_ID, "port": port,
        "uploaded": uploaded, "downloaded": downloaded, "left": left,
        "compact": 1, "numwant": want,
    }
    if event:                       # a re-announce carries no event at all
        fields["event"] = event
    sep = "&" if "?" in url else "?"
    r = requests.get(url + sep + urlencode(fields), timeout=TRACKER_TIMEOUT)
    if r.status_code >= 400:
        raise TorrentError(f"tracker said {r.status_code}")
    body, _ = bdecode(r.content)
    if body.get(b"failure reason"):
        raise TorrentError(body[b"failure reason"].decode("utf-8", "replace"))
    interval = int(body.get(b"interval") or 1800)
    found = body.get(b"peers")
    if isinstance(found, bytes):
        return _peers_from_compact(found), interval
    return [(p[b"ip"].decode(), p[b"port"]) for p in found or []], interval


def _peers_from_compact(blob: bytes):
    out = []
    for i in range(0, len(blob) - 5, 6):
        ip = ".".join(str(b) for b in blob[i:i + 4])
        port, = struct.unpack(">H", blob[i + 4:i + 6])
        if port:
            out.append((ip, port))
    return out


EVENTS = {0: "", 1: "completed", 2: "started", 3: "stopped"}


def announce(url, port=None, left=None, uploaded=0, downloaded=0, event=2,
             want=200):
    """One announce to one tracker, either protocol. Returns (peers, interval).

    Seeding lives or dies on this call: ``left`` is what tells the swarm how
    much of the torrent we already hold, and ``port`` is where they reach us.
    """
    m = meta()
    port = listen_port() if port is None else port
    if left is None:
        # What we are still missing, so a seeder is not announced as a leecher.
        held = getattr(_upload_source, "bytes_have", 0) if _upload_source else 0
        left = max(0, m.total - held)
    else:
        left = max(0, left)
    if url.startswith("udp://"):
        return _udp_announce(url, m.info_hash, left, want, port, uploaded,
                             downloaded, event)
    return _http_announce(url, m.info_hash, left, want, port, uploaded,
                          downloaded, EVENTS.get(event, ""))


def peers(limit=200, deadline=25, port=None, left=None):
    """Ask trackers for peers until we have enough or run out of time."""
    m = meta()
    found, seen = [], set()
    stop = time.time() + deadline
    for url in m.trackers:
        if len(found) >= limit or time.time() > stop:
            break
        try:
            got, _interval = announce(url, port=port, left=left)
        except Exception:  # noqa: BLE001 - a dead tracker is the normal case
            continue
        for peer in got:
            if peer not in seen:
                seen.add(peer)
                found.append(peer)
    return found


# --------------------------------------------------------------------------- #
# peer wire protocol
# --------------------------------------------------------------------------- #
class Peer:
    """One peer connection, driven synchronously by a single worker thread."""

    def __init__(self, addr, info_hash, piece_count, source=None):
        self.addr = addr
        self.info_hash = info_hash
        self.piece_count = piece_count
        self.sock = None
        self.choked = True
        self.has = bytearray(piece_count)      # 1 byte per piece: simple and fast
        self.buf = b""
        # Whatever we can serve back over this same socket, if seeding is on.
        self.source = source if source is not None else upload_source()
        self.uploaded = 0

    def connect(self):
        self.sock = socket.create_connection(self.addr, CONNECT_TIMEOUT)
        self.sock.settimeout(PEER_TIMEOUT)
        shake = (bytes([len(HANDSHAKE_PSTR)]) + HANDSHAKE_PSTR + b"\x00" * 8
                 + self.info_hash + PEER_ID)
        self.sock.sendall(shake)
        reply = self._read_exact(68)
        if reply[1:20] != HANDSHAKE_PSTR or reply[28:48] != self.info_hash:
            raise TorrentError("peer speaks something else")
        # The bitfield has to come first if it comes at all, so this is the one
        # place it can go. Telling the peer what we hold is what turns a
        # download connection into an upload one as well.
        field = self.source.bitfield() if self.source else None
        if field:
            self._send(5, field)
            self._send(1, b"")                 # unchoke: ask us for any of it
        self._send(2, b"")                     # interested
        return self

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass

    def _read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self.buf)))
            if not chunk:
                raise TorrentError("peer hung up")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send(self, msg_id, payload):
        self.sock.sendall(struct.pack(">IB", len(payload) + 1, msg_id) + payload)

    def message(self):
        """Next message as (id, payload). id is None for keep-alives."""
        length, = struct.unpack(">I", self._read_exact(4))
        if length == 0:
            return None, b""
        body = self._read_exact(length)
        return body[0], body[1:]

    def pump(self, until=None):
        """Read messages until a condition is met or the peer goes quiet."""
        deadline = time.time() + PEER_TIMEOUT
        while time.time() < deadline:
            msg_id, payload = self.message()
            if msg_id == 0:
                self.choked = True
            elif msg_id == 1:
                self.choked = False
            elif msg_id == 4 and len(payload) >= 4:
                index, = struct.unpack(">I", payload[:4])
                if index < self.piece_count:
                    self.has[index] = 1
            elif msg_id == 5:
                for index in range(min(self.piece_count, len(payload) * 8)):
                    if payload[index // 8] >> (7 - index % 8) & 1:
                        self.has[index] = 1
            elif msg_id == 7:
                index, begin = struct.unpack(">II", payload[:8])
                return ("block", index, begin, payload[8:])
            elif msg_id == 2 and self.source is not None:
                self._send(1, b"")             # they are interested: unchoke
            elif msg_id == 6 and self.source is not None:
                self._serve(payload)
            if until and until(self):
                return ("ready", None, None, None)
        raise TorrentError("peer went quiet")

    def _serve(self, payload):
        """Answer a request from a peer we dialled ourselves.

        Failing to serve must never cost us the download, so any trouble here
        just retires the upload half of this connection.
        """
        if len(payload) < 12:
            return
        index, begin, length = struct.unpack(">III", payload[:12])
        try:
            data = self.source.read_block(index, begin, length)
            if not data:
                return
            self._send(7, struct.pack(">II", index, begin) + data)
            self.uploaded += len(data)
            note_uploaded(len(data))
            self.source.note_uploaded(len(data))
        except Exception:  # noqa: BLE001
            self.source = None

    def wait_unchoke(self):
        if not self.choked:
            return True
        kind, *_ = self.pump(until=lambda p: not p.choked)
        return kind == "ready" and not self.choked

    def fetch(self, index, begin, length):
        """Ask for one block and wait for it, ignoring blocks we did not ask for."""
        self._send(6, struct.pack(">III", index, begin, length))
        deadline = time.time() + PEER_TIMEOUT
        while time.time() < deadline:
            kind, got_index, got_begin, data = self.pump()
            if kind == "block" and got_index == index and got_begin == begin:
                return data
        raise TorrentError("block never arrived")


# --------------------------------------------------------------------------- #
# the download itself
# --------------------------------------------------------------------------- #
def _blocks_for(offset, length, piece_length):
    """The (piece, begin, size) blocks covering one file's byte range."""
    out = []
    end = offset + length
    pos = offset
    while pos < end:
        index = pos // piece_length
        piece_start = index * piece_length
        begin = pos - piece_start
        size = min(BLOCK - (begin % BLOCK), end - pos, piece_length - begin)
        out.append((index, begin, size))
        pos += size
    return out


class Download:
    """State for one file fetched out of the swarm."""

    def __init__(self, path, offset, length, expect_sha256=None, progress=None,
                 should_stop=None):
        self.path = path
        self.offset = offset
        self.length = length
        self.expect = expect_sha256
        self.progress = progress
        self.should_stop = should_stop or (lambda: False)
        self.piece_length = meta().piece_length
        self.todo = _blocks_for(offset, length, self.piece_length)
        self.data = bytearray(length)
        self.done = bytearray(len(self.todo))
        self.lock = threading.Lock()
        self.bytes_done = 0
        self.peers_ok = 0

    def next_block(self, peer):
        """Hand out the next block this peer can actually serve."""
        with self.lock:
            for i, (index, begin, size) in enumerate(self.todo):
                if self.done[i] or not peer.has[index]:
                    continue
                self.done[i] = 2                  # 2 = handed out, 1 = finished
                return i, index, begin, size
        return None

    def give_back(self, i):
        with self.lock:
            if self.done[i] == 2:
                self.done[i] = 0

    def store(self, i, index, begin, data):
        with self.lock:
            start = index * self.piece_length + begin - self.offset
            self.data[start:start + len(data)] = data
            self.done[i] = 1
            self.bytes_done += len(data)
        note_downloaded(len(data))
        if self.progress:
            self.progress(self.bytes_done, self.length)

    @property
    def finished(self):
        with self.lock:
            return all(state == 1 for state in self.done)

    def verify(self):
        if not self.expect:
            return True
        return hashlib.sha256(self.data).hexdigest() == self.expect.lower()


def _worker(addr, job: Download, info_hash, piece_count):
    peer = Peer(addr, info_hash, piece_count)
    try:
        peer.connect()
        peer.pump(until=lambda p: any(p.has))     # wait for a bitfield
        if not peer.wait_unchoke():
            return
        with job.lock:
            job.peers_ok += 1
        while not job.finished and not job.should_stop():
            task = job.next_block(peer)
            if task is None:
                return                            # nothing this peer can give us
            i, index, begin, size = task
            try:
                data = peer.fetch(index, begin, size)
            except Exception:  # noqa: BLE001 - hand the block to another peer
                job.give_back(i)
                return
            job.store(i, index, begin, data)
    except Exception:  # noqa: BLE001 - a peer failing is completely routine
        return
    finally:
        peer.close()


def fetch(name: str, kind: str, out_path, expect_sha256=None, progress=None,
          should_stop=None, swarm=None, workers=12, deadline=900):
    """Pull one blob or dat out of the swarm and write it to ``out_path``.

    Returns the number of bytes written. Raises TorrentError if the swarm could
    not produce the file - the caller is expected to have tried the mirrors
    first, so this really is the end of the line.
    """
    if not available():
        raise TorrentError("no steam2.torrent alongside SteamFlix")

    torrent_path, where = find_file(name, kind)
    if where is None:
        raise TorrentError(f"{name} is not in the torrent")
    offset, length = where

    if expect_sha256 is None:
        # Every mirror filename ends in the file's own SHA-256, which is a
        # better check than the torrent's SHA-1 pieces.
        stem = name.rsplit(".", 1)[0]
        tail = stem.rsplit("_", 1)[-1]
        if len(tail) == 64 and all(c in "0123456789abcdef" for c in tail.lower()):
            expect_sha256 = tail.lower()

    job = Download(torrent_path, offset, length, expect_sha256, progress, should_stop)
    m = meta()
    found = swarm if swarm is not None else peers()
    if not found:
        raise TorrentError("no peers answered - the swarm may be empty right now")

    logbook.info("torrent", f"{name}: asking {len(found)} peer(s) for "
                            f"{length:,} bytes")
    random.shuffle(found)
    index = 0
    stop = time.time() + deadline
    while not job.finished and index < len(found) and time.time() < stop:
        if job.should_stop():
            raise TorrentError("cancelled")
        batch = found[index:index + workers]
        index += workers
        threads = [threading.Thread(target=_worker, args=(addr, job, m.info_hash,
                                                          m.piece_count), daemon=True)
                   for addr in batch]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=max(1, stop - time.time()))

    if not job.finished:
        raise TorrentError(f"the swarm only produced {job.bytes_done:,} of "
                           f"{length:,} bytes")
    if not job.verify():
        raise TorrentError("the file arrived but its SHA-256 does not match")

    out_path = os.fspath(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tmp = out_path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(job.data)
    os.replace(tmp, out_path)
    logbook.info("torrent", f"{name}: {length:,} bytes recovered from the swarm")
    return length


def status():
    """What the UI shows about the fallback."""
    if not available():
        return {"available": False, "path": str(config.TORRENT_PATH)}
    try:
        m = meta()
        indexed = int(db.get_kv("torrent_files") or 0)
        return {
            "available": True,
            "path": str(config.TORRENT_PATH),
            "name": m.name,
            "files": len(m.files),
            "indexed": indexed,
            "total_bytes": m.total,
            "piece_length": m.piece_length,
            "trackers": len(m.trackers),
        }
    except Exception as exc:  # noqa: BLE001 - a broken torrent must not 500
        return {"available": False, "path": str(config.TORRENT_PATH),
                "error": str(exc)}
