"""Serve the dashboard locally, re-reading the Google Sheet as you go.

    python serve.py                 # http://localhost:8777
    python serve.py --max-age 0     # rebuild on every page load
    python serve.py --port 9000

Opening the page rebuilds it when the data is older than --max-age seconds, so
the sheet is re-read and prices re-fetched without you running anything. The
Refresh button on the page forces an immediate rebuild at any time.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))

# Launched with pythonw (no console) Python gives us no stdout at all, and a
# bare print() then raises and takes the server down mid-request.
if sys.stdout is None or sys.stderr is None:
    _log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.log")
    _log = open(_log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _log
DATA = os.path.join(HERE, "data.json")
PAGE = os.path.join(HERE, "dashboard.html")

build_lock = threading.Lock()
last_error = None
building = False


def data_age():
    """Seconds since the last successful build, or None if there isn't one."""
    if not os.path.exists(DATA) or not os.path.exists(PAGE):
        return None
    return time.time() - os.path.getmtime(DATA)


def rebuild():
    """Re-read the sheet and re-fetch prices. One at a time."""
    global last_error, building
    with build_lock:
        building = True
        started = time.time()
        print("  rebuilding from the Google Sheet ...", flush=True)
        try:
            result = subprocess.run(
                [sys.executable, os.path.join(HERE, "refresh.py"), "--no-open"],
                cwd=HERE, capture_output=True, text=True, timeout=600,
            )
        except subprocess.TimeoutExpired:
            last_error = "the refresh took longer than 10 minutes and was stopped"
            print("  ! " + last_error, flush=True)
            building = False
            return False
        finally:
            building = False
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip().splitlines()
            last_error = tail[-1] if tail else "refresh.py failed"
            print("  ! rebuild failed: %s" % last_error, flush=True)
            return False
        last_error = None
        print("  rebuilt in %.0fs" % (time.time() - started), flush=True)
        return True


def rebuild_async():
    """Kick a rebuild off without making the caller wait for it."""
    if building:
        return
    threading.Thread(target=rebuild, daemon=True).start()


def rebuild_if_stale(max_age):
    """Never block a page load on a rebuild - a large sheet takes a minute.

    Serve whatever is on disk straight away and refresh behind it; the page
    polls /healthz and reloads itself when the new build lands. Only the very
    first run, with nothing to serve, waits.
    """
    age = data_age()
    if age is None:
        return rebuild()
    if age >= max_age:
        rebuild_async()
    return True


class Handler(SimpleHTTPRequestHandler):
    max_age = 600

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=HERE, **kwargs)

    def log_message(self, fmt, *args):        # quieter than the default
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def status_payload(self, ok=True):
        stamp = None
        if os.path.exists(DATA):
            try:
                with open(DATA, encoding="utf-8") as fh:
                    stamp = json.load(fh).get("generated_display")
            except (OSError, ValueError):
                stamp = None
        return {"ok": ok, "generated": stamp, "age": data_age(),
                "building": building, "error": last_error}

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/healthz":
            return self._json(self.status_payload())

        if path == "/refresh":
            rebuild_async()
            return self._json(self.status_payload(True))

        if path in ("/", "/index.html", "/dashboard.html"):
            if not rebuild_if_stale(self.max_age) and not os.path.exists(PAGE):
                return self._json(
                    {"ok": False, "error": last_error or "no dashboard built yet"}, 503)
            self.path = "/dashboard.html"

        return super().do_GET()

    def end_headers(self):
        # Without this the browser keeps showing the previous build.
        if self.path.endswith((".html", ".json")):
            self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()


def main():
    ap = argparse.ArgumentParser(description="Serve the portfolio dashboard locally.")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--max-age", type=int, default=600,
                    help="rebuild when the data is older than this many seconds "
                         "(default 600; use 0 to rebuild on every page load)")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    Handler.max_age = args.max_age
    url = "http://localhost:%d/" % args.port

    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        print("Could not start on port %d: %s" % (args.port, exc))
        print("Something else is using it. Try:  python serve.py --port 8778")
        return 1

    print("Holdings Ledger is running at %s" % url)
    print("  the sheet is re-read whenever the data is older than %ds" % args.max_age)
    print("  press Ctrl+C to stop\n")
    if not args.no_open:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
