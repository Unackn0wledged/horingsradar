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


def test_report_for_the_last_weeks_records_the_choice(env, capsys):
    assert cli.main(["report", "--weeks", "6"]) == 0
    assert "siste-6-uker" in capsys.readouterr().out
    conn = db.connect(get_settings().db_path)
    job = conn.execute("SELECT * FROM job ORDER BY id DESC LIMIT 1").fetchone()
    assert str(db.loads(job["args"], {})["weeks"]) == "6"
    conn.close()


@pytest.mark.parametrize(
    "args", [["--weeks", "0"], ["--weeks", "seks"], ["--weeks", "6", "--week", "2026-W42"]]
)
def test_report_rejects_a_bad_period(env, args):
    with pytest.raises(SystemExit) as exc:
        cli.main(["report", *args])
    assert exc.value.code == 2


def test_backtest_from_web_requires_confirmation(env):
    with pytest.raises(SystemExit):
        cli.main(["backtest", "--split", "dev", "--triggered-by", "web"])


def test_hash_password(monkeypatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "hemmelig")
    assert cli.main(["hash-password"]) == 0
    assert verify_password("hemmelig", capsys.readouterr().out.strip())


def test_unwritable_data_dir_gives_clear_error(tmp_path):
    from horingsradar.db import DatabaseUnavailable, connect

    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    with pytest.raises(DatabaseUnavailable, match="DATA_DIR"):
        connect(blocker / "sub" / "horingsradar.sqlite3")


def test_relative_data_dir_is_made_absolute(tmp_path, monkeypatch):
    from horingsradar.config import load_settings

    monkeypatch.chdir(tmp_path)
    settings = load_settings(env_file=tmp_path / "none.env", data_dir="./data")
    assert settings.data_dir == (tmp_path / "data").resolve()
