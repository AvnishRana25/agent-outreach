"""Minimal Turso (hosted libSQL/SQLite) client over its HTTP API, no extra dependency.

Statements in one `run()` call share a connection, so BEGIN ... COMMIT works across them.
TursoConnection provides a DB-API compatible connection wrapper over the libSQL pipeline session.
"""
from __future__ import annotations

import base64
import os
import time

import requests


class TursoError(RuntimeError):
    pass


def _url() -> str:
    url = os.getenv("TURSO_DATABASE_URL", "")
    if not url:
        raise TursoError("TURSO_DATABASE_URL is not set (see README -> Dashboard)")
    return url.replace("libsql://", "https://", 1).rstrip("/")


def _arg(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, bytes):
        return {"type": "blob", "base64": base64.b64encode(v).decode()}
    return {"type": "text", "value": str(v)}


def _val(cell):
    t = cell.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(cell["value"])
    if t == "float":
        return float(cell["value"])
    if t == "blob":
        return base64.b64decode(cell.get("base64", ""))
    return cell.get("value")


def run(statements: list[tuple[str, tuple] | str]) -> list[list[dict]]:
    """Execute statements in order; returns each statement's rows as dicts."""
    reqs = []
    for st in statements:
        sql, args = (st, ()) if isinstance(st, str) else st
        reqs.append({"type": "execute", "stmt": {"sql": sql, "args": [_arg(a) for a in args]}})
    reqs.append({"type": "close"})
    try:
        r = requests.post(f"{_url()}/v2/pipeline", json={"requests": reqs}, timeout=60,
                          headers={"Authorization": f"Bearer {os.getenv('TURSO_AUTH_TOKEN', '')}"})
    except requests.RequestException as e:
        raise TursoError(f"cannot reach Turso: {e}") from e
    if not r.ok:
        raise TursoError(f"Turso HTTP {r.status_code}: {r.text[:200]}")
    out = []
    for res in r.json().get("results", [])[:-1]:
        if res.get("type") != "ok":
            raise TursoError(f"Turso: {res.get('error', {}).get('message', res)}")
        result = res["response"]["result"]
        cols = [c["name"] for c in result.get("cols", [])]
        out.append([dict(zip(cols, map(_val, row))) for row in result.get("rows", [])])
    return out


class Row(dict):
    """Dictionary that also allows integer indexing by column index, matching sqlite3.Row."""

    def __init__(self, cols: list[str], vals: list):
        super().__init__(zip(cols, vals))
        self._vals = tuple(vals)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._vals[key]
        return super().__getitem__(key)


class TursoCursor:
    def __init__(self, cols: list[str], rows: list[Row], affected_row_count: int = 0, last_insert_rowid=None):
        self.cols = cols
        self.rows = rows
        self.rowcount = affected_row_count
        self.lastrowid = int(last_insert_rowid) if last_insert_rowid is not None else None
        self.description = tuple((c, None, None, None, None, None, None) for c in cols)
        self._idx = 0

    def fetchone(self) -> Row | None:
        if self._idx < len(self.rows):
            r = self.rows[self._idx]
            self._idx += 1
            return r
        return None

    def fetchall(self) -> list[Row]:
        remaining = self.rows[self._idx:]
        self._idx = len(self.rows)
        return remaining

    def __iter__(self):
        return iter(self.fetchall())


class TursoConnection:
    """Stateful libSQL connection session using Turso's /v2/pipeline API with baton tracking."""

    def __init__(self):
        self.baton: str | None = None
        self.in_transaction: bool = False
        self.row_factory = None

    def _post(self, reqs: list[dict]) -> dict:
        url = f"{_url()}/v2/pipeline"
        headers = {"Authorization": f"Bearer {os.getenv('TURSO_AUTH_TOKEN', '')}"}
        payload: dict = {"requests": reqs}
        if self.baton:
            payload["baton"] = self.baton
        last_exc = None
        for attempt in range(3):
            try:
                r = requests.post(url, json=payload, headers=headers, timeout=60)
                if r.ok:
                    data = r.json()
                    self.baton = data.get("baton")
                    return data
                raise TursoError(f"Turso HTTP {r.status_code}: {r.text[:200]}")
            except (requests.RequestException, TursoError) as e:
                last_exc = e
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
        raise TursoError(f"cannot reach Turso after retries: {last_exc}") from last_exc

    def execute(self, sql: str, args: tuple | list = ()) -> TursoCursor:
        clean = sql.strip()
        upper = clean.upper()
        # Pass through / ignore unsupported PRAGMAs on Turso
        if upper.startswith("PRAGMA BUSY_TIMEOUT") or upper.startswith("PRAGMA JOURNAL_MODE"):
            return TursoCursor([], [])
        if upper.startswith("BEGIN"):
            self.in_transaction = True
        elif upper.startswith("COMMIT"):
            self.in_transaction = False
        elif upper.startswith("ROLLBACK"):
            self.in_transaction = False

        req = {"type": "execute", "stmt": {"sql": sql, "args": [_arg(a) for a in args]}}
        data = self._post([req])
        res = data.get("results", [{}])[0]
        if res.get("type") != "ok":
            raise TursoError(f"Turso: {res.get('error', {}).get('message', res)}")
        result = res["response"]["result"]
        cols = [c["name"] for c in result.get("cols", [])]
        rows = [Row(cols, [_val(cell) for cell in row]) for row in result.get("rows", [])]
        affected = result.get("affected_row_count", 0)
        last_rowid = result.get("last_insert_rowid")
        return TursoCursor(cols, rows, affected, last_rowid)

    def executescript(self, script: str) -> list[TursoCursor]:
        stmts = [s.strip() for s in script.split(";") if s.strip()]
        if not stmts:
            return []
        reqs = []
        for s in stmts:
            upper = s.upper()
            if upper.startswith("PRAGMA BUSY_TIMEOUT") or upper.startswith("PRAGMA JOURNAL_MODE"):
                continue
            reqs.append({"type": "execute", "stmt": {"sql": s, "args": []}})
        if not reqs:
            return []
        data = self._post(reqs)
        cursors = []
        for res in data.get("results", []):
            if res.get("type") != "ok":
                raise TursoError(f"Turso: {res.get('error', {}).get('message', res)}")
            result = res["response"]["result"]
            cols = [c["name"] for c in result.get("cols", [])]
            rows = [Row(cols, [_val(cell) for cell in row]) for row in result.get("rows", [])]
            affected = result.get("affected_row_count", 0)
            last_rowid = result.get("last_insert_rowid")
            cursors.append(TursoCursor(cols, rows, affected, last_rowid))
        return cursors

    def commit(self) -> None:
        if self.in_transaction:
            self.execute("COMMIT")

    def rollback(self) -> None:
        if self.in_transaction:
            self.execute("ROLLBACK")

    def close(self) -> None:
        if self.baton:
            try:
                url = f"{_url()}/v2/pipeline"
                headers = {"Authorization": f"Bearer {os.getenv('TURSO_AUTH_TOKEN', '')}"}
                requests.post(url, json={"baton": self.baton, "requests": [{"type": "close"}]},
                              headers=headers, timeout=15)
            except Exception:
                pass
            finally:
                self.baton = None
        self.in_transaction = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is not None:
                self.rollback()
            else:
                self.commit()
        finally:
            self.close()


def connect() -> TursoConnection:
    return TursoConnection()
