"""Structured logging with GitHub Actions context, metrics, and secret masking.

Guarantees:
- Emits machine-readable structured JSON or clean terminal logs
- Tracks GitHub Actions run context (workflow, run_id, job)
- Tracks lead_id, message_id, mode, operation, result, duration_ms, error_type
- Masks sensitive credentials, tokens, passwords, and API keys.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from . import config

SECRET_PATTERNS = [
    re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]{8,}", re.I),
    re.compile(r"(password[=:\s]+)['\"]?[^'\"\s&]+['\"]?", re.I),
    re.compile(r"(api[_-]?key[=:\s]+)['\"]?[^'\"\s&]+['\"]?", re.I),
    re.compile(r"(token[=:\s]+)['\"]?[^'\"\s&]+['\"]?", re.I),
    re.compile(r"(secret[=:\s]+)['\"]?[^'\"\s&]+['\"]?", re.I),
    re.compile(r"(ghp_[A-Za-z0-9]{20,})", re.I),
    re.compile(r"(AIza[0-9A-Za-z-_]{35})", re.I),
]


def mask_secrets(val: Any) -> str:
    """Mask credentials and sensitive strings."""
    if not isinstance(val, str):
        val = str(val)
    masked = val
    for p in SECRET_PATTERNS:
        masked = p.sub(r"\1***REDACTED***", masked)
    return masked


def get_execution_context() -> dict[str, str]:
    """Extract runtime execution context (local vs GitHub Actions runner)."""
    return {
        "env": config.env(),
        "github_run_id": os.getenv("GITHUB_RUN_ID", ""),
        "github_workflow": os.getenv("GITHUB_WORKFLOW", ""),
        "github_job": os.getenv("GITHUB_JOB", ""),
        "github_run_number": os.getenv("GITHUB_RUN_NUMBER", ""),
        "github_actor": os.getenv("GITHUB_ACTOR", ""),
    }


def log_event(
    operation: str,
    result: str,
    lead_id: int | None = None,
    message_id: int | None = None,
    mode: str | None = None,
    duration_ms: float | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
    level: str = "INFO",
    **extra: Any
) -> dict[str, Any]:
    """Emit a structured log entry."""
    entry: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "level": level.upper(),
        "operation": operation,
        "result": result,
    }
    if lead_id is not None:
        entry["lead_id"] = lead_id
    if message_id is not None:
        entry["message_id"] = message_id
    if mode is not None:
        entry["mode"] = mode
    if duration_ms is not None:
        entry["duration_ms"] = round(duration_ms, 2)
    if error_type:
        entry["error_type"] = error_type
    if error_message:
        entry["error_message"] = mask_secrets(error_message)

    # Add environment context
    ctx = get_execution_context()
    for k, v in ctx.items():
        if v:
            entry[k] = v

    # Add extra fields (with secret masking)
    for k, v in extra.items():
        entry[k] = mask_secrets(v) if isinstance(v, (str, bytes)) else v

    # Output formatting:
    # If in CI/GitHub Actions, output compact JSON line for observability
    # Otherwise output clean readable format
    if os.getenv("CI") or os.getenv("GITHUB_ACTIONS") or config.is_production():
        print(json.dumps(entry), file=sys.stderr if level in ("ERROR", "CRITICAL") else sys.stdout)
    else:
        dur_str = f" in {duration_ms:.1f}ms" if duration_ms is not None else ""
        lead_str = f" lead_id={lead_id}" if lead_id else ""
        msg_str = f" msg_id={message_id}" if message_id else ""
        mode_str = f" [{mode}]" if mode else ""
        err_str = f" err={error_type}: {mask_secrets(error_message)}" if error_type else ""
        line = f"[{entry['timestamp']}] {level:<5} {operation}{mode_str} -> {result}{dur_str}{lead_str}{msg_str}{err_str}"
        print(line, file=sys.stderr if level in ("ERROR", "CRITICAL") else sys.stdout)

    return entry


@contextmanager
def timed_operation(operation: str, lead_id: int | None = None, message_id: int | None = None, mode: str | None = None, **extra: Any):
    """Context manager to measure and log operation duration and status."""
    start = time.perf_counter()
    try:
        yield
        dur = (time.perf_counter() - start) * 1000.0
        log_event(operation, "success", lead_id=lead_id, message_id=message_id, mode=mode, duration_ms=dur, **extra)
    except Exception as e:
        dur = (time.perf_counter() - start) * 1000.0
        log_event(
            operation,
            "error",
            lead_id=lead_id,
            message_id=message_id,
            mode=mode,
            duration_ms=dur,
            error_type=type(e).__name__,
            error_message=str(e),
            level="ERROR",
            **extra
        )
        raise
