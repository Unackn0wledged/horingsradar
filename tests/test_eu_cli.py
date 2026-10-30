"""The EU/EEA radar runs as its own instance and never touches the regjeringen.no radar's data."""

import pytest
from eu_fakes import EEASite, EUSite, make_eea_fetcher, make_eu_fetcher

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
    # Never the network: `run` gets the fake SPARQL endpoint and the fake EEA-Lex.
    site, eea = EUSite(), EEASite()
    monkeypatch.setattr(
        "horingsradar.eu.pipeline.Fetcher", lambda settings: make_eu_fetcher(settings, site)
    )
    monkeypatch.setattr(
        "horingsradar.eu.sources.eealex.make_fetcher",
        lambda settings: make_eea_fetcher(settings, eea),
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


def test_there_is_no_backtest_for_the_eu_radar(env):
    # Left out of scope 29.10.2026 (docs/EU-PLAN.md).
    with pytest.raises(SystemExit) as exc:
        cli.main(["backtest"])
    assert exc.value.code == 2


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


def test_assess_one_case_by_com_reference_or_celex(env, monkeypatch, capsys):
    from fakes import FakeLLM
    from test_eu_assess import eu_output

    assert cli.main(["run", "--no-assess"]) == 0
    llm = FakeLLM(default=eu_output("middels"))
    monkeypatch.setattr("horingsradar.llm.client.get_client", lambda settings: llm)
    assert cli.main(["assess", "COM(2026) 681"]) == 0
    assert "ok middels" in capsys.readouterr().out
    assert cli.main(["assess", "52026pc0681"]) == 0
    conn = db.connect(env / "horingsradar.sqlite3")
    jobs = [r["kind"] for r in conn.execute("SELECT kind FROM job ORDER BY id")]
    assert jobs == ["run", "assess", "assess"]
    assert conn.execute("SELECT COUNT(*) FROM eu_assessment WHERE status = 'ok'").fetchone()[0] == 2
    with pytest.raises(SystemExit, match="Fant ingen sak"):
        cli.main(["assess", "COM(1999) 1"])


def test_report_command_writes_a_pdf(env, capsys):
    assert cli.main(["run", "--no-assess"]) == 0
    capsys.readouterr()
    assert cli.main(["report"]) == 0
    path = capsys.readouterr().out.strip().splitlines()[-1]
    assert path.endswith(".pdf") and "horingsradar-eu_" in path
    conn = db.connect(env / "horingsradar.sqlite3")
    job = conn.execute("SELECT * FROM job WHERE kind = 'report'").fetchone()
    assert job["status"] == "ok"
