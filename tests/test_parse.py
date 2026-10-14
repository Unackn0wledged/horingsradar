from datetime import date

from conftest import LIST_URL, fixture_text, hearing_url

from horingsradar.sources.regjeringen.parse import (
    hearing_id_from_url,
    html_to_text,
    normalize_status,
    parse_date,
    parse_hearing,
    parse_listing,
    parse_responses,
)


def hearing(hearing_id: int):
    return parse_hearing(fixture_text(f"hearing_{hearing_id}.html"), hearing_url(hearing_id))


def test_parse_date_and_status_variants():
    assert parse_date("Høringsfrist: 15.01.2027") == date(2027, 1, 15)
    assert parse_date("Høyringsfrist: 1.2.2025") == date(2025, 2, 1)
    assert parse_date("31.02.2025") is None
    assert parse_date(None) is None
    assert normalize_status("På høring") == "open"
    assert normalize_status("På  høyring") == "open"
    assert normalize_status("Under behandling") == "under_behandling"
    assert normalize_status("Ferdigbehandla") == "ferdigbehandlet"
    assert normalize_status("Noe nytt") == "unknown"
    assert normalize_status(None) == "unknown"


def test_hearing_id_from_url():
    assert hearing_id_from_url("https://www.regjeringen.no/no/dokumenter/x/id3177107/") == 3177107
    assert hearing_id_from_url("/no/dokumenter/x/id3177107/?showSvar=true") == 3177107
    assert hearing_id_from_url("https://www.regjeringen.no/contentassets/abc/x.pdf") is None


def test_listing_page_current():
    page = parse_listing(fixture_text("list_page1.html"), LIST_URL)
    assert len(page.items) == 20
    assert page.total == 7007
    assert page.next_url == f"{LIST_URL}?page=2"
    first = page.items[0]
    assert first.id == 3177107
    assert first.title == "Høring forskrift om utdanningsstøtte"
    assert first.published_date == date(2026, 10, 14)
    assert first.deadline == date(2027, 1, 15)
    assert first.department == "Kunnskapsdepartementet"
    assert first.status_raw == "På høring"
    assert first.status == "open"
    assert first.excerpt.startswith("Kunnskapsdepartementet sender")


def test_listing_page_nynorsk_and_closed_statuses():
    page = parse_listing(fixture_text("list_page15_2025.html"), LIST_URL)
    by_id = {item.id: item for item in page.items}
    nynorsk = by_id[3117588]
    assert nynorsk.doc_type == "Høyring"
    assert nynorsk.deadline == date(2025, 12, 1)
    assert nynorsk.status_raw == "Ferdigbehandla"
    assert nynorsk.status == "ferdigbehandlet"
    assert by_id[3119016].status == "under_behandling"
    assert all(item.published_date is not None for item in page.items)


def test_hearing_open_bokmal():
    h = hearing(3177107)
    assert h.title == "Høring forskrift om utdanningsstøtte"
    assert h.doc_type == "Høring"
    assert h.published_date == date(2026, 10, 14)
    assert h.department == "Kunnskapsdepartementet"
    assert h.original_department is None
    assert h.status == "open"
    assert h.deadline == date(2027, 1, 15)
    assert h.ingress.startswith("Kunnskapsdepartementet sender med dette")
    assert h.responses_url is None
    assert not h.dt_on_address_list
    kinds = [(d.kind, d.is_inline) for d in h.documents]
    assert kinds == [("horingsbrev", True), ("horingsnotat", False)]
    assert h.documents[1].url.endswith("horingsnotat-forskrift-om-utdanningsstotte.pdf")
    assert "15. januar 2027" in html_to_text(h.letter_html)


def test_hearing_nynorsk_labels():
    h = hearing(3176758)
    assert h.doc_type == "Høyring"
    assert h.status_raw == "På høyring"
    assert h.status == "open"
    assert h.deadline == date(2026, 11, 23)
    assert h.documents[1].kind == "horingsnotat"
    assert len(h.address_list) > 100


def test_hearing_original_department():
    h = hearing(3017719)
    assert h.department == "Energidepartementet"
    assert h.original_department == "Olje- og energidepartementet"
    assert h.published_date == date(2023, 12, 8)


def test_hearing_address_list_and_datatilsynet():
    h = hearing(3170511)
    assert h.dt_on_address_list
    assert "Datatilsynet" in h.address_list
    assert h.address_list[0] == "Departementene"  # "Høringsinstanser:" label skipped
    br_separated = hearing(3118997)
    assert "Datatilsynet" in br_separated.address_list
    assert "Akademikerne" in br_separated.address_list


def test_hearing_attachments_and_draft_classification():
    h = hearing(3118997)
    kinds = [d.kind for d in h.documents]
    assert kinds == ["horingsbrev", "horingsnotat", "annet", "annet", "annet", "annet"]
    draft = hearing(3117588)
    assert [d.kind for d in draft.documents] == ["horingsbrev", "horingsnotat", "lovutkast"]


def test_hearing_inline_notat():
    h = hearing(3116453)
    inline = [d for d in h.documents if d.is_inline and d.kind == "horingsnotat"]
    assert len(inline) == 1
    assert "klimarådgiving" in html_to_text(inline[0].inline_html)
    # The report linked from the letter is an attachment, not a second notat.
    assert [d.kind for d in h.documents if not d.is_inline] == ["annet"]


def test_hearing_closed_has_responses_url():
    h = hearing(3014731)
    assert h.status == "ferdigbehandlet"
    assert h.responses_url == hearing_url(3014731) + "?showSvar=true"
    # Links to the result (Lovdata) and responses are not documents.
    assert all("lovdata" not in d.url for d in h.documents)


def test_hearing_external_links_are_not_documents():
    h = hearing(3171614)
    assert all("regjeringen.no" in d.url for d in h.documents)
    assert [d.kind for d in h.documents] == ["horingsbrev", "horingsnotat"]


def test_responses_page():
    page = parse_responses(fixture_text("svar_3118997.html"))
    assert page.total == 35
    assert len(page.entries) == 35
    dt = page.find("Datatilsynet")
    assert dt is not None and not dt.no_comments
    brreg = page.find("Brønnøysundregistrene")
    assert brreg.no_comments
    assert parse_responses(fixture_text("svar_3116453.html")).find("Datatilsynet") is None


def test_listing_pagination_on_later_pages():
    # Only page 1 has <link rel="next">; later pages must use the visible pager.
    page = parse_listing(fixture_text("list_page2.html"), LIST_URL)
    assert page.next_url == f"{LIST_URL}?page=3"
    assert len(page.items) == 20
