#!/usr/bin/env python3
"""Local dev server for the CRM site (docs/) with a voice-agent proxy.

Why this exists: the deployed site (propagentic-crm.web.app) is CORS-allowed by
the voice-agent server, but localhost is not. This server makes local viewing
work without touching the voice-agent: it serves docs/ AND proxies
/voice-api/* to the voice-agent backend server-side (no CORS involved),
injecting the ops token so it never has to live in frontend code.

Usage:
  python3 dev-server.py                                   # port 8123, backend http://localhost:3000
  python3 dev-server.py --backend https://xyz.trycloudflare.com --token <OPS_TOKEN>
  VOICE_BACKEND=https://xyz.trycloudflare.com OPS_TOKEN=abc python3 dev-server.py

Token resolution order: --token, $OPS_TOKEN, OPS_TOKEN in ~/.hermes/.env.
Then open http://localhost:8123/  (CRM shell)  or  /call-center.html directly.
"""
from __future__ import annotations

import argparse
import http.server
import os
import re
import socketserver
import ssl
import threading
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "docs"
PROXY_PREFIX = "/voice-api"


def read_hermes_token() -> str:
    env_path = Path.home() / ".hermes" / ".env"
    try:
        for line in env_path.read_text().splitlines():
            m = re.match(r"\s*(?:export\s+)?OPS_TOKEN\s*=\s*(.+?)\s*$", line)
            if m:
                return m.group(1).strip().strip("'\"")
    except OSError:
        pass
    return ""


class Handler(http.server.SimpleHTTPRequestHandler):
    backend = "http://localhost:3000"
    token = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        if self.path.startswith(PROXY_PREFIX + "/") or self.path == PROXY_PREFIX:
            self.proxy("GET")
        else:
            super().do_GET()

    def do_POST(self):
        if self.path.startswith(PROXY_PREFIX + "/") or self.path == PROXY_PREFIX:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            self.proxy("POST", body)
        else:
            self.send_error(404)

    def proxy(self, method, body=None):
        upstream_path = self.path[len(PROXY_PREFIX):] or "/"
        # Inject the ops token unless the request already carries one.
        parsed = urllib.parse.urlparse(upstream_path)
        query = urllib.parse.parse_qs(parsed.query)
        if self.token and "t" not in query:
            query["t"] = [self.token]
        new_query = urllib.parse.urlencode(query, doseq=True)
        url = self.backend + urllib.parse.urlunparse(parsed._replace(query=new_query))

        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Accept", self.headers.get("Accept", "*/*"))
        if body is not None:
            req.add_header("Content-Type", self.headers.get("Content-Type", "application/json"))
        # python.org macOS builds ship without CA certs wired up; use certifi's
        # bundle when present so https tunnels verify properly.
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ctx = ssl.create_default_context()
        try:
            resp = urllib.request.urlopen(req, timeout=310, context=ctx)
        except urllib.error.HTTPError as e:
            body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        except Exception as e:  # backend down / tunnel dead
            body = ('{"error": "voice-agent backend unreachable: %s"}' % str(e).replace('"', "'")).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        ctype = resp.headers.get("Content-Type", "application/octet-stream")
        self.send_response(resp.status)
        self.send_header("Content-Type", ctype)
        is_sse = "text/event-stream" in ctype
        if is_sse:
            self.send_header("Cache-Control", "no-cache")
        else:
            length = resp.headers.get("Content-Length")
            if length:
                self.send_header("Content-Length", length)
        self.end_headers()
        # SSE: EventSource needs the response headers immediately, before the
        # first event arrives — without this flush they sit in the write buffer
        # and the browser times the stream out.
        self.wfile.flush()
        try:
            # Stream so SSE events flush through immediately. read1() returns as
            # soon as ANY bytes are available; read(n) would block until a full
            # n-byte buffer accumulates and hold events hostage.
            while True:
                chunk = resp.read1(1024) if is_sse else resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                if is_sse:
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser tab closed / EventSource reconnected
        finally:
            resp.close()

    def log_message(self, fmt, *args):
        # Quiet the static noise; keep proxy lines visible.
        if self.path.startswith(PROXY_PREFIX) and "/events" not in self.path:
            super().log_message(fmt, *args)


class ThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True   # SSE connections must not block shutdown


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--backend", default=os.environ.get("VOICE_BACKEND", "http://localhost:3000"),
                    help="voice-agent base URL (local port or cloudflared tunnel URL)")
    ap.add_argument("--token", default=os.environ.get("OPS_TOKEN", "") or read_hermes_token(),
                    help="ops token to inject into proxied requests")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    Handler.backend = args.backend.rstrip("/")
    Handler.token = args.token

    url = f"http://localhost:{args.port}/"
    with ThreadingServer(("127.0.0.1", args.port), Handler) as httpd:
        print(f"Serving {ROOT}")
        print(f"CRM shell:        {url}")
        print(f"Call Center only: {url}call-center.html")
        print(f"Proxying {PROXY_PREFIX}/* -> {Handler.backend}  (token: {'injected' if Handler.token else 'NONE — set --token or OPS_TOKEN'})")
        print("Press Ctrl+C to stop.\n")
        if not args.no_open:
            threading.Timer(0.4, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping.")


if __name__ == "__main__":
    main()
