"""Loads settings.yaml, profile.yaml and .env into one place."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"

load_dotenv(ROOT / ".env")


def _load_yaml(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        example = CONFIG_DIR / (path.stem + ".example.yaml")
        if example.exists():
            path = example
        else:
            raise FileNotFoundError(f"Missing {path}")
    with path.open() as f:
        return yaml.safe_load(f) or {}


def _fill_missing(user: dict, default: dict) -> dict:
    """Your values always win; keys you don't have yet (new features) come from the example."""
    for k, v in default.items():
        if k not in user:
            user[k] = v
        elif isinstance(v, dict) and isinstance(user[k], dict):
            _fill_missing(user[k], v)
    return user


@lru_cache
def settings() -> dict:
    user = _load_yaml("settings.yaml")
    example = CONFIG_DIR / "settings.example.yaml"
    if (CONFIG_DIR / "settings.yaml").exists() and example.exists():
        with example.open() as f:
            default = yaml.safe_load(f) or {}
        # Only fill segments you have; don't resurrect ones you deleted.
        default_segments = default.pop("segments", {})
        for name, seg in (user.get("segments") or {}).items():
            _fill_missing(seg, default_segments.get(name, {}))
        _fill_missing(user, default)
    return user


@lru_cache
def profile() -> dict:
    return _load_yaml("profile.yaml")


def segment(name: str) -> dict:
    segs = settings()["segments"]
    if name not in segs:
        raise KeyError(f"Unknown segment '{name}'. Known: {', '.join(segs)}")
    return segs[name]


def inboxes(include_disabled: bool = False) -> list[dict]:
    """Inboxes from settings.yaml (enabled ones unless asked); passwords come from the env var each one names."""
    result = []
    for box in settings().get("inboxes", []):
        if not include_disabled and not box.get("enabled", True):
            continue
        box = dict(box)
        box["password"] = os.getenv(box.get("password_env", ""), "")
        result.append(box)
    return result


def inbox(email: str) -> dict:
    for box in inboxes(include_disabled=True):
        if box["email"].lower() == email.lower():
            return box
    raise KeyError(f"Inbox {email} is not configured in settings.yaml")


def db_path() -> Path:
    return Path(os.getenv("OUTREACH_DB", DATA_DIR / "outreach.db"))
