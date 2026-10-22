import base64
import os
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from horingsradar import db
from horingsradar.web import runner
from horingsradar.web.app import create_app
from horingsradar.web.auth import csrf_token, hash_password, verify_password

PASSWORD = "riktig-passord"
HASH = hash_password(PASSWORD)


def auth(user="operator", password=PASSWORD):
    raw = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


@pytest.fixture
def web(settings, monkeypatch):
    settings.web_password_hash = HASH
    settings.anthropic_api_key = "sk-ant-hemmelig-test"
    spawned: list[list[str]] = []
    monkeypatch.setattr(runner, "spawn", lambda s, args: spawned.append(args) or 4242)
    client = TestClient(create_app(settings), headers=auth())
    client.spawned = spawned
    client.csrf = csrf_token(HASH, settings.web_username)
    conn = db.connect(settings.db_path)
    client.conn = conn
    yield client
    conn.close()


def post(client, path, **data):
    return client.post(path, data={"csrf_token": client.csrf, **data}, follow_redirects=False)


def test_password_hash_roundtrip():
    assert verify_password(PASSWORD, HASH)
    assert not verify_password("feil", HASH)
    assert not verify_password(PASSWORD, "garbage")


def test_requires_basic_auth(settings, web):
    anonymous = TestClient(create_app(settings))
    response = anonymous.get("/")
    assert response.status_code == 401
    assert "Basic" in response.headers["www-authenticate"]
    assert anonymous.get("/", headers=auth(password="feil")).status_code == 401
    assert anonymous.get("/static/htmx.min.js").status_code == 401
    assert web.get("/").status_code == 200


def test_panel_locked_without_password_hash(settings):
    settings.web_password_hash = ""
    client = TestClient(create_app(settings))
    assert client.get("/", headers=auth()).status_code == 401


@pytest.mark.parametrize(
    "path", ["/", "/hearings", "/reports", "/stats", "/backtest", "/jobs", "/partials/status"]
)
def test_pages_render_and_never_show_secrets(web, path):
    response = web.get(path)
    assert response.status_code == 200
    assert "sk-ant-hemmelig-test" not in response.text
    assert HASH not in response.text


def test_get_does_not_change_state(web):
    before = web.conn.execute("SELECT COUNT(*) FROM job").fetchone()[0]
    for path in ("/actions/run", "/actions/pause", "/actions/report"):
        assert web.get(path).status_code == 405
    assert web.spawned == []
    assert web.conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == before
    assert not db.runs_paused(web.conn)


def test_post_requires_csrf(web):
    assert web.post("/actions/run", data={}, follow_redirects=False).status_code == 403
    assert (
        web.post("/actions/run", data={"csrf_token": "x"}, follow_redirects=False).status_code
        == 403
    )
    assert web.spawned == []
    response = post(web, "/actions/run")
    assert response.status_code == 303
    assert web.spawned == [["run"]]


def test_csrf_header_from_htmx_and_origin_check(web):
    ok = web.post("/actions/pause", headers={"X-CSRF-Token": web.csrf}, follow_redirects=False)
    assert ok.status_code == 303 and db.runs_paused(web.conn)
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
        "INSERT INTO job (kind, triggered_by, started_at, status, pid) VALUES ('run', 'timer', ?, 'running', ?)",
        (db.now_iso(), os.getpid()),
    )
    response = post(web, "/actions/report")
    assert "kjører allerede" in unquote(response.headers["location"])
    assert web.spawned == []
    assert "Avbryt jobb" in web.get("/partials/status").text


def test_assess_from_hearing_list(web):
    web.conn.execute(
        "INSERT INTO hearing (id, url, title, tracked, first_seen) VALUES (3170511, 'u', 'Tittel', 1, ?)",
        (db.now_iso(),),
    )
    assert "Vurder på nytt" in web.get("/hearings").text
    assert web.get("/hearings/3170511").status_code == 200
    post(web, "/actions/assess/3170511")
    assert web.spawned == [["assess", "3170511"]]
    post(web, "/actions/assess/999")
    assert len(web.spawned) == 1


def _estimate_job(conn, split="dev", limit=None, status="ok"):
    args = {"split": split, "estimate_only": "True"}
    if limit:
        args["limit"] = str(limit)
    summary = {
        "estimate": {
            "hearings": 5,
            "requests": 5,
            "input_tokens": 1000,
            "output_tokens": 100,
            "estimated_cost": 1.23,
            "batch": True,
            "cached_requests": 0,
        }
    }
    cur = conn.execute(
        "INSERT INTO job (kind, triggered_by, args, started_at, finished_at, status, summary)"
        " VALUES ('backtest', 'web', ?, ?, ?, ?, ?)",
        (db.dumps(args), db.now_iso(), db.now_iso(), status, db.dumps(summary)),
    )
    return cur.lastrowid


def test_backtest_requires_estimate_and_confirmation(web):
    post(web, "/actions/backtest/estimate", split="dev", limit="5")
    assert web.spawned == [["backtest", "--split", "dev", "--limit", "5", "--estimate-only"]]

    job_id = _estimate_job(web.conn, limit=5)
    page = web.get("/backtest").text
    assert "$1.23" in page and "Jeg bekrefter" in page

    # Missing confirmation
    post(web, "/actions/backtest/start", split="dev", limit="5", estimate_job_id=str(job_id))
    # Changed choices since the estimate
    post(
        web,
        "/actions/backtest/start",
        split="test",
        limit="5",
        estimate_job_id=str(job_id),
        confirm="ja",
    )
    # Wrong estimate id
    post(web, "/actions/backtest/start", split="dev", limit="5", estimate_job_id="0", confirm="ja")
    assert len(web.spawned) == 1

    post(
        web,
        "/actions/backtest/start",
        split="dev",
        limit="5",
        estimate_job_id=str(job_id),
        confirm="ja",
    )
    assert web.spawned[-1] == ["backtest", "--split", "dev", "--limit", "5", "--yes"]


def test_backtest_validates_input(web):
    post(web, "/actions/backtest/estimate", split="prod")
    post(web, "/actions/backtest/estimate", split="dev", limit="-3")
    assert web.spawned == []


def test_report_download_only_inside_reports_dir(web, settings, tmp_path):
    good = settings.reports_dir / "r.pdf"
    good.write_bytes(b"%PDF-1.7 test")
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF-1.7 secret")
    for path in (good, outside):
        web.conn.execute(
            "INSERT INTO report (created_at, period_label, path, hearing_count) VALUES (?, 'p', ?, 0)",
            (db.now_iso(), str(path)),
        )
    assert web.get("/reports/1/download").content == b"%PDF-1.7 test"
    assert web.get("/reports/2/download").status_code == 404


def test_cancel_marks_dead_job_cancelled(web):
    cur = web.conn.execute(
        "INSERT INTO job (kind, triggered_by, started_at, status, pid) VALUES ('run', 'web', ?, 'running', 999999)",
        (db.now_iso(),),
    )
    message = runner.cancel_job(web.conn, cur.lastrowid, wait_seconds=0)
    assert "avbrutt" in message
    assert (
        web.conn.execute("SELECT status FROM job WHERE id = ?", (cur.lastrowid,)).fetchone()[0]
        == "cancelled"
    )


def test_spawn_only_allows_known_jobs(settings):
    with pytest.raises(ValueError):
        runner.spawn(settings, ["web"])


def test_hearing_list_filters_by_relevance_and_text(web):
    now = db.now_iso()
    for hid, title, relevance in ((1, "Politiregister", "hoy"), (2, "Fiskekvoter", "ingen")):
        web.conn.execute(
            "INSERT INTO hearing (id, url, title, department, tracked, first_seen, status)"
            " VALUES (?, 'u', ?, 'JD', 1, ?, 'open')",
            (hid, title, now),
        )
        web.conn.execute(
            "INSERT INTO assessment (hearing_id, created_at, model, prompt_version, status, context, relevance)"
            " VALUES (?, ?, 'm', 'v1', 'ok', 'drift', ?)",
            (hid, now, relevance),
        )
    web.conn.execute(
        "INSERT INTO hearing (id, url, title, tracked, first_seen) VALUES (3, 'u', 'Uvurdert sak', 1, ?)",
        (now,),
    )
    assert (
        "Politiregister" in web.get("/hearings").text and "Fiskekvoter" in web.get("/hearings").text
    )
    hoy = web.get("/hearings?relevans=hoy").text
    assert "Politiregister" in hoy and "Fiskekvoter" not in hoy
    assert "Uvurdert sak" in web.get("/hearings?relevans=ikke_vurdert").text
    search = web.get("/hearings?q=fiske").text
    assert "Fiskekvoter" in search and "Politiregister" not in search
    # Unknown filter values are ignored, and the radar on the status page links to the filters.
    assert "Politiregister" in web.get("/hearings?relevans=drop table").text
    assert 'href="/hearings?relevans=hoy"' in web.get("/").text
