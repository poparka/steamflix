"""SteamFlix first-time setup.

Run this once after unpacking the project:

    python setup.py

It asks where the project lives, checks Python and the handful of packages
SteamFlix needs, finds the extractor and the torrent if they are around, and
writes a start.bat wired to those paths so the app can be launched by
double-clicking from then on.

Everything it asks has a sensible default in brackets - pressing Enter accepts
it, so on a normal unpack the whole thing is four Enters.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

REQUIRED = [
    ("flask", "Flask", "the local web server"),
    ("requests", "requests", "downloading from the mirrors"),
    ("cryptography", "cryptography", "testing depot keys"),
]

HERE = Path(__file__).resolve().parent


def say(msg=""):
    print(msg, flush=True)


def rule(title):
    say()
    say(title)
    say("-" * len(title))


def ask(prompt, default=""):
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        say()
        sys.exit(1)
    return answer or default


def ask_yes(prompt, default=True):
    hint = "Y/n" if default else "y/N"
    answer = ask(f"{prompt} ({hint})").lower()
    if not answer:
        return default
    return answer.startswith("y")


def ask_dir(prompt, default, must_contain=None):
    """Ask for a folder, and keep asking until it is a real one.

    ``must_contain`` names a file that proves the folder is the right one, which
    catches the usual mistake of pointing at the parent or the zip.
    """
    while True:
        raw = ask(prompt, str(default))
        path = Path(raw.strip('"').strip("'")).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if not path.exists():
            say(f"  ! {path} does not exist.")
            continue
        if must_contain and not (path / must_contain).exists():
            say(f"  ! {path} has no {must_contain} in it - that is not the "
                f"project folder.")
            continue
        return path


# --------------------------------------------------------------------------- #
def check_python():
    rule("Python")
    version = ".".join(str(n) for n in sys.version_info[:3])
    say(f"  running {version} from {sys.executable}")
    if sys.version_info < (3, 9):
        say("  ! SteamFlix needs Python 3.9 or newer.")
        return False
    say("  version is fine")
    return True


def check_packages():
    rule("Packages")
    missing = []
    for module, package, why in REQUIRED:
        try:
            __import__(module)
            say(f"  {package:<14} present")
        except ImportError:
            say(f"  {package:<14} MISSING  ({why})")
            missing.append(package)
    if not missing:
        return True

    say()
    if not ask_yes(f"Install {', '.join(missing)} now with pip?"):
        say("  Install them yourself with:")
        say(f"    {sys.executable} -m pip install {' '.join(missing)}")
        return False
    cmd = [sys.executable, "-m", "pip", "install", *missing]
    say(f"  running: {' '.join(cmd)}")
    if subprocess.call(cmd) != 0:
        say("  ! pip failed. Install the packages by hand and run setup again.")
        return False
    return True


def find_extractor(project: Path):
    """extract.exe is fetched from the mirror on first use, so this is a note."""
    rule("Extractor")
    exe = project / "tools" / "extract.exe"
    if exe.exists():
        say(f"  found {exe} ({exe.stat().st_size:,} bytes)")
    else:
        say("  not here yet - SteamFlix downloads it from the mirror the first")
        say("  time you extract something. Nothing to do.")
    return exe


def find_torrent(project: Path):
    """The torrent is the fallback when every mirror is down."""
    rule("Torrent fallback")
    candidates = [
        project.parent / "steam2.torrent",
        project / "steam2.torrent",
        Path.cwd() / "steam2.torrent",
    ]
    for path in candidates:
        if path.exists():
            size = path.stat().st_size
            say(f"  found {path} ({size / 2 ** 20:.1f} MB)")
            say("  SteamFlix will use its built-in torrent client if the mirrors fail.")
            return path
    say("  no steam2.torrent found.")
    say("  Without it SteamFlix is HTTP-only: if both mirrors are down, downloads")
    say("  stop. Drop steam2.torrent next to the project folder to enable the")
    say("  fallback - setup can be run again later to pick it up.")
    return None


def pick_library(project: Path):
    rule("Library folder")
    say("  This is where downloaded blobs and extracted games are kept.")
    say("  It gets large - tens of gigabytes if you pull a few big titles.")
    default = project.parent / "library"
    while True:
        raw = ask("  Library folder", str(default))
        path = Path(raw.strip('"').strip("'")).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            say(f"  ! cannot create {path}: {exc}")
            continue
        try:
            total, used, free = shutil.disk_usage(path)
            say(f"  {free / 2 ** 30:.1f} GB free on {Path(path).anchor}")
            if free < 5 * 2 ** 30:
                say("  ! under 5 GB free - SteamFlix will refuse most downloads.")
        except OSError:
            pass
        return path


def pick_port():
    rule("Port")
    while True:
        raw = ask("  Port for the local server", "8777")
        if raw.isdigit() and 1 <= int(raw) <= 65535:
            return int(raw)
        say("  ! that is not a port number.")


BAT_TEMPLATE = """@echo off
rem  Generated by setup.py - re-run it to change these paths.
setlocal
title SteamFlix Server - close this window to stop
cd /d "{project}"

set "PORT={port}"
if not "%~1"=="" set "PORT=%~1"

set "PYTHON={python}"
set "STEAMFLIX_LIBRARY={library}"
{torrent_line}
echo.
echo   SteamFlix
echo   ---------
echo   Server  : http://127.0.0.1:%PORT%/
echo   Library : %STEAMFLIX_LIBRARY%
echo.
echo   Close this window (or press Ctrl+C) to stop the server.
echo.

rem A leftover server from a previous run would hold the port, so clear it first.
call :free_port

"%PYTHON%" server.py --port %PORT%

echo.
echo   Server stopped. Cleaning up...
call :free_port
echo   Done.
timeout /t 2 >nul
exit /b 0

:free_port
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /r /c:"LISTENING" ^| findstr ":%PORT% "') do (
    taskkill /f /pid %%P >nul 2>&1
)
exit /b 0
"""


def write_bat(project: Path, python: Path, library: Path, port: int, torrent):
    rule("start.bat")
    target = project / "start.bat"
    if target.exists() and not ask_yes(f"  {target.name} already exists. Overwrite?"):
        say("  left alone.")
        return target
    torrent_line = (f'set "STEAMFLIX_TORRENT={torrent}"\n' if torrent else
                    "rem  No steam2.torrent found at setup time.\n")
    target.write_text(BAT_TEMPLATE.format(
        project=project, port=port, python=python, library=library,
        torrent_line=torrent_line,
    ), encoding="utf-8")
    say(f"  wrote {target}")
    return target


def main():
    say()
    say("  STEAMFLIX SETUP")
    say("  ===============")
    say("  Sets this copy up on your machine and writes a start.bat for it.")

    rule("Project folder")
    say("  Where did you unpack SteamFlix? (the folder holding server.py)")
    project = ask_dir("  Project folder", HERE, must_contain="server.py")

    if not check_python():
        sys.exit(1)
    if not check_packages():
        sys.exit(1)

    library = pick_library(project)
    port = pick_port()
    find_extractor(project)
    torrent = find_torrent(project)
    bat = write_bat(project, Path(sys.executable), library, port, torrent)

    rule("Done")
    say(f"  Start SteamFlix by double-clicking:  {bat}")
    say(f"  It will open                        http://127.0.0.1:{port}/")
    say()
    say("  The first run builds the catalogue from the mirror listings, which")
    say("  takes a minute or two. After that it starts instantly.")
    say()

    if ask_yes("  Start SteamFlix now?", default=False):
        env = dict(os.environ, STEAMFLIX_LIBRARY=str(library))
        if torrent:
            env["STEAMFLIX_TORRENT"] = str(torrent)
        subprocess.call([sys.executable, "server.py", "--port", str(port)],
                        cwd=str(project), env=env)


if __name__ == "__main__":
    main()
