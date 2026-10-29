"""EEA status from EEA-Lex: parsing the real pages, and the EEA step of the EU run."""

from datetime import date

import pytest
from eu_fakes import EEALEX, EEASite, EUSite, teaser
from pydantic import ValidationError
from test_eu_eurlex import do_run

from horingsradar import db
from horingsradar.eu import db as eu_db
from horingsradar.eu.config import load_eu_settings
from horingsradar.eu.sources import eealex
from horingsradar.http import RobotsRules


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


def page(name: str) -> str:
    return (EEALEX / name).read_text(encoding="utf-8")


def case(conn, com_ref):
    return conn.execute("SELECT * FROM eu_case WHERE com_ref = ?", (com_ref,)).fetchone()


# -- parsing ------------------------------------------------------------------


def test_list_rows_have_key_com_reference_and_title():
    entries = eealex.parse_list(page("list_page0.html"))
    assert len(entries) == 60
    methane = next(e for e in entries if e.key == "329709")
    assert methane.com_refs == ["COM(2026) 569"] and not methane.is_act
    assert methane.title.startswith("Proposal for a Regulation")
    assert methane.url == "https://www.efta.int/eea-lex/329709"
    act = next(e for e in entries if e.key == "32026D2221")
    assert act.is_act and act.com_refs == ["COM(2026) 358"]


def test_com_references_are_normalised():
    assert eealex.com_refs("COM(2026) 0569") == ["COM(2026) 569"]
    assert eealex.com_refs("JOIN (2025) 12 and COM(2025)7") == ["JOIN(2025) 12", "COM(2025) 7"]


def test_factsheets_of_adopted_acts():
    gdpr = eealex.parse_factsheet(page("factsheet_32016r0679.html"))
    assert gdpr.status == "IN_FORCE" and gdpr.jcd == "154/2018" and gdpr.in_force == "Yes"
    assert gdpr.areas == [
        "XI Electronic Communication, Audiovisual Services and Information Society",
        "XI.III Data Protection",
    ]
    ai_act = eealex.parse_factsheet(page("factsheet_32024r1689.html"))
    assert ai_act.status == "SCRUTINY" and ai_act.jcd is None


def test_robots_allow_eea_lex_for_us_but_not_filtered_lists():
    rules = RobotsRules(page("robots.txt"), "Horingsradar-EU/0.1 (kontakt: navn@example.no)")
    assert rules.allowed(eealex.LIST_URL.format(page=3))
    assert rules.allowed("https://www.efta.int/eea-lex/32016r0679")
    assert not rules.allowed("https://www.efta.int/eea-lex?f%5B0%5D=case_status%3A1")
    assert not rules.allowed("https://www.efta.int/eea-lex?keyword=COM")


def test_the_eu_instance_keeps_five_seconds_between_requests(tmp_path):
    settings = load_eu_settings(env_file=tmp_path / "none.env", radar="eu")
    assert settings.request_interval_seconds == 5
    with pytest.raises(ValidationError, match="at least 5"):
        load_eu_settings(env_file=tmp_path / "none.env", radar="eu", request_interval_seconds=2)


# -- the EEA step -------------------------------------------------------------


def test_registered_proposals_and_unregistered_candidates(eu_settings, conn):
    eea = EEASite()
    summary = do_run(eu_settings, conn, EUSite(), eea=eea)
    kids = case(conn, "COM(2026) 681")  # in the list with a numeric key
    assert kids["eea_lex_id"] == "329607" and kids["eea_status"] == "PROPOSED"
    assert kids["eea_status_derived"] == 1
    europol = case(conn, "COM(2026) 558")  # a candidate EEA-Lex does not list
    assert europol["eea_status"] == "NOT_REGISTERED" and europol["eea_lex_id"] is None
    assert case(conn, "COM(2026) 569")["eea_status"] is None  # not a candidate: not looked up
    assert summary["eea_checked"] == 14 and summary["eea_list_pages"] == 2
    assert summary["eea_registered"] == 4 and summary["eea_not_registered"] == 10
    assert "eea_lex" not in summary
    lists = [u for u in eea.requests if "/eea-lex?" in u]
    assert len(lists) == 2  # page 2 is older than every candidate: the scan stops there
    assert not any("/eea-lex/3297" in u for u in eea.requests)  # no proposal fact sheets


def test_adopted_act_gets_status_area_and_jcd_from_its_factsheet(eu_settings, conn):
    site = EUSite()
    site.proposal("681")["eurovoc"]["value"] += " http://eurovoc.europa.eu/5181"
    eea = EEASite()
    eea.add_rows([teaser("32024r1689", "COM(2026) 681", "Regulation (EU) 2024/1689 (test row)")])
    do_run(eu_settings, conn, site, eea=eea)
    kids = case(conn, "COM(2026) 681")
    assert kids["eea_lex_id"] == "32024R1689" and kids["eea_status"] == "SCRUTINY"
    assert kids["eea_status_derived"] == 0
    assert "XI.I Telecommunication Services" in db.loads(kids["eea_areas"])


def test_status_change_is_logged_and_final_status_not_asked_again(eu_settings, conn):
    site, eea = EUSite(), EEASite()
    do_run(eu_settings, conn, site, eea=eea)
    eea.add_rows([teaser("32016r0679", "COM(2026) 681", "Now adopted and in force (test row)")])
    summary = do_run(eu_settings, conn, site, today=date(2026, 11, 5), eea=eea)
    assert summary["eea_status_changes"] == 1
    change = conn.execute("SELECT * FROM eu_case_change WHERE kind = 'eea_status'").fetchone()
    assert (change["old_value"], change["new_value"]) == ("PROPOSED", "IN_FORCE")

    eea.requests.clear()
    do_run(eu_settings, conn, site, today=date(2026, 11, 12), eea=eea)
    assert not any("32016r0679" in u for u in eea.requests)


def test_rate_limit_stops_the_step_without_guessing(eu_settings, conn):
    eea = EEASite(list_status=429)
    summary = do_run(eu_settings, conn, EUSite(), eea=eea)
    assert summary["eea_lex"] == "ufullstendig (se loggen)"
    assert summary["eea_checked"] == 0 and summary["eea_not_registered"] == 0
    assert summary["candidates"] == 14  # the rest of the run is done
    assert len([u for u in eea.requests if "/eea-lex?" in u]) == 3  # the Fetcher's retries


def test_redirect_loop_stops_fact_sheets_after_a_few_requests(eu_settings, conn):
    site = EUSite()
    site.proposal("681")["eurovoc"]["value"] += " http://eurovoc.europa.eu/5181"
    eea = EEASite(loop={"32024r1689"})
    eea.add_rows([teaser("32024r1689", "COM(2026) 681", "Looping fact sheet (test row)")])
    summary = do_run(eu_settings, conn, site, eea=eea)
    assert summary["eea_lex"] == "ufullstendig (se loggen)"
    assert len([u for u in eea.requests if u.endswith("/eea-lex/32024r1689")]) <= 3
    assert case(conn, "COM(2026) 681")["eea_status"] is None
    assert case(conn, "COM(2026) 980")["eea_status"] == "PROPOSED"  # from the list, still set
