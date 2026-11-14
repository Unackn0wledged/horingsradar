"""Hearings whose notat is a separate document page on regjeringen.no (such as a NOU).

Regression: NOU 2026:12 Én helsetjeneste first linked the PDF, then the NOU's page. The PDF was
taken as removed, and the hearing was reassessed on the letter alone (high became low).
"""

import pytest
from conftest import hearing_url
from fakes import FakeSite, make_fetcher

from horingsradar import db
from horingsradar.http import FetchError
from horingsradar.sources.regjeringen.crawler import RegjeringenSource

HEARING = 3190689
DOC_PAGE = "https://www.regjeringen.no/no/dokumenter/nou-2026-12/id3190350/"
PDF = (
    "https://www.regjeringen.no/contentassets/8089c2908e604358ac67a5b9cbdf8d6e"
    "/no/pdfs/nou202620260012000dddpdfs.pdf"
)


@pytest.fixture
def site():
    return FakeSite()


@pytest.fixture
def source(settings, site):
    conn = db.connect(settings.db_path)
    conn.execute(
        "INSERT INTO hearing (id, url, title, tracked, needs_assessment, first_seen)"
        " VALUES (?, ?, 'Høring - NOU 2026:12 Én helsetjeneste', 1, 1, ?)",
        (HEARING, hearing_url(HEARING), db.now_iso()),
    )
    yield RegjeringenSource(settings, conn, make_fetcher(settings, site))
    conn.close()


def documents(conn):
    return conn.execute(
        "SELECT url, title, kind, removed, fetch_status FROM document WHERE hearing_id = ?"
        " ORDER BY position",
        (HEARING,),
    ).fetchall()


def changes(conn):
    return [
        (r["kind"], r["document_title"])
        for r in conn.execute("SELECT * FROM hearing_change WHERE hearing_id = ?", (HEARING,))
    ]


def page_linking_the_pdf_directly() -> bytes:
    """The hearing page as it was first: the «Høringsnotat» box linked the PDF itself."""
    from conftest import fixture_text

    html = fixture_text(f"hearing_{HEARING}.html").replace(
        '"/no/dokumenter/nou-2026-12/id3190350/"',
        '"' + PDF.removeprefix("https://www.regjeringen.no") + '"',
    )
    return html.encode("utf-8")


def test_the_notat_is_fetched_from_its_document_page(source):
    result = source.refresh_hearing(HEARING, max_age=0)
    docs = documents(source.conn)
    assert [(d["kind"], d["url"]) for d in docs] == [
        ("horingsbrev", hearing_url(HEARING) + "#horingsbrev"),
        ("horingsnotat", PDF),
    ]
    assert docs[1]["title"] == "NOU 2026: 12" and docs[1]["fetch_status"] == "ok"
    assert result.new_documents == 2 and result.removed_documents == 0


def test_link_moved_from_pdf_to_document_page_is_not_a_removal(source, site):
    site.overrides[hearing_url(HEARING)] = (
        200,
        page_linking_the_pdf_directly(),
        {"content-type": "text/html; charset=utf-8"},
    )
    source.refresh_hearing(HEARING, max_age=0)
    assert [d["url"] for d in documents(source.conn)][1] == PDF
    source.conn.execute("UPDATE hearing SET needs_assessment = 0 WHERE id = ?", (HEARING,))

    del site.overrides[hearing_url(HEARING)]  # now the page links the NOU's page
    result = source.refresh_hearing(HEARING, max_age=0)
    assert not result.changed and result.removed_documents == 0
    assert all(not d["removed"] for d in documents(source.conn))
    assert ("document_removed", "NOU 2026: 12") not in changes(source.conn)
    needs = source.conn.execute(
        "SELECT needs_assessment FROM hearing WHERE id = ?", (HEARING,)
    ).fetchone()[0]
    assert needs == 0  # no reassessment on the letter alone


def test_a_temporary_error_on_the_document_page_changes_nothing(source, site):
    source.refresh_hearing(HEARING, max_age=0)
    site.overrides[DOC_PAGE] = (503, b"busy", {})
    with pytest.raises(FetchError):
        source.refresh_hearing(HEARING, max_age=0)
    assert all(not d["removed"] for d in documents(source.conn))
    assert ("document_removed", "NOU 2026: 12") not in changes(source.conn)


def test_a_document_page_without_pdf_is_kept_and_visible(source, site):
    site.overrides[DOC_PAGE] = (
        200,
        b"<html><body><main><p>Ingen fil</p></main></body></html>",
        {"content-type": "text/html; charset=utf-8"},
    )
    source.refresh_hearing(HEARING, max_age=0)
    nou = documents(source.conn)[1]
    assert nou["url"] == DOC_PAGE and nou["title"] == "NOU 2026: 12"
    assert nou["fetch_status"] == "unsupported"  # listed as not read, never silently dropped
