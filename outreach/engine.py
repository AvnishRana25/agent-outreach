"""The background engine: one `tick` every 5 minutes does whatever is due, so you never need the terminal.

  every tick      apply dashboard actions -> send due emails -> push a fresh dashboard snapshot
  every 20 min    read replies (stop sequences, triage, Telegram)
  every 30 min    community [Hiring] posts
  daily 07:30     prepare: find companies -> research -> draft (Mon-Sat, your time zone)
  daily 09:45     Ad Library searches and LinkedIn tasks for the dashboard

Long jobs (prepare, community) run as separate background processes, so sending and syncing
keep going while they work. `install` registers the tick with macOS launchd (Linux: prints a
crontab line) and, when the dashboard is configured, keeps the local dashboard running too.
"""
from __future__ import annotations

import fcntl
import json
import os
import plistlib
import subprocess
import sys
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, db

JOBS = ("prepare", "community")
LABEL = "com.agent-outreach"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _local_now() -> datetime:
    tz = ZoneInfo(config.settings().get("sending", {}).get("home_timezone", "Asia/Kolkata"))
    return _now().astimezone(tz)


@contextmanager
def lock(name: str, wait: bool = False):
    """Yields True if this process holds the lock, False if another process already has it."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    f = open(config.DATA_DIR / f"{name}.lock", "w")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            held = True
        except BlockingIOError:
            held = False
        yield held
    finally:
        f.close()


def is_running(job: str) -> bool:
    with lock(job) as free:
        return not free


# --------------------------------------------------------------------------- job bookkeeping
def job_state(conn, job: str) -> dict:
    try:
        return json.loads(db.get_state(conn, f"job:{job}", "{}"))
    except json.JSONDecodeError:
        return {}


def _set_job(job: str, **fields) -> None:
    with db.connect() as conn:
        st = job_state(conn, job)
        st.update(fields)
        db.set_state(conn, f"job:{job}", json.dumps(st))


def run_job(job: str, fn) -> None:
    """Run `fn` as `job` under its lock, recording start, finish, result and error for the dashboard."""
    with lock(job) as held:
        if not held:
            print(f"{job} is already running")
            return
        _set_job(job, started=_now().isoformat(timespec="seconds"), finished=None, error=None, running=True)
        try:
            result = fn()
            _set_job(job, finished=_now().isoformat(timespec="seconds"), result=str(result)[:500], running=False)
        except BaseException as e:  # noqa: BLE001 - recorded for the dashboard, then re-raised
            _set_job(job, finished=_now().isoformat(timespec="seconds"), running=False,
                     error=f"{e.__class__.__name__}: {e}"[:500])
            if not isinstance(e, (KeyboardInterrupt, SystemExit)):
                traceback.print_exc()
            raise


def spawn(job: str) -> str:
    """Start a long job in the background (its output goes to data/<job>.log)."""
    if job not in JOBS:
        return f"error: unknown job {job}"
    if is_running(job):
        return "already running"
    log = open(config.DATA_DIR / f"{job}.log", "a")
    log.write(f"\n===== {job} started {_local_now():%Y-%m-%d %H:%M} =====\n")
    log.flush()
    subprocess.Popen([sys.executable, "-m", "outreach", job], cwd=config.ROOT, stdout=log, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    _set_job(job, requested=_now().isoformat(timespec="seconds"))
    return "started"


# --------------------------------------------------------------------------- the tick
def _due(conn, key: str, every: timedelta) -> bool:
    last = db.get_state(conn, key)
    if last and _now() - datetime.fromisoformat(last) < every:
        return False
    db.set_state(conn, key, _now().isoformat(timespec="seconds"))
    return True


def _daily_due(conn, key: str, hh: int, mm: int, weekdays=range(7)) -> bool:
    local = _local_now()
    if local.weekday() not in weekdays or (local.hour, local.minute) < (hh, mm):
        return False
    today = local.date().isoformat()
    if db.get_state(conn, key) == today:
        return False
    db.set_state(conn, key, today)
    return True


def _step(name: str, fn) -> None:
    try:
        out = fn()
        if out not in (None, 0, "", {}):
            print(f"  {name}: {out}")
    except Exception as e:  # one failing step must not stop the others
        print(f"  {name} failed: {e.__class__.__name__}: {e}")


def tick() -> None:
    from . import dashboard_sync, replies, review, sender, sources
    with lock("tick") as held:
        if not held:
            print("previous tick still running; skipping")
            return
        with db.connect() as conn:
            db.set_state(conn, "engine:heartbeat", _now().isoformat(timespec="seconds"))
            db.purge_mock(conn)
            need_sync = _due(conn, "tick:replies", timedelta(minutes=20))
            need_community = _due(conn, "tick:community", timedelta(minutes=30))
            need_prepare = _daily_due(conn, "tick:prepare", 7, 30, weekdays=range(6))
            need_daily = _daily_due(conn, "tick:daily_tasks", 9, 45)
        dashboard_on = bool(os.getenv("TURSO_DATABASE_URL"))
        if dashboard_on:
            _step("dashboard actions", lambda: dashboard_sync.pull_safe())
        if need_prepare:
            _step("prepare", lambda: spawn("prepare"))
        if need_community and config.settings().get("community"):
            _step("community", lambda: spawn("community"))
        _step("send", lambda: sender.tick(2))
        if need_sync:
            _step("replies", lambda: replies.sync(4))
        if need_daily:
            _step("ad library", lambda: sources.adlibrary_tasks(config.DATA_DIR / "adlibrary_today.md"))
            _step("linkedin", lambda: review.linkedin_tasks(config.DATA_DIR / "linkedin_today.md"))
        if dashboard_on:
            _step("dashboard push", lambda: dashboard_sync.push())


# --------------------------------------------------------------------------- install
PROTECTED = ("Desktop", "Documents", "Downloads")


def _protected_folder() -> str:
    home = Path.home()
    for name in PROTECTED:
        try:
            config.ROOT.resolve().relative_to(home / name)
            return name
        except ValueError:
            continue
    return ""


def _plist(label: str, args: list[str], interval: int | None, keep_alive: bool) -> dict:
    log = str(config.DATA_DIR / ("engine.log" if interval else "dashboard.log"))
    p = {"Label": label, "ProgramArguments": [sys.executable, "-m", "outreach", *args],
         "WorkingDirectory": str(config.ROOT), "StandardOutPath": log, "StandardErrorPath": log,
         "RunAtLoad": True, "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONUNBUFFERED": "1"}}
    if interval:
        p["StartInterval"] = interval
    if keep_alive:
        p["KeepAlive"] = True
    return p


def install(force: bool = False, port: int | None = None) -> None:
    from .dashboard_local import default_port
    port = port or default_port()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if sys.platform != "darwin":
        print("Add this line with `crontab -e` (runs the engine every 5 minutes):\n")
        print(f"*/5 * * * * cd {config.ROOT} && {sys.executable} -m outreach tick >> data/engine.log 2>&1")
        return
    folder = _protected_folder()
    if folder and not force:
        raise SystemExit(
            f"This folder is inside ~/{folder}. macOS blocks background jobs from reading ~/{folder}, so the engine\n"
            f"would silently never run. Move the project to your home folder once, then install from there:\n\n"
            f"  mv \"{config.ROOT}\" ~/agent-outreach\n"
            f"  cd ~/agent-outreach\n"
            f"  rm -rf .venv && python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt\n"
            f"  python -m outreach install\n\n"
            f"(Or give Python Full Disk Access in System Settings -> Privacy & Security and rerun with --force.)")
    agents = Path.home() / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    jobs = [(f"{LABEL}.engine", _plist(f"{LABEL}.engine", ["tick"], 300, False))]
    dash_ready = all(os.getenv(k) for k in ("TURSO_DATABASE_URL", "DASHBOARD_PASSWORD", "SESSION_SECRET"))
    if dash_ready:
        jobs.append((f"{LABEL}.dashboard", _plist(f"{LABEL}.dashboard", ["dashboard", "--port", str(port)], None, True)))
    uid = os.getuid()
    for label, plist in jobs:
        path = agents / f"{label}.plist"
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)], capture_output=True)
        path.write_bytes(plistlib.dumps(plist))
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)], capture_output=True, text=True)
        if r.returncode != 0:
            r = subprocess.run(["launchctl", "load", "-w", str(path)], capture_output=True, text=True)
        print(f"{'installed' if r.returncode == 0 else 'FAILED'}: {label}  {r.stderr.strip()}")
    print("\nThe engine now runs every 5 minutes while your Mac is awake and you're logged in.")
    if dash_ready:
        print(f"Dashboard: http://127.0.0.1:{port} (always on; bookmark it).")
    else:
        print("Dashboard not started: set TURSO_DATABASE_URL, TURSO_AUTH_TOKEN, DASHBOARD_PASSWORD and SESSION_SECRET "
              "in .env, then run `python -m outreach install` again.")
    print("Logs: data/engine.log. Remove with: python -m outreach uninstall")


def uninstall() -> None:
    if sys.platform != "darwin":
        print("Remove the `outreach tick` line with `crontab -e`.")
        return
    for label in (f"{LABEL}.engine", f"{LABEL}.dashboard"):
        path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True)
        if path.exists():
            path.unlink()
            print(f"removed {label}")
