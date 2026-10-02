import sys
from outreach import cli, db

def test_reset_database_wipes_all_data(tmp_path, monkeypatch):
    with db.connect() as conn:
        conn.execute("INSERT INTO leads (email, segment, company) VALUES ('test1@example.com', 'test', 'Test Co')")
        conn.execute("INSERT INTO prospect_state (key, value) VALUES ('sending_paused', '1')")
        conn.execute("INSERT INTO posts (title, url, source) VALUES ('Post 1', 'https://example.com/1', 'reddit')")
        conn.execute("INSERT INTO prospects (full_name, domain) VALUES ('John Doe', 'example.com')")
        conn.execute("INSERT INTO provider_credits (provider, request_timestamp) VALUES ('hunter', '2026-10-02')")
        conn.commit()

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM prospects").fetchone()[0] == 1

    counts = db.reset_database(keep_suppression=True, backup=True)
    assert counts["leads"] == 1
    assert counts["posts"] == 1
    assert counts["prospects"] == 1

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM prospects").fetchone()[0] == 0
        assert db.get_state(conn, "sending_paused") == "0"
        assert db.get_state(conn, "engine:heartbeat") is not None


def test_reset_db_cli(monkeypatch):
    with db.connect() as conn:
        conn.execute("INSERT INTO leads (email, segment, company) VALUES ('cli_test@example.com', 'test', 'CLI Co')")
        conn.commit()

    monkeypatch.setattr(sys, "argv", ["outreach", "reset-db", "--yes"])
    cli.main()

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
