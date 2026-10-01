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


class ReloadingServer(ThreadingHTTPServer):
    """Picks up a new dashboard/api/index.py after a `git pull` without a restart: the background
    dashboard runs for days, and stale API code would reject actions the new page sends."""

    def __init__(self, address):
        self._mtime = self._api_mtime()
        super().__init__(address, make_handler())

    @staticmethod
    def _api_mtime() -> float:
        try:
            return (DASH_DIR / "api" / "index.py").stat().st_mtime
        except OSError:
            return 0.0

    def finish_request(self, request, client_address):
        mtime = self._api_mtime()
        if mtime != self._mtime:
            try:
                self.RequestHandlerClass = make_handler()
                self._mtime = mtime
                print("dashboard API code changed on disk; reloaded")
            except Exception as e:  # a half-written file mid-pull: keep serving the old code, retry next request
                print(f"dashboard API reload failed, keeping the previous version: {e}")
        super().finish_request(request, client_address)


def server(port: int | None = None, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    return ReloadingServer((host, default_port() if port is None else port))


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
