"""Playback support for extracted depots.

An extracted Steam2 depot is just a folder of files. This module classifies what
came out, picks the obvious thing to launch or play, and serves media files back
to the browser with byte-range support so seeking works.
"""
import mimetypes
import os
import subprocess
from pathlib import Path

from . import config

VIDEO_EXT = {".mp4", ".webm", ".m4v", ".mov", ".avi", ".mkv", ".wmv", ".mpg",
             ".mpeg", ".ogv", ".bik", ".roq"}
AUDIO_EXT = {".mp3", ".ogg", ".wav", ".flac", ".m4a", ".aac", ".wma", ".mid"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tga", ".webp"}
TEXT_EXT = {".txt", ".ini", ".cfg", ".log", ".md", ".xml", ".json", ".nfo", ".res"}
DOC_EXT = {".pdf"}
RUN_EXT = {".exe", ".bat", ".cmd", ".com"}

# Browsers can play these natively; anything else needs the desktop player.
WEB_VIDEO = {".mp4", ".webm", ".m4v", ".ogv", ".mov"}
WEB_AUDIO = {".mp3", ".ogg", ".wav", ".m4a", ".flac", ".aac"}

# Support binaries that are never the game itself.
LAUNCH_NOISE = ("unins", "setup", "vcredist", "dxsetup", "dotnet", "directx",
                "redist", "crashreport", "installer", "updater", "config",
                "dxwebsetup", "eula", "readme")


def kind_of(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in IMAGE_EXT:
        return "image"
    if ext in DOC_EXT:
        return "doc"
    if ext in TEXT_EXT:
        return "text"
    if ext in RUN_EXT:
        return "run"
    return "file"


def playable_in_browser(path: Path) -> bool:
    ext = path.suffix.lower()
    return ext in WEB_VIDEO or ext in WEB_AUDIO or ext in IMAGE_EXT or ext in TEXT_EXT \
        or ext in DOC_EXT


def safe_under(root: Path, target: Path) -> bool:
    """Guard against path traversal out of the library folder."""
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def resolve(rel: str) -> Path:
    """Map a client-supplied relative path onto the extracted library."""
    root = config.EXTRACT_DIR.resolve()
    target = (root / rel.replace("\\", "/").lstrip("/")).resolve()
    if not safe_under(root, target):
        raise PermissionError("path escapes the library folder")
    return target


def _score_executable(path: Path, root: Path) -> int:
    """Rank candidate launchers; the game's own exe usually sits shallow and
    is the biggest thing in the folder."""
    name = path.stem.lower()
    if any(n in name for n in LAUNCH_NOISE):
        return -1
    depth = len(path.relative_to(root).parts)
    try:
        size = path.stat().st_size
    except OSError:
        return -1
    score = 100 - depth * 15
    score += min(40, size // (2 << 20))
    if name in {root.name.lower(), root.name.split(" [")[0].lower()}:
        score += 60
    if path.parent == root:
        score += 20
    return score


def scan(folder: Path, limit=4000) -> dict:
    """Inventory an extracted depot: what it holds and what to open first."""
    root = folder.resolve()
    entries = []
    counts = {"video": 0, "audio": 0, "image": 0, "text": 0, "doc": 0, "run": 0, "file": 0}
    total = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        kind = kind_of(path)
        counts[kind] += 1
        total += size
        if len(entries) < limit:
            entries.append({
                "name": path.name,
                "rel": str(path.relative_to(config.EXTRACT_DIR)).replace("\\", "/"),
                "dir": str(path.parent.relative_to(root)).replace("\\", "/").strip("."),
                "size": size,
                "kind": kind,
                "web": playable_in_browser(path),
            })

    runnables = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in RUN_EXT]
    ranked = sorted(((_score_executable(p, root), p) for p in runnables),
                    key=lambda t: t[0], reverse=True)
    best = ranked[0][1] if ranked and ranked[0][0] > 0 else None

    videos = [e for e in entries if e["kind"] == "video"]
    videos.sort(key=lambda e: e["size"], reverse=True)

    if best is not None:
        mode = "play"
    elif videos:
        mode = "watch"
    else:
        mode = "browse"

    return {
        "path": str(root),
        "folder": root.name,
        "counts": counts,
        "total_bytes": total,
        "file_count": sum(counts.values()),
        "truncated": sum(counts.values()) > len(entries),
        "entries": entries,
        "mode": mode,
        "launcher": {
            "name": best.name,
            "rel": str(best.relative_to(config.EXTRACT_DIR)).replace("\\", "/"),
        } if best else None,
        "executables": [
            {"name": p.name,
             "rel": str(p.relative_to(config.EXTRACT_DIR)).replace("\\", "/"),
             "size": p.stat().st_size}
            for score, p in ranked[:12] if score > 0
        ],
        "featured_video": videos[0] if videos else None,
    }


def launch(path: Path, settle=1.5):
    """Start a program from the library in its own folder.

    A 2005 executable dropped onto a modern machine frequently dies on the spot -
    a missing runtime, a 16-bit installer, a refusal to run without admin - and
    a detached Popen reports none of that. Waiting a moment and looking at the
    exit code turns a silent nothing-happened into a message worth reading.
    """
    if path.suffix.lower() not in RUN_EXT:
        raise ValueError(f"{path.name} is not an executable")
    proc = subprocess.Popen([str(path)], cwd=str(path.parent),
                            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    try:
        code = proc.wait(timeout=settle)
    except subprocess.TimeoutExpired:
        return {"pid": proc.pid, "running": True, "exit_code": None}
    return {"pid": proc.pid, "running": False, "exit_code": code}


# Exit codes a Steam2-era binary hits most often on a modern desktop.
LAUNCH_HINTS = {
    -1073741701: "the executable is missing a DLL it needs (0xC0000135)",
    -1073741515: "a dependency DLL could not be found (0xC0000139)",
    -1073741819: "the program crashed on startup (access violation)",
    -1073741795: "the program used an instruction this CPU refuses (0xC000001D)",
    740: "the program wants to run as administrator",
    216: "this is a 16-bit program, which 64-bit Windows cannot run",
}


def open_with_default(path: Path):
    """Hand a file to whatever Windows uses for it (used for .bik, .avi, PDFs)."""
    os.startfile(str(path))  # noqa: S606 - deliberate local desktop hand-off


def guess_type(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    if mime:
        return mime
    ext = path.suffix.lower()
    if ext in VIDEO_EXT:
        return "video/mp4"
    if ext in AUDIO_EXT:
        return "audio/mpeg"
    if ext in TEXT_EXT:
        return "text/plain; charset=utf-8"
    return "application/octet-stream"
