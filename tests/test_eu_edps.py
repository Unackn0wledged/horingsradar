"""EDPS opinions from the Council's documents in Cellar, and the EDPS rule (at least middels)."""

import copy
import json
from datetime import date, datetime, timedelta

import pytest
from eu_fakes import EDPS, EUSite, load_edps, make_eu_fetcher
from fakes import FakeLLM
from test_eu_assess import case_id, do_run, eu_output
from test_eu_report import EUROPOL, KIDS, NOW, SCRIPT, links_of, text_of

from horingsradar.eu import db as eu_db
from horingsradar.eu import rules
from horingsradar.eu.assess import assess_case
from horingsradar.eu.config import load_eu_settings
from horingsradar.eu.pipeline import update_edps
from horingsradar.eu.report import build_report
from horingsradar.eu.sources import edps
from horingsradar.eu.sources.eurlex import EurLexClient

TODAY = date(2026, 10, 29)
EIO = "COM(2026) 313"  # European Investigation Order recast: not a candidate on its own


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


def notes() -> list[edps.CouncilNote]:
    return edps.parse_notes(json.loads((EDPS / "council_notes.json").read_text("utf-8")))


def note(doc: str) -> edps.CouncilNote:
    return next(n for n in notes() if n.doc == doc)


def case(conn, celex):
    return conn.execute("SELECT * FROM eu_case WHERE celex = ?", (celex,)).fetchone()


def links(conn, celex) -> list[tuple[str, str]]:
    return [
        (r["doc"], r["method"])
        for r in conn.execute(
            "SELECT e.doc, l.method FROM eu_edps_link l JOIN eu_edps e USING (work_uri)"
            " JOIN eu_case c ON c.id = l.case_id WHERE c.celex = ?",
            (celex,),
        )
    ]


def note_for(site: EUSite, celex: str) -> None:
    """A Council note (the real Europol one, copied) about the proposal `celex`."""
    binding = copy.deepcopy(
        next(
            b
            for b in load_edps("council_notes.json")["results"]["bindings"]
            if "ST_12655" in b["doc"]["value"]
        )
    )
    binding["work"]["value"] += "-copy"
    binding["doc"]["value"] = "consil:ST_99999_2026_INIT"
    binding["celexes"]["value"] = celex
    site.edps_notes.append(binding)


# -- parsing the real response ------------------------------------------------------


def test_council_notes_have_document_procedure_and_proposals():
    found = notes()
    assert len(found) == 31
    europol = note("ST 12655 2026 INIT")
    assert europol.date == "2026-09-02"
    assert (europol.procedures, europol.celexes) == (["2026/0165/COD"], ["52026PC0580"])
    assert europol.url == "https://data.consilium.europa.eu/doc/document/ST-12655-2026-INIT/en/pdf"
    cloud = note("WK 13042 2026 INIT")
    assert cloud.celexes == ["52026PC0502"]
    assert cloud.url.endswith("/WK-13042-2026-INIT/en/pdf")
    pair = note("ST 7502 2025 INIT")  # one opinion on two proposals
    assert pair.celexes == ["52025PC0022", "52025PC0023"]
    assert sum(1 for n in found if not n.procedures) == 14


def test_proposal_title_from_both_ways_of_naming_the_note():
    eurojust = note("ST 12449 2026 INIT")  # "Proposal for ... - Opinion of the EDPS (EDPS)"
    assert eurojust.proposal_title == edps.normalize_title(
        "Proposal for a REGULATION OF THE EUROPEAN PARLIAMENT AND OF THE COUNCIL on the European"
        " Union Agency for Criminal Justice Cooperation (Eurojust) and repealing Regulation (EU)"
        " 2018/1727"
    )
    pension = note("ST 5225 2026 INIT")  # "Opinion of the EDPS on the Proposal for ..."
    assert pension.proposal_title.startswith("proposal for a directive of the european parliament")
    assert note("ST 7502 2025 INIT").proposal_title is None  # "on the Proposals for Council ..."


def test_query_asks_for_council_documents_in_a_window():
    q = edps.notes_query(date(2026, 1, 1), date(2026, 7, 1))
    assert "corporate-body/CONSIL" in q and "Data Protection Supervisor" in q
    assert '"2026-01-01"^^xsd:date' in q and '"2026-07-01"^^xsd:date' in q


# -- linking in `run` ------------------------------------------------------------------


def test_run_links_by_procedure_and_by_exact_title(eu_settings, conn):
    site = EUSite()
    site.add_edps()
    summary = do_run(eu_settings, conn, site, assess=False)
    assert summary["edps_notes"] == 11  # the notes from the last 6 months
    assert links(conn, "52026PC0580") == [("ST 12655 2026 INIT", "procedure")]  # Europol
    assert links(conn, "52026PC0502") == [("WK 13042 2026 INIT", "procedure")]  # Cloud and AI
    assert links(conn, "52026PC0570") == [("ST 12449 2026 INIT", "title")]  # Eurojust
    assert links(conn, "52026PC0313") == [("ST 12445 2026 INIT", "title")]  # EIO recast
    assert summary["edps_new_links"] == 4 and summary["edps_cases"] == 4
    changes = conn.execute(
        "SELECT new_value FROM eu_case_change WHERE kind = 'edps' ORDER BY new_value"
    ).fetchall()
    assert len(changes) == 4


def test_an_opinion_makes_the_proposal_a_candidate(eu_settings, conn):
    site = EUSite()
    site.add_edps(proposals=False)
    do_run(eu_settings, conn, site, assess=False)
    eio = case(conn, "52026PC0313")
    assert eio["candidate"] == 1 and eio["needs_assessment"] == 1
    assert json.loads(eio["prefilter"])["edps"] == ["ST 12445 2026 INIT"]
    kinds = [
        r["kind"]
        for r in conn.execute("SELECT kind FROM eu_case_change WHERE case_id = ?", (eio["id"],))
    ]
    assert sorted(kinds) == ["candidate", "edps"]
    # A second run finds the same notes and logs nothing new.
    before = conn.execute("SELECT COUNT(*) FROM eu_case_change").fetchone()[0]
    summary = do_run(eu_settings, conn, site, assess=False, today=date(2026, 11, 5))
    assert summary["edps_new_links"] == 0
    assert conn.execute("SELECT COUNT(*) FROM eu_case_change").fetchone()[0] == before


def test_a_title_that_fits_two_cases_links_neither(eu_settings, conn):
    site = EUSite()
    site.add_edps()
    twin = copy.deepcopy(next(b for b in site.proposals if b["celex"]["value"] == "52026PC0570"))
    twin["celex"]["value"], twin["number"]["value"] = "52026PC0999", "999"
    twin["work"]["value"] += "-twin"
    site.proposals.append(twin)
    do_run(eu_settings, conn, site, assess=False)
    assert links(conn, "52026PC0570") == [] and links(conn, "52026PC0999") == []


# -- the rule ------------------------------------------------------------------------


def test_rule_raises_low_and_none_and_never_lowers():
    assert rules.final_relevance("lav", True) == ("middels", "edps")
    assert rules.final_relevance("ingen", True) == ("middels", "edps")
    assert rules.final_relevance("middels", True) == ("middels", None)
    assert rules.final_relevance("hoy", True) == ("hoy", None)
    assert rules.final_relevance("lav", False) == ("lav", None)


def test_rule_applies_when_a_case_with_an_opinion_is_assessed(eu_settings, conn):
    site = EUSite()
    note_for(site, "52026PC0558")  # the Europol-Peru agreement has a fixture PDF
    do_run(eu_settings, conn, site, assess=False)
    assess_case(eu_settings, conn, case_id(conn, EUROPOL), FakeLLM(default=eu_output("lav")))
    a = conn.execute("SELECT * FROM eu_assessment ORDER BY id DESC LIMIT 1").fetchone()
    assert (a["llm_relevance"], a["relevance"], a["rule"]) == ("lav", "middels", "edps")


def test_opinion_after_the_assessment_raises_it_without_an_llm_call(eu_settings, conn):
    site = EUSite()
    do_run(eu_settings, conn, site, client=FakeLLM(default=SCRIPT))  # Europol-Peru: lav
    first = build_report(eu_settings, conn, now=NOW)
    old = conn.execute(
        "SELECT * FROM eu_assessment WHERE case_id = ? ORDER BY id DESC LIMIT 1",
        (case_id(conn, EUROPOL),),
    ).fetchone()
    assert old["relevance"] == "lav"

    note_for(site, "52026PC0558")
    fetcher = make_eu_fetcher(eu_settings, site)
    update_edps(conn, EurLexClient(fetcher), TODAY - timedelta(days=60), TODAY)
    new = conn.execute(
        "SELECT * FROM eu_assessment WHERE case_id = ? ORDER BY id DESC LIMIT 1",
        (case_id(conn, EUROPOL),),
    ).fetchone()
    assert new["id"] != old["id"]
    assert (new["llm_relevance"], new["relevance"], new["rule"]) == ("lav", "middels", "edps")
    assert new["input_tokens"] == new["output_tokens"] == 0 and new["cost_estimate"] == 0
    assert rules.EDPS_NOTE in json.loads(new["notes"])
    assert (new["summary"], new["relevance_reason"]) == (old["summary"], old["relevance_reason"])

    # Shown again in full: the level changed. Detected after the first report.
    reported = conn.execute("SELECT created_at FROM eu_report WHERE id = ?", (first["report_id"],))
    later = (datetime.fromisoformat(reported.fetchone()[0]) + timedelta(seconds=1)).isoformat()
    conn.execute("UPDATE eu_assessment SET created_at = ? WHERE id = ?", (later, new["id"]))
    conn.execute("UPDATE eu_case_change SET detected_at = ? WHERE kind = 'edps'", (later,))
    result = build_report(eu_settings, conn, now=NOW)
    assert result["detailed"] == 1
    text = text_of(result)
    assert "Endret relevans" in text and "Lav → Middels" in text
    assert "Relevans: Middels (EDPS-regelen; modellen vurderte: Lav)" in text
    assert "EDPS-uttalelse funnet i Rådets dokumenter (ST 99999 2026 INIT)" in text
    assert "https://data.consilium.europa.eu/doc/document/ST-99999-2026-INIT/en/pdf" in (
        links_of(result)
    )


def test_a_high_case_is_not_changed_by_an_opinion(eu_settings, conn):
    site = EUSite()
    do_run(eu_settings, conn, site, client=FakeLLM(default=SCRIPT))  # EU KIDS Act: hoy
    before = conn.execute("SELECT COUNT(*) FROM eu_assessment").fetchone()[0]
    note_for(site, "52026PC0681")
    fetcher = make_eu_fetcher(eu_settings, site)
    update_edps(conn, EurLexClient(fetcher), TODAY - timedelta(days=60), TODAY)
    assert conn.execute("SELECT COUNT(*) FROM eu_assessment").fetchone()[0] == before
    assert links(conn, "52026PC0681") == [("ST 99999 2026 INIT", "procedure")]
    assert case(conn, "52026PC0681")["com_ref"] == KIDS


def test_report_marks_opinions_and_says_the_list_is_incomplete(eu_settings, conn):
    site = EUSite()
    site.add_edps()
    do_run(eu_settings, conn, site, client=FakeLLM(default=SCRIPT))
    text = text_of(build_report(eu_settings, conn, now=NOW))
    assert "Med EDPS-uttalelse: 4" in text
    assert "EDPS-uttalelser er hentet fra Rådets offentlige dokumenter" in text
    assert "ikke fullstendige" in text
