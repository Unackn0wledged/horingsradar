"""Have Your Say: feedback periods for EUR-Lex candidates, found by their COM number."""

import json
from dataclasses import asdict
from datetime import date, datetime, timedelta

import pytest
from eu_fakes import HYS, EUSite, hys_group
from fakes import FakeLLM
from test_eu_eurlex import do_run
from test_eu_report import KIDS, NOW, SCRIPT, links_of, text_of
from test_eu_report import do_run as assessed_run

from horingsradar.eu import db as eu_db
from horingsradar.eu.config import load_eu_settings
from horingsradar.eu.report import CaseView, build_report
from horingsradar.eu.sources import haveyoursay

KIDS_PAGE = (
    "https://ec.europa.eu/info/law/better-regulation/have-your-say/initiatives/"
    "19418-Child-Safety-Online_en"
)
EUROPOL = "COM(2026) 558"


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


def case(conn, com_ref):
    return conn.execute("SELECT * FROM eu_case WHERE com_ref = ?", (com_ref,)).fetchone()


def lookups(site) -> list[str]:
    return [u for u in site.hys.requests if "findInitiativePathByReference" in u]


def hys_changes(conn) -> list[tuple]:
    return [
        (r["old_value"], r["new_value"])
        for r in conn.execute(
            "SELECT * FROM eu_case_change WHERE kind = 'hys_feedback' ORDER BY id"
        )
    ]


# -- parsing the real responses -------------------------------------------------


def test_lookup_reference_has_no_space():
    assert haveyoursay.api_ref("COM(2026) 681") == "COM(2026)681"
    assert haveyoursay.api_ref("JOIN(2025) 12") == "JOIN(2025)12"


def test_lookup_answer_is_the_initiative_path():
    assert haveyoursay.parse_path("14628-Cloud-and-AI-Development-Act") == (
        "14628-Cloud-and-AI-Development-Act"
    )
    assert haveyoursay.parse_path('"19418-Child-Safety-Online"\n') == "19418-Child-Safety-Online"
    assert haveyoursay.parse_path('{"code":"not_found"}') is None


def test_initiative_has_the_com_number_in_the_proposal_stage():
    init = haveyoursay.parse_initiative(hys_group(14628), "14628-Cloud-and-AI-Development-Act")
    assert init.id == 14628 and init.reference == "Ares(2025)2876603"
    assert init.has_com_ref("COM(2026) 502") and not init.has_com_ref("COM(2026) 681")
    proposal = next(p for p in init.publications if p.stage == haveyoursay.ADOPTION)
    assert (proposal.reference, proposal.deadline, proposal.status) == (
        "COM(2026)502",
        "2026-11-11",
        "OPEN",
    )
    cfe = next(p for p in init.publications if p.type == "CFE_IMPACT_ASSESS")
    assert (cfe.deadline, cfe.status) == ("2025-07-03", "CLOSED")
    assert init.still_open
    assert init.url.endswith("/initiatives/14628-Cloud-and-AI-Development-Act_en")


def test_planned_stage_has_a_quarter_and_no_deadline():
    init = haveyoursay.parse_initiative(hys_group(15792), "15792-Review")
    planned = next(p for p in init.publications if p.status == haveyoursay.UPCOMING)
    assert planned.deadline is None and planned.planned_period == "Q-2027-4"
    assert haveyoursay.planned_label(planned.planned_period) == "4. kvartal 2027"
    assert sorted(p.deadline for p in init.publications if p.status == "OPEN") == [
        "2026-11-24",
        "2026-11-24",
    ]


def keys(o) -> set[str]:
    if isinstance(o, dict):
        return set(o) | {k for v in o.values() for k in keys(v)}
    if isinstance(o, list):
        return {k for v in o for k in keys(v)}
    return set()


def test_officials_are_not_in_fixtures_or_the_database(conn):
    for path in HYS.glob("group_*.json"):
        assert "@" not in path.read_text("utf-8")
        found = keys(json.loads(path.read_text("utf-8")))
        assert not any(w in k.lower() for k in found for w in ("author", "mail", "phone"))
    for table in ("eu_hys_initiative", "eu_hys_publication"):
        columns = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert not any(w in c.lower() for c in columns for w in ("author", "mail", "phone"))


# -- the step in `run` ------------------------------------------------------------


def test_run_links_eu_kids_act_by_its_com_number(eu_settings, conn):
    site = EUSite()
    summary = do_run(eu_settings, conn, site)
    assert summary["hys_looked_up"] == 14 and summary["hys_new_links"] == 1
    assert summary["hys_linked"] == 1 and summary["hys_open"] == 1
    assert all(" " not in u and "%20" not in u for u in lookups(site))
    kids = case(conn, KIDS)
    assert (kids["hys_initiative_id"], kids["hys_status"]) == (19418, "found")
    source = conn.execute(
        "SELECT * FROM eu_case_source WHERE case_id = ? AND source = 'hys'", (kids["id"],)
    ).fetchone()
    assert source["source_id"] == "19418/COM(2026)681" and source["url"] == KIDS_PAGE
    pub = conn.execute("SELECT * FROM eu_hys_publication WHERE initiative_id = 19418").fetchone()
    assert (pub["reference"], pub["deadline"], pub["status"]) == (
        "COM(2026)681",
        "2026-12-03",
        "OPEN",
    )
    assert case(conn, EUROPOL)["hys_status"] == "not_found"
    assert hys_changes(conn) == [(None, "2026-12-03")]  # the open period, logged once
    # Have Your Say never changes who is a candidate.
    assert summary["candidates"] == 14


def test_proposals_without_a_page_are_looked_up_again_for_twelve_weeks(eu_settings, conn):
    site = EUSite()
    do_run(eu_settings, conn, site)
    site.hys.requests.clear()
    do_run(eu_settings, conn, site, today=date(2026, 11, 5))
    again = lookups(site)
    assert len(again) == 13  # all not found, all recent; EU KIDS Act is linked already
    assert not any("COM(2026)681" in u for u in again)
    assert any(u.endswith("/groupInitiatives/19418") for u in site.hys.requests)  # still open

    site.hys.requests.clear()
    site.hys.groups[19418]["publications"][0]["receivingFeedbackStatus"] = "CLOSED"
    do_run(eu_settings, conn, site, today=date(2027, 3, 1))  # all older than 12 weeks
    assert lookups(site) == []
    assert any(u.endswith("/groupInitiatives/19418") for u in site.hys.requests)
    site.hys.requests.clear()
    do_run(eu_settings, conn, site, today=date(2027, 3, 8))
    assert site.hys.requests == []  # closed: not fetched again


def test_a_moved_deadline_is_logged(eu_settings, conn):
    site = EUSite()
    do_run(eu_settings, conn, site)
    site.hys.groups[19418]["publications"][0]["endDate"] = "2026/12/10 23:59:59"
    summary = do_run(eu_settings, conn, site, today=date(2026, 11, 5))
    assert summary["hys_feedback_changes"] == 1
    assert hys_changes(conn) == [(None, "2026-12-03"), ("2026-12-03", "2026-12-10")]


def test_initiative_without_the_com_number_is_not_linked(eu_settings, conn):
    site = EUSite()
    site.hys.lookups["COM(2026)558"] = "14628-Cloud-and-AI-Development-Act"
    do_run(eu_settings, conn, site)
    europol = case(conn, EUROPOL)
    assert europol["hys_status"] == "mismatch" and europol["hys_initiative_id"] is None


def test_portal_errors_stop_only_this_step(eu_settings, conn):
    site = EUSite()
    site.hys.status = 503
    summary = do_run(eu_settings, conn, site)
    assert summary["have_your_say"] == "ufullstendig (se loggen)"
    assert summary["hys_linked"] == 0 and summary["documents_fetched"] == 2
    assert case(conn, KIDS)["hys_status"] is None  # looked up again next run


# -- the report -------------------------------------------------------------------


def test_report_shows_the_deadline_and_links_to_the_page(eu_settings, conn):
    assessed_run(eu_settings, conn, EUSite(), client=FakeLLM(default=SCRIPT))
    result = build_report(eu_settings, conn, now=NOW)
    text = text_of(result)
    assert "Kandidater på Have Your Say: 1 (åpne for tilbakemelding nå: 1)" in text
    assert "Tilbakemelding på forslaget åpen til 03.12.2026" in text
    assert "Tilbakemelding til 03.12.2026" in text  # in the overview
    assert KIDS_PAGE in links_of(result)


def test_a_moved_deadline_of_a_reported_case_is_one_line(eu_settings, conn):
    assessed_run(eu_settings, conn, EUSite(), client=FakeLLM(default=SCRIPT))
    first = build_report(eu_settings, conn, now=NOW)
    reported = conn.execute("SELECT created_at FROM eu_report WHERE id = ?", (first["report_id"],))
    later = (datetime.fromisoformat(reported.fetchone()[0]) + timedelta(seconds=1)).isoformat()
    conn.execute(
        "INSERT INTO eu_case_change (case_id, detected_at, kind, old_value, new_value)"
        " VALUES (?, ?, 'hys_feedback', '2026-12-03', '2026-12-10')",
        (case(conn, KIDS)["id"], later),
    )
    result = build_report(eu_settings, conn, now=NOW)
    assert result["cases"] == result["status_changes"] == 1 and result["detailed"] == 0
    text = text_of(result)
    assert "Frist for tilbakemelding på Have Your Say: 03.12.2026 → 10.12.2026" in text


def view(today: date, initiative_id: int | None = None, d: str = "2026-10-01") -> CaseView:
    v = CaseView({"date_document": d}, None, [], None, [], today)
    if initiative_id:
        init = haveyoursay.parse_initiative(hys_group(initiative_id), f"{initiative_id}-x")
        v.hys = {"path": init.path}
        v.hys_publications = [asdict(p) for p in init.publications]
    return v


def test_feedback_lines_for_open_closed_and_planned_stages():
    v = view(date(2026, 10, 29), 15792)
    assert v.feedback == [
        "Tilbakemelding på innspillsfasen (call for evidence) åpen til 24.11.2026",
        "Tilbakemelding på den offentlige konsultasjonen åpen til 24.11.2026",
        "Tilbakemelding på forslaget planlagt 4. kvartal 2027",
    ]
    assert v.open_deadline == "2026-11-24"
    late = view(date(2026, 11, 25), 15792)
    assert (
        late.feedback[0] == "Tilbakemelding på innspillsfasen (call for evidence) lukket 24.11.2026"
    )
    assert late.open_deadline is None
    assert view(date(2026, 10, 29), 14628).feedback == [
        "Tilbakemelding på forslaget åpen til 11.11.2026"
    ]


def test_open_deadlines_come_first_within_a_relevance_level():
    today = date(2026, 10, 29)
    newest = view(today, d="2026-10-20")
    far = view(today, 19418, d="2026-09-17")  # open to 03.12.2026
    near = view(today, 14628, d="2026-07-02")  # open to 11.11.2026
    order = sorted([newest, far, near], key=lambda v: v.sort_key)
    assert order == [near, far, newest]
