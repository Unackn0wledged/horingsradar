from datetime import date, datetime

import pymupdf
import pytest
from fakes import FakeLLM, FakeSite, by_hearing, finding, make_fetcher, output
from test_assess import QUOTE_LINEBREAK, QUOTE_P7

from horingsradar import db
from horingsradar.assess import assess_hearing
from horingsradar.jobs import job_context
from horingsradar.pipeline import run
from horingsradar.report.build import build_report, format_ranges, parse_week

TODAY = date(2026, 10, 14)
NOW = datetime(2026, 10, 14, 9, 30).astimezone()

SCRIPTED = by_hearing(
    {
        3170511: output(
            "hoy",
            findings=[
                finding(QUOTE_P7, 7),
                finding(QUOTE_LINEBREAK, 7, category="register"),
                finding("Et sitat som ikke står i dokumentet og ikke kan verifiseres.", 7),
            ],
            privacy_section={
                "exists": True,
                "document": "D2",
                "chapter": "3 Rettslige rammer",
                "pages": {"start": 6, "end": 9},
            },
        ),
        3176758: output("middels", sectors=["utdanning_forskning"]),
        3177107: output(
            "lav", sectors=["utdanning_forskning"], relevance_reason="Personvern berøres perifert."
        ),
    },
    output(
        "ingen",
        sectors=["annet"],
        relevance_reason="Personopplysninger berøres ikke.",
        reading_guide=[],
    ),
)


@pytest.fixture
def conn(settings):
    c = db.connect(settings.db_path)
    with job_context(settings, c, "run") as job:
        run(
            settings,
            c,
            job,
            fetcher=make_fetcher(settings, FakeSite()),
            client=FakeLLM(default=SCRIPTED),
            today=TODAY,
        )
    yield c
    c.close()


def pdf_text(path: str) -> str:
    with pymupdf.open(path) as doc:
        return "\n".join(page.get_text() for page in doc)


def test_report_contains_all_sections_and_marks_reported(settings, conn):
    result = build_report(settings, conn, now=NOW)
    assert result["hearings"] == 40 and result["detailed"] == 2
    text = pdf_text(result["path"])
    for heading in (
        "Høringsradar",
        "Oversikt",
        "Frister som nærmer seg",
        "Høy og middels relevans",
        "Lav og ingen relevans",
    ):
        assert heading in text
    assert "KI-generert" in text
    assert "fake-model / prompt v1" in text
    assert "Sitat verifisert" in text
    assert "UVERIFISERT" in text
    assert (
        "Høy" in text and "Middels" in text and "Ingen" in text
    )  # levels as text, not only colour
    assert "Datatilsynet bør" not in text
    assert "s. 6–9" in text
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM hearing WHERE reported_in IS NULL AND tracked = 1"
        ).fetchone()[0]
        == 0
    )
    with pymupdf.open(result["path"]) as doc:
        links = [link for page in doc for link in page.get_links()]
    assert any(link.get("uri", "").startswith("https://www.regjeringen.no/") for link in links)


def test_second_report_is_empty_then_shows_updated(settings, conn):
    build_report(settings, conn, now=NOW)
    empty = build_report(settings, conn, now=NOW)
    assert empty["hearings"] == 0
    assert "Ingen nye eller oppdaterte" in pdf_text(empty["path"])
    assess_hearing(settings, conn, 3170511, FakeLLM(default=output("hoy")))
    updated = build_report(settings, conn, now=datetime(2026, 10, 21, 9, 0).astimezone())
    assert updated["hearings"] == 1
    text = pdf_text(updated["path"])
    assert "Endret siden rapporten" in text and "Endrede funn" in text
    assert "3 fra forrige rapport er ikke med lenger" in text
    assert "Oppdatert" not in text


def test_upcoming_deadlines_from_earlier_reports(settings, conn):
    build_report(settings, conn, now=NOW)
    later = datetime(2026, 11, 1, 9, 0).astimezone()  # 3170511 has deadline 2026-11-20
    result = build_report(settings, conn, now=later)
    assert result["upcoming"] >= 1
    assert "forskrift om deling av taushetsbelagte" in pdf_text(result["path"]).lower()


def test_week_report_does_not_touch_reported_in(settings, conn):
    result = build_report(settings, conn, week="2026-W42", now=NOW)
    assert result["kind"] == "week"
    expected = conn.execute(
        "SELECT COUNT(*) FROM hearing WHERE tracked = 1 AND published_date BETWEEN '2026-10-12' AND '2026-10-18'"
    ).fetchone()[0]
    assert result["hearings"] == expected > 0
    assert (
        conn.execute("SELECT COUNT(*) FROM hearing WHERE reported_in IS NOT NULL").fetchone()[0]
        == 0
    )


def test_failed_assessment_is_listed_not_hidden(settings):
    c = db.connect(settings.db_path)
    llm = FakeLLM(default=by_hearing({3170511: "not json"}, output("ingen", reading_guide=[])))
    with job_context(settings, c, "run") as job:
        run(settings, c, job, fetcher=make_fetcher(settings, FakeSite()), client=llm, today=TODAY)
    result = build_report(settings, c, now=NOW)
    text = pdf_text(result["path"])
    assert "Ikke vurdert" in text and "Vurderingen feilet" in text
    c.close()


def test_helpers():
    assert format_ranges([(22, 41), (88, 95)]) == "22–41 og 88–95"
    assert format_ranges([(1, 1), (3, 4), (7, 9)]) == "1, 3–4 og 7–9"
    assert parse_week("2026-W42") == (date(2026, 10, 12), date(2026, 10, 18))
    with pytest.raises(ValueError):
        parse_week("2026-42")


def test_every_overview_row_jumps_to_its_analysis(settings, conn):
    result = build_report(settings, conn, now=NOW)
    ids = [r[0] for r in conn.execute("SELECT id FROM hearing WHERE tracked = 1")]
    with pymupdf.open(result["path"]) as doc:
        names = doc.resolve_names()
        targets = {
            link["nameddest"]
            for page in doc
            for link in page.get_links()
            if link["kind"] == pymupdf.LINK_NAMED
        }
    # Every hearing has an anchor in the document, every overview row links to it,
    # and the analyses link back to the overview.
    assert {f"h{i}" for i in ids} <= set(names)
    assert {f"h{i}" for i in ids} <= targets
    assert "oversikt" in names and "oversikt" in targets
    assert names["oversikt"]["page"] == 0


def test_page_link_helpers():
    from horingsradar.report.build import linked_ranges, page_link

    pdf = "https://www.regjeringen.no/contentassets/abc/notat.pdf"
    assert page_link(pdf, 22) == pdf + "#page=22"
    assert page_link(pdf + "#page=3", 22) == pdf + "#page=22"
    assert page_link(pdf, None) == pdf
    # The letter and inline notat live on the hearing page: link to the page itself.
    inline = "https://www.regjeringen.no/no/dokumenter/x/id1/#horingsbrev"
    assert page_link(inline, 1) == inline
    assert page_link(None, 5) is None
    assert linked_ranges(pdf, [(22, 41), (88, 88)]) == [
        {"label": "22–41", "href": pdf + "#page=22"},
        {"label": "88", "href": pdf + "#page=88"},
    ]


def test_report_page_numbers_open_the_document_on_that_page(settings, conn):
    result = build_report(settings, conn, now=NOW)
    pdf_url = next(
        d["url"] for d in db.documents_for(conn, 3170511) if d["url"].lower().endswith(".pdf")
    )
    with pymupdf.open(result["path"]) as doc:
        uris = {
            link["uri"]
            for page in doc
            for link in page.get_links()
            if link["kind"] == pymupdf.LINK_URI
        }
    assert f"{pdf_url}#page=7" in uris  # findings on page 7
    assert f"{pdf_url}#page=6" in uris  # reading guide and privacy chapter start on page 6


def test_panel_hearing_page_has_page_links(settings, conn):
    import base64

    from fastapi.testclient import TestClient

    from horingsradar.web.app import create_app
    from horingsradar.web.auth import hash_password

    settings.web_password_hash = hash_password("pw")
    auth = {"Authorization": "Basic " + base64.b64encode(b"operator:pw").decode()}
    html = TestClient(create_app(settings), headers=auth).get("/hearings/3170511").text
    pdf_url = next(
        d["url"] for d in db.documents_for(conn, 3170511) if d["url"].lower().endswith(".pdf")
    )
    assert f'href="{pdf_url}#page=7"' in html
    assert f'href="{pdf_url}#page=6"' in html
