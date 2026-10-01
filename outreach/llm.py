"""Gemini wrapper: JSON output validated against a Pydantic schema, paced for the free tier.

Free-tier limits change without notice (check yours in AI Studio -> Rate limits) and are small per
model. Each lead uses 2 calls (research + draft), so 38 leads/day is ~80-100 calls plus reply
triage: that's why each kind of call walks a chain of models (see CHAINS).
Google retires model names for new keys (the 2.5 models already return 404 for them), so the
defaults are the 3.5 family; override with GEMINI_MODEL / GEMINI_RESEARCH_MODEL / GEMINI_REPLY_MODEL.
"""
from __future__ import annotations

import os
import re
import time
from typing import TypeVar

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from . import config

T = TypeVar("T", bound=BaseModel)

_client: genai.Client | None = None
_last_call = 0.0


class QuotaExhausted(RuntimeError):
    """Daily free-tier quota is used up; stop and resume tomorrow."""


def client() -> genai.Client:
    global _client
    if _client is None:
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise SystemExit("GEMINI_API_KEY is not set (get a free key at aistudio.google.com/apikey)")
        _client = genai.Client(api_key=key)
    return _client


# Each model has its own free daily quota, so each kind of call walks a chain: when one model is
# used up for the day (or isn't available to this key) the next one takes over. Quotas reset at
# midnight Pacific time. Override a chain with GEMINI_RESEARCH_MODELS / GEMINI_DRAFT_MODELS /
# GEMINI_REPLY_MODELS (comma-separated); GEMINI_MODEL etc. still put one model first.
CHAINS = {
    "research": ["gemini-3.1-flash-lite", "gemini-flash-lite-latest", "gemini-3.5-flash-lite",
                 "gemini-3.6-flash", "gemini-3.7-flash"],
    "draft": ["gemini-3.5-flash", "gemini-3.8-flash", "gemini-flash-latest", "gemini-3.7-flash", "gemini-3.6-flash"],
    "reply": ["gemini-3.5-flash-lite", "gemini-flash-lite-latest", "gemini-3.1-flash-lite"],
}
_FIRST = {"research": "GEMINI_RESEARCH_MODEL", "draft": "GEMINI_MODEL", "reply": "GEMINI_REPLY_MODEL"}
_skip: set[str] = set()          # models known to be out for today in this process


def chain(kind: str = "draft") -> list[str]:
    kind = kind if kind in CHAINS else "draft"
    custom = os.getenv(f"GEMINI_{kind.upper()}_MODELS", "")
    models = [m.strip() for m in custom.split(",") if m.strip()] or list(CHAINS[kind])
    first = os.getenv(_FIRST[kind]) or (os.getenv("GEMINI_MODEL") if kind == "research" else None)
    if first:
        models = [first] + [m for m in models if m != first]
    return models


def model(kind: str = "draft") -> str:
    return chain(kind)[0]


def quota_day() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()


def _state(update=None) -> dict:
    """Today's per-model usage and exhausted models, shared by every engine process via the database."""
    import json
    from . import db
    key = f"gemini:{quota_day()}"
    try:
        with db.connect() as conn:
            st = json.loads(db.get_state(conn, key, "{}"))
            if update:
                update(st)
                db.set_state(conn, key, json.dumps(st))
            return st
    except Exception:  # the database is optional here (tests, first run); never block a call on it
        return {}


def _mark(model_id: str, why: str, detail: str = "") -> None:
    _skip.add(model_id)

    def upd(st):
        st.setdefault("out", {})[model_id] = why
        limit = re.search(r"limit:\s*(\d+)", detail)
        if limit:
            st.setdefault("limits", {})[model_id] = int(limit.group(1))
    _state(upd)


def _count(model_id: str) -> None:
    def upd(st):
        st.setdefault("calls", {})[model_id] = st.get("calls", {}).get(model_id, 0) + 1
    _state(upd)


def status() -> dict:
    """For the dashboard: today's calls per model, which are used up, and when quotas reset."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    st = _state()
    pt = ZoneInfo("America/Los_Angeles")
    reset = (datetime.now(pt) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    chains = {k: chain(k) for k in CHAINS}
    return {"day": quota_day(), "calls": st.get("calls", {}), "out": st.get("out", {}),
            "limits": st.get("limits", {}), "chains": chains, "reset_at": reset.isoformat(),
            "exhausted": {k: all(m in st.get("out", {}) for m in v) for k, v in chains.items()}}


def _pace() -> None:
    global _last_call
    rpm = float(os.getenv("GEMINI_RPM", "9"))
    wait = 60.0 / rpm - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def _daily(e: errors.ClientError) -> bool:
    msg = str(e).lower()
    return "per day" in msg or "perday" in msg or "daily" in msg or "requestsperday" in msg


class ModelsBusy(QuotaExhausted):
    """No model could answer right now: some are out of quota, the rest overloaded. Worth retrying soon."""


BUSY_WAIT = 20          # seconds before a second try at models that were overloaded (503)
last_stop: str = ""     # "quota" or "busy" after a call gave up; read by prepare to schedule a retry


def _try(model_id: str, prompt: str, cfg, schema):
    """One model: returns ("ok", result) | ("quota",) | ("unavailable",) | ("busy",)."""
    for attempt in range(3):
        _pace()
        try:
            resp = client().models.generate_content(model=model_id, contents=prompt, config=cfg)
        except errors.ClientError as e:
            if e.code == 429 and _daily(e):
                _mark(model_id, "quota", str(e))
                return ("quota",)
            if e.code == 429:
                time.sleep(20 * (attempt + 1))              # per-minute limit: back off and retry
                continue
            if e.code in (400, 403, 404):                   # retired, not on this key, or no JSON mode
                _mark(model_id, f"unavailable ({e.code})")
                return ("unavailable",)
            raise
        except errors.ServerError:
            if attempt == 1:
                return ("busy",)
            time.sleep(4)
            continue
        _count(model_id)
        if isinstance(resp.parsed, schema):
            return ("ok", resp.parsed)
        try:  # some responses arrive as text only
            return ("ok", schema.model_validate_json(resp.text or ""))
        except ValidationError:
            return ("ok", None)
    return ("busy",)


def generate(system: str, prompt: str, schema: type[T], kind: str = "draft",
             temperature: float = 0.7) -> T | None:
    """Returns a validated instance of `schema`, or None if the model's answer wasn't usable.
    Raises QuotaExhausted when every model in this kind's chain is used up for today, and
    ModelsBusy (a QuotaExhausted) when the rest of the chain was only overloaded."""
    global last_stop
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        response_mime_type="application/json",
        response_schema=schema,
        max_output_tokens=8192,
    )
    out_today = _state().get("out", {})
    reasons: dict[str, str] = {}
    busy: list[str] = []
    for model_id in chain(kind):
        if model_id in _skip or model_id in out_today:
            reasons[model_id] = out_today.get(model_id, "skipped")
            continue
        result = _try(model_id, prompt, cfg, schema)
        if result[0] == "ok":
            return result[1]
        reasons[model_id] = result[0]
        if result[0] == "busy":
            busy.append(model_id)
    if busy:                                               # overloads are usually brief: one more round
        time.sleep(BUSY_WAIT)
        for model_id in busy:
            result = _try(model_id, prompt, cfg, schema)
            if result[0] == "ok":
                return result[1]
            reasons[model_id] = result[0]
    detail = ", ".join(f"{m.replace('gemini-', '')}: {r}" for m, r in reasons.items())
    if any(r == "busy" for r in reasons.values()):
        last_stop = "busy"
        raise ModelsBusy(f"no {kind} model available right now ({detail}); it will retry later")
    if any(r == "quota" for r in reasons.values()):
        last_stop = "quota"
        raise QuotaExhausted(f"every {kind} model is out of free quota for today ({detail})")
    return None


def load_prompt(name: str) -> str:
    return (config.ROOT / "prompts" / name).read_text()
