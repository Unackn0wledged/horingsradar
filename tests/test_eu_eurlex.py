"""EUR-Lex proposals: queries, parsing, prefilter and the EU run, against captured responses."""

import copy
from datetime import date

import pytest
from eu_fakes import EEASite, EUSite, load, make_eea_fetcher, make_eu_fetcher

from horingsradar import db
from horingsradar.eu import db as eu_db
from horingsradar.eu.config import load_eu_settings
from horingsradar.eu.pipeline import counts, run
from horingsradar.eu.prefilter import DEFAULT_KEYWORDS, Prefilter, load_keywords
from horingsradar.eu.sources import eurlex
from horingsradar.jobs import job_context

TODAY = date(2026, 10, 29)
RECALL = {
    "52020PC0823": "NIS2",
    "52022PC0454": "Cyber Resilience Act",
    "52021PC0281": "eIDAS2",
    "52022PC0209": "CSAM",
    "52022PC0197": "EHDS",
    "52022PC0068": "Data Act",
    "52021PC0206": "AI Act",
}


@pytest.fixture
def eu_settings(tmp_path):
    s = load_eu_settings(
        env_file=tmp_path / "missing.env", data_dir=tmp_path / "data-eu", radar="eu"
    )
    s.ensure_dirs()
    return s


@pytest.fixture
def site():
    return EUSite()


@pytest.fixture
def conn(eu_settings):
    c = eu_db.connect(eu_settings.db_path)
    yield c
    c.close()


def do_run(settings, conn, site, today=TODAY, eea=None):
    with job_context(settings, conn, "run") as job:
        return run(
            settings,
            conn,
            job,
            fetcher=make_eu_fetcher(settings, site),
            today=today,
            eea_fetcher=make_eea_fetcher(settings, eea or EEASite()),
        )


# -- parsing ------------------------------------------------------------------


def test_com_reference_from_internal_number_or_celex():
    assert eurlex.com_ref("COM", "2026", "569", "52026PC0569") == "COM(2026) 569"
    assert eurlex.com_ref(None, None, None, "52022PC0068") == "COM(2022) 68"
    assert eurlex.com_ref(None, None, None, "52023JC0011") == "JOIN(2023) 11"
    assert eurlex.com_ref(None, None, None, "32016R0679") is None


def test_parse_proposals_from_real_response():
    proposals = eurlex.parse_proposals(load("proposals_2026-10.json"))
    assert len(proposals) == 27
    p = next(p for p in proposals if p.celex == "52026PC0569")
    assert p.com_ref == "COM(2026) 569" and p.doc_type == "PROP_REG"
    assert p.title.startswith("Proposal for a REGULATION") and p.date_document == "2026-10-14"
    assert p.eea_flag == 1 and "1274" in p.eurovoc and all("/" not in e for e in p.eurovoc)
    assert p.url == "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:52026PC0569"


def test_parse_dossiers_marks_withdrawn_and_adopted():
    dossiers = eurlex.parse_dossiers(load("dossiers.json"))
    by_status = {}
    for procedure, status in dossiers.values():
        by_status.setdefault(status, []).append(procedure)
    assert by_status["WITHDRAWN"] == ["2023/0133/COD"]
    assert "2022/0155/COD" in by_status["PROP_PENDING"]  # CSAM
    assert "2021/0106/COD" in by_status["PROP_ADOPTED"]  # AI Act


def test_queries_are_bounded():
    q = eurlex.proposals_between_query(date(2026, 9, 1), date(2026, 10, 1), 200, 100)
    assert '?date >= "2026-09-01"^^xsd:date && ?date < "2026-10-01"^^xsd:date' in q
    assert "LIMIT 100 OFFSET 200" in q and q.count("{") == q.count("}")
    q = eurlex.proposals_by_celex_query(["52022PC0068"])
    assert '"52022PC0068"^^xsd:string' in q and "LIMIT" not in q


# -- prefilter ----------------------------------------------------------------


def test_whole_recall_set_passes_the_prefilter():
    pf = Prefilter()
    for p in eurlex.parse_proposals(load("recall_set.json")):
        assert pf.is_candidate(p.title, p.eurovoc), RECALL[p.celex]
    cra = next(
        p for p in eurlex.parse_proposals(load("recall_set.json")) if p.celex == "52022PC0454"
    )
    # Not data protection or personal data: caught by the security and digital topics.
    assert pf.reasons(cra.title, cra.eurovoc)["eurovoc"] == ["3027", "7219", "c_04ae3ba8"]


def test_keywords_match_whole_words_and_acronyms_by_case():
    pf = Prefilter()
    assert pf.reasons("Regulation on AI systems", [])["keywords"] == ["AI"]
    assert pf.reasons("State aid for farmers", [])["keywords"] == []
    assert pf.reasons("Rules on algorithms in hiring", [])["keywords"] == ["algorithm"]
    assert pf.reasons("Common rules on NIS", [])["keywords"] == ["NIS"]
    assert pf.reasons("Measures on nisin in food", [])["keywords"] == []
    assert not pf.is_candidate("Fishing opportunities in the Baltic Sea", ["1274"])


def test_keywords_from_file(tmp_path):
    path = tmp_path / "keywords.txt"
    path.write_text("# comment\nsmart meter\n\nAI  # acronym\n", encoding="utf-8")
    assert load_keywords(path) == ("smart meter", "AI")
    assert load_keywords(None) == DEFAULT_KEYWORDS


# -- client -------------------------------------------------------------------


def test_discovery_runs_in_windows_and_pages(eu_settings, site):
    client = eurlex.EurLexClient(make_eu_fetcher(eu_settings, site))
    proposals = list(client.proposals_since(date(2026, 9, 1), TODAY, page_size=10))
    assert len(proposals) == 67 and len({p.celex for p in proposals}) == 67
    windows = {q.split('?date >= "')[1][:10] for q in site.queries}
    assert windows == {"2026-09-01", "2026-10-01"}
    assert all("LIMIT 10" in q for q in site.queries)


# -- run ----------------------------------------------------------------------


def test_first_run_stores_every_proposal_and_marks_candidates(eu_settings, conn, site):
    summary = do_run(eu_settings, conn, site)
    assert summary["since"] == "2024-10-29"  # 730 days back
    assert summary["fetched"] == summary["new"] == summary["cases"] == 67
    assert summary["candidates"] == summary["new_candidates"] == 14
    assert summary["filtered_out"] == 53
    assert (
        conn.execute("SELECT COUNT(*) FROM eu_case_source WHERE source = 'eurlex'").fetchone()[0]
        == 67
    )

    kids = conn.execute("SELECT * FROM eu_case WHERE com_ref = 'COM(2026) 681'").fetchone()
    assert kids["candidate"] == 1 and kids["needs_assessment"] == 1 and kids["indexed"] == 1
    assert "5181" in db.loads(kids["prefilter"])["eurovoc"]
    out = conn.execute("SELECT * FROM eu_case WHERE com_ref = 'COM(2026) 569'").fetchone()
    assert out["candidate"] == 0 and out["needs_assessment"] == 0

    label = conn.execute("SELECT label FROM eu_eurovoc WHERE id = '5181'").fetchone()[0]
    assert label == "data protection"
    statuses = {
        r[0] for r in conn.execute("SELECT procedure_status FROM eu_case WHERE candidate = 1")
    }
    assert "PROP_PENDING" in statuses
    assert db.get_setting(conn, "eu_last_discovery_date") == "2026-10-29"


def test_second_run_is_idempotent_and_overlaps(eu_settings, conn, site):
    do_run(eu_settings, conn, site)
    site.queries.clear()
    summary = do_run(eu_settings, conn, site, today=date(2026, 11, 5))
    assert summary["since"] == "2026-08-30"  # previous run (29.10) minus 60 days
    assert summary["new"] == 0 and summary["new_candidates"] == 0 and summary["cases"] == 67
    assert conn.execute("SELECT COUNT(*) FROM eu_case_change").fetchone()[0] == 0


def test_late_eurovoc_indexing_makes_a_candidate_and_it_stays(eu_settings, conn, site):
    binding = site.proposal("569")  # energy proposal: not a candidate
    indexed = binding["eurovoc"]["value"]
    binding["eurovoc"]["value"] = ""  # just published, not indexed yet
    do_run(eu_settings, conn, site)
    row = conn.execute("SELECT * FROM eu_case WHERE com_ref = 'COM(2026) 569'").fetchone()
    assert row["indexed"] == 0 and row["candidate"] == 0
    assert counts(conn)["not_indexed"] == 1

    binding["eurovoc"]["value"] = indexed + " http://eurovoc.europa.eu/5595"
    do_run(eu_settings, conn, site, today=date(2026, 11, 5))
    row = conn.execute("SELECT * FROM eu_case WHERE com_ref = 'COM(2026) 569'").fetchone()
    assert row["indexed"] == 1 and row["candidate"] == 1 and row["needs_assessment"] == 1
    change = conn.execute("SELECT * FROM eu_case_change").fetchone()
    assert (change["kind"], change["old_value"], change["new_value"]) == ("candidate", "0", "1")

    binding["eurovoc"]["value"] = indexed  # index changed again: still a candidate
    do_run(eu_settings, conn, site, today=date(2026, 11, 12))
    assert (
        conn.execute("SELECT candidate FROM eu_case WHERE com_ref = 'COM(2026) 569'").fetchone()[0]
        == 1
    )


def test_procedure_status_change_is_logged_and_finished_ones_not_asked_again(
    eu_settings, conn, site
):
    do_run(eu_settings, conn, site)
    pending = conn.execute(
        "SELECT * FROM eu_case WHERE procedure_status = 'PROP_PENDING' LIMIT 1"
    ).fetchone()
    status = site.dossier(pending["work_uri"])["status"]
    status["value"] = status["value"].replace("PROP_PENDING", "PROP_ADOPTED")
    summary = do_run(eu_settings, conn, site, today=date(2026, 11, 5))
    assert summary["status_changes"] == 1
    change = conn.execute("SELECT * FROM eu_case_change WHERE kind = 'procedure_status'").fetchone()
    assert (change["old_value"], change["new_value"]) == ("PROP_PENDING", "PROP_ADOPTED")

    site.queries.clear()
    do_run(eu_settings, conn, site, today=date(2026, 11, 12))
    asked = " ".join(q for q in site.queries if "work_part_of_dossier" in q)
    assert pending["work_uri"] not in asked


def test_variants_of_a_proposal_are_one_case(eu_settings, conn, site):
    # Real data: only the extra part of COM(2026) 313 is in the window, not the proposal itself.
    corrigendum = copy.deepcopy(site.proposal("569"))
    corrigendum["celex"]["value"] = "52026PC0569R(01)"
    corrigendum["date"]["value"] = "2026-10-20"
    corrigendum["title"]["value"] = "Corrigendum to the proposal"
    corrigendum["eurovoc"]["value"] = "http://eurovoc.europa.eu/5595"
    site.proposals.append(corrigendum)
    summary = do_run(eu_settings, conn, site)
    assert summary["fetched"] == 68 and summary["cases"] == 67

    part = conn.execute("SELECT * FROM eu_case WHERE com_ref = 'COM(2026) 313'").fetchone()
    assert part["celex"] == "52026PC0313"
    sources = [
        r[0]
        for r in conn.execute(
            "SELECT source_id FROM eu_case_source WHERE case_id = ?", (part["id"],)
        )
    ]
    assert sources == ["52026PC0313(01)"]

    case = conn.execute("SELECT * FROM eu_case WHERE celex = '52026PC0569'").fetchone()
    assert case["title"].startswith("Proposal for a REGULATION")  # the proposal's own title wins
    assert case["date_document"] == "2026-10-14"
    assert "5595" in db.loads(case["eurovoc"]) and "1274" in db.loads(case["eurovoc"])
    assert case["candidate"] == 1  # through the corrigendum's EuroVoc concept
    sources = {
        r[0]
        for r in conn.execute(
            "SELECT source_id FROM eu_case_source WHERE case_id = ?", (case["id"],)
        )
    }
    assert sources == {"52026PC0569", "52026PC0569R(01)"}
    kinds = [
        r[0]
        for r in conn.execute("SELECT kind FROM eu_case_change WHERE case_id = ?", (case["id"],))
    ]
    assert "variant" in kinds

    do_run(eu_settings, conn, site, today=date(2026, 11, 5))  # seen again: nothing new logged
    assert (
        conn.execute("SELECT COUNT(*) FROM eu_case_change WHERE kind = 'variant'").fetchone()[0]
        == 1
    )
