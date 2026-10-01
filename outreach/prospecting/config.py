"""Configuration and scoring weights for the prospecting pipeline."""
from __future__ import annotations

import os
from .. import config as base_config

# Daily targets
DEFAULT_DAILY_VERIFIED_TARGET = 38

# Scoring weights (0 - 100)
DEFAULT_WEIGHTS = {
    "exact_public_match": 45,
    "confirmed_company_pattern": 25,
    "multiple_employees_pattern": 10,
    "valid_mx": 10,
    "name_identity_match": 10,
    "catch_all_penalty": -15,
    "unverified_pattern_penalty": -20,
    "conflicting_patterns_penalty": -10,
    "role_address_penalty": -30,
}

# Confidence thresholds
THRESHOLD_VERIFIED = 90
THRESHOLD_HIGH = 75
THRESHOLD_REVIEW = 55

# Provider limits and thresholds
CONFIDENCE_TO_SKIP_PROVIDERS = 75
DEFAULT_PROSPEO_MONTHLY_LIMIT = 100
DEFAULT_HUNTER_MONTHLY_LIMIT = 50
DEFAULT_SKRAPP_MONTHLY_LIMIT = 50


def get_weights() -> dict[str, int]:
    """Return scoring weights with settings/env overrides if present."""
    weights = dict(DEFAULT_WEIGHTS)
    try:
        custom = base_config.settings().get("prospecting_scoring", {})
        for k, v in custom.items():
            if k in weights and isinstance(v, (int, float)):
                weights[k] = int(v)
    except Exception:
        pass
    return weights


def daily_verified_target() -> int:
    val = os.getenv("DAILY_VERIFIED_TARGET")
    if val:
        try:
            return int(val)
        except ValueError:
            pass
    try:
        desk = base_config.settings().get("prospecting_desk") or {}   # "prospecting" is the list of lead sources
        return int(desk.get("daily_verified_target", DEFAULT_DAILY_VERIFIED_TARGET))
    except Exception:
        return DEFAULT_DAILY_VERIFIED_TARGET


def provider_monthly_limit(provider: str) -> int:
    env_map = {
        "prospeo": "PROSPEO_MONTHLY_LIMIT",
        "hunter": "HUNTER_MONTHLY_LIMIT",
        "skrapp": "SKRAPP_MONTHLY_LIMIT",
    }
    defaults = {
        "prospeo": DEFAULT_PROSPEO_MONTHLY_LIMIT,
        "hunter": DEFAULT_HUNTER_MONTHLY_LIMIT,
        "skrapp": DEFAULT_SKRAPP_MONTHLY_LIMIT,
    }
    env_name = env_map.get(provider.lower())
    if env_name and os.getenv(env_name):
        try:
            return int(os.environ[env_name])
        except ValueError:
            pass
    return defaults.get(provider.lower(), 50)
