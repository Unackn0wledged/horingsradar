"""The explanatory memorandum and the LLM assessment of EU cases, with a scripted model."""

from datetime import date
from pathlib import Path

import pytest
from eu_fakes import EURLEX, EEASite, EUSite, make_eea_fetcher, make_eu_fetcher
from fakes import FakeLLM

from horingsradar import db
from horingsradar.eu import db as eu_db
from horingsradar.eu.assess import FAILED_ATTEMPTS_BEFORE_PAUSE, assess_case, pending_assessments
from horingsradar.eu.config import load_eu_settings
from horingsradar.eu.pipeline import run
from horingsradar.eu.schema import EU_SECTORS
from horingsradar.eu.text import memorandum
from horingsradar.extract import extract_pdf, split_pages
from horingsradar.jobs import job_context

TODAY = date(2026, 10, 29)
KIDS_PDF = EURLEX / "pdf" / "52026PC0681.pdf"


def pdf_pages(path: Path) -> dict[int, str]:
    return split_pages(extract_pdf(path).text)


def quote_from(page_text: str, words: int = 18) -> str:
    """A verbatim run of words from the middle of a page."""
    lines = [line for line in page_text.splitlines() if len(line.split()) >= 8]
    return " ".join(lines[len(lines) // 2].split()[:words])


def eu_output(relevance="hoy", findings=None, **overrides):
    data = {
        "relevance": relevance,
        "relevance_reason": "Forslaget gir regler om aldersverifisering og behandling av barns personopplysninger.",
        "sectors": ["digitale_tjenester_plattformer"],
        "summary": "Kommisjonen foreslår regler for plattformer som brukes av barn. "
        "Forslaget omfatter aldersverifisering og krav til tjenesteleverandører.",
        "findings": findings if findings is not None else [],
    }
    data.update(overrides)
    return data


def eu_finding(quote, page, **overrides):
    data = {
        "category": "barn",
        "description": "Begrunnelsen beskriver hvordan forslaget berører barns personopplysninger.",
        "chapter": "1. CONTEXT OF THE PROPOSAL",
        "page": page,
        "quote": quote,
    }
    data.update(overrides)
    return data


@pytest.fixture
def eu_settings(tmp_path):
    s = load_eu_settings(
        env_file=tmp_path / "missing.env", data_dir=tmp_path / "data-eu", radar="eu"
    )
    s.ensure_dirs()
    return s


@pytest.fixture
def conn(eu_settings):
    c = eu_db.connect(eu_settings.db_path)
    yield c
    c.close()


@pytest.fixture
def site():
    return EUSite()


def do_run(settings, conn, site, client=None, assess=True, today=TODAY):
    with job_context(settings, conn, "run") as job:
        return run(
            settings,
            conn,
            job,
            fetcher=make_eu_fetcher(settings, site),
            today=today,
            eea_fetcher=make_eea_fetcher(settings, EEASite()),
            client=client,
            assess=assess,
        )


def case_id(conn, com_ref):
    return conn.execute("SELECT id FROM eu_case WHERE com_ref = ?", (com_ref,)).fetchone()[0]


# -- memorandum -----------------------------------------------------------------


def test_memorandum_runs_to_the_page_where_the_legal_act_starts():
    kids = memorandum(pdf_pages(KIDS_PDF))
    assert kids.found and (kids.first_page, kids.last_page) == (2, 15)
    assert "Proposal for a" not in kids.pages[15][:600]
    europol = memorandum(pdf_pages(EURLEX / "pdf" / "52026PC0558.pdf"))
    assert (europol.first_page, europol.last_page) == (2, 9)
    assert kids.text.startswith("[[side 2]]") and "[[side 16]]" not in kids.text


def test_memorandum_is_cut_and_marked_when_too_long():
    memo = memorandum(pdf_pages(KIDS_PDF), max_chars=10_000)
    assert memo.truncated and memo.first_page == 2 and memo.last_page < 15
    assert memo.chars <= 10_000


def test_memorandum_without_its_heading_starts_at_section_one():
    pages = {
        1: "Cover: Proposal for a REGULATION",
        2: "1. CONTEXT OF THE PROPOSAL\nReasons",
        3: "More",
        4: "EN 3 EN\nProposal for a\nREGULATION",
    }
    memo = memorandum(pages)
    assert memo.found and sorted(memo.pages) == [2, 3]


def test_without_a_memorandum_heading_the_first_pages_are_used():
    memo = memorandum({1: "Cover", 2: "Some text", 3: "Proposal for a\nDECISION", 4: "Article 1"})
    assert not memo.found and sorted(memo.pages) == [1, 2]


# -- documents --------------------------------------------------------------------


def test_run_without_assessment_fetches_the_english_pdfs_only(eu_settings, conn, site):
    llm = FakeLLM(default=eu_output())
    summary = do_run(eu_settings, conn, site, client=llm, assess=False)
    assert summary["documents_fetched"] == 2 and summary["documents_missing"] == 12
    assert llm.calls == [] and "assessed" not in summary
    doc = conn.execute(
        "SELECT * FROM eu_document WHERE case_id = ?", (case_id(conn, "COM(2026) 681"),)
    ).fetchone()
    assert (doc["memo_first_page"], doc["memo_last_page"], doc["page_count"]) == (2, 15, 17)
    assert doc["fetch_status"] == "ok" and Path(doc["text_path"]).is_file()
    missing = conn.execute("SELECT note FROM eu_document WHERE fetch_status = 'missing'").fetchone()
    assert missing["note"] == "Ingen engelsk PDF i Cellar ennå"
    assert len(site.downloads) == 2

    site.downloads.clear()
    do_run(eu_settings, conn, site, assess=False, today=date(2026, 11, 5))
    assert site.downloads == []  # texts already fetched are not downloaded again


# -- assessment -------------------------------------------------------------------


def test_assessment_stores_relevance_findings_and_verified_quotes(eu_settings, conn, site):
    pages = pdf_pages(KIDS_PDF)
    good = quote_from(pages[5])
    llm = FakeLLM(
        default=eu_output(
            findings=[
                eu_finding(good, 4),  # stated one page off: corrected
                eu_finding("This sentence is not in the memorandum at all, anywhere.", 6),
            ]
        )
    )
    summary = do_run(eu_settings, conn, site, client=llm)
    assert summary["assessed"] == 2 and summary["assessment_failed"] == 0
    assert summary["cases_assessed"] == summary["hoy"] == 2
    assert summary["waiting"] == 12  # the rest have no text yet

    kids = case_id(conn, "COM(2026) 681")
    a = conn.execute("SELECT * FROM eu_assessment WHERE case_id = ?", (kids,)).fetchone()
    assert (
        a["status"] == "ok" and a["relevance"] == a["llm_relevance"] == "hoy" and a["rule"] is None
    )
    assert a["prompt_version"] == "eu_v1" and a["model"] == "fake-model"
    assert db.loads(a["sectors"]) == ["digitale_tjenester_plattformer"]
    assert Path(a["raw_response_path"]).is_file() and a["input_tokens"] > 0
    findings = conn.execute(
        "SELECT * FROM eu_finding WHERE assessment_id = ? ORDER BY id", (a["id"],)
    ).fetchall()
    assert (findings[0]["quote_verified"], findings[0]["page"], findings[0]["page_stated"]) == (
        1,
        5,
        4,
    )
    assert findings[1]["quote_verified"] == 0 and findings[1]["page"] == 6  # kept, flagged
    assert (
        conn.execute("SELECT needs_assessment FROM eu_case WHERE id = ?", (kids,)).fetchone()[0]
        == 0
    )


def test_prompt_has_the_memorandum_and_names_no_organisation(eu_settings, conn, site):
    llm = FakeLLM(default=eu_output())
    do_run(eu_settings, conn, site, assess=False)
    assess_case(eu_settings, conn, case_id(conn, "COM(2026) 681"), llm)
    params = llm.calls[0]
    user = params["messages"][0]["content"]
    assert "COM(2026) 681" in user and "52026PC0681" in user and "Forslag til forordning" in user
    assert "[[side 2]]" in user and "[[side 15]]" in user and "[[side 16]]" not in user
    assert "Begrunnelsen står på side 2–15 av 17" in user
    for text in (params["system"], user):
        assert "datatilsyn" not in text.lower() and "jurist" not in text.lower()
    schema = params["output_config"]["format"]["schema"]
    assert schema["properties"]["sectors"]["items"]["enum"] == list(EU_SECTORS)


def test_invalid_output_is_retried_once_then_fails_and_pauses(eu_settings, conn, site):
    do_run(eu_settings, conn, site, assess=False)
    kids = case_id(conn, "COM(2026) 681")
    own_advice = eu_output(summary="Vi anbefaler at forslaget følges nøye.")
    wrong_sector = eu_output(sectors=["finans_skatt"])  # a sector from the other radar's list
    llm = FakeLLM(script=[own_advice, eu_output()])
    first = assess_case(eu_settings, conn, kids, llm)
    assert (
        conn.execute("SELECT status FROM eu_assessment WHERE id = ?", (first,)).fetchone()[0]
        == "ok"
    )
    assert len(llm.calls) == 2

    for _ in range(FAILED_ATTEMPTS_BEFORE_PAUSE):
        conn.execute("UPDATE eu_case SET needs_assessment = 1 WHERE id = ?", (kids,))
        failed = assess_case(eu_settings, conn, kids, FakeLLM(script=[wrong_sector, "not json"]))
        row = conn.execute(
            "SELECT status, error FROM eu_assessment WHERE id = ?", (failed,)
        ).fetchone()
        assert row["status"] == "failed" and "ugyldig JSON" in row["error"]
    assert kids not in pending_assessments(conn)  # waits until the text changes


def test_run_without_api_key_skips_assessment(eu_settings, conn, site):
    summary = do_run(eu_settings, conn, site)  # no client and no ANTHROPIC_API_KEY
    assert summary["assessment_skipped"] == "ANTHROPIC_API_KEY is not set"
    assert summary["pending_assessments"] == 2 and summary["documents_fetched"] == 2
