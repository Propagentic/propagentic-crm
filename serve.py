#!/usr/bin/env python3
"""Local server launcher for the parcel explorer.

Why this exists: a 143 MB single-file HTML is too big to reliably double-click
into a browser. Loading it over http://localhost (instead of file://) is
roughly 3x faster because the browser can stream + cache the response,
and macOS Finder won't try to memory-map the whole file.

Usage:
  python3 serve.py            # serves on http://localhost:8000, opens browser
  python3 serve.py --port N   # use a different port
  python3 serve.py --no-open  # don't auto-open browser
"""
from __future__ import annotations

import argparse
import http.server
import socketserver
import threading
import time
import webbrowser
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--file", default="app.html")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent
    target = root / args.file
    if not target.exists():
        raise SystemExit(f"ERROR: {target} not found — run `python3 build.py` first")

    # Switch into the project directory so http.server serves files from here.
    import os
    os.chdir(str(root))

    Handler = http.server.SimpleHTTPRequestHandler
    # Allow port reuse so re-running doesn't fail with "Address already in use".
    socketserver.TCPServer.allow_reuse_address = True

    with socketserver.TCPServer(("127.0.0.1", args.port), Handler) as httpd:
        url = f"http://localhost:{args.port}/{args.file}"
        print(f"Serving {root} on http://localhost:{args.port}")
        print(f"Open: {url}")
        print(f"Press Ctrl+C to stop.\n")
        if not args.no_open:
            # short delay so the server is up before we open the URL
            threading.Timer(0.4, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping.")


if __name__ == "__main__":
    main()
