"""Exponential backoff retry engine for transient production failures.

Distinguishes between:
- Transient failures (HTTP 429, 5xx, network timeout, DB locks, temporary provider drops): RETRIED
- Permanent failures (hard bounce, 401/403 auth, 400 bad request, syntax/validation): RAISED IMMEDIATELY
"""
from __future__ import annotations

import functools
import smtplib
import socket
import sqlite3
import time
from typing import Any, Callable, TypeVar
import requests

F = TypeVar("F", bound=Callable[..., Any])

PERMANENT_HTTP_STATUS_CODES = {400, 401, 403, 404, 422}
TRANSIENT_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}


def is_transient_error(e: BaseException) -> bool:
    """Classify whether an exception is safe and transient to retry."""
    # Permanent mail errors
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return False
    if isinstance(e, smtplib.SMTPResponseException):
        if 500 <= e.smtp_code <= 559:
            return False  # Permanent mail rejection (mailbox unavailable, etc)
        if 400 <= e.smtp_code <= 499:
            return True   # Transient mailbox busy / greylisting

    # Permanent HTTP errors
    if isinstance(e, requests.HTTPError) and e.response is not None:
        status = e.response.status_code
        if status in PERMANENT_HTTP_STATUS_CODES:
            return False
        if status in TRANSIENT_HTTP_STATUS_CODES:
            return True

    # Transient Network errors
    if isinstance(e, (
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
        socket.timeout,
        ConnectionResetError,
        ConnectionRefusedError,
        BrokenPipeError
    )):
        return True

    # Transient Database errors
    if isinstance(e, sqlite3.OperationalError):
        err_msg = str(e).lower()
        if "locked" in err_msg or "busy" in err_msg or "timeout" in err_msg:
            return True

    return False


def with_retry(
    func: Callable[..., Any],
    *args: Any,
    max_retries: int = 3,
    initial_delay: float = 0.5,
    backoff_factor: float = 2.0,
    max_delay: float = 10.0,
    operation_name: str = "",
    **kwargs: Any
) -> Any:
    """Execute a callable with exponential backoff on transient errors."""
    delay = initial_delay
    last_err: BaseException | None = None
    op = operation_name or getattr(func, "__name__", "operation")

    for attempt in range(1, max_retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_err = e
            if not is_transient_error(e) or attempt == max_retries:
                raise
            time.sleep(delay)
            delay = min(max_delay, delay * backoff_factor)

    if last_err is not None:
        raise last_err


def retryable(
    max_retries: int = 3,
    initial_delay: float = 0.5,
    backoff_factor: float = 2.0,
    max_delay: float = 10.0,
    operation_name: str = ""
) -> Callable[[F], F]:
    """Decorator for functions that should be retried on transient failures."""
    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return with_retry(
                fn,
                *args,
                max_retries=max_retries,
                initial_delay=initial_delay,
                backoff_factor=backoff_factor,
                max_delay=max_delay,
                operation_name=operation_name or fn.__name__,
                **kwargs
            )
        return wrapper  # type: ignore
    return decorator
