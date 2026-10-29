"""Status in the European Parliament from OEIL, for high and medium cases."""

from datetime import date, datetime, timedelta

import pytest
from eu_fakes import OEIL, EUSite
from fakes import FakeLLM
from test_eu_assess import do_run
from test_eu_report import EUROPOL, KIDS, NOW, SCRIPT, links_of, text_of

from horingsradar.eu import db as eu_db
from horingsradar.eu.config import load_eu_settings
from horingsradar.eu.report import build_report
from horingsradar.eu.sources import oeil

KIDS_OEIL = "2026/0286(COD)"
EUCCS = "COM(2026) 1010"  # not assessed in these tests (no PDF): looked up too


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


def page(ref: str) -> oeil.ProcedureFile:
    name = ref.replace("/", "_").replace("(", "_").removesuffix(")")
    return oeil.parse_page((OEIL / f"procedure_{name}.html").read_text("utf-8"))


def case(conn, com_ref):
    return conn.execute("SELECT * FROM eu_case WHERE com_ref = ?", (com_ref,)).fetchone()


def assessed(eu_settings, conn, site, today=date(2026, 10, 29)):
    return do_run(eu_settings, conn, site, client=FakeLLM(default=SCRIPT), today=today)


# -- parsing the real pages ---------------------------------------------------------


def test_reference_from_cellar_form():
    assert oeil.oeil_ref("2026/0165/COD") == "2026/0165(COD)"
    assert oeil.oeil_ref("2026/0320/NLE") == "2026/0320(NLE)"
    assert oeil.oeil_ref("COM(2026) 580") is None
    assert oeil.page_url("2026/0165(COD)") == (
        "https://oeil.europarl.europa.eu/oeil/en/procedure-file?reference=2026/0165(COD)"
    )


def test_procedure_file_has_title_status_committee_and_events():
    europol = page("2026/0165(COD)")
    assert (europol.reference, europol.title) == ("2026/0165(COD)", "New Europol Regulation")
    assert europol.status == "Awaiting committee decision" and not europol.final
    assert europol.committees == ["LIBE Civil Liberties, Justice and Home Affairs"]
    assert [(e.date, e.event, e.reference) for e in europol.events] == [
        ("2026-06-24", "Legislative proposal published", "COM(2026)0580"),
        ("2026-09-14", "Committee referral announced in Parliament, 1st reading", None),
    ]


def test_joint_committee_completed_and_pending_referral():
    ai_act = page("2021/0106(COD)")
    assert ai_act.status == "Procedure completed" and ai_act.final
    assert [c.split()[0] for c in ai_act.committees] == ["IMCO", "LIBE"]
    assert ai_act.events[-1].event == "Final act published in Official Journal"
    euccs = page("2026/0301(COD)")
    assert euccs.status == "Preparatory phase in Parliament" and euccs.committees == []
    csam = page("2022/0155(COD)")
    assert csam.status == "Awaiting Parliament's position in 1st reading"
    assert len(csam.events) == 9


def test_no_person_names_are_read():
    for ref in ("2026/0165(COD)", "2021/0106(COD)", "2022/0155(COD)"):
        p = page(ref)
        text = " ".join([p.title or "", p.status or "", *p.committees])
        text += " ".join(f"{e.event} {e.reference}" for e in p.events)
        assert "NAVN" not in text  # names are replaced in the fixtures; none is parsed


def test_a_page_that_is_not_a_procedure_file_stops_the_step():
    with pytest.raises(oeil.OEILUnavailable):
        oeil.parse_page((OEIL / "not_found.html").read_text("utf-8"))


def test_status_labels_in_norwegian_and_unknown_as_is():
    assert oeil.status_label("Awaiting committee decision") == "Venter på komitébehandling"
    assert oeil.status_label("Something new") == "Something new"
    assert oeil.status_label(None) == "–"


# -- the step in `run` ------------------------------------------------------------


def test_run_looks_up_high_medium_and_unassessed_cases_only(eu_settings, conn):
    site = EUSite()
    summary = assessed(eu_settings, conn, site)
    kids = case(conn, KIDS)
    assert (kids["oeil_ref"], kids["oeil_title"]) == (KIDS_OEIL, "EU KIDS Act")
    assert kids["oeil_status"] == "Awaiting committee decision"
    events = conn.execute(
        "SELECT date, event FROM eu_oeil_event WHERE case_id = ? ORDER BY date", (kids["id"],)
    ).fetchall()
    assert [tuple(e) for e in events] == [
        ("2026-09-17", "Legislative proposal published"),
        ("2026-10-22", "Committee referral announced in Parliament, 1st reading"),
    ]
    assert case(conn, EUCCS)["oeil_status"] == "Preparatory phase in Parliament"
    # Europol-Peru is assessed as low: not looked up.
    europol = case(conn, EUROPOL)
    assert oeil.oeil_ref(europol["procedure_ref"]) not in site.oeil_requests
    assert europol["oeil_checked_at"] is None
    # New NLE procedures are not in OEIL yet: checked, nothing stored, tried again next run.
    missing = [r for r in site.oeil_requests if r.endswith("(NLE)")]
    assert missing and summary["oeil_found"] == 2
    assert summary["oeil_checked"] == len(site.oeil_requests)


def test_status_change_is_logged_and_one_line_in_the_report(eu_settings, conn):
    site = EUSite()
    assessed(eu_settings, conn, site)
    first = build_report(eu_settings, conn, now=NOW)
    site.oeil_pages[KIDS_OEIL] = site.oeil_pages[KIDS_OEIL].replace(
        "Awaiting committee decision", "Awaiting Parliament's position in 1st reading"
    )
    summary = assessed(eu_settings, conn, site, today=date(2026, 11, 5))
    assert summary["oeil_status_changes"] == 1
    change = conn.execute("SELECT * FROM eu_case_change WHERE kind = 'oeil_status'").fetchone()
    assert (change["old_value"], change["new_value"]) == (
        "Awaiting committee decision",
        "Awaiting Parliament's position in 1st reading",
    )
    reported = conn.execute("SELECT created_at FROM eu_report WHERE id = ?", (first["report_id"],))
    later = (datetime.fromisoformat(reported.fetchone()[0]) + timedelta(seconds=1)).isoformat()
    conn.execute("UPDATE eu_case_change SET detected_at = ? WHERE id = ?", (later, change["id"]))
    result = build_report(eu_settings, conn, now=NOW)
    assert result["status_changes"] == 1 and result["detailed"] == 0
    assert (
        "Europaparlamentet: Venter på komitébehandling → "
        "Venter på Parlamentets standpunkt, 1. lesning" in text_of(result)
    )


def test_finished_procedures_are_not_fetched_again(eu_settings, conn):
    site = EUSite()
    assessed(eu_settings, conn, site)
    conn.execute(
        "UPDATE eu_case SET oeil_status = 'Procedure completed' WHERE com_ref = ?", (KIDS,)
    )
    site.oeil_requests.clear()
    assessed(eu_settings, conn, site, today=date(2026, 11, 5))
    assert KIDS_OEIL not in site.oeil_requests
    assert "2026/0301(COD)" in site.oeil_requests  # still open


def test_an_unreadable_page_stops_only_this_step(eu_settings, conn):
    site = EUSite()
    site.oeil_pages[KIDS_OEIL] = "<html><body>Maintenance</body></html>"
    summary = assessed(eu_settings, conn, site)
    assert summary["oeil"] == "ufullstendig (se loggen)"
    assert summary["cases_assessed"] == 2  # the rest of the run is done


def test_report_shows_the_parliament_row_with_a_link(eu_settings, conn):
    assessed(eu_settings, conn, EUSite())
    result = build_report(eu_settings, conn, now=NOW)
    text = text_of(result)
    assert (
        "EU KIDS Act: Venter på komitébehandling · Komité: IMCO Internal Market and Consumer"
        " Protection" in text
    )
    assert "Siste hendelse 22.10.2026: Committee referral announced in Parliament" in text
    assert "Status i Europaparlamentet er hentet fra Legislative Observatory" in text
    assert oeil.page_url(KIDS_OEIL) in links_of(result)
