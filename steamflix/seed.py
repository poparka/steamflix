"""Seeding: giving the archive back to the swarm.

torrent.py is the leech half. It works out which byte range of the 12 TiB
torrent a wanted blob occupies and pulls exactly that. This module is the other
half, and it exists because everything SteamFlix downloads is byte-identical to
what the torrent carries - the mirrors and the torrent are the same archive - so
those files can be served straight back to anyone else pulling them, no matter
which of the two routes they arrived by.

BitTorrent trades in pieces, not files, and this torrent's pieces are 16 MiB
while its median file is 150 KiB. So a byte can only be uploaded once three
things are true:

  * the file is on disk at its full length, under library/depots/<id>/blobs or
    dats, which is what the torrent stores as blobs/... and dats/...;
  * every other file overlapping its 16 MiB piece is on disk too - a piece is
    all or nothing, and one usually spans a hundred small blobs;
  * that piece's SHA-1 matches the torrent's own hash for it.

Only then does the piece enter the bitfield we advertise, which is why nothing
here can serve corrupt data even if the library is edited underneath it. The
verified bitfield is cached in data/torrent_have.bin, so a restart re-reads only
what changed rather than the whole library.

There are two ways to actually upload, and SteamFlix uses both:

  * over connections we opened. While downloading from a peer, that same socket
    serves anything the peer asks of us (torrent.Peer). This works from behind
    any NAT and needs no setup whatsoever.
  * over connections others open. That means a listening socket announced to
    the trackers, which only works if the port is reachable - so start() asks
    the router for a forward over UPnP/NAT-PMP first and reports what it got.

The one thing this deliberately does not do is claim to have the whole torrent.
``left`` in every announce is the real number of bytes we are missing, so we
appear as exactly what we are: a partial seed for the handful of games this
machine has downloaded.
"""
import hashlib
import socket
import struct
import threading
import time
from bisect import bisect_right
from pathlib import Path

from . import config, db, logbook, portmap, settings
from . import torrent as tor

HAVE_PATH = config.DATA_DIR / "torrent_have.bin"

BLOCK_MAX = 1 << 17             # biggest block we will answer; peers ask 16 KiB
HANDSHAKE_TIMEOUT = 15
SESSION_TIMEOUT = 240           # a peer that says nothing for this long is gone
KEEPALIVE = 110
MIN_ANNOUNCE = 900              # never re-announce faster than this
SAVE_EVERY = 64                 # pieces between bitfield saves while verifying


# --------------------------------------------------------------------------- #
# what this machine can serve
# --------------------------------------------------------------------------- #
class Store:
    """The pieces of the torrent this machine holds, in full and verified.

    Everything the seeder and the download connections need to answer "can we
    send this?" comes from here, and the answer is only ever yes for a piece
    whose SHA-1 has been checked against the torrent.
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.spans = []                 # (start, end, local path), sorted
        self.starts = []                # span starts, for bisect
        self.have = bytearray()         # one byte per piece
        self._field = None              # cached wire bitfield
        self.pieces_have = 0
        self.bytes_have = 0
        self.files_have = 0
        self.uploaded = 0
        self.state = "idle"             # idle | scanning | verifying | ready
        self.checked = 0
        self.to_check = 0
        self.last_scan = 0
        self.loaded = False
        self._stop = threading.Event()

    # -- persistence -------------------------------------------------------- #
    def _load(self):
        """Bring back the bitfield from the last run, if it is still ours."""
        m = tor.meta()
        if len(self.have) != m.piece_count:
            self.have = bytearray(m.piece_count)
        if db.get_kv("torrent_have_hash") != m.info_hash.hex():
            self.loaded = True          # a different torrent: start clean
            return
        try:
            raw = HAVE_PATH.read_bytes()
        except OSError:
            self.loaded = True
            return
        for i in range(min(m.piece_count, len(raw) * 8)):
            if raw[i >> 3] >> (7 - (i & 7)) & 1:
                self.have[i] = 1
        self.loaded = True
        self._recount()

    def save(self):
        m = tor.meta()
        with self.lock:
            field = bytearray((len(self.have) + 7) // 8)
            for i, bit in enumerate(self.have):
                if bit:
                    field[i >> 3] |= 128 >> (i & 7)
        try:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = HAVE_PATH.with_suffix(".part")
            tmp.write_bytes(bytes(field))
            tmp.replace(HAVE_PATH)
            db.set_kv("torrent_have_hash", m.info_hash.hex())
        except OSError as exc:
            logbook.warn("torrent", "could not save the seeding bitfield",
                         detail=str(exc))

    def _recount(self):
        m = tor.meta()
        with self.lock:
            self._field = None
            self.pieces_have = sum(1 for bit in self.have if bit)
            self.bytes_have = sum(m.piece_size(i)
                                  for i, bit in enumerate(self.have) if bit)

    # -- what is on disk ---------------------------------------------------- #
    def _local_files(self):
        """{torrent path: (local path, offset, length)} for what we hold."""
        found = {}
        for kinds, folder in _library_dirs():
            try:
                names = list(folder.iterdir())
            except OSError:
                continue
            for path in names:
                try:
                    if not path.is_file():
                        continue
                    size = path.stat().st_size
                except OSError:
                    continue
                for kind in kinds:
                    tpath = f"{kind}/{path.name}"
                    where = tor.locate(tpath)
                    # A half-downloaded file is not a shorter file, it is a
                    # file with holes, and seeding one would poison the swarm.
                    if where and where[1] == size:
                        found[tpath] = (str(path), where[0], where[1])
                        break
        return found

    def _clear_range(self, offset, length):
        """Forget every piece touching a byte range we no longer hold."""
        m = tor.meta()
        first = offset // m.piece_length
        last = (offset + max(0, length - 1)) // m.piece_length
        with self.lock:
            for i in range(first, min(last + 1, len(self.have))):
                self.have[i] = 0
            self._field = None

    def _apply(self, found):
        stored = {r["tpath"]: (r["local"], r["offset"], r["length"])
                  for r in db.query("SELECT tpath, local, offset, length "
                                    "FROM torrent_local")}
        for tpath in set(stored) - set(found):
            _, offset, length = stored[tpath]
            self._clear_range(offset, length)
        for tpath, (local, offset, length) in found.items():
            if stored.get(tpath, (None,))[0] not in (None, local):
                self._clear_range(offset, length)

        spans = sorted((offset, offset + length, local)
                       for local, offset, length in found.values())
        with self.lock:
            self.spans = spans
            self.starts = [s[0] for s in spans]
            self.files_have = len(spans)

        with db.transaction() as conn:
            conn.execute("DELETE FROM torrent_local")
            conn.executemany(
                "INSERT OR REPLACE INTO torrent_local(tpath, local, offset, length)"
                " VALUES(?,?,?,?)",
                [(tpath, local, offset, length)
                 for tpath, (local, offset, length) in found.items()])

    def _merged(self):
        """The byte ranges we hold, with touching files joined up.

        Joining matters: two adjacent blobs can complete a piece that neither
        of them covers on its own, and in a torrent packed file-after-file that
        happens constantly.
        """
        out = []
        with self.lock:
            spans = list(self.spans)
        for start, end, _ in spans:
            if out and start <= out[-1][1]:
                out[-1][1] = max(out[-1][1], end)
            else:
                out.append([start, end])
        return out

    def _candidates(self):
        """Pieces we cover completely but have not checked yet."""
        m = tor.meta()
        todo = []
        for start, end in self._merged():
            first = (start + m.piece_length - 1) // m.piece_length
            # A piece is ours only if its last byte is ours too, and the final
            # piece of the torrent is short.
            last = m.piece_count if end >= m.total else end // m.piece_length
            for i in range(first, min(last, m.piece_count)):
                if not self.have[i]:
                    todo.append(i)
        return todo

    # -- the scan itself ---------------------------------------------------- #
    def scan(self):
        """Re-read the library and verify whatever it newly completes."""
        if not tor.available():
            return self.stats()
        with self.lock:
            if self.state in ("scanning", "verifying"):
                return self.stats()
            self.state = "scanning"
        try:
            tor.build_index()
            if not self.loaded:
                self._load()
            self._apply(self._local_files())
            todo = self._candidates()
            with self.lock:
                self.state = "verifying"
                self.to_check, self.checked = len(todo), 0
            m = tor.meta()
            gained = 0
            for n, index in enumerate(todo, 1):
                if self._stop.is_set():
                    break
                data = self.read(index * m.piece_length, m.piece_size(index))
                if data is not None and hashlib.sha1(data).digest() == m.piece_hash(index):
                    with self.lock:
                        self.have[index] = 1
                        self._field = None
                    gained += 1
                with self.lock:
                    self.checked = n
                if n % SAVE_EVERY == 0:
                    self._recount()
                    self.save()
            self._recount()
            self.save()
            self.last_scan = time.time()
            if gained:
                logbook.info("torrent",
                             f"seeding {self.pieces_have:,} verified piece(s) "
                             f"({_human(self.bytes_have)}) from {self.files_have} "
                             f"local file(s)")
        except Exception as exc:  # noqa: BLE001 - seeding must never break a run
            logbook.warn("torrent", "the seeding scan stopped early", detail=str(exc))
        finally:
            with self.lock:
                self.state = "ready"
        return self.stats()

    # -- reading ------------------------------------------------------------ #
    def _span_for(self, pos):
        with self.lock:
            idx = bisect_right(self.starts, pos) - 1
            if idx < 0 or idx >= len(self.spans):
                return None
            start, end, path = self.spans[idx]
        return (start, end, path) if start <= pos < end else None

    def read(self, offset, n):
        """``n`` bytes at a torrent offset, or None if we do not hold them all."""
        out = bytearray()
        pos, end = offset, offset + n
        while pos < end:
            span = self._span_for(pos)
            if span is None:
                return None
            start, stop, path = span
            take = min(end, stop) - pos
            try:
                with open(path, "rb") as fh:
                    fh.seek(pos - start)
                    chunk = fh.read(take)
            except OSError:
                return None
            if len(chunk) != take:
                return None
            out += chunk
            pos += take
        return bytes(out)

    # -- the interface the wire protocol uses ------------------------------- #
    def has(self, index) -> bool:
        with self.lock:
            return 0 <= index < len(self.have) and bool(self.have[index])

    def bitfield(self):
        """Our have-bitfield on the wire, or None when we hold nothing."""
        with self.lock:
            if self._field is None:
                if not any(self.have):
                    return None
                field = bytearray((len(self.have) + 7) // 8)
                for i, bit in enumerate(self.have):
                    if bit:
                        field[i >> 3] |= 128 >> (i & 7)
                self._field = bytes(field)
            return self._field

    def read_block(self, index, begin, length):
        """One block for a peer, or None if we cannot honestly serve it."""
        if length <= 0 or length > BLOCK_MAX or begin < 0:
            return None
        m = tor.meta()
        if not self.has(index) or begin + length > m.piece_size(index):
            return None
        data = self.read(index * m.piece_length + begin, length)
        if data is None:
            # The file moved or was deleted under us. Stop advertising it
            # rather than handing out silence.
            self._clear_range(index * m.piece_length, m.piece_size(index))
            self._recount()
        return data

    def note_uploaded(self, n):
        with self.lock:
            self.uploaded += n

    def stats(self):
        m = tor.meta() if tor.available() else None
        with self.lock:
            return {
                "state": self.state,
                "files": self.files_have,
                "pieces": self.pieces_have,
                "piece_total": m.piece_count if m else 0,
                "bytes": self.bytes_have,
                "uploaded": self.uploaded,
                "checked": self.checked,
                "to_check": self.to_check,
                "last_scan": self.last_scan,
            }


def _library_dirs():
    """Every folder that may hold files the torrent also carries."""
    out = []
    depots = config.DEPOT_DIR
    if depots.exists():
        try:
            for depot in depots.iterdir():
                if not depot.is_dir():
                    continue
                for kind in ("blobs", "dats"):
                    folder = depot / kind
                    if folder.is_dir():
                        out.append(((kind,), folder))
        except OSError:
            pass
    # Anything the user points at is matched against both halves of the
    # torrent, so a folder of loose blobs and dats works as-is.
    for extra in settings.get("seed_dirs") or []:
        try:
            folder = Path(extra)
        except Exception:  # noqa: BLE001
            continue
        if folder.is_dir():
            out.append((("blobs", "dats"), folder))
    return out


def _human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# --------------------------------------------------------------------------- #
# uploading to peers who connect to us
# --------------------------------------------------------------------------- #
class Throttle:
    """A token bucket, so seeding cannot eat the whole uplink."""

    def __init__(self, kbps=0):
        self.rate = max(0, kbps) * 1024
        self.allowance = float(self.rate)
        self.stamp = time.monotonic()
        self.lock = threading.Lock()

    def take(self, n):
        if not self.rate:
            return
        with self.lock:
            now = time.monotonic()
            self.allowance = min(float(self.rate),
                                 self.allowance + (now - self.stamp) * self.rate)
            self.stamp = now
            self.allowance -= n
            wait = -self.allowance / self.rate if self.allowance < 0 else 0
        if wait > 0:
            time.sleep(min(wait, 5))


class Session(threading.Thread):
    """One peer that dialled us, served for as long as it keeps asking."""

    def __init__(self, sock, addr, server):
        super().__init__(daemon=True)
        self.sock = sock
        self.addr = addr
        self.server = server
        self.store = server.store
        self.buf = b""
        self.choked = True
        self.interested = False
        self.uploaded = 0
        self.started = time.time()
        self.last = time.time()

    # -- framing ------------------------------------------------------------ #
    def _read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self.buf)))
            if not chunk:
                raise OSError("peer hung up")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send(self, msg_id, payload=b""):
        self.sock.sendall(struct.pack(">IB", len(payload) + 1, msg_id) + payload)

    def _message(self):
        length, = struct.unpack(">I", self._read_exact(4))
        if length == 0:
            return None, b""
        if length > (1 << 20):
            raise OSError("oversized message")
        body = self._read_exact(length)
        return body[0], body[1:]

    # -- the session -------------------------------------------------------- #
    def run(self):
        try:
            self.sock.settimeout(HANDSHAKE_TIMEOUT)
            info_hash = tor.meta().info_hash
            shake = self._read_exact(68)
            if shake[1:20] != tor.HANDSHAKE_PSTR or shake[28:48] != info_hash:
                return
            self.sock.sendall(bytes([len(tor.HANDSHAKE_PSTR)]) + tor.HANDSHAKE_PSTR
                              + b"\x00" * 8 + info_hash + tor.PEER_ID)
            field = self.store.bitfield()
            if not field:
                return                      # nothing to offer, do not hold a slot
            self._send(5, field)
            self.sock.settimeout(KEEPALIVE)
            self.server.joined(self)
            self._loop()
        except Exception:  # noqa: BLE001 - a peer dropping is the normal ending
            pass
        finally:
            self.server.left(self)
            try:
                self.sock.close()
            except OSError:
                pass

    def _loop(self):
        idle = time.time()
        while not self.server.stopping.is_set():
            try:
                msg_id, payload = self._message()
            except socket.timeout:
                if time.time() - idle > SESSION_TIMEOUT:
                    return
                self._send_keepalive()
                idle = time.time()
                continue
            idle = self.last = time.time()
            if msg_id is None:
                continue
            if msg_id == 2:                 # interested
                self.interested = True
                self.server.consider(self)
            elif msg_id == 3:               # not interested
                self.interested = False
                self.server.consider(self)
            elif msg_id == 6:               # request
                self._request(payload)
            # have/bitfield/cancel need no answer: we only ever upload here.

    def _send_keepalive(self):
        self.sock.sendall(b"\x00\x00\x00\x00")

    def _request(self, payload):
        if self.choked or len(payload) < 12:
            return
        index, begin, length = struct.unpack(">III", payload[:12])
        data = self.store.read_block(index, begin, length)
        if not data:
            return
        self.server.throttle.take(len(data))
        self._send(7, struct.pack(">II", index, begin) + data)
        self.uploaded += len(data)
        self.store.note_uploaded(len(data))
        tor.note_uploaded(len(data))

    def choke(self, choked):
        if choked == self.choked:
            return
        try:
            self._send(0 if choked else 1)
            self.choked = choked
        except OSError:
            pass

    def info(self):
        return {"peer": f"{self.addr[0]}:{self.addr[1]}", "uploaded": self.uploaded,
                "choked": self.choked, "interested": self.interested,
                "for": int(time.time() - self.started)}


class Server(threading.Thread):
    """The listening socket, and the handful of peers it is serving."""

    def __init__(self, store, port=6881, max_peers=24, slots=6, up_kbps=0):
        super().__init__(daemon=True)
        self.store = store
        self.want_port = port or 6881
        self.port = 0
        self.max_peers = max(2, max_peers)
        self.slots = max(1, slots)
        self.throttle = Throttle(up_kbps)
        self.sock = None
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.sessions = []
        self.served = 0                     # peers that ever connected in

    def bind(self):
        """Take the wanted port if we can, a nearby one if not."""
        last = None
        for candidate in [self.want_port + n for n in range(10)] + [0]:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind(("0.0.0.0", candidate))
                sock.listen(32)
                sock.settimeout(1.0)
                self.sock = sock
                self.port = sock.getsockname()[1]
                return self.port
            except OSError as exc:
                last = exc
                sock.close()
        raise OSError(f"no port to listen on: {last}")

    def run(self):
        while not self.stopping.is_set():
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self.lock:
                full = len(self.sessions) >= self.max_peers
            if full:
                conn.close()
                continue
            self.served += 1
            Session(conn, addr, self).start()

    def stop(self):
        self.stopping.set()
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        with self.lock:
            sessions = list(self.sessions)
        for session in sessions:
            try:
                session.sock.close()
            except OSError:
                pass

    # -- session bookkeeping ------------------------------------------------ #
    def joined(self, session):
        with self.lock:
            self.sessions.append(session)
        self.consider(session)

    def left(self, session):
        with self.lock:
            if session in self.sessions:
                self.sessions.remove(session)
        self.rotate()

    def consider(self, session):
        """Unchoke an interested peer whenever a slot is free."""
        if not session.interested:
            session.choke(True)
            self.rotate()
            return
        with self.lock:
            busy = sum(1 for s in self.sessions if not s.choked)
        if busy < self.slots:
            session.choke(False)

    def rotate(self):
        with self.lock:
            waiting = [s for s in self.sessions if s.interested and s.choked]
            busy = sum(1 for s in self.sessions if not s.choked)
        for session in waiting[:max(0, self.slots - busy)]:
            session.choke(False)

    def peers(self):
        with self.lock:
            return [s.info() for s in self.sessions]


# --------------------------------------------------------------------------- #
# telling the trackers we are here
# --------------------------------------------------------------------------- #
class Announcer(threading.Thread):
    """Keeps us in the swarm's peer lists for as long as we are seeding.

    Without this nobody ever learns our address, and a seeder nobody can find
    is not seeding at all.
    """

    def __init__(self, store, port_of, mapping_of=None):
        super().__init__(daemon=True)
        self.store = store
        self.port_of = port_of
        self.mapping_of = mapping_of or (lambda: None)
        self.stopping = threading.Event()
        self.interval = MIN_ANNOUNCE
        self.good = []
        self.last = 0
        self.last_error = None
        self.peers_seen = 0

    def run(self):
        first = True
        while not self.stopping.is_set():
            try:
                self.cycle(event=2 if first else 0)
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
            first = False
            self.stopping.wait(max(MIN_ANNOUNCE, self.interval))
        self.cycle(event=3, limit=6)        # tell them we are going, politely

    def cycle(self, event=0, limit=10):
        if not tor.available():
            return
        m = tor.meta()
        left = max(0, m.total - self.store.bytes_have)
        moved = tor.traffic()
        port = self.port_of()
        urls = self.good or list(m.trackers)
        ok, seen, intervals = [], set(), []
        for url in urls[:limit if self.good else 24]:
            if self.stopping.is_set() and event != 3:
                break
            try:
                found, interval = tor.announce(
                    url, port=port, left=left, uploaded=moved["uploaded"],
                    downloaded=moved["downloaded"], event=event, want=50)
            except Exception as exc:  # noqa: BLE001 - dead trackers are routine
                self.last_error = f"{url}: {exc}"
                continue
            ok.append(url)
            intervals.append(max(MIN_ANNOUNCE, int(interval or MIN_ANNOUNCE)))
            seen.update(found)
        if ok:
            self.good = ok
            self.interval = min(intervals) if intervals else MIN_ANNOUNCE
            self.last = time.time()
            self.peers_seen = len(seen)
            self.last_error = None
        _remember_traffic(moved)


def _remember_traffic(moved):
    """Keep a lifetime tally so a restart does not reset the share ratio."""
    try:
        base_up = int(db.get_kv("torrent_uploaded_base") or 0)
        base_down = int(db.get_kv("torrent_downloaded_base") or 0)
        db.set_kv("torrent_uploaded", base_up + moved["uploaded"])
        db.set_kv("torrent_downloaded", base_down + moved["downloaded"])
    except Exception:  # noqa: BLE001 - accounting must not break seeding
        pass


def lifetime():
    try:
        return (int(db.get_kv("torrent_uploaded") or 0),
                int(db.get_kv("torrent_downloaded") or 0))
    except Exception:  # noqa: BLE001
        return (0, 0)


# --------------------------------------------------------------------------- #
# the seeder as a whole
# --------------------------------------------------------------------------- #
_store = None
_server = None
_announcer = None
_mapping = None
_map_error = None
_lock = threading.RLock()


def store() -> Store:
    global _store
    with _lock:
        if _store is None:
            _store = Store()
        return _store


def running() -> bool:
    return _server is not None and not _server.stopping.is_set()


def start(force=False):
    """Start seeding, unless it is switched off or there is nothing to seed."""
    global _server, _announcer
    if not tor.available():
        return status("no steam2.torrent to seed")
    if not force and not settings.seeding_wanted():
        return status("seeding is switched off in Settings")
    with _lock:
        if running():
            return status()
        conf = settings.all()
        here = store()
        server = Server(here, conf["seed_port"], conf["seed_max_peers"],
                        conf["seed_slots"], conf["seed_up_kbps"])
        try:
            port = server.bind()
        except OSError as exc:
            logbook.warn("torrent", "could not open a port to seed from",
                         detail=str(exc))
            return status(str(exc))
        server.start()
        _server = server
        # This run's counters start from zero, so remember where the lifetime
        # totals stood or the share ratio would reset on every restart.
        db.set_kv("torrent_uploaded_base", db.get_kv("torrent_uploaded") or 0)
        db.set_kv("torrent_downloaded_base", db.get_kv("torrent_downloaded") or 0)
        # Downloads now advertise and serve what we hold, on their own sockets.
        tor.set_upload_source(here, port)
        _announcer = Announcer(here, lambda: _server.port if _server else port)
        _announcer.start()
    logbook.info("torrent", f"seeding from port {_server.port}",
                 detail="SteamFlix serves back the blobs and dats it already "
                        "holds. Windows may ask to allow it through the firewall "
                        "the first time.")
    if settings.get("seed_portmap", True):
        threading.Thread(target=_map_port, args=(_server.port,), daemon=True).start()
    rescan()
    return status()


def stop():
    """Stop seeding and take the router mapping back down."""
    global _server, _announcer, _mapping
    with _lock:
        server, announcer, mapping = _server, _announcer, _mapping
        _server = _announcer = _mapping = None
    tor.set_upload_source(None)
    if announcer:
        announcer.stopping.set()            # its last cycle announces "stopped"
    if server:
        server.stop()
    if mapping:
        threading.Thread(target=portmap.close_port, args=(mapping,),
                         daemon=True).start()
    if server:
        logbook.info("torrent", "stopped seeding")
    return status()


def apply_settings():
    """Bring the seeder in line with the settings as they now stand."""
    want = settings.seeding_wanted() and tor.available()
    if not want:
        return stop() if running() else status()
    conf = settings.all()
    if running():
        wrong_port = conf["seed_port"] not in (0, _server.want_port)
        if wrong_port:
            stop()
        else:
            _server.max_peers = max(2, conf["seed_max_peers"])
            _server.slots = max(1, conf["seed_slots"])
            _server.throttle = Throttle(conf["seed_up_kbps"])
            return status()
    return start()


def rescan(block=False):
    """Re-read the library for anything new to seed."""
    here = store()
    if block:
        return here.scan()
    threading.Thread(target=here.scan, daemon=True).start()
    return here.stats()


def _map_port(port):
    global _mapping, _map_error
    try:
        mapping = portmap.open_port(port)
    except Exception as exc:  # noqa: BLE001
        mapping, _map_error = None, str(exc)
    with _lock:
        _mapping = mapping
    if mapping:
        where = mapping.get("external_ip") or "the router"
        logbook.info("torrent",
                     f"port {port} forwarded by {mapping['method'].upper()} "
                     f"({where}) - peers can reach this machine")
    else:
        _map_error = _map_error or "the router did not offer a mapping"
        logbook.info("torrent",
                     f"could not forward port {port} automatically",
                     detail="SteamFlix still uploads to every peer it connects "
                            f"to itself. To accept incoming peers as well, "
                            f"forward TCP port {port} to this machine.")


def status(reason=None):
    """Everything the UI shows about seeding."""
    here = store()
    stats = here.stats()
    up, down = lifetime()
    moved = tor.traffic()
    with _lock:
        server, announcer, mapping = _server, _announcer, _mapping
    out = {
        "available": tor.available(),
        "enabled": settings.seeding_wanted(),
        "running": running(),
        "port": server.port if server else 0,
        "peers": server.peers() if server else [],
        "peers_served": server.served if server else 0,
        "slots": server.slots if server else settings.get("seed_slots"),
        "max_peers": server.max_peers if server else settings.get("seed_max_peers"),
        "up_kbps": settings.get("seed_up_kbps"),
        "mapping": mapping,
        "map_error": _map_error,
        "reachable": bool(mapping),
        "uploaded_session": moved["uploaded"],
        "downloaded_session": moved["downloaded"],
        "uploaded_total": max(up, moved["uploaded"]),
        "downloaded_total": max(down, moved["downloaded"]),
        "ratio": (round(max(up, moved["uploaded"]) / max(down, moved["downloaded"]), 3)
                  if max(down, moved["downloaded"]) else None),
        "announced": announcer.last if announcer else 0,
        "announce_interval": announcer.interval if announcer else 0,
        "trackers_ok": len(announcer.good) if announcer else 0,
        "swarm_peers": announcer.peers_seen if announcer else 0,
        "announce_error": announcer.last_error if announcer else None,
        "reason": reason,
        **stats,
    }
    return out
