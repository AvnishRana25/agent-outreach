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


@lru_cache
def settings() -> dict:
    return _load_yaml("settings.yaml")


@lru_cache
def profile() -> dict:
    return _load_yaml("profile.yaml")


def segment(name: str) -> dict:
    segs = settings()["segments"]
    if name not in segs:
        raise KeyError(f"Unknown segment '{name}'. Known: {', '.join(segs)}")
    return segs[name]


def inboxes() -> list[dict]:
    """Inboxes from settings.yaml; passwords come from the env var each one names."""
    result = []
    for box in settings().get("inboxes", []):
        box = dict(box)
        box["password"] = os.getenv(box.get("password_env", ""), "")
        result.append(box)
    return result


def inbox(email: str) -> dict:
    for box in inboxes():
        if box["email"].lower() == email.lower():
            return box
    raise KeyError(f"Inbox {email} is not configured in settings.yaml")


def db_path() -> Path:
    return Path(os.getenv("OUTREACH_DB", DATA_DIR / "outreach.db"))
