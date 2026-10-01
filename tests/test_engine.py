"""The background engine: what a tick runs and when, job bookkeeping, and the installer's guard rails."""
from datetime import datetime, timezone

import pytest


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_DB", str(tmp_path / "t.db"))
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    from outreach import config, db
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db.init()


def fake_clock(monkeypatch, *when):
    from outreach import engine
    t = datetime(*when, tzinfo=timezone.utc)
    monkeypatch.setattr(engine, "_now", lambda: t)


def test_tick_schedules(monkeypatch):
    from outreach import db, engine, replies, sender
    calls = []
    monkeypatch.setattr(engine, "spawn", lambda job: calls.append(job) or "started")
    monkeypatch.setattr(sender, "tick", lambda n: calls.append("send") or 0)
    monkeypatch.setattr(replies, "sync", lambda d: calls.append("replies") or 0)
    fake_clock(monkeypatch, 2026, 10, 5, 2, 30)          # Mon 08:00 IST: after 07:30
    engine.tick()
    assert calls == ["prepare", "community", "content", "send", "replies"]   # Monday 08:00 also drafts posts
    calls.clear()
    fake_clock(monkeypatch, 2026, 10, 5, 2, 35)          # 5 minutes later: only sending is due
    engine.tick()
    assert calls == ["send"]
    with db.connect() as conn:
        assert db.get_state(conn, "engine:heartbeat").startswith("2026-10-05T02:35")


def test_no_prepare_on_sunday_or_before_0730(monkeypatch):
    from outreach import engine, replies, sender
    calls = []
    monkeypatch.setattr(engine, "spawn", lambda job: calls.append(job) or "started")
    monkeypatch.setattr(sender, "tick", lambda n: 0)
    monkeypatch.setattr(replies, "sync", lambda d: 0)
    fake_clock(monkeypatch, 2026, 10, 4, 4, 0)           # Sunday
    engine.tick()
    fake_clock(monkeypatch, 2026, 10, 5, 1, 0)           # Monday 06:30 IST
    engine.tick()
    assert "prepare" not in calls


def test_failing_step_does_not_stop_others(monkeypatch):
    from outreach import engine, replies, sender
    calls = []
    monkeypatch.setattr(engine, "spawn", lambda job: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(sender, "tick", lambda n: calls.append("send") or 0)
    monkeypatch.setattr(replies, "sync", lambda d: calls.append("replies") or 0)
    fake_clock(monkeypatch, 2026, 10, 5, 2, 30)
    engine.tick()
    assert calls == ["send", "replies"]


def test_run_job_records_result_and_error():
    from outreach import db, engine
    engine.run_job("prepare", lambda: "3 drafted")
    with pytest.raises(ValueError):
        engine.run_job("community", lambda: (_ for _ in ()).throw(ValueError("bad")))
    with db.connect() as conn:
        assert engine.job_state(conn, "prepare")["result"] == "3 drafted"
        st = engine.job_state(conn, "community")
        assert st["error"] == "ValueError: bad" and st["running"] is False
    assert not engine.is_running("prepare")


def test_lock_blocks_second_holder():
    from outreach import engine
    with engine.lock("prepare") as first:
        assert first and engine.is_running("prepare")
    assert not engine.is_running("prepare")


def test_install_refuses_protected_folder(monkeypatch, tmp_path):
    from outreach import config, engine
    monkeypatch.setattr(engine.sys, "platform", "darwin")
    monkeypatch.setattr(engine.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(config, "ROOT", tmp_path / "Desktop" / "work" / "agent-outreach")
    (tmp_path / "Desktop" / "work" / "agent-outreach").mkdir(parents=True)
    with pytest.raises(SystemExit, match="Desktop"):
        engine.install()


def test_install_on_linux_prints_cron(monkeypatch, capsys):
    from outreach import engine
    monkeypatch.setattr(engine.sys, "platform", "linux")
    engine.install()
    assert "*/5 * * * *" in capsys.readouterr().out


def test_install_writes_dashboard_port(monkeypatch, tmp_path, capsys):
    import plistlib
    from outreach import config, engine
    monkeypatch.setattr(engine.sys, "platform", "darwin")
    monkeypatch.setattr(engine.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(config, "ROOT", tmp_path / "agent-outreach")
    monkeypatch.setattr(engine.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stderr": ""})())
    for k in ("TURSO_DATABASE_URL", "DASHBOARD_PASSWORD", "SESSION_SECRET"):
        monkeypatch.setenv(k, "x" * 20)
    monkeypatch.delenv("DASHBOARD_PORT", raising=False)
    agent = tmp_path / "Library" / "LaunchAgents" / "com.agent-outreach.dashboard.plist"
    engine.install()
    assert plistlib.loads(agent.read_bytes())["ProgramArguments"][-2:] == ["--port", "7347"]
    assert "http://127.0.0.1:7347" in capsys.readouterr().out
    monkeypatch.setenv("DASHBOARD_PORT", "9123")
    engine.install()
    assert plistlib.loads(agent.read_bytes())["ProgramArguments"][-1] == "9123"
    engine.install(port=9200)
    assert plistlib.loads(agent.read_bytes())["ProgramArguments"][-1] == "9200"


def test_monday_runs_posts_and_digest(monkeypatch):
    from outreach import engine, growth, replies, sender
    calls = []
    monkeypatch.setattr(engine, "spawn", lambda job: calls.append(job) or "started")
    monkeypatch.setattr(growth, "weekly_digest", lambda: calls.append("digest") or "digest sent")
    monkeypatch.setattr(sender, "tick", lambda n: 0)
    monkeypatch.setattr(replies, "sync", lambda d: 0)
    fake_clock(monkeypatch, 2026, 10, 5, 3, 45)          # Monday 09:15 IST
    engine.tick()
    assert "content" in calls and "digest" in calls
    calls.clear()
    fake_clock(monkeypatch, 2026, 10, 6, 3, 45)          # Tuesday: neither
    engine.tick()
    assert "content" not in calls and "digest" not in calls
