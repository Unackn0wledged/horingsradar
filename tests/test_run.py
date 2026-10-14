from datetime import date

import pytest
from fakes import FakeLLM, FakeSite, by_hearing, make_fetcher, output

from horingsradar import db
from horingsradar.jobs import JobSkipped, job_context
from horingsradar.pipeline import run

TODAY = date(2026, 10, 14)


def do_run(settings, conn, site, llm, today=TODAY, triggered_by="cli"):
    with job_context(settings, conn, "run", triggered_by) as job:
        return run(
            settings, conn, job, fetcher=make_fetcher(settings, site), client=llm, today=today
        )


@pytest.fixture
def conn(settings):
    c = db.connect(settings.db_path)
    yield c
    c.close()


def count(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


def test_first_run_discovers_refreshes_and_assesses(settings, conn):
    site, llm = FakeSite(), FakeLLM()
    summary = do_run(settings, conn, site, llm)
    assert summary["new"] == 40  # pages 1-2 are within 90 days; page 3 (2025) stops discovery
    assert summary["list_pages"] == 3
    assert count(conn, "SELECT COUNT(*) FROM hearing WHERE tracked = 1") == 40
    assert count(conn, "SELECT COUNT(*) FROM hearing WHERE last_checked IS NULL") == 0
    assert count(conn, "SELECT COUNT(*) FROM assessment WHERE status = 'ok'") == 40
    assert count(conn, "SELECT COUNT(*) FROM hearing WHERE needs_assessment = 1") == 0
    h = conn.execute("SELECT * FROM hearing WHERE id = 3170511").fetchone()
    assert h["dt_on_address_list"] == 1
    assert h["deadline"] == "2026-11-20"
    assert h["status"] == "open"
    docs = db.documents_for(conn, 3170511)
    assert [d["kind"] for d in docs] == ["horingsbrev", "horingsnotat"]
    assert docs[1]["page_count"] == 10 and docs[1]["fetch_status"] == "ok"
    assert db.get_setting(conn, "last_discovery_date") == "2026-10-14"
    job = conn.execute("SELECT * FROM job ORDER BY id DESC LIMIT 1").fetchone()
    assert job["status"] == "ok" and job["kind"] == "run"


def test_second_run_is_idempotent(settings, conn):
    site, llm = FakeSite(), FakeLLM()
    do_run(settings, conn, site, llm)
    calls = len(llm.calls)
    summary = do_run(settings, conn, site, llm)
    assert summary["new"] == 0
    assert summary["changed"] == 0
    assert len(llm.calls) == calls
    assert count(conn, "SELECT COUNT(*) FROM hearing") == 40
    assert count(conn, "SELECT COUNT(*) FROM assessment") == 40


def test_changed_document_triggers_reassessment(settings, conn):
    site, llm = FakeSite(), FakeLLM()
    do_run(settings, conn, site, llm)
    pdf_url = db.documents_for(conn, 3170511)[1]["url"]
    site.overrides[pdf_url] = (
        200,
        site.pdf_bytes + b"\n% changed",
        {"content-type": "application/pdf"},
    )
    settings.html_cache_max_age_seconds = 0
    summary = do_run(settings, conn, site, llm, today=date(2026, 10, 15))
    assert summary["changed"] == 1
    assert summary["assessed"] == 1
    assert count(conn, "SELECT COUNT(*) FROM assessment WHERE hearing_id = 3170511") == 2
    assert conn.execute("SELECT content_changed_at FROM hearing WHERE id = 3170511").fetchone()[0]


def test_failed_assessment_is_retried_at_most_twice(settings, conn):
    site = FakeSite()
    llm = FakeLLM(default=by_hearing({3170511: "not json"}, output()))
    do_run(settings, conn, site, llm)
    assert (
        count(
            conn, "SELECT COUNT(*) FROM assessment WHERE hearing_id = 3170511 AND status = 'failed'"
        )
        == 1
    )
    do_run(settings, conn, site, llm, today=date(2026, 10, 15))
    do_run(settings, conn, site, llm, today=date(2026, 10, 16))
    assert (
        count(
            conn, "SELECT COUNT(*) FROM assessment WHERE hearing_id = 3170511 AND status = 'failed'"
        )
        == 2
    )
    assert (
        conn.execute("SELECT needs_assessment FROM hearing WHERE id = 3170511").fetchone()[0] == 1
    )


def test_timer_run_is_skipped_when_paused(settings, conn):
    db.set_setting(conn, "scheduled_runs_paused", "1")
    with pytest.raises(JobSkipped):
        do_run(settings, conn, FakeSite(), FakeLLM(), triggered_by="timer")
    job = conn.execute("SELECT * FROM job").fetchone()
    assert job["status"] == "skipped" and db.loads(job["summary"])["reason"] == "paused"
    # A manual run still goes through
    do_run(settings, conn, FakeSite(), FakeLLM(), triggered_by="cli")


def test_run_without_api_key_skips_assessment(settings, conn):
    settings.anthropic_api_key = ""
    with job_context(settings, conn, "run") as job:
        summary = run(settings, conn, job, fetcher=make_fetcher(settings, FakeSite()), today=TODAY)
    assert "assessment_skipped" in summary
    assert count(conn, "SELECT COUNT(*) FROM hearing WHERE needs_assessment = 1") == 40


def test_lock_prevents_parallel_jobs(settings, conn):
    with job_context(settings, conn, "report"):
        with pytest.raises(JobSkipped) as err:
            do_run(settings, conn, FakeSite(), FakeLLM(), triggered_by="timer")
        assert err.value.reason == "lock_taken"
    statuses = [r[0] for r in conn.execute("SELECT status FROM job ORDER BY id")]
    assert statuses == ["ok", "skipped"] or statuses == ["skipped", "ok"]


def test_subsequent_run_uses_overlap_cutoff(settings, conn):
    site, llm = FakeSite(), FakeLLM()
    do_run(settings, conn, site, llm)
    summary = do_run(settings, conn, site, llm, today=date(2026, 10, 20))
    assert summary["cutoff"] == "2026-09-30"


def test_empty_first_listing_page_fails_loudly(settings, conn):
    site = FakeSite(list_pages={1: "svar_3116453.html"})  # a page without listing items
    with pytest.raises(RuntimeError, match="HTML-strukturen"):
        do_run(settings, conn, site, FakeLLM())
    job = conn.execute("SELECT * FROM job").fetchone()
    assert job["status"] == "failed"
