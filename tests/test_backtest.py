import json
from pathlib import Path

import pytest
from fakes import FakeLLM, FakeSite, by_hearing, make_fetcher, output

from horingsradar import db
from horingsradar.backtest import resolve_period, run_backtest
from horingsradar.jobs import job_context
from horingsradar.llm.client import LLMError


@pytest.fixture
def conn(settings):
    c = db.connect(settings.db_path)
    yield c
    c.close()


def backtest(settings, conn, llm, **kwargs):
    kwargs.setdefault("split", "test")
    kwargs.setdefault("limit", 6)
    with job_context(settings, conn, "backtest") as job:
        return run_backtest(
            settings, conn, job, llm, fetcher=make_fetcher(settings, FakeSite()), **kwargs
        )


def test_estimate_only_sends_nothing(settings, conn):
    llm = FakeLLM()
    summary = backtest(settings, conn, llm, estimate_only=True)
    est = summary["estimate"]
    assert est["hearings"] == 6 and est["requests"] >= 6
    assert est["input_tokens"] > 0 and est["estimated_cost"] > 0 and est["batch"] is True
    assert llm.calls == [] and llm.batches == {}
    assert conn.execute("SELECT COUNT(*) FROM backtest_run").fetchone()[0] == 0
    # Backtest hearings are not part of operations
    assert conn.execute("SELECT COUNT(*) FROM hearing WHERE tracked = 1").fetchone()[0] == 0


def test_not_confirmed_sends_nothing(settings, conn):
    llm = FakeLLM()
    summary = backtest(settings, conn, llm, confirm=lambda est: False)
    assert summary["result"] == "not confirmed"
    assert llm.calls == [] and llm.batches == {}


def test_full_backtest_with_batch_and_metrics(settings, conn):
    llm = FakeLLM(default=by_hearing({3118997: output("hoy")}, output("ingen", reading_guide=[])))
    summary = backtest(settings, conn, llm, limit=None, confirm=lambda est: True)
    run_id = summary["backtest_run_id"]
    run = conn.execute("SELECT * FROM backtest_run WHERE id = ?", (run_id,)).fetchone()
    assert run["status"] == "ok" and run["split"] == "test"
    assert run["period_from"] == "2025-01-01" and run["period_to"] == "2025-12-31"
    assert len(llm.batches) == 1
    metrics = json.loads(run["metrics"])
    assert metrics["hearings"] == run["hearing_count"] == 20
    # 3118997 had a response from Datatilsynet and was rated high
    assert metrics["dt_responded"] >= 1
    assert metrics["recall_high_medium"] == 1.0
    assert 0 < metrics["flagged_share"] < 1
    out = Path(run["results_path"])
    assert (out / "results.csv").is_file() and (out / "summary.md").is_file()
    assert "Recall" in (out / "summary.md").read_text(encoding="utf-8")
    contexts = {r[0] for r in conn.execute("SELECT DISTINCT context FROM assessment")}
    assert contexts == {"backtest"}
    assert (
        conn.execute("SELECT COUNT(*) FROM hearing WHERE reported_in IS NOT NULL").fetchone()[0]
        == 0
    )
    h = conn.execute("SELECT * FROM hearing WHERE id = 3118997").fetchone()
    assert h["dt_responded"] == 1 and h["tracked"] == 0


def test_invalid_output_is_retried_in_second_batch(settings, conn):
    llm = FakeLLM(["not json"], default=output("lav"))
    summary = backtest(settings, conn, llm, limit=2, confirm=lambda est: True)
    assert len(llm.batches) == 2
    statuses = [r[0] for r in conn.execute("SELECT status FROM assessment")]
    assert statuses == ["ok", "ok"]
    assert summary["backtest_run_id"]


def test_cache_makes_rerun_free(settings, conn):
    llm = FakeLLM(default=output("lav"))
    backtest(settings, conn, llm, limit=3, confirm=lambda est: True)
    calls = len(llm.calls)
    summary = backtest(settings, conn, llm, limit=3, estimate_only=True)
    assert summary["estimate"]["cached_requests"] == summary["estimate"]["requests"]
    assert summary["estimate"]["estimated_cost"] == 0
    backtest(settings, conn, llm, limit=3, confirm=lambda est: True)
    assert len(llm.calls) == calls
    second = conn.execute("SELECT * FROM backtest_run ORDER BY id DESC LIMIT 1").fetchone()
    assert second["actual_input_tokens"] == 0


def test_sync_mode_and_api_error(settings, conn):
    llm = FakeLLM([LLMError("HTTP 400: bad request", status=400)], default=output("lav"))
    backtest(settings, conn, llm, limit=2, use_batch=False, confirm=lambda est: True)
    statuses = sorted(r[0] for r in conn.execute("SELECT status FROM assessment"))
    assert statuses == ["failed", "ok"]
    assert llm.batches == {}


def test_resolve_period():
    from datetime import date

    assert resolve_period("dev", None, None) == (date(2024, 1, 1), date(2024, 12, 31))
    assert resolve_period(None, date(2025, 3, 1), date(2025, 4, 1)) == (
        date(2025, 3, 1),
        date(2025, 4, 1),
    )
    with pytest.raises(ValueError):
        resolve_period(None, None, None)
