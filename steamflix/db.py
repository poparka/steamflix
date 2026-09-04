"""SQLite storage. One connection per thread, WAL mode, tiny helper layer."""
import contextlib
import sqlite3
import threading

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    filename TEXT PRIMARY KEY,
    kind     TEXT NOT NULL,          -- 'blob' | 'dat'
    depot    INTEGER NOT NULL,
    version  INTEGER NOT NULL,
    crc      TEXT NOT NULL,
    hash     TEXT NOT NULL,
    mtime    TEXT,
    size     INTEGER                 -- filled in lazily from HTTP HEAD
);
CREATE INDEX IF NOT EXISTS idx_files_depot ON files(depot, kind, version);

CREATE TABLE IF NOT EXISTS depots (
    depot        INTEGER PRIMARY KEY,
    blob_count   INTEGER NOT NULL DEFAULT 0,
    dat_count    INTEGER NOT NULL DEFAULT 0,
    max_version  INTEGER NOT NULL DEFAULT 0,
    has_reset    INTEGER NOT NULL DEFAULT 0,
    first_date   TEXT,
    last_date    TEXT,
    has_key      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS depot_meta (
    depot       INTEGER PRIMARY KEY,
    appid       INTEGER,
    name        TEXT,
    app_type    TEXT,
    confidence  TEXT,                -- 'exact' | 'likely' | 'manifest' | 'none'
    state       TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_meta_state ON depot_meta(state);
CREATE INDEX IF NOT EXISTS idx_meta_name ON depot_meta(name);

CREATE TABLE IF NOT EXISTS app_cache (
    appid    INTEGER PRIMARY KEY,
    name     TEXT,
    app_type TEXT,
    depots   TEXT,                   -- comma separated depot ids
    found    INTEGER NOT NULL DEFAULT 0,
    fetched  TEXT
);

CREATE TABLE IF NOT EXISTS manifest_cache (
    depot       INTEGER NOT NULL,
    version     INTEGER NOT NULL,
    crc         TEXT NOT NULL,
    appid       INTEGER,
    verid       INTEGER,
    file_count  INTEGER,
    total_bytes INTEGER,
    dat_size    INTEGER,
    prev_crc    TEXT,
    root_dirs   TEXT,
    PRIMARY KEY (depot, version, crc)
);

CREATE TABLE IF NOT EXISTS depot_keys (
    depot  INTEGER NOT NULL,          -- -1 means "no depot stated, try anywhere"
    key    TEXT NOT NULL,             -- 32 hex characters
    source TEXT NOT NULL,             -- bundled | user | keyfile | trial
    PRIMARY KEY (depot, key)
);
CREATE INDEX IF NOT EXISTS idx_keys_depot ON depot_keys(depot);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS torrent_files (
    path   TEXT PRIMARY KEY,
    offset INTEGER NOT NULL,
    length INTEGER NOT NULL
);

-- Local files that are also in the torrent, which is what makes seeding
-- possible: each row says where on disk a torrent path actually lives.
CREATE TABLE IF NOT EXISTS torrent_local (
    tpath  TEXT PRIMARY KEY,         -- 'blobs/441_0_....blob'
    local  TEXT NOT NULL,            -- where it sits in the library
    offset INTEGER NOT NULL,
    length INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS favourites (
    depot INTEGER PRIMARY KEY,
    added TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id        TEXT PRIMARY KEY,
    depot     INTEGER NOT NULL,
    version   INTEGER NOT NULL,
    crc       TEXT,
    title     TEXT,
    state     TEXT NOT NULL,
    payload   TEXT,
    created   TEXT,
    updated   TEXT
);
"""


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        config.ensure_dirs()
        # isolation_level=None means autocommit, which matters more than it
        # sounds: with Python's default, a plain SELECT opens a deferred
        # transaction and keeps it open, and in WAL mode that pins the reader
        # to the snapshot it first saw. These connections live for the life of
        # a thread, so a background worker's writes would stay invisible to a
        # request thread that had read the table once - a stale read that never
        # resolves. Autocommit ends every statement, so readers always see the
        # newest committed data; bulk writes take an explicit transaction below.
        conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        _local.conn = conn
    return conn


@contextlib.contextmanager
def transaction():
    """Group many writes into one commit.

    In autocommit mode every statement is its own transaction, which is right
    for the one-row writes all over SteamFlix and far too slow for the 116,000
    rows the catalogue and torrent indexes insert. Those use this instead.
    """
    conn = connect()
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# Columns added after the first release. SQLite has no "ADD COLUMN IF NOT
# EXISTS", so every one of these is applied and its "duplicate column" error
# swallowed, which is both idempotent and cheap.
MIGRATIONS = [
    "ALTER TABLE app_cache  ADD COLUMN developer TEXT",
    "ALTER TABLE app_cache  ADD COLUMN publisher TEXT",
    "ALTER TABLE app_cache  ADD COLUMN franchise TEXT",
    "ALTER TABLE app_cache  ADD COLUMN genres    TEXT",   # comma separated genre ids
    "ALTER TABLE app_cache  ADD COLUMN released  TEXT",
    "ALTER TABLE app_cache  ADD COLUMN detailed  INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE depot_meta ADD COLUMN developer TEXT",
    "ALTER TABLE depot_meta ADD COLUMN publisher TEXT",
    "ALTER TABLE depot_meta ADD COLUMN franchise TEXT",
    "ALTER TABLE depot_meta ADD COLUMN genres    TEXT",
    "ALTER TABLE depot_meta ADD COLUMN released  TEXT",
]

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_meta_dev   ON depot_meta(developer)",
    "CREATE INDEX IF NOT EXISTS idx_meta_pub   ON depot_meta(publisher)",
    "CREATE INDEX IF NOT EXISTS idx_cache_detail ON app_cache(detailed)",
]


def init():
    conn = connect()
    conn.executescript(SCHEMA)
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass                       # already there
    for stmt in INDEXES:
        conn.execute(stmt)


def query(sql, args=()):
    return connect().execute(sql, args).fetchall()


def one(sql, args=()):
    return connect().execute(sql, args).fetchone()


def execute(sql, args=()):
    return connect().execute(sql, args)


def get_kv(key, default=None):
    row = one("SELECT value FROM kv WHERE key = ?", (key,))
    return row["value"] if row else default


def set_kv(key, value):
    execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
