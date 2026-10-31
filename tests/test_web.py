import base64
import os
from datetime import date, timedelta
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from horingsradar import db
from horingsradar.web import runner
from horingsradar.web.app import create_app, period_options
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


def _published(conn, hearing_id, days_ago):
    published = (date.today() - timedelta(days=days_ago)).isoformat()
    conn.execute(
        "INSERT INTO hearing (id, url, title, tracked, published_date, first_seen)"
        " VALUES (?, 'u', 'Tittel', 1, ?, ?)",
        (hearing_id, published, db.now_iso()),
    )


def test_period_choices_show_how_much_each_holds(web):
    assert "Ingen høringer med publiseringsdato" in web.get("/reports").text
    for hearing_id, days_ago in ((1, 3), (2, 10), (3, 11), (4, 400)):
        _published(web.conn, hearing_id, days_ago)
    text = web.get("/reports").text
    assert "Siste 1 uke: 1 høring, 0 med full analyse" in text
    assert "Siste 2 uker: 3 høringer, 0 med full analyse" in text
    assert "Siste 3 uker" not in text  # adds nothing


def test_period_choice_starts_at_the_first_with_enough_hearings():
    today = date(2026, 10, 14)
    items = [
        {"published_date": str(today - timedelta(days=d)), "relevance": rel}
        for d, rel in [(1, "hoy")] * 5 + [(8, "lav")] * 10 + [(15, "ingen")] * 10
    ]
    options = period_options(items, today)
    assert [(o["weeks"], o["count"], o["detailed"]) for o in options] == [
        (1, 5, 5),
        (2, 15, 5),
        (3, 25, 5),
    ]
    assert [o["weeks"] for o in options if o.get("selected")] == [2]


def test_period_report_from_the_panel(web):
    response = post(web, "/actions/report", weeks="6")
    assert "Rapport for siste 6 uker" in unquote(response.headers["location"])
    assert web.spawned == [["report", "--weeks", "6"]]
    post(web, "/actions/report")  # the ordinary button is unchanged
    assert web.spawned[-1] == ["report"]
    for bad in ("0", "27", "seks", "-1"):
        assert "Ugyldig" in unquote(post(web, "/actions/report", weeks=bad).headers["location"])
    assert len(web.spawned) == 2


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


def test_hearing_list_sorts(web):
    today = date.today()
    now = db.now_iso()
    # title, published, days to deadline, relevance (None = not assessed)
    rows = (
        ("Sak Alfa", "2026-01-01", 30, "lav"),
        ("Sak Bravo", "2026-01-02", None, "hoy"),
        ("Sak Charlie", "2026-01-03", -2, "ingen"),
        ("Sak Delta", "2026-01-04", 5, None),
        ("Sak Echo", "2026-01-05", -20, "middels"),
    )
    for hid, (title, published, days, relevance) in enumerate(rows, start=1):
        deadline = (today + timedelta(days=days)).isoformat() if days is not None else None
        web.conn.execute(
            "INSERT INTO hearing (id, url, title, tracked, first_seen, status, published_date, deadline)"
            " VALUES (?, 'u', ?, 1, ?, 'open', ?, ?)",
            (hid, title, now, published, deadline),
        )
        if relevance:
            web.conn.execute(
                "INSERT INTO assessment (hearing_id, created_at, model, prompt_version, status, context, relevance)"
                " VALUES (?, ?, 'm', 'v1', 'ok', 'drift', ?)",
                (hid, now, relevance),
            )

    def order(sort):
        text = web.get(f"/hearings?sorter={sort}").text
        return [t.removeprefix("Sak ") for t in sorted((r[0] for r in rows), key=text.index)]

    assert order("nyeste") == ["Echo", "Delta", "Charlie", "Bravo", "Alfa"]
    assert order("eldste") == ["Alfa", "Bravo", "Charlie", "Delta", "Echo"]
    # Not assessed and no deadline always come last.
    assert order("relevans") == ["Bravo", "Echo", "Alfa", "Charlie", "Delta"]
    assert order("relevans-lav") == ["Charlie", "Alfa", "Echo", "Bravo", "Delta"]
    # Upcoming deadlines first, then passed ones, most recently passed first.
    assert order("frist") == ["Delta", "Alfa", "Charlie", "Echo", "Bravo"]
    assert order("frist-synkende") == ["Alfa", "Delta", "Charlie", "Echo", "Bravo"]
    assert order("drop table") == order("nyeste")

    # Filter links keep the sort, and the default sort stays out of the URL.
    page = web.get("/hearings?sorter=frist&q=sak").text
    assert 'href="/hearings?relevans=lav&amp;q=sak&amp;sorter=frist"' in page
    assert '<option value="frist" selected>' in page
    assert 'href="/hearings?relevans=lav"' in web.get("/hearings?sorter=nyeste").text


def test_failed_assessment_is_shown_as_failed(web):
    now = db.now_iso()
    web.conn.execute(
        "INSERT INTO hearing (id, url, title, tracked, first_seen) VALUES (1, 'u', 'Feilet sak', 1, ?)",
        (now,),
    )
    # A failed assessment has no relevance.
    web.conn.execute(
        "INSERT INTO assessment (hearing_id, created_at, model, prompt_version, status, context, error)"
        " VALUES (1, ?, 'm', 'v1', 'failed', 'drift', 'ugyldig svar')",
        (now,),
    )
    assert "Feilet sak" in web.get("/hearings?relevans=feilet").text
    assert "Feilet sak" not in web.get("/hearings?relevans=ikke_vurdert").text
