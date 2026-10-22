"""What the report says about hearings that were in an earlier report."""

from datetime import date, datetime

import pymupdf
import pytest
from fakes import FakeLLM, FakeSite, finding, make_fetcher, output
from test_assess import QUOTE_P7
from test_report import NOW, SCRIPTED, TODAY

from horingsradar import db
from horingsradar.assess import assess_hearing
from horingsradar.jobs import job_context
from horingsradar.pipeline import run
from horingsradar.report.build import build_report

LATER = datetime(2026, 10, 21, 9, 0).astimezone()


@pytest.fixture
def site():
    return FakeSite()


@pytest.fixture
def conn(settings, site):
    c = db.connect(settings.db_path)
    do_run(settings, c, site, TODAY)
    build_report(settings, c, now=NOW)  # baseline: every hearing has been reported once
    yield c
    c.close()


def do_run(settings, conn, site, today):
    with job_context(settings, conn, "run") as job:
        run(
            settings,
            conn,
            job,
            fetcher=make_fetcher(settings, site),
            client=FakeLLM(default=SCRIPTED),
            today=today,
        )


def rerun(settings, conn, site):
    settings.html_cache_max_age_seconds = 0  # fetch pages again, as a later day would
    do_run(settings, conn, site, date(2026, 10, 20))


def text_of(result) -> str:
    with pymupdf.open(result["path"]) as doc:
        return "\n".join(page.get_text() for page in doc)


def test_same_result_reassessment_is_not_an_update(settings, conn):
    assess_hearing(settings, conn, 3170511, FakeLLM(default=SCRIPTED))
    result = build_report(settings, conn, now=LATER)
    assert result["hearings"] == 0 and result["deadline_changes"] == 0


def test_changed_document_is_named(settings, conn, site):
    pdf = next(d for d in db.documents_for(conn, 3170511) if d["url"].endswith(".pdf"))
    site.overrides[pdf["url"]] = (
        200,
        site.pdf_bytes + b"\n% revised",
        {"content-type": "application/pdf"},
    )
    rerun(settings, conn, site)
    change = conn.execute("SELECT * FROM hearing_change WHERE hearing_id = 3170511").fetchone()
    assert change["kind"] == "document_changed" and change["document_title"] == pdf["title"]

    result = build_report(settings, conn, now=LATER)
    assert result["hearings"] == 1
    text = text_of(result)
    assert "Endret dokument" in text  # tag in the overview and line in the box
    assert "Endret siden rapporten" in text
    assert pdf["title"][:40] in text.replace("\n", " ")


def test_relevance_change_and_new_findings_are_marked(settings, conn):
    upgraded = output("hoy", findings=[finding(QUOTE_P7, 7)])
    assess_hearing(settings, conn, 3176758, FakeLLM(default=upgraded))  # was middels
    result = build_report(settings, conn, now=LATER)
    assert result["hearings"] == 1
    text = text_of(result)
    assert "Endret relevans" in text
    assert "Middels → Høy" in text.replace("\n", " ")
    assert "1 nytt (merket «Ny» under)" in text.replace("\n", " ")
    assert "\nNy\n" in text or "Ny " in text


def test_deadline_only_change_is_one_line(settings, conn, site):
    url = conn.execute("SELECT url FROM hearing WHERE id = 3170511").fetchone()[0]
    from conftest import REGJERINGEN

    html = (REGJERINGEN / "hearing_3170511.html").read_text(encoding="utf-8")
    site.overrides[url] = (
        200,
        html.replace("20.11.2026", "04.12.2026").encode("utf-8"),
        {"content-type": "text/html; charset=utf-8"},
    )
    rerun(settings, conn, site)
    change = conn.execute(
        "SELECT * FROM hearing_change WHERE hearing_id = 3170511 AND kind = 'deadline'"
    ).fetchone()
    assert (change["old_value"], change["new_value"]) == ("2026-11-20", "2026-12-04")

    result = build_report(settings, conn, now=LATER)
    assert result["hearings"] == 0 and result["deadline_changes"] == 1
    text = text_of(result).replace("\n", " ")
    assert "Endringer i tidligere rapporterte høringer" in text
    assert "Frist endret fra 20.11.2026 til 04.12.2026" in text

    again = build_report(settings, conn, now=LATER)  # already told: not repeated
    assert again["deadline_changes"] == 0


def test_low_and_none_are_not_shown_again(settings, conn):
    changed = output("lav", sectors=["annet"], findings=[finding(QUOTE_P7, 7)])
    assess_hearing(settings, conn, 3177107, FakeLLM(default=changed))  # lav before and after
    assess_hearing(settings, conn, 3171614, FakeLLM(default=output("ingen", reading_guide=[])))
    result = build_report(settings, conn, now=LATER)
    assert result["hearings"] == 0


def test_report_items_store_the_baseline(conn):
    rows = conn.execute("SELECT COUNT(*) FROM report_item").fetchone()[0]
    tracked = conn.execute("SELECT COUNT(*) FROM hearing WHERE tracked = 1").fetchone()[0]
    assert rows == tracked
