"""The EU/EEA radar's PDF report: sections, links and what is shown again."""

from datetime import datetime, timedelta

import pymupdf
import pytest
from eu_fakes import EUSite
from fakes import FakeLLM
from test_eu_assess import KIDS_PDF, case_id, do_run, eu_finding, eu_output, pdf_pages, quote_from

from horingsradar.eu import db as eu_db
from horingsradar.eu.assess import assess_case
from horingsradar.eu.config import load_eu_settings
from horingsradar.eu.report import build_report

NOW = datetime(2026, 10, 29, 12, 0).astimezone()
KIDS, EUROPOL = "COM(2026) 681", "COM(2026) 558"


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


def by_case(mapping):
    """Scripted output depending on the COM reference in the prompt."""

    def pick(params):
        user = params["messages"][0]["content"]
        return next((out for ref, out in mapping.items() if ref in user), eu_output("ingen"))

    return pick


def kids_output(relevance="hoy"):
    page5 = quote_from(pdf_pages(KIDS_PDF)[5])
    return eu_output(
        relevance,
        findings=[
            eu_finding(page5, 5),
            eu_finding("A sentence the memorandum does not contain anywhere at all.", 7),
        ],
    )


SCRIPT = by_case(
    {
        KIDS: kids_output(),
        EUROPOL: eu_output("lav", relevance_reason="Avtalen berører personopplysninger perifert."),
    }
)


def assessed(eu_settings, conn):
    do_run(eu_settings, conn, EUSite(), client=FakeLLM(default=SCRIPT))


def text_of(result) -> str:
    with pymupdf.open(result["path"]) as doc:
        return " ".join(page.get_text() for page in doc).replace("\n", " ")


def links_of(result) -> list[str]:
    with pymupdf.open(result["path"]) as doc:
        return [link.get("uri") for page in doc for link in page.get_links() if link.get("uri")]


def test_first_report_has_every_section_and_marks_quotes(eu_settings, conn):
    assessed(eu_settings, conn)
    result = build_report(eu_settings, conn, now=NOW)
    assert result["cases"] == 14 and result["detailed"] == 1 and result["older"] == 0
    text = text_of(result)
    assert "EU/EØS-radar" in text and "Forslag fra Europakommisjonen" in text
    assert "67 forslag, 14 kandidater, 53 filtrert bort uten LLM" in text
    for heading in ("Oversikt", "Høy og middels relevans", "Lav og ingen relevans", "Ikke vurdert"):
        assert heading in text
    assert "EU KIDS ACT" in text and "COM(2026) 681" in text
    assert "Sitat verifisert mot teksten" in text and "UVERIFISERT" in text
    assert "Registrert i EEA-Lex som mulig EØS-relevant (utledet)" in text
    assert "Avtalen berører personopplysninger perifert." in text  # the low one, one line
    assert "Ingen engelsk PDF i Cellar ennå" in text  # why the rest are not assessed
    assert "Tidligere forslag" not in text  # nothing older than 8 weeks
    links = links_of(result)
    assert "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:52026PC0681" in links
    assert any(u.endswith("/DOC_1#page=5") for u in links)  # the finding's page in the PDF
    assert "https://www.efta.int/eea-lex/329607" in links
    reported = conn.execute(
        "SELECT COUNT(*) FROM eu_case WHERE reported_in = ?", (result["report_id"],)
    )
    assert reported.fetchone()[0] == 14
    for word in ("datatilsyn", "jurist"):
        assert word not in text.lower()


def test_next_report_shows_nothing_again_without_changes(eu_settings, conn):
    assessed(eu_settings, conn)
    build_report(eu_settings, conn, now=NOW)
    again = build_report(eu_settings, conn, now=NOW)
    assert again["cases"] == 0
    assert "Ingen nye forslag fra de siste 8 ukene" in text_of(again)


def test_changed_relevance_is_shown_again_and_same_result_is_not(eu_settings, conn):
    assessed(eu_settings, conn)
    build_report(eu_settings, conn, now=NOW)
    assess_case(eu_settings, conn, case_id(conn, KIDS), FakeLLM(default=kids_output("middels")))
    assess_case(eu_settings, conn, case_id(conn, EUROPOL), FakeLLM(default=SCRIPT))  # lav again
    result = build_report(eu_settings, conn, now=NOW)
    assert result["cases"] == 1 and result["detailed"] == 1
    text = text_of(result)
    assert "Endret relevans" in text and "Høy → Middels" in text
    assert "Avtalen berører" not in text


def test_status_change_alone_is_one_line(eu_settings, conn):
    assessed(eu_settings, conn)
    first = build_report(eu_settings, conn, now=NOW)
    # Detected after the report (in practice by a later run; here maybe the same millisecond).
    reported = conn.execute("SELECT created_at FROM eu_report WHERE id = ?", (first["report_id"],))
    later = (datetime.fromisoformat(reported.fetchone()[0]) + timedelta(seconds=1)).isoformat()
    for ref, old in ((KIDS, "PROP_PENDING"), (EUROPOL, None)):  # Europol is low: not shown
        conn.execute(
            "INSERT INTO eu_case_change (case_id, detected_at, kind, old_value, new_value)"
            " VALUES (?, ?, 'procedure_status', ?, 'PROP_ADOPTED')",
            (case_id(conn, ref), later, old),
        )
    result = build_report(eu_settings, conn, now=NOW)
    assert result["cases"] == result["status_changes"] == 1 and result["detailed"] == 0
    text = text_of(result)
    assert "Endringer i tidligere rapporterte saker" in text
    assert "Prosedyren: Til behandling → Vedtatt" in text


def test_first_assessment_after_not_assessed_is_shown_whatever_the_level(eu_settings, conn):
    do_run(eu_settings, conn, EUSite(), assess=False)
    first = build_report(eu_settings, conn, now=NOW)
    assert "Venter på vurdering." in text_of(first)
    assess_case(eu_settings, conn, case_id(conn, EUROPOL), FakeLLM(default=SCRIPT))  # lav
    result = build_report(eu_settings, conn, now=NOW)
    assert result["cases"] == 1
    text = text_of(result)
    assert "Ny vurdering" in text and "Avtalen berører personopplysninger perifert." in text


def test_cases_older_than_eight_weeks_get_one_line(eu_settings, conn):
    assessed(eu_settings, conn)
    result = build_report(eu_settings, conn, now=datetime(2026, 12, 31, 12, 0).astimezone())
    assert result["detailed"] == 0 and result["older"] == 14
    text = text_of(result)
    assert "Tidligere forslag" in text and "Kandidater fra før 05.11.2026" in text
    assert "EU KIDS ACT" in text and "UVERIFISERT" not in text  # one line, no findings


def test_report_dates_use_the_report_day(eu_settings, conn):
    assessed(eu_settings, conn)
    result = build_report(eu_settings, conn, now=NOW)
    assert "til og med 29.10.2026" in text_of(result)
    later = build_report(eu_settings, conn, now=datetime(2026, 11, 5, 12, 0).astimezone())
    assert "–05.11.2026" in text_of(later)
