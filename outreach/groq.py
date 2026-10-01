"""Groq as a backup when Gemini can't answer (free quota used up, or every model overloaded).

Groq's free tier has its own daily limits per model, separate from Gemini's, so with both keys the
engine rarely stops for the day. It speaks the OpenAI chat API; we ask for a JSON object that
matches the same Pydantic schema and validate it exactly like a Gemini answer.
Get a free key at console.groq.com/keys and put GROQ_API_KEY=... in .env. Without the key this is off.
Override the models with GROQ_MODELS (comma-separated); free-tier limits and model names change,
so check console.groq.com/settings/limits.
"""
from __future__ import annotations

import json
import os
import time

import requests
from pydantic import BaseModel, ValidationError

API = "https://api.groq.com/openai/v1/chat/completions"
MODELS = ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "openai/gpt-oss-20b", "llama-3.1-8b-instant"]
_last_call = 0.0


def enabled() -> bool:
    return bool(os.getenv("GROQ_API_KEY"))


def models() -> list[str]:
    custom = [m.strip() for m in os.getenv("GROQ_MODELS", "").split(",") if m.strip()]
    return [f"groq/{m}" for m in (custom or MODELS)]


def _pace() -> None:
    global _last_call
    wait = 60.0 / float(os.getenv("GROQ_RPM", "20")) - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def _daily(r: requests.Response) -> bool:
    text = r.text.lower()
    return "per day" in text or "(rpd)" in text or "(tpd)" in text or "daily" in text


def _instructions(schema: type[BaseModel]) -> str:
    return ("\n\nAnswer with one JSON object only, no other text. It must match this JSON schema exactly "
            "(same field names and types):\n" + json.dumps(schema.model_json_schema()))


def try_model(model_id: str, system: str, prompt: str, schema: type[BaseModel], temperature: float):
    """Same contract as llm._try: ("ok", result|None) | ("quota",) | ("unavailable",) | ("busy",)."""
    name = model_id.removeprefix("groq/")
    body = {"model": name, "temperature": temperature, "max_tokens": 8192,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system + _instructions(schema)},
                         {"role": "user", "content": prompt}]}
    for attempt in range(3):
        _pace()
        try:
            r = requests.post(API, json=body, timeout=90,
                              headers={"Authorization": f"Bearer {os.getenv('GROQ_API_KEY', '')}"})
        except requests.RequestException as e:
            if attempt == 2:
                return ("busy", f"can't reach Groq: {e.__class__.__name__}")
            time.sleep(4)
            continue
        if r.status_code == 429:
            if _daily(r):
                return ("quota", r.text[:300])
            time.sleep(min(float(r.headers.get("retry-after", 0) or 0) or 10 * (attempt + 1), 60))
            continue
        if r.status_code in (400, 401, 403, 404) and "json_validate_failed" not in r.text:
            return ("unavailable", f"{r.status_code}" + (" wrong API key" if r.status_code == 401 else ""))
        if r.status_code >= 500:
            if attempt == 2:
                return ("busy",)
            time.sleep(4)
            continue
        try:
            text = r.json()["choices"][0]["message"]["content"] if r.ok else ""
            return ("ok", schema.model_validate_json(text))
        except (ValueError, KeyError, IndexError, ValidationError):
            if attempt < 2:   # the model broke the format: ask once more
                continue
            return ("ok", None)
    return ("busy",)
