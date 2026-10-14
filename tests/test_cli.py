import pytest

from horingsradar import cli, db
from horingsradar.config import get_settings
from horingsradar.web.auth import verify_password


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield tmp_path / "data"
    get_settings.cache_clear()


def test_pause_resume_and_status(env, capsys):
    assert cli.main(["pause"]) == 0
    conn = db.connect(env / "horingsradar.sqlite3")
    assert db.runs_paused(conn)
    assert cli.main(["status"]) == 0
    assert "pauset" in capsys.readouterr().out
    assert cli.main(["resume"]) == 0
    assert not db.runs_paused(conn)


def test_timer_run_when_paused_exits_cleanly(env):
    cli.main(["pause"])
    assert cli.main(["run", "--triggered-by", "timer"]) == 0
    conn = db.connect(env / "horingsradar.sqlite3")
    job = conn.execute("SELECT * FROM job").fetchone()
    assert job["status"] == "skipped" and job["triggered_by"] == "timer"


def test_report_command_writes_pdf(env, capsys):
    assert cli.main(["report"]) == 0
    path = capsys.readouterr().out.strip().splitlines()[-1]
    assert path.endswith(".pdf")
    conn = db.connect(env / "horingsradar.sqlite3")
    assert conn.execute("SELECT status FROM job").fetchone()[0] == "ok"


def test_backtest_from_web_requires_confirmation(env):
    with pytest.raises(SystemExit):
        cli.main(["backtest", "--split", "dev", "--triggered-by", "web"])


def test_hash_password(monkeypatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "hemmelig")
    assert cli.main(["hash-password"]) == 0
    assert verify_password("hemmelig", capsys.readouterr().out.strip())
