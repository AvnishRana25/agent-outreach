"""The background engine: one `tick` every 5 minutes does whatever is due, so you never need the terminal.

  every tick      apply dashboard actions -> send due emails -> push a fresh dashboard snapshot
  every 10 min    read replies (stop sequences, triage, Telegram) as a background job
  every 30 min    community [Hiring] posts
  daily 07:30     prepare: find companies -> research -> draft (Mon-Sat, your time zone)
  daily 09:45     Ad Library searches and LinkedIn tasks for the dashboard
  Mondays         08:00 three LinkedIn post drafts, 09:00 the weekly summary on Telegram

Long jobs (prepare, community) run as separate background processes, so sending and syncing
keep going while they work. `install` registers the tick with macOS launchd (Linux: prints a
crontab line) and, when the dashboard is configured, keeps the local dashboard running too.
"""
from __future__ import annotations

import fcntl
import json
import os
import plistlib
import signal
import subprocess
import sys
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, db

JOBS = ("prepare", "community", "content", "inbox", "assist")
TICK_LIMIT = 8 * 60  # seconds; a tick that runs longer is stopped so the next one can start
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


def schedule_retry(job: str, why: str) -> datetime:
    """After a run stopped on Gemini limits: retry in 45 minutes (overload) or just after the daily reset."""
    from . import llm
    if why == "busy":
        at = _now() + timedelta(minutes=45)
    else:
        at = datetime.fromisoformat(llm.status()["reset_at"]) + timedelta(minutes=10)
    with db.connect() as conn:
        db.set_state(conn, f"retry:{job}", at.astimezone(timezone.utc).isoformat(timespec="seconds"))
    return at.astimezone(ZoneInfo("Asia/Kolkata"))


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


def kick_tick() -> str:
    """Start one engine run now (used by the dashboard's Sync now and its watchdog)."""
    if is_running("tick"):
        return "busy"
    log = open(config.DATA_DIR / "engine.log", "a")
    subprocess.Popen([sys.executable, "-m", "outreach", "tick"], cwd=config.ROOT, stdout=log, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, start_new_session=True)
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


class TickTimeout(BaseException):
    """BaseException so no step's `except Exception` swallows it."""


def _timeout(signum, frame):
    raise TickTimeout(f"engine run took longer than {TICK_LIMIT // 60} minutes; stopped so the next one can start")


def tick() -> None:
    """One engine run. It only does quick work; anything that waits on Gemini or the inbox runs as a job,
    so a run finishes in seconds and a slow model can't make the engine look stopped."""
    from . import dashboard_sync, growth, review, sender, sources
    with lock("tick") as held:
        if not held:
            print("previous tick still running; skipping")
            return
        if hasattr(signal, "SIGALRM"):
            signal.signal(signal.SIGALRM, _timeout)
            signal.alarm(TICK_LIMIT)
        try:
            _tick(dashboard_sync, growth, review, sender, sources)
        except TickTimeout as e:
            print(f"  {e}")
        finally:
            if hasattr(signal, "SIGALRM"):
                signal.alarm(0)


def report_config_error(err: str) -> None:
    """Show a broken settings file on the dashboard; without settings nothing else can run."""
    print(f"  STOPPED: {err}")
    if os.getenv("TURSO_DATABASE_URL"):
        from . import turso
        _step("dashboard notice", lambda: turso.run([("INSERT OR REPLACE INTO dash_meta (key, value) VALUES (?,?)",
                                                      ("engine_error", json.dumps({"at": _now().isoformat(timespec="seconds"),
                                                                                  "text": err})))]))


def _provider_balances():
    from .prospecting.providers import budget
    return budget.sync_balances()


def _tick(dashboard_sync, growth, review, sender, sources) -> None:
    err = config.check()
    if err:
        report_config_error(err)
        return
    with db.connect() as conn:
        db.set_state(conn, "engine:heartbeat", _now().isoformat(timespec="seconds"))
        db.purge_mock(conn)
        need_sync = _due(conn, "tick:replies", timedelta(minutes=10))
        need_community = _due(conn, "tick:community", timedelta(minutes=30))
        need_prepare = _daily_due(conn, "tick:prepare", 7, 30, weekdays=range(6))
        need_daily = _daily_due(conn, "tick:daily_tasks", 9, 45)
        need_posts = _daily_due(conn, "tick:posts", 8, 0, weekdays=[0])
        need_digest = _daily_due(conn, "tick:digest", 9, 0, weekdays=[0])
    dashboard_on = bool(os.getenv("TURSO_DATABASE_URL"))
    if dashboard_on:
        _step("dashboard actions", lambda: dashboard_sync.pull_safe())
    with db.connect() as conn:
        retry_at = db.get_state(conn, "retry:prepare")
        if retry_at and datetime.fromisoformat(retry_at) <= _now() and not is_running("prepare"):
            db.set_state(conn, "retry:prepare", "")
            need_prepare = True
    if need_prepare:
        _step("prepare", lambda: spawn("prepare"))
    if need_community and config.settings().get("community"):
        _step("community", lambda: spawn("community"))
    if need_posts:
        _step("linkedin posts", lambda: spawn("content"))
    if need_digest:
        _step("weekly digest", growth.weekly_digest)
    # Replies are read by the background inbox job (every 10 minutes); the sender only sends from an inbox
    # read in the last 30 minutes, so a reply always stops its sequence before the next email goes out.
    if need_sync:
        _step("replies", lambda: spawn("inbox"))
    _step("send", lambda: sender.tick(2))
    if need_daily:
        _step("ad library", lambda: sources.adlibrary_tasks(config.DATA_DIR / "adlibrary_today.md"))
        _step("linkedin", lambda: review.linkedin_tasks(config.DATA_DIR / "linkedin_today.md"))
        _step("provider balances", _provider_balances)
    if dashboard_on:
        _step("dashboard push", lambda: dashboard_sync.push())
    with db.connect() as conn:
        db.set_state(conn, "engine:last_done", _now().isoformat(timespec="seconds"))


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


def doctor() -> None:
    """Plain-English check of why the engine isn't running."""
    err = config.check()
    if err:
        print(f"PROBLEM: {err}\nThe engine can't run until this is fixed.")
    with db.connect() as conn:
        beat = db.get_state(conn, "engine:heartbeat")
    age = (_now() - datetime.fromisoformat(beat)).total_seconds() / 60 if beat else None
    print(f"Last engine run: {f'{age:.0f} min ago' if age is not None else 'never'}")
    print(f"An engine run is going right now: {'yes' if is_running('tick') else 'no'}")
    for j in JOBS:
        if is_running(j):
            print(f"Background job running: {j}")
    if _protected_folder():
        print(f"PROBLEM: the project is inside ~/{_protected_folder()}; macOS blocks background jobs there.")
    if sys.platform == "darwin":
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
        for label in (f"{LABEL}.engine", f"{LABEL}.dashboard"):
            row = next((ln.split() for ln in out.splitlines() if ln.endswith(label)), None)
            if not row:
                print(f"PROBLEM: {label} is not installed. Fix: python -m outreach install")
            else:
                pid, code = row[0], row[1]
                note = "running" if pid != "-" else ("ok, waits for its next run" if code == "0" else f"last exit code {code}")
                print(f"{label}: {note}")
    log = config.DATA_DIR / "engine.log"
    if log.exists():
        print("\nLast lines of data/engine.log:")
        print("\n".join(log.read_text(errors="replace").splitlines()[-15:]))
    if age is not None and age > 15 and not err:
        print("\nIf both agents look fine, the Mac was probably asleep: background jobs pause during sleep. "
              "Starting one engine run now.")
        print(kick_tick())


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
