"""Gemini wrapper: JSON output validated against a Pydantic schema, paced for the free tier.

Free-tier limits change without notice (check yours in AI Studio -> Rate limits). Each lead
uses 2 calls (research + draft), so 38 leads/day is ~80-100 calls plus reply triage.
Google retires model names for new keys (the 2.5 models already return 404 for them), so the
defaults are the 3.5 family; override with GEMINI_MODEL / GEMINI_RESEARCH_MODEL / GEMINI_REPLY_MODEL.
"""
from __future__ import annotations

import os
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


def model(kind: str = "draft") -> str:
    if kind == "research":
        return os.getenv("GEMINI_RESEARCH_MODEL", os.getenv("GEMINI_MODEL", "gemini-3.5-flash"))
    if kind == "reply":
        return os.getenv("GEMINI_REPLY_MODEL", "gemini-3.5-flash-lite")
    return os.getenv("GEMINI_MODEL", "gemini-3.5-flash")


def _pace() -> None:
    global _last_call
    rpm = float(os.getenv("GEMINI_RPM", "9"))
    wait = 60.0 / rpm - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def generate(system: str, prompt: str, schema: type[T], kind: str = "draft",
             temperature: float = 0.7) -> T | None:
    """Returns a validated instance of `schema`, or None if the model gave nothing usable."""
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        response_mime_type="application/json",
        response_schema=schema,
        max_output_tokens=8192,
    )
    for attempt in range(4):
        _pace()
        try:
            resp = client().models.generate_content(model=model(kind), contents=prompt, config=cfg)
        except errors.ClientError as e:
            if e.code == 404:
                raise SystemExit(f"Gemini model '{model(kind)}' is not available to this key: {e.message}\n"
                                 "Set GEMINI_MODEL / GEMINI_RESEARCH_MODEL / GEMINI_REPLY_MODEL in .env to a "
                                 "model listed in AI Studio.") from e
            if e.code == 429:
                msg = str(e).lower()
                if "per day" in msg or "perday" in msg or "daily" in msg:
                    raise QuotaExhausted(str(e)) from e
                time.sleep(30 * (attempt + 1))  # per-minute limit: back off and retry
                continue
            raise
        except errors.ServerError:
            time.sleep(10 * (attempt + 1))
            continue
        if isinstance(resp.parsed, schema):
            return resp.parsed
        try:  # some responses arrive as text only
            return schema.model_validate_json(resp.text or "")
        except ValidationError:
            return None
    return None


def load_prompt(name: str) -> str:
    return (config.ROOT / "prompts" / name).read_text()
