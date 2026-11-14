"""PDF of one hearing (hearing.html.j2), made for the website's «Last ned PDF».

h4 headings are upper case in the PDF (CSS text-transform), so the text has «HVA FORESLÅS».
"""

import pymupdf
import pytest
from fakes import FakeLLM, FakeSite, by_hearing, make_fetcher, output
from test_report import NOW, SCRIPTED, TODAY, pdf_text

from horingsradar import cli, db
from horingsradar.config import get_settings
from horingsradar.jobs import job_context
from horingsradar.pipeline import run
from horingsradar.report.build import (
    HearingNotFound,
    build_hearing_pdf,
    build_report,
    hearing_pdf_path,
)

HOY, MIDDELS, LAV = 3170511, 3176758, 3177107

# Parts that only belong in the report of many hearings.
REPORT_ONLY = (
    "Oversikt",
    "Periode:",
    "Høringer:",
    "Frister som nærmer seg",
    "Endringer i tidligere rapporterte høringer",
    "Endret siden rapporten",
    "Høy og middels relevans",
    "Lav og ingen relevans",
    "Til oversikten",
    "Ingen høringer",
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


def hearing_text(settings, conn, hearing_id):
    path = build_hearing_pdf(settings, conn, hearing_id, now=NOW)
    return pdf_text(str(path))


def assert_no_report_parts(text):
    for part in REPORT_ONLY:
        assert part not in text, part


def report_state(conn):
    return (
        conn.execute("SELECT COUNT(*) FROM report").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM report_item").fetchone()[0],
        conn.execute("SELECT id, reported_in FROM hearing ORDER BY id").fetchall(),
    )


def test_high_relevance_has_the_full_analysis(settings, conn):
    text = hearing_text(settings, conn, HOY)
    title = conn.execute("SELECT title FROM hearing WHERE id = ?", (HOY,)).fetchone()[0]
    assert "Høringsradar" in text and "KI-generert" in text
    assert title.split()[0] in text and "Høy" in text
    for part in (
        "Generert: 14.10.2026 kl. 09:30",
        "Vurdert:",
        "Modell og prompt:",
        "Departement",
        "Datatilsynet på adresselisten",
        "Eget personvernkapittel",
        "HVA FORESLÅS",
        "BEGRUNNELSE FOR RELEVANSNIVÅ",
        "FUNN",
        "Sitat verifisert mot dokumentteksten",
        "LESEGUIDE",
    ):
        assert part in text, part
    assert_no_report_parts(text)


def test_unverified_quote_is_marked(settings, conn):
    text = hearing_text(settings, conn, HOY)
    assert "UVERIFISERT" in text
    assert "Et sitat som ikke står i dokumentet" in text  # shown, not removed


def test_page_numbers_link_to_the_page(settings, conn):
    path = build_hearing_pdf(settings, conn, HOY, now=NOW)
    pdf_url = next(
        d["url"] for d in db.documents_for(conn, HOY) if d["url"].lower().endswith(".pdf")
    )
    with pymupdf.open(path) as doc:
        uris = {link["uri"] for page in doc for link in page.get_links() if "uri" in link}
    assert f"{pdf_url}#page=7" in uris and f"{pdf_url}#page=6" in uris


@pytest.mark.parametrize("hearing_id,label", [(MIDDELS, "Middels"), (LAV, "Lav")])
def test_medium_and_low_have_the_full_analysis(settings, conn, hearing_id, label):
    text = hearing_text(settings, conn, hearing_id)
    assert label in text
    for part in ("HVA FORESLÅS", "BEGRUNNELSE FOR RELEVANSNIVÅ", "FUNN", "LESEGUIDE"):
        assert part in text, part
    assert "Departementet foreslår en ny forskrift" in text  # the summary
    assert_no_report_parts(text)


def test_no_relevance_has_the_full_analysis(settings, conn):
    hearing_id = conn.execute(
        "SELECT hearing_id FROM assessment WHERE relevance = 'ingen' LIMIT 1"
    ).fetchone()[0]
    text = hearing_text(settings, conn, hearing_id)
    assert "Ingen" in text and "Personopplysninger berøres ikke." in text
    assert "HVA FORESLÅS" in text and "Ingen konkrete funn oppgitt." in text
    assert "Ingen sider pekt ut." in text
    assert_no_report_parts(text)


def test_failed_assessment_without_internal_error_text(settings):
    llm = FakeLLM(default=by_hearing({HOY: "not json"}, output("ingen", reading_guide=[])))
    conn = _run(settings, llm)
    a = db.latest_assessment(conn, HOY)
    assert a["status"] != "ok" and a["error"]
    text = hearing_text(settings, conn, HOY)
    assert "Feilet" in text and "Vurderingen feilet" in text
    assert a["error"] not in text
    assert "HVA FORESLÅS" not in text
    assert_no_report_parts(text)
    conn.close()


def test_not_assessed(settings):
    conn = _run(settings, assess=False)
    text = hearing_text(settings, conn, HOY)
    assert "Ikke vurdert" in text and "Venter på vurdering" in text
    assert "Vurdert: –" in text and "HVA FORESLÅS" not in text
    assert_no_report_parts(text)
    conn.close()


def test_does_not_touch_report_state(settings, conn):
    build_report(settings, conn, now=NOW)  # something reported, so reported_in is set
    before = report_state(conn)
    path = build_hearing_pdf(settings, conn, HOY, now=NOW)
    assert path == hearing_pdf_path(settings, HOY) and path.is_file()
    assert report_state(conn) == before


def test_changes_since_last_report_are_not_shown(settings, conn):
    build_report(settings, conn, now=NOW)
    conn.execute("UPDATE hearing SET deadline = '2026-12-31' WHERE id = ?", (HOY,))
    conn.execute(
        "INSERT INTO hearing_change (hearing_id, detected_at, kind, old_value, new_value)"
        " VALUES (?, ?, 'deadline', '2026-11-01', '2026-12-31')",
        (HOY, db.now_iso()),
    )
    conn.commit()
    text = hearing_text(settings, conn, HOY)
    assert "31.12.2026" in text
    assert "Ny frist" not in text and "Endret siden rapporten" not in text


def test_uses_the_operational_assessment_not_a_backtest_one(settings, conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(assessment)") if r[1] != "id"]
    copy = [
        {
            "context": "'backtest'",
            "relevance": "'ingen'",
            "created_at": "'2099-01-01T00:00:00'",
        }.get(c, c)
        for c in cols
    ]
    conn.execute(
        f"INSERT INTO assessment ({', '.join(cols)}) SELECT {', '.join(copy)}"
        " FROM assessment WHERE hearing_id = ? AND context = 'drift'",
        (HOY,),
    )
    conn.commit()
    text = hearing_text(settings, conn, HOY)
    assert "Høy" in text and "UVERIFISERT" in text


def test_unknown_or_untracked_hearing_is_an_error(settings, conn):
    with pytest.raises(HearingNotFound):
        build_hearing_pdf(settings, conn, 1, now=NOW)
    conn.execute("UPDATE hearing SET tracked = 0 WHERE id = ?", (LAV,))
    conn.commit()
    with pytest.raises(HearingNotFound):
        build_hearing_pdf(settings, conn, LAV, now=NOW)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def test_cli_writes_the_pdf_and_takes_no_job(env, capsys, tmp_path):
    _run(env).close()
    out = tmp_path / "en-horing.pdf"
    assert cli.main(["hearing-pdf", str(HOY), "--out", str(out)]) == 0
    assert capsys.readouterr().out.strip() == str(out)
    assert out.is_file() and "HVA FORESLÅS" in pdf_text(str(out))
    conn = db.connect(env.db_path)
    assert conn.execute("SELECT COUNT(*) FROM job WHERE kind != 'run'").fetchone()[0] == 0
    conn.close()


def test_cli_unknown_hearing(env, capsys):
    _run(env).close()
    assert cli.main(["hearing-pdf", "1"]) == 1
    assert "Fant ingen høring i drift med ID 1" in capsys.readouterr().err
