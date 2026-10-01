"""Run the dashboard on your laptop (same page and API code that Vercel runs).

`python -m outreach dashboard` -> http://127.0.0.1:7347 (or DASHBOARD_PORT from .env). It talks to the same Turso database,
so it needs TURSO_DATABASE_URL, TURSO_AUTH_TOKEN, DASHBOARD_PASSWORD and SESSION_SECRET in .env.
"""
from __future__ import annotations

import errno
import importlib.util
from http.server import ThreadingHTTPServer

from . import config

DASH_DIR = config.ROOT / "dashboard"
DEFAULT_PORT = 7347  # uncommon on purpose: 8787/8080/3000 are often taken by other local tools


def default_port() -> int:
    import os
    try:
        return int(os.getenv("DASHBOARD_PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


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


def server(port: int | None = None, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, default_port() if port is None else port), make_handler())


def serve(port: int | None = None) -> None:
    port = port or default_port()
    try:
        srv = server(port)
    except OSError as e:
        if e.errno not in (errno.EADDRINUSE, 10048):  # 10048 = Windows "address in use"
            raise
        raise SystemExit(
            f"Port {port} is already in use - probably a dashboard you started earlier.\n"
            f"  Already running? Open http://127.0.0.1:{port}\n"
            f"  Stop the old one: lsof -ti :{port} | xargs kill   (then run this again)\n"
            f"  Or use another port: python -m outreach dashboard --port {port + 1}") from None
    print(f"Dashboard on http://127.0.0.1:{port}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
