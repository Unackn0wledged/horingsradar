"""The EU/EEA radar's own admin panel: login, pages, filters and starting EU jobs."""

import base64
import os
import subprocess
import sys
from urllib.parse import unquote

import pytest
from eu_fakes import EUSite
from fakes import FakeLLM
from fastapi.testclient import TestClient
from test_eu_assess import case_id, do_run
from test_eu_report import EUROPOL, KIDS, SCRIPT

from horingsradar import db
from horingsradar.eu import cli
from horingsradar.eu import db as eu_db
from horingsradar.eu.config import load_eu_settings
from horingsradar.eu.web import runner
from horingsradar.eu.web.app import create_app
from horingsradar.eu.web.server import serve
from horingsradar.web.auth import csrf_token, hash_password

PASSWORD = "riktig-passord"
HASH = hash_password(PASSWORD)
SECRET = "sk-ant-hemmelig-test"
PAGES = ["/", "/cases", "/filtered", "/reports", "/stats", "/jobs", "/partials/status"]


def auth(user="operator", password=PASSWORD):
    raw = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


@pytest.fixture
def eu_settings(tmp_path):
    s = load_eu_settings(
        env_file=tmp_path / "missing.env",
        data_dir=tmp_path / "data-eu",
        radar="eu",
        web_password_hash=HASH,
        anthropic_api_key=SECRET,
    )
    s.ensure_dirs()
    return s


@pytest.fixture
def conn(eu_settings):
    c = eu_db.connect(eu_settings.db_path)
    yield c
    c.close()


@pytest.fixture
def web(eu_settings, conn, monkeypatch):
    spawned: list[list[str]] = []
    monkeypatch.setattr(runner, "spawn", lambda s, args: spawned.append(args) or 4242)
    client = TestClient(create_app(eu_settings), headers=auth())
    client.spawned = spawned
    client.csrf = csrf_token(HASH, eu_settings.web_username)
    client.conn = conn
    return client


@pytest.fixture
def filled(eu_settings, conn, web):
    """A database after one run with assessments (EU KIDS Act high, Europol-Peru low) and the
    real Council notes on EDPS opinions."""
    site = EUSite()
    site.add_edps()
    do_run(eu_settings, conn, site, client=FakeLLM(default=SCRIPT))
    return web


def post(client, path, **data):
    return client.post(path, data={"csrf_token": client.csrf, **data}, follow_redirects=False)


def message(response) -> str:
    return unquote(response.headers["location"])


# -- access -------------------------------------------------------------------------


def test_requires_basic_auth(eu_settings, web):
    anonymous = TestClient(create_app(eu_settings))
    assert anonymous.get("/").status_code == 401
    assert anonymous.get("/", headers=auth(password="feil")).status_code == 401
    assert anonymous.get("/static/htmx.min.js").status_code == 401
    assert web.get("/static/htmx.min.js").status_code == 200  # the other panel's, unchanged


@pytest.mark.parametrize("path", PAGES)
def test_empty_database_pages_render_without_secrets(web, path):
    response = web.get(path)
    assert response.status_code == 200
    assert SECRET not in response.text and HASH not in response.text
    if path != "/partials/status":  # the status partial has no page header
        assert "EU/EØS-radar" in response.text


@pytest.mark.parametrize("path", PAGES)
def test_filled_database_pages_render(filled, path):
    response = filled.get(path)
    assert response.status_code == 200
    assert SECRET not in response.text


def test_link_to_the_other_panel_only_when_set(eu_settings, web):
    assert "Høringsradar ↗" not in web.get("/").text
    eu_settings.other_panel_url = "http://192.0.2.10:8080"
    other = TestClient(create_app(eu_settings), headers=auth())
    assert 'href="http://192.0.2.10:8080"' in other.get("/").text


# -- pages ----------------------------------------------------------------------------


def test_status_counts_candidates_and_filtered(filled):
    text = filled.get("/").text
    assert "18 kandidater av 70 forslag" in text and "52 er" in text
    assert "/cases?relevans=hoy" in text


def test_cases_filters(filled):
    every = filled.get("/cases").text
    assert "EU KIDS ACT" in every and "18 kandidater" in every
    high = filled.get("/cases?relevans=hoy").text
    assert "EU KIDS ACT" in high and "Viser 1 av 18" in high
    edps = filled.get("/cases?edps=1").text
    assert "Viser 4 av 18" in edps and "Europol" in edps
    # EU KIDS Act, and Cloud and AI (a candidate through its EDPS opinion), nearest first.
    deadline = filled.get("/cases?frist=1&sorter=frist").text
    assert "Viser 2 av 18" in deadline
    assert deadline.index("Tilbakemelding til 11.11.2026") < deadline.index(
        "Tilbakemelding til 03.12.2026"
    )
    search = filled.get("/cases?q=peru europol").text
    assert "Viser 2 av 18" in search
    not_registered = filled.get("/cases?eos=ikke").text
    assert "Ikke registrert i EEA-Lex" in not_registered


def test_case_page_has_everything_about_the_case(filled):
    kids = filled.get(f"/cases/{case_id(filled.conn, KIDS)}").text
    for text in (
        "Hva foreslås",
        "Tilbakemelding på forslaget åpen til 03.12.2026",
        "EU KIDS Act",  # OEIL's short title
        "Venter på komitébehandling",
        "Sitatet er funnet i begrunnelsen",
        "Ikke verifisert",
        "Vurderingshistorikk",
        "Vurder på nytt",
    ):
        assert text in kids, text
    eio = filled.conn.execute("SELECT id FROM eu_case WHERE celex = '52026PC0313'").fetchone()[0]
    page = filled.get(f"/cases/{eio}").text
    assert "koblet på tittel" in page and "EDPS-uttalelse" in page
    assert filled.get("/cases/999999").status_code == 404


def test_filtered_page_lists_and_searches_the_rest(filled):
    text = filled.get("/filtered").text
    assert "52 forslag, nyeste først" in text
    assert "COM(2026) 569" in text
    narrowed = filled.get("/filtered?q=methane").text
    assert "Viser 1 av 52" in narrowed


def test_statistics_per_source(filled):
    text = filled.get("/stats").text
    for part in (
        "Forslag hentet",
        'bare EDPS-uttalelse: <span class="num">3</span>',
        'koblet på prosedyre <span class="num">2</span> og på tittel <span class="num">2</span>',
        "Listen er ikke fullstendig",
        'Venter på komitébehandling: <span class="num">2</span>',
        'Ikke i OEIL ennå: <span class="num">13</span>',
        "API-forbruk per måned",
    ):
        assert part in text, part


# -- actions -------------------------------------------------------------------------


def test_post_requires_csrf_and_starts_eu_jobs(filled):
    assert filled.post("/actions/run", data={}, follow_redirects=False).status_code == 403
    assert filled.get("/actions/run").status_code == 405
    assert filled.spawned == []
    post(filled, "/actions/run")
    post(filled, "/actions/run", no_assess="1")
    post(filled, "/actions/report")
    kids = case_id(filled.conn, KIDS)
    response = post(filled, f"/actions/assess/{kids}")
    assert "Ny vurdering er startet" in message(response)
    assert filled.spawned == [["run"], ["run", "--no-assess"], ["report"], ["assess", str(kids)]]
    assert "Ukjent sak" in message(post(filled, "/actions/assess/999999"))


def test_pause_and_resume(web):
    post(web, "/actions/pause")
    assert db.runs_paused(web.conn)
    evil = web.post(
        "/actions/resume",
        headers={"X-CSRF-Token": web.csrf, "Origin": "http://evil.example"},
        follow_redirects=False,
    )
    assert evil.status_code == 403 and db.runs_paused(web.conn)
    post(web, "/actions/resume")
    assert not db.runs_paused(web.conn)


def test_rejects_new_job_while_one_runs(web):
    web.conn.execute(
        "INSERT INTO job (kind, triggered_by, started_at, status, pid)"
        " VALUES ('run', 'timer', ?, 'running', ?)",
        (db.now_iso(), os.getpid()),
    )
    assert "kjører allerede" in message(post(web, "/actions/report"))
    assert web.spawned == []
    assert "Avbryt jobb" in web.get("/partials/status").text


def test_report_download_only_inside_reports_dir(web, eu_settings, tmp_path):
    good = eu_settings.reports_dir / "r.pdf"
    good.write_bytes(b"%PDF-1.7 test")
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF-1.7 secret")
    for path in (good, outside):
        web.conn.execute(
            "INSERT INTO eu_report (created_at, period_label, path, case_count)"
            " VALUES (?, 'p', ?, 0)",
            (db.now_iso(), str(path)),
        )
    assert web.get("/reports/1/download").content == b"%PDF-1.7 test"
    assert web.get("/reports/2/download").status_code == 404


def test_spawn_starts_the_eu_command_and_nothing_else(eu_settings, monkeypatch):
    with pytest.raises(ValueError):
        runner.spawn(eu_settings, ["backtest"])
    started = []

    class FakePopen:
        def __init__(self, command, **kwargs):
            started.append(command)
            self.pid = 1

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    runner.spawn(eu_settings, ["assess", "7"])
    assert started == [
        [sys.executable, "-m", "horingsradar.eu", "assess", "7", "--triggered-by", "web"]
    ]


# -- CLI -------------------------------------------------------------------------------


def test_hash_password_needs_no_instance(monkeypatch, capsys):
    monkeypatch.delenv("RADAR", raising=False)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "et-passord")
    assert cli.main(["hash-password"]) == 0
    assert capsys.readouterr().out.startswith("scrypt$")


def test_web_refuses_to_start_without_password_hash(eu_settings):
    eu_settings.web_password_hash = ""
    with pytest.raises(SystemExit, match="WEB_PASSWORD_HASH"):
        serve(eu_settings)


def test_europol_case_is_in_the_list(filled):
    assert (
        "Proposal for a COUNCIL DECISION on the signing of the Agreement"
        in filled.get(f"/cases?q={EUROPOL.replace(' ', '%20')}").text
    )
