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
    env_override = os.getenv("SETTINGS_YAML") if name.startswith("settings") else (
        os.getenv("PROFILE_YAML") if name.startswith("profile") else None
    )
    if env_override and env_override.strip():
        return yaml.safe_load(env_override) or {}
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
        if user.get("sources_from_example", True):
            _add_new_sources(user, default)
        _fill_missing(user, default)
    return user


def _add_new_sources(user: dict, default: dict) -> None:
    """New kinds of lead source reach an existing settings.yaml without copying anything: community boards
    you don't have yet (by name), and prospecting jobs of a source type you don't use at all. Sources you
    already configured are left exactly as they are. Turn off with `sources_from_example: false`."""
    if isinstance(user.get("community"), list):
        have = {c.get("name") for c in user["community"]}
        user["community"] += [c for c in default.get("community") or [] if c.get("name") not in have]
    if isinstance(user.get("prospecting"), list):
        types = {j.get("source") for j in user["prospecting"]}
        segs = set((user.get("segments") or {}))
        user["prospecting"] += [j for j in default.get("prospecting") or []
                                if j.get("source") not in types and j.get("segment") in segs]


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


def env() -> str:
    """Current environment ('local' or 'production'). Default: 'local'."""
    return os.getenv("OUTREACH_ENV", "local").strip().lower()


def is_production() -> bool:
    """True when running with OUTREACH_ENV=production."""
    return env() == "production"


def allocation_settings() -> dict:
    """Retrieve daily allocation settings with environment overrides.
    
    The 28 limit applies strictly as a daily ceiling for NEW initial emails.
    Follow-ups are unconstrained and do not count against or block the 28 new email cap.
    """
    s = settings().get("allocation", {})
    send_limit = int(os.getenv("DAILY_NEW_LIMIT", os.getenv("DAILY_SEND_LIMIT", s.get("daily_new_limit", s.get("daily_send_limit", 28)))))
    freelance_limit = int(os.getenv("DAILY_FREELANCE_NEW_LIMIT", s.get("daily_freelance_new_limit", 16)))
    internship_limit = int(os.getenv("DAILY_INTERNSHIP_NEW_LIMIT", s.get("daily_internship_new_limit", 12)))
    followup_limit = int(os.getenv("DAILY_FOLLOWUP_LIMIT", s.get("daily_followup_limit", 0)))
    realloc_env = os.getenv("ALLOW_UNUSED_QUOTA_REALLOCATION")
    if realloc_env is not None:
        realloc = realloc_env.strip().lower() in ("true", "1", "yes")
    else:
        realloc = bool(s.get("allow_unused_quota_reallocation", True))

    return {
        "daily_send_limit": send_limit,
        "daily_new_limit": send_limit,
        "daily_freelance_new_limit": freelance_limit,
        "daily_internship_new_limit": internship_limit,
        "daily_followup_limit": followup_limit,
        "allow_unused_quota_reallocation": realloc,
    }


def is_sending_enabled() -> bool:
    """Global send kill-switch.
    
    In production (OUTREACH_ENV=production): Fail-safe behavior requires OUTREACH_SENDING_ENABLED='true'.
    If missing, malformed, or anything other than 'true', sending is strictly blocked.
    In local development: Defaults to True unless OUTREACH_SENDING_ENABLED is set to 'false'/'0'/'no'
    or settings.yaml disables it.
    """
    env_val = os.getenv("OUTREACH_SENDING_ENABLED")
    if is_production():
        if env_val is None:
            return False
        return env_val.strip().lower() in ("true", "1", "yes")
    
    if env_val is not None:
        return env_val.strip().lower() in ("true", "1", "yes")
    return bool(settings().get("sending", {}).get("sending_enabled", True))


def is_dry_run() -> bool:
    """Production-safe dry-run flag. True if OUTREACH_DRY_RUN is set to true/1/yes."""
    val = os.getenv("OUTREACH_DRY_RUN")
    if val is None:
        return False
    return val.strip().lower() in ("true", "1", "yes")


def max_bounce_rate() -> float:
    """Maximum allowable bounce rate before pausing outbound dispatch (default: 0.05 / 5%)."""
    val = os.getenv("MAX_BOUNCE_RATE")
    if val is not None:
        try:
            return float(val)
        except ValueError:
            pass
    return float(settings().get("sending", {}).get("max_bounce_rate", 0.05))


def min_bounce_sample() -> int:
    """Minimum sample size of sent emails before bounce rate triggers an automatic pause (default: 20)."""
    val = os.getenv("MIN_BOUNCE_SAMPLE")
    if val is not None:
        try:
            return int(val)
        except ValueError:
            pass
    return int(settings().get("sending", {}).get("min_bounce_sample", 20))

