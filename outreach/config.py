"""Loads settings.yaml, profile.yaml and .env into one place."""
from __future__ import annotations

import os
import re
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


# Template text that must never reach a prospect: "github.com/YOUR-GITHUB", "[First Name]", "{company}".
PLACEHOLDER = re.compile(r"YOUR[-_ ]?[A-Z]{3,}|(?i:\[(?:first[ _]?name|last[ _]?name|name|company|your [^\]]+)\]|"
                         r"\{(?:first_?name|company|name)\})")  # upper-case YOUR- only: "your team" is fine
PLACEHOLDER_STRICT = re.compile(r"YOUR[-_ ]?[A-Z]{3,}(?:[-_ ][A-Z]{3,})*")  # the profile's own markers are upper case


def placeholders() -> list[str]:
    """Unfilled template text in profile.yaml's signatures and booking link, as readable lines."""
    try:
        p = _load_yaml("profile.yaml")
    except (OSError, yaml.YAMLError):
        return []
    found = []
    for name, text in (p.get("signatures") or {}).items():
        for m in PLACEHOLDER_STRICT.finditer(str(text)):
            found.append(f"signature '{name}' still says {m.group(0)}")
    if PLACEHOLDER_STRICT.search(str(p.get("calendar_link", ""))):
        found.append(f"calendar_link is still {p['calendar_link']}")
    return found


def check() -> str:
    """'' if settings.yaml and profile.yaml read fine, else a plain-English description of the mistake.
    Reads the files fresh (not the cached copy), so it sees an edit made a moment ago."""
    for name in ("settings.yaml", "profile.yaml"):
        try:
            _load_yaml(name)
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            where = f" near line {mark.line + 1}" if mark else ""
            return (f"config/{name} has a formatting mistake{where}: {getattr(e, 'problem', None) or e}. "
                    "Usually a heading line went missing or the indentation changed while editing. "
                    "Compare that spot with the same part of the .example.yaml file.")
        except OSError as e:
            return f"can't read config/{name}: {e}"
    return ""


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
