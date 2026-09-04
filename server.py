"""SteamFlix launcher.

    python server.py            start on http://127.0.0.1:8777
    python server.py --port N   use another port
    python server.py --rebuild  re-download the mirror listings first
"""
import argparse
import sys
import threading
import webbrowser

from steamflix import api, config, index


def main():
    parser = argparse.ArgumentParser(description="SteamFlix - Steam2 archive browser")
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--rebuild", action="store_true",
                        help="re-download the mirror listings and rebuild the catalogue")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    config.ensure_dirs()
    if args.rebuild:
        print("rebuilding catalogue from the mirror listings...")
        index.sync(refresh=True, force=True)

    app = api.bootstrap()
    url = f"http://{args.host}:{args.port}/"
    print(f"\n  SteamFlix -> {url}")
    print(f"  library   -> {config.LIB_DIR}")
    print(f"  mirrors   -> {', '.join(config.MIRRORS)}\n")

    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    try:
        app.run(host=args.host, port=args.port, threaded=True, debug=False)
    except KeyboardInterrupt:
        print("\nbye")
        sys.exit(0)


if __name__ == "__main__":
    main()
