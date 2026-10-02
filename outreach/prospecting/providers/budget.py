"""Guard rails for the paid-tier email finders (Prospeo, Hunter, Skrapp) on their free plans.

Every lookup goes through `lookup()`, which:
  1. answers from memory when this provider was already asked about this person (never pay twice);
  2. checks the provider isn't paused (wrong key, out of credits, rate-limited);
  3. reserves one call against the budget, atomically, so parallel bulk workers can't overspend:
       - monthly: at most `use_pct` of the plan's free credits (default 80%, the rest stays untouched),
       - daily: the month's remaining budget spread evenly over the remaining days (and `daily_max`),
       - per run: at most `per_run_max` calls in one bulk run;
  4. records the call, and pauses the provider on 401/403 (key), 402 (credits gone) or 429 (too fast).
Every call counts against the budget, found or not: some plans charge for misses, and we would rather
stop early than surprise you. `sync_balances()` reads the real remaining credits from each provider's
free account endpoint and stops a provider that has less left than the reserve.
"""
from __future__ import annotations

import calendar
import json
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

from ... import config as base_config, db

# Free plans at the time of writing; check yours and override in settings.yaml (provider_budget).
DEFAULTS = {
    "prospeo": {"plan_monthly": 75, "use_pct": 80, "daily_max": 5, "per_run_max": 10, "min_interval_s": 1.0},
    "hunter": {"plan_monthly": 25, "use_pct": 80, "daily_max": 2, "per_run_max": 4, "min_interval_s": 1.0},
    "skrapp": {"plan_monthly": 50, "use_pct": 80, "daily_max": 3, "per_run_max": 6, "min_interval_s": 1.0},
}
ENV_PLAN = {"prospeo": "PROSPEO_MONTHLY_LIMIT", "hunter": "HUNTER_MONTHLY_LIMIT", "skrapp": "SKRAPP_MONTHLY_LIMIT"}
REMEMBER_DAYS = 120          # don't ask the same provider about the same person again for this long
_lock = threading.Lock()     # bulk runs use threads; the reservation below must be one at a time
_last_call: dict[str, float] = {}
_run_calls: dict[str, int] = {}


def settings(provider: str) -> dict:
    s = dict(DEFAULTS.get(provider, DEFAULTS["skrapp"]))
    try:
        s.update((base_config.settings().get("provider_budget") or {}).get(provider) or {})
    except Exception:
        pass
    env = os.getenv(ENV_PLAN.get(provider, ""), "")
    if env.isdigit():
        s["plan_monthly"] = int(env)
    return s


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _month_start(now: datetime) -> str:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def _calls_since(conn, provider: str, since: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM provider_credits WHERE provider=? AND request_timestamp >= ?",
                        (provider, since)).fetchone()[0]


def status(provider: str, conn=None) -> dict:
    """Budget numbers for one provider (also shown on the dashboard)."""
    if conn is None:
        with db.connect() as own:
            return status(provider, own)
    s, now = settings(provider), _now()
    usable = math.floor(s["plan_monthly"] * s["use_pct"] / 100)
    month = _calls_since(conn, provider, _month_start(now))
    today = _calls_since(conn, provider, now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat())
    days_left = calendar.monthrange(now.year, now.month)[1] - now.day + 1
    left = max(0, usable - month)
    daily = min(s["daily_max"], math.ceil((left + today) / days_left))   # spread evenly over the month
    paused = json.loads(db.get_state(conn, f"provider_pause:{provider}") or "{}")
    if paused and paused.get("until", "") <= now.isoformat():
        paused = {}
    balance = json.loads(db.get_state(conn, f"provider_balance:{provider}") or "{}")
    return {"provider": provider, "plan_monthly": s["plan_monthly"], "usable_monthly": usable,
            "used_month": month, "used_today": today, "daily_cap": daily, "left_month": left,
            "paused": paused.get("why", ""), "paused_until": paused.get("until", ""),
            "balance": balance.get("remaining"), "balance_checked": balance.get("at", "")}


def _pause(conn, provider: str, why: str, hours: float | None = None) -> None:
    now = _now()
    if hours is None:  # until the start of next month
        last = calendar.monthrange(now.year, now.month)[1]
        until = (now.replace(day=last, hour=23, minute=59, second=59) + timedelta(seconds=1)).isoformat()
    else:
        until = (now + timedelta(hours=hours)).isoformat()
    db.set_state(conn, f"provider_pause:{provider}", json.dumps({"why": why, "until": until}))


def _person_key(first: str, last: str, domain: str) -> str:
    return f"{first.strip().lower()}|{last.strip().lower()}|{domain.strip().lower().removeprefix('www.')}"


def remembered(provider: str, key: str):
    """The earlier answer for this person, or None if never asked (or asked too long ago)."""
    since = (_now() - timedelta(days=REMEMBER_DAYS)).isoformat()
    with db.connect() as conn:
        row = conn.execute("SELECT result FROM provider_credits WHERE provider=? AND lookup_key=? AND request_timestamp >= ? "
                           "AND result NOT LIKE 'error%' AND result NOT LIKE 'http_%' AND result != 'reserved' "
                           "ORDER BY id DESC LIMIT 1", (provider, key, since)).fetchone()
    return row[0] if row else None


def reserve(provider: str, key: str, prospect_id: int | None = None) -> tuple[int | None, str]:
    """(ledger row id, '') when a call may be made now; (None, reason) otherwise."""
    with _lock:
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")   # one reservation at a time, across processes too
            st = status(provider, conn)
            if st["paused"]:
                return None, f"paused: {st['paused']}"
            if st["left_month"] <= 0:
                return None, f"monthly budget used ({st['used_month']}/{st['usable_monthly']})"
            if st["used_today"] >= st["daily_cap"]:
                return None, f"daily budget used ({st['used_today']}/{st['daily_cap']})"
            if _run_calls.get(provider, 0) >= settings(provider)["per_run_max"]:
                return None, "per-run budget used"
            if st["balance"] is not None and st["balance"] <= max(1, st["plan_monthly"] - st["usable_monthly"]) \
                    and st["balance_checked"][:7] == _now().isoformat()[:7]:
                return None, f"only {st['balance']} credits left on the account (kept in reserve)"
            cur = conn.execute("INSERT INTO provider_credits (provider, credits_used, request_timestamp, result, "
                               "prospect_id, lookup_key) VALUES (?,1,?,'reserved',?,?)",
                               (provider, _now().isoformat(timespec="seconds"), prospect_id, key))
            _run_calls[provider] = _run_calls.get(provider, 0) + 1
            return cur.lastrowid, ""


def finish(row_id: int, provider: str, result: str, http_status: int | None = None) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE provider_credits SET result=? WHERE id=?", (result[:200], row_id))
        if http_status in (401, 403):
            _pause(conn, provider, f"the API key was rejected (HTTP {http_status}); check it in .env", hours=24 * 30)
        elif http_status == 402:
            _pause(conn, provider, "the account is out of credits")
        elif http_status == 429:
            _pause(conn, provider, "rate-limited by the provider; trying again in an hour", hours=1)


def new_run() -> None:
    """Start counting per-run calls from zero (called at the start of each bulk run)."""
    _run_calls.clear()


def lookup(provider_obj, first: str, last: str, company: str, domain: str, prospect_id: int | None = None):
    """The only way the pipeline asks a provider. Returns a ProviderResult (status explains a skip)."""
    from ..models import ProviderResult
    name = provider_obj.name
    if not provider_obj.is_available():
        return ProviderResult(provider=name, status="unconfigured")
    key = _person_key(first, last, domain)
    earlier = remembered(name, key)
    if earlier is not None:
        if earlier.startswith("found:"):
            email, _, conf = earlier[6:].partition("|")
            return ProviderResult(provider=name, email=email, confidence=int(conf or 80), status="remembered")
        return ProviderResult(provider=name, status="remembered_not_found")
    row_id, why = reserve(name, key, prospect_id)
    if row_id is None:
        return ProviderResult(provider=name, status=f"skipped: {why}")
    wait = settings(name)["min_interval_s"] - (time.monotonic() - _last_call.get(name, 0))
    if wait > 0:
        time.sleep(wait)
    _last_call[name] = time.monotonic()
    try:
        res = provider_obj.find_email(first, last, company, domain, prospect_id=prospect_id)
    except Exception as e:  # a bug in one adapter must not stop the pipeline
        res = ProviderResult(provider=name, status=f"error_{type(e).__name__}")
    code = int(res.status[5:]) if res.status.startswith("http_") and res.status[5:].isdigit() else None
    result = f"found:{res.email}|{res.confidence}" if res.email else res.status
    finish(row_id, name, result, code)
    return res


# --------------------------------------------------------------------------- real balances (free calls)
def _hunter_balance(key: str) -> int | None:
    r = requests.get("https://api.hunter.io/v2/account", headers={"X-API-KEY": key}, timeout=15)
    if r.status_code != 200:
        return None
    searches = ((r.json().get("data") or {}).get("requests") or {}).get("searches") or {}
    if "available" in searches and "used" in searches:
        return int(searches["available"]) - int(searches["used"])
    return None


def _prospeo_balance(key: str) -> int | None:
    r = requests.get("https://api.prospeo.io/account-information", headers={"X-KEY": key}, timeout=15)
    if r.status_code != 200:
        return None
    resp = r.json().get("response") or {}
    val = resp.get("remaining_credits", resp.get("credits"))
    return int(val) if val is not None else None


BALANCE = {"hunter": ("HUNTER_API_KEY", _hunter_balance), "prospeo": ("PROSPEO_API_KEY", _prospeo_balance)}


def sync_balances() -> dict:
    """Read each provider's real remaining credits (these account endpoints cost nothing)."""
    out = {}
    for name, (env, fn) in BALANCE.items():
        key = os.getenv(env, "").strip()
        if not key:
            continue
        try:
            remaining = fn(key)
        except (requests.RequestException, ValueError, TypeError):
            remaining = None
        if remaining is not None:
            with db.connect() as conn:
                db.set_state(conn, f"provider_balance:{name}",
                             json.dumps({"remaining": remaining, "at": _now().isoformat(timespec="seconds")}))
        out[name] = remaining
    return out


def summary() -> dict:
    return {p: status(p) for p in DEFAULTS}
