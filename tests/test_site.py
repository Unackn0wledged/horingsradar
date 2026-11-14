"""The static website: `horingsradar site` (build) and `horingsradar publish` (upload)."""

import json
import subprocess
from datetime import datetime

import pytest
from fakes import FakeLLM, FakeSite, by_hearing, make_fetcher, output
from test_report import SCRIPTED, TODAY

import horingsradar.site.build as site_build
from horingsradar import cli, db
from horingsradar.config import get_settings
from horingsradar.jobs import job_context
from horingsradar.pipeline import run
from horingsradar.report.build import build_report
from horingsradar.site.build import build_site
from horingsradar.site.publish import (
    PublishError,
    build_and_publish,
    last_publish,
    publish_site,
)

NOW = datetime(2026, 10, 14, 6, 15).astimezone()
HOY, MIDDELS, LAV = 3170511, 3176758, 3177107
TOKEN = "cf-secret-token-1234567890"

# Assessment fields that must never reach the website.
INTERNAL = (
    "input_tokens",
    "output_tokens",
    "cost",
    "raw_path",
    "error",
    "request_id",
    "llm_raw",
    "cache_read",
)


def _run(settings, llm=None, assess=True):
    conn = db.connect(settings.db_path)
    with job_context(settings, conn, "run") as job:
        run(
            settings,
            conn,
            job,
            fetcher=make_fetcher(settings, FakeSite()),
            client=llm or FakeLLM(default=SCRIPTED),
            today=TODAY,
            assess=assess,
        )
    return conn


@pytest.fixture
def conn(settings):
    c = _run(settings)
    yield c
    c.close()


def load(settings):
    return json.loads((settings.site_dir / "data.json").read_text("utf-8"))


def by_id(data):
    return {h["id"]: h for h in data["hearings"]}


def test_site_has_all_files(settings, conn):
    result = build_site(settings, conn, now=NOW)
    site = settings.site_dir
    for name in (
        "index.html",
        "data.json",
        "robots.txt",
        "_headers",
        "assets/site.css",
        "assets/site.js",
        "assets/fonts/SourceSans3-Regular.ttf",
    ):
        assert (site / name).is_file(), name
    assert result["hearings"] == 40
    assert len(list((site / "pdf").glob("*.pdf"))) == 40
    assert (site / "robots.txt").read_text() == "User-agent: *\nDisallow: /\n"
    index = (site / "index.html").read_text("utf-8")
    assert 'name="robots" content="noindex,nofollow"' in index
    assert "X-Robots-Tag: noindex" in (site / "_headers").read_text()
    assert not (settings.data_dir / "site.new").exists()


def test_all_levels_are_in_data_json(settings, conn):
    build_site(settings, conn, now=NOW)
    data = load(settings)
    hearings = by_id(data)
    assert data["generated_label"] == "14.10.2026 kl. 06:15" and data["site_weeks"] == 26
    assert {h["relevance"] for h in hearings.values()} == {"hoy", "middels", "lav", "ingen"}
    assert hearings[LAV]["relevance_reason"] == "Personvern berøres perifert."
    assert hearings[LAV]["summary"]  # low gets the full content, like on the hearing PDF


def test_hearing_content_matches_the_report(settings, conn):
    build_site(settings, conn, now=NOW)
    h = by_id(load(settings))[HOY]
    row = conn.execute("SELECT * FROM hearing WHERE id = ?", (HOY,)).fetchone()
    assert h["title"] == row["title"] and h["url"] == row["url"]
    assert h["deadline"] == row["deadline"] and h["department"] == row["department"]
    assert h["status"] == "open" and h["status_label"]
    assert h["relevance"] == "hoy" and h["relevance_label"] == "Høy"
    assert h["pdf"] == f"pdf/{HOY}.pdf"
    assert len(h["findings"]) == 3
    unverified = [f for f in h["findings"] if not f["verified"]]
    assert len(unverified) == 1  # shown and marked, never removed
    assert unverified[0]["quote"].startswith("Et sitat som ikke står")
    assert all(f["page_href"].endswith("#page=7") for f in h["findings"])
    assert h["privacy_section"]["exists"] and h["privacy_section"]["chapter"]
    assert h["reading_guide"] and h["reading_guide"][0]["ranges"][0]["href"].endswith("#page=6")
    assert h["documents"] and all("read" in d for d in h["documents"])
    assert h["first_seen"] and h["changes"] == [] and h["changed_at"] is None


def test_no_internal_fields_or_secrets(settings, conn):
    settings.cloudflare_api_token = TOKEN
    build_site(settings, conn, now=NOW)
    text = (settings.site_dir / "data.json").read_text("utf-8")
    keys = set()

    def walk(value):
        if isinstance(value, dict):
            keys.update(value)
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(json.loads(text))
    assert not keys & set(INTERNAL), keys & set(INTERNAL)
    assert TOKEN not in text and "test-key" not in text
    for path in settings.site_dir.rglob("*"):
        if path.is_file() and path.suffix in (".html", ".json", ".js", ".css", ".txt", ""):
            assert TOKEN not in path.read_text("utf-8", errors="ignore")


def test_failed_assessment_has_no_error_text(settings):
    llm = FakeLLM(default=by_hearing({HOY: "not json"}, output("ingen", reading_guide=[])))
    conn = _run(settings, llm)
    build_site(settings, conn, now=NOW)
    h = by_id(load(settings))[HOY]
    error = db.latest_assessment(conn, HOY)["error"]
    assert h["relevance"] == "feilet" and h["relevance_label"] == "Feilet"
    assert h["summary"] is None and h["findings"] == [] and h["relevance_reason"] is None
    assert error not in (settings.site_dir / "data.json").read_text("utf-8")
    conn.close()


def test_not_assessed_is_included(settings):
    conn = _run(settings, assess=False)
    build_site(settings, conn, now=NOW)
    hearings = load(settings)["hearings"]
    assert len(hearings) == 40 and {h["relevance"] for h in hearings} == {"ikke_vurdert"}
    assert all(h["assessed_at"] is None and h["model"] is None for h in hearings)
    conn.close()


def test_period_keeps_open_hearings_and_drops_old_closed_ones(settings, conn):
    conn.execute(
        "UPDATE hearing SET published_date = '2025-01-10', status = 'ferdigbehandlet' WHERE id = ?",
        (LAV,),
    )
    conn.execute("UPDATE hearing SET published_date = '2025-01-10' WHERE id = ?", (MIDDELS,))
    conn.commit()
    build_site(settings, conn, now=NOW)
    ids = set(by_id(load(settings)))
    assert LAV not in ids  # closed and older than SITE_WEEKS
    assert MIDDELS in ids  # open hearings are always on the site
    assert not (settings.site_dir / "pdf" / f"{LAV}.pdf").exists()


def test_untracked_and_backtest_are_never_on_the_site(settings, conn):
    conn.execute("UPDATE hearing SET tracked = 0 WHERE id = ?", (LAV,))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(assessment)") if r[1] != "id"]
    override = {
        "context": "'backtest'",
        "relevance": "'ingen'",
        "created_at": "'2099-01-01T00:00:00'",
    }
    copy = [override.get(c, c) for c in cols]
    conn.execute(
        f"INSERT INTO assessment ({', '.join(cols)}) SELECT {', '.join(copy)}"
        " FROM assessment WHERE hearing_id = ? AND context = 'drift'",
        (HOY,),
    )
    conn.commit()
    build_site(settings, conn, now=NOW)
    hearings = by_id(load(settings))
    assert LAV not in hearings
    assert hearings[HOY]["relevance"] == "hoy"


def test_changes_are_listed_newest_first(settings, conn):
    conn.execute(
        "INSERT INTO hearing_change (hearing_id, detected_at, kind, old_value, new_value)"
        " VALUES (?, '2026-10-01T05:00:00.000+00:00', 'deadline', '2026-11-01', '2026-12-01')",
        (HOY,),
    )
    conn.execute(
        "INSERT INTO hearing_change (hearing_id, detected_at, kind, document_title)"
        " VALUES (?, '2026-10-05T05:00:00.000+00:00', 'document_added', 'Nytt vedlegg')",
        (HOY,),
    )
    conn.commit()
    build_site(settings, conn, now=NOW)
    h = by_id(load(settings))[HOY]
    assert h["changes"] == [
        {"date": "2026-10-05", "text": "Nytt dokument: «Nytt vedlegg»"},
        {"date": "2026-10-01", "text": "Ny frist: 01.11.2026 → 01.12.2026"},
    ]
    assert h["changed_at"] == "2026-10-05T05:00:00.000+00:00"


def test_pdfs_are_reused_until_the_content_changes(settings, conn, monkeypatch):
    first = build_site(settings, conn, now=NOW)
    assert first["pdf_rendered"] == 40 and first["pdf_reused"] == 0

    later = datetime(2026, 10, 15, 6, 15).astimezone()
    second = build_site(settings, conn, now=later)
    assert second["pdf_rendered"] == 0 and second["pdf_reused"] == 40

    conn.execute("UPDATE hearing SET deadline = '2026-12-24' WHERE id = ?", (HOY,))
    conn.commit()
    third = build_site(settings, conn, now=later)
    assert third["pdf_rendered"] == 1 and third["pdf_reused"] == 39
    assert (settings.site_dir / "pdf" / f"{HOY}.pdf").is_file()


def test_a_failed_build_keeps_the_previous_site(settings, conn, monkeypatch):
    build_site(settings, conn, now=NOW)
    before = (settings.site_dir / "data.json").read_text("utf-8")

    def broken(*args, **kwargs):
        raise RuntimeError("template error")

    monkeypatch.setattr(site_build, "hearing_data", broken)
    with pytest.raises(RuntimeError):
        build_site(settings, conn, now=NOW)
    assert (settings.site_dir / "data.json").read_text("utf-8") == before


def test_site_does_not_touch_report_state(settings, conn):
    build_report(settings, conn)
    state = conn.execute("SELECT id, reported_in FROM hearing ORDER BY id").fetchall()
    reports = conn.execute("SELECT COUNT(*) FROM report").fetchone()[0]
    build_site(settings, conn, now=NOW)
    assert conn.execute("SELECT id, reported_in FROM hearing ORDER BY id").fetchall() == state
    assert conn.execute("SELECT COUNT(*) FROM report").fetchone()[0] == reports


# -- publish -----------------------------------------------------------------


class FakeWrangler:
    def __init__(self, returncode=0, stdout=None, stderr=""):
        self.calls = []
        self.returncode = returncode
        self.stdout = stdout or (
            "Uploading... (3/3)\n✨ Deployment complete! Take a peek over at "
            "https://abc123.horingsradar.pages.dev\n"
        )
        self.stderr = stderr

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, self.stderr)


@pytest.fixture
def configured(settings, monkeypatch):
    settings.cloudflare_api_token = TOKEN
    settings.cloudflare_account_id = "acc123"
    settings.cloudflare_pages_project = "horingsradar"
    monkeypatch.setattr(
        "horingsradar.site.publish.shutil.which", lambda name: f"/usr/local/bin/{name}"
    )
    return settings


def test_publish_runs_wrangler_with_the_token_only_in_the_environment(configured, conn, caplog):
    build_site(configured, conn, now=NOW)
    wrangler = FakeWrangler()
    result = publish_site(configured, conn, run=wrangler)
    assert result["ok"] and result["url"] == "https://abc123.horingsradar.pages.dev"
    ((command, kwargs),) = wrangler.calls
    assert command[:3] == ["/usr/local/bin/wrangler", "pages", "deploy"]
    assert command[3] == str(configured.site_dir)
    assert "--project-name=horingsradar" in command and "--branch=main" in command
    assert TOKEN not in " ".join(command)
    assert kwargs["env"]["CLOUDFLARE_API_TOKEN"] == TOKEN
    assert kwargs["env"]["CLOUDFLARE_ACCOUNT_ID"] == "acc123"
    assert kwargs["env"]["WRANGLER_SEND_METRICS"] == "false"
    assert kwargs["env"]["HOME"].startswith(str(configured.data_dir))
    assert kwargs["cwd"] == str(configured.data_dir)  # not the git checkout
    assert TOKEN not in caplog.text
    assert last_publish(conn)["ok"] and not last_publish(conn)["preview"]


def test_preview_goes_to_the_preview_branch(configured, conn):
    build_site(configured, conn, now=NOW)
    wrangler = FakeWrangler()
    result = publish_site(configured, conn, preview=True, run=wrangler)
    assert result["preview"] and "--branch=preview" in wrangler.calls[0][0]
    assert "--branch=main" not in wrangler.calls[0][0]


def test_wrangler_failure_is_recorded_without_the_token(configured, conn, caplog):
    build_site(configured, conn, now=NOW)
    wrangler = FakeWrangler(returncode=1, stdout="", stderr=f"Authentication error for {TOKEN}")
    with pytest.raises(PublishError) as excinfo:
        publish_site(configured, conn, run=wrangler)
    assert TOKEN not in str(excinfo.value)
    last = last_publish(conn)
    assert not last["ok"] and "Authentication error" in last["error"]
    assert TOKEN not in last["error"] and TOKEN not in caplog.text
    assert TOKEN not in (db.get_setting(conn, "site_last_publish") or "")


def test_publish_needs_config_and_a_built_site(settings, conn, monkeypatch):
    with pytest.raises(PublishError, match="CLOUDFLARE_API_TOKEN"):
        publish_site(settings, conn, run=FakeWrangler())
    settings.cloudflare_api_token = TOKEN
    settings.cloudflare_account_id = "acc123"
    monkeypatch.setattr("horingsradar.site.publish.shutil.which", lambda name: "/x/wrangler")
    with pytest.raises(PublishError, match="ikke bygget"):
        publish_site(settings, conn, run=FakeWrangler())
    build_site(settings, conn, now=NOW)
    monkeypatch.setattr("horingsradar.site.publish.shutil.which", lambda name: None)
    with pytest.raises(PublishError, match="Fant ikke"):
        publish_site(settings, conn, run=FakeWrangler())
    assert not last_publish(conn)["ok"]


def test_build_and_publish_never_raises(configured, conn):
    ok = build_and_publish(configured, conn, run=FakeWrangler())
    assert ok["ok"] and ok["hearings"] == 40
    failed = build_and_publish(configured, conn, run=FakeWrangler(returncode=1))
    assert not failed["ok"] and "wrangler feilet" in failed["error"]
    assert not last_publish(conn)["ok"]


# -- CLI and run hook ----------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_site_command_builds_under_the_job_lock(env, capsys):
    settings = get_settings()
    settings.ensure_dirs()
    _run(settings).close()
    assert cli.main(["site"]) == 0
    assert capsys.readouterr().out.strip().endswith("site")
    assert (settings.site_dir / "data.json").is_file()
    conn = db.connect(settings.db_path)
    job = conn.execute("SELECT * FROM job WHERE kind = 'site'").fetchone()
    assert job["status"] == "ok" and json.loads(job["summary"])["hearings"] == 40
    conn.close()


def test_publish_command_without_config_fails_cleanly(env):
    settings = get_settings()
    settings.ensure_dirs()
    assert cli.main(["publish", "--no-build"]) == 1
    conn = db.connect(settings.db_path)
    job = conn.execute("SELECT * FROM job WHERE kind = 'publish'").fetchone()
    assert job["status"] == "failed" and "CLOUDFLARE" in job["summary"]
    conn.close()


def test_run_publishes_only_when_site_is_enabled(env, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "horingsradar.site.publish.build_and_publish",
        lambda settings, conn: calls.append(1) or {"ok": True},
    )
    monkeypatch.setattr("horingsradar.pipeline.run", lambda *a, **k: {})
    assert cli.main(["run", "--no-assess"]) == 0
    assert calls == []
    monkeypatch.setenv("SITE_ENABLED", "true")
    get_settings.cache_clear()
    assert cli.main(["run", "--no-assess"]) == 0
    assert calls == [1]
    conn = db.connect(get_settings().db_path)
    summary = conn.execute("SELECT summary FROM job ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert json.loads(summary)["site"] == {"ok": True}
    conn.close()


def test_failed_run_does_not_publish(env, monkeypatch):
    calls = []
    monkeypatch.setenv("SITE_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "horingsradar.site.publish.build_and_publish",
        lambda settings, conn: calls.append(1) or {"ok": True},
    )

    def broken(*args, **kwargs):
        raise RuntimeError("regjeringen.no is down")

    monkeypatch.setattr("horingsradar.pipeline.run", broken)
    assert cli.main(["run"]) == 1
    assert calls == []
