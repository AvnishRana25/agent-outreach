"""Minimal Turso (hosted libSQL/SQLite) client over its HTTP API, no extra dependency.

Statements in one `run()` call share a connection, so BEGIN ... COMMIT works across them.
"""
from __future__ import annotations

import base64
import os

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
