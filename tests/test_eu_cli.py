"""The EU/EEA radar runs as its own instance and never touches the regjeringen.no radar's data."""

import pytest
from eu_fakes import EUSite, make_eu_fetcher

from horingsradar import db
from horingsradar.config import REPO_ROOT, load_settings
from horingsradar.eu import cli
from horingsradar.eu.config import get_eu_settings, load_eu_settings
from horingsradar.lock import JobLock


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data-eu"))
    monkeypatch.setenv("RADAR", "eu")
    # Never the network: `run` gets the fake SPARQL endpoint.
    site = EUSite()
    monkeypatch.setattr(
        "horingsradar.eu.pipeline.Fetcher", lambda settings: make_eu_fetcher(settings, site)
    )
    get_eu_settings.cache_clear()
    yield tmp_path / "data-eu"
    get_eu_settings.cache_clear()


def test_refuses_to_start_without_radar_eu(env, monkeypatch):
    monkeypatch.delenv("RADAR")
    get_eu_settings.cache_clear()
    with pytest.raises(SystemExit, match="RADAR=eu"):
        cli.main(["status"])
    assert not env.exists()  # nothing created in the data folder


def test_run_registers_a_job_in_its_own_data_dir(env, tmp_path):
    pilot = load_settings(env_file=tmp_path / "none.env", data_dir=tmp_path / "data")
    pilot_lock = JobLock(pilot.lock_path)
    pilot.data_dir.mkdir()
    assert pilot_lock.acquire()  # a job running in the other radar does not block this one
    try:
        assert cli.main(["run"]) == 0
    finally:
        pilot_lock.release()
    conn = db.connect(env / "horingsradar.sqlite3")
    job = conn.execute("SELECT * FROM job").fetchone()
    assert job["kind"] == "run" and job["status"] == "ok"
    assert (env / "logs" / f"job-{job['id']}.log").is_file()
    assert sorted(p.name for p in pilot.data_dir.iterdir()) == ["horingsradar.lock"]


def test_timer_run_when_paused_exits_cleanly(env):
    assert cli.main(["pause"]) == 0
    assert cli.main(["run", "--triggered-by", "timer"]) == 0
    conn = db.connect(env / "horingsradar.sqlite3")
    job = conn.execute("SELECT * FROM job").fetchone()
    assert job["status"] == "skipped" and job["triggered_by"] == "timer"
    assert cli.main(["resume"]) == 0
    assert not db.runs_paused(conn)


@pytest.mark.parametrize("command", ["report", "backtest"])
def test_commands_not_built_yet_say_so(env, command, capsys):
    assert cli.main([command]) == 2
    assert "milepæl" in capsys.readouterr().err


def test_status_shows_the_eu_data_dir(env, capsys):
    assert cli.main(["status"]) == 0
    assert str(env) in capsys.readouterr().out


def test_example_env_files_load(monkeypatch):
    for name in ("RADAR", "DATA_DIR", "ENV_FILE"):
        monkeypatch.delenv(name, raising=False)
    eu = load_eu_settings(env_file=REPO_ROOT / ".env.eu.example")
    assert eu.radar == "eu" and eu.eu_initial_lookback_days == 730
    assert eu.data_dir.name == "data-eu" and eu.web_binds[0][1] == 8081
    assert eu.run_timer_unit == "horingsradar-eu-run.timer"
    pilot = load_settings(env_file=REPO_ROOT / ".env.example")
    assert pilot.data_dir.name == "data" and pilot.web_binds[0][1] == 8080
