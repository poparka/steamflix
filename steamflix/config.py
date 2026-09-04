"""Static configuration for SteamFlix."""
import os
from pathlib import Path

APP_NAME = "SteamFlix"

# Mirrors are tried in order and a failing host is skipped for a while, so a
# slow or dead mirror never stalls a download for long.
MIRRORS = [m.strip().rstrip("/") for m in os.environ.get(
    "STEAMFLIX_MIRRORS",
    "http://ro.steam2.download,https://de.steam2.download",
).split(",") if m.strip()]
MIRROR = MIRRORS[0]
MIRROR_COOLDOWN = 120           # seconds a mirror is benched after failing

BLOB_URL = MIRROR + "/blobs/"
DAT_URL = MIRROR + "/dats/"
BLOB_INDEX_URL = MIRROR + "/blobs_dates.txt"
DAT_INDEX_URL = MIRROR + "/dats_dates.txt"
BLOB_SHA_URL = MIRROR + "/blobs.sha256"
DAT_SHA_URL = MIRROR + "/dats.sha256"
EXTRACTOR_URL = MIRROR + "/extractor/extract.exe"

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
DATA_DIR = Path(os.environ.get("STEAMFLIX_DATA", ROOT / "data"))
LIB_DIR = Path(os.environ.get("STEAMFLIX_LIBRARY", ROOT.parent / "library"))
TOOLS_DIR = ROOT / "tools"

DB_PATH = DATA_DIR / "steamflix.db"
CACHE_DIR = DATA_DIR / "cache"
EXTRACTOR_PATH = TOOLS_DIR / "extract.exe"

DEPOT_DIR = LIB_DIR / "depots"
EXTRACT_DIR = LIB_DIR / "extracted"

# Last-resort source when every mirror is down. SteamFlix does not run a
# bittorrent client itself; it points at the torrent and names the exact files
# to select inside it.
TORRENT_PATH = Path(os.environ.get("STEAMFLIX_TORRENT", ROOT.parent / "steam2.torrent"))

# SteamFlix carries its own small BitTorrent client (steamflix/torrent.py) and
# uses it automatically when every mirror refuses a file. Set to 0 to keep
# SteamFlix strictly on HTTP.
TORRENT_FALLBACK = os.environ.get("STEAMFLIX_TORRENT_FALLBACK", "1") not in ("0", "false", "no")
TORRENT_WORKERS = int(os.environ.get("STEAMFLIX_TORRENT_PEERS", "16"))

# Depots with no bundled key sometimes still extract with an all-zero key,
# which is how unencrypted Steam2 content ends up recorded.
FALLBACK_KEYS = ["00000000000000000000000000000000", "0"]

# Keep this much room free on the library drive before starting a download.
STORAGE_RESERVE = int(os.environ.get("STEAMFLIX_RESERVE_GB", "5")) * (1 << 30)

# SteamFlix will not let its own blobs, dats and extracted games grow past this,
# regardless of how much room the drive has. Unset means "no cap of our own" -
# the limit is then simply the room the library drive actually has, which is
# what most people expect the header to be telling them.
_budget_gb = os.environ.get("STEAMFLIX_BUDGET_GB", "").strip()
STORAGE_BUDGET = int(_budget_gb) * (1 << 30) if _budget_gb.isdigit() else None

# Where to look for the reference extractor sources, used to harvest the
# built-in depot key table so the UI can flag depots that cannot be decrypted.
# Checked in order. The first two are where the reference extractor's source
# usually ends up next to a checkout; the rest let someone simply drop the file
# into the project or the data folder without moving anything else.
KEYS_CPP_CANDIDATES = [
    ROOT.parent / "src" / "src" / "keys.cpp",
    ROOT / "src" / "keys.cpp",
    ROOT / "keys.cpp",
    ROOT / "tools" / "keys.cpp",
    DATA_DIR / "keys.cpp",
]

# Steam metadata sources. SteamDB itself sits behind Cloudflare and returns 403
# to anything that is not a real browser, so depot -> app resolution goes
# through the public PICS mirror at api.steamcmd.net instead and the UI links
# out to the matching SteamDB pages.
STEAMCMD_API = "https://api.steamcmd.net/v1/info/{appid}"
STEAMDB_DEPOT_URL = "https://steamdb.info/depot/{depot}/"
STEAMDB_APP_URL = "https://steamdb.info/app/{appid}/"
CDN_BASE = "https://cdn.akamai.steamstatic.com/steam/apps/{appid}/"

# A depot id is usually the owning app id plus a small offset, so resolution
# walks backwards from the depot id looking for an app that claims it.
DEPOT_APP_SPAN = 24

DOWNLOAD_THREADS = int(os.environ.get("STEAMFLIX_DL_THREADS", "4"))

# Download-manager style transfers: a file bigger than the threshold is split
# into this many parallel byte ranges, spread round-robin across the mirrors.
SEGMENTS_PER_FILE = int(os.environ.get("STEAMFLIX_SEGMENTS", "8"))
SEGMENT_THRESHOLD = int(os.environ.get("STEAMFLIX_SEGMENT_MIN_MB", "8")) * (1 << 20)
RESOLVE_THREADS = int(os.environ.get("STEAMFLIX_RESOLVE_THREADS", "3"))
RESOLVE_DELAY = float(os.environ.get("STEAMFLIX_RESOLVE_DELAY", "0.12"))
HTTP_TIMEOUT = 30
CHUNK_SIZE = 1 << 20

HOST = os.environ.get("STEAMFLIX_HOST", "127.0.0.1")
PORT = int(os.environ.get("STEAMFLIX_PORT", "8777"))

USER_AGENT = f"{APP_NAME}/1.0 (local archive browser)"


def ensure_dirs():
    for p in (DATA_DIR, CACHE_DIR, TOOLS_DIR, LIB_DIR, DEPOT_DIR, EXTRACT_DIR):
        p.mkdir(parents=True, exist_ok=True)
