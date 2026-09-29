"""Run the dashboard on your laptop (same page and API code that Vercel runs).

`python -m outreach dashboard` -> http://127.0.0.1:8787. It talks to the same Turso database,
so it needs TURSO_DATABASE_URL, TURSO_AUTH_TOKEN, DASHBOARD_PASSWORD and SESSION_SECRET in .env.
"""
from __future__ import annotations

import importlib.util
from http.server import ThreadingHTTPServer

from . import config

DASH_DIR = config.ROOT / "dashboard"


def api_module():
    spec = importlib.util.spec_from_file_location("dashboard_api", DASH_DIR / "api" / "index.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_handler():
    base = api_module().handler

    class Local(base):
        def do_GET(self):
            if self.path.startswith("/api/"):
                return super().do_GET()
            page = (DASH_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
    return Local


def server(port: int = 8787, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler())


def serve(port: int = 8787) -> None:
    srv = server(port)
    print(f"Dashboard on http://127.0.0.1:{port}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
