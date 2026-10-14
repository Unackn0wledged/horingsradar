from pathlib import Path

import pytest
from conftest import REGJERINGEN, fixture_text, hearing_url
from fakes import FakeLLM, finding, output

from horingsradar import db
from horingsradar.assess import (
    DocInput,
    assess_hearing,
    merge_outputs,
    parse_output,
    plan_assessment,
    plan_chunks,
)
from horingsradar.chunking import find_chapters, merge_ranges, sections_for
from horingsradar.extract import extract_html, extract_pdf, split_pages
from horingsradar.llm.client import LLMError, LLMResponse
from horingsradar.llm.prompt import load_prompt
from horingsradar.llm.schema import AssessmentOutput, output_json_schema
from horingsradar.quotes import normalize, normalize_pages, verify_quote
from horingsradar.sources.regjeringen.parse import parse_hearing

PDF = REGJERINGEN / "notat_3170511_excerpt.pdf"
QUOTE_P7 = "Hjemmelskravet kan også innebære et krav om særskilte garantier («safeguards»)."
QUOTE_LINEBREAK = (
    "Det avgjørende er at rettsgrunnlaget materielt sett er tilgjengelig og forutberegnelig."
)


@pytest.fixture(scope="module")
def pdf_pages():
    return split_pages(extract_pdf(PDF).text)


# -- extraction ------------------------------------------------------------


def test_pdf_extraction_has_page_markers(pdf_pages):
    extracted = extract_pdf(PDF)
    assert extracted.page_count == 10
    assert extracted.text.startswith("[[side 1]]\n")
    assert "[[side 10]]" in extracted.text
    assert not extracted.probably_scanned
    assert "Hjemmelskravet" in pdf_pages[7]


def test_scanned_pdf_warning(tmp_path: Path, caplog):
    import pymupdf

    doc = pymupdf.open()
    for _ in range(3):
        doc.new_page()
    path = tmp_path / "scan.pdf"
    doc.save(path)
    result = extract_pdf(path)
    assert result.probably_scanned
    assert "No OCR" in caplog.text


def test_html_letter_extraction():
    page = parse_hearing(fixture_text("hearing_3177107.html"), hearing_url(3177107))
    extracted = extract_html(page.letter_html)
    assert extracted.page_count == 1
    assert extracted.text.startswith("[[side 1]]")
    assert "Høringsfristen er 15. januar 2027" in extracted.text


# -- quotes ----------------------------------------------------------------


def test_normalize_handles_hyphenation_ligatures_and_quotes():
    assert normalize("Ø-\nkriminformasjons-\nforskriften") == normalize(
        "Ø-kriminformasjonsforskriften"
    )
    assert normalize("ﬁnansforetak") == "finansforetak"
    assert normalize("«clear, detailed rules»") == normalize('"clear, detailed rules"')
    assert normalize("2026–2027") == normalize("2026-2027")
    assert normalize("a  \n b") == "a b"


def test_verify_quote_on_stated_page(pdf_pages):
    pages = normalize_pages(pdf_pages)
    check = verify_quote(QUOTE_P7, pages, 7)
    assert check.verified and check.page == 7 and check.score >= 95


def test_verify_quote_across_line_break(pdf_pages):
    check = verify_quote(QUOTE_LINEBREAK, normalize_pages(pdf_pages), 7)
    assert check.verified


def test_verify_quote_corrects_page(pdf_pages):
    check = verify_quote(QUOTE_P7, normalize_pages(pdf_pages), 2)
    assert check.verified and check.page == 7


def test_verify_quote_rejects_paraphrase(pdf_pages):
    check = verify_quote(
        "Departementet foreslår at alle banker skal dele kundedata med politiet hver uke.",
        normalize_pages(pdf_pages),
        7,
    )
    assert not check.verified and check.score < 95


# -- chunking --------------------------------------------------------------


def _synthetic_pages(chapters: int, pages_per_chapter: int, filler: int = 2000) -> dict[int, str]:
    pages: dict[int, str] = {
        1: "Høringsnotat\nForside",
        2: "Innhold\n" + "1 Innledning ........ 3\n" * 6,
    }
    n = 3
    for c in range(1, chapters + 1):
        for i in range(pages_per_chapter):
            head = f"{n}\n{c}\nKapittel om tema {c}\n" if i == 0 else f"{n}\n"
            pages[n] = head + ("tekst " * (filler // 6))
            n += 1
    return pages


def test_find_chapters_skips_toc_and_page_numbers():
    pages = _synthetic_pages(chapters=4, pages_per_chapter=3)
    chapters = find_chapters(pages)
    assert [c[1] for c in chapters] == [1, 2, 3, 4]
    assert [c[0] for c in chapters] == [3, 6, 9, 12]
    sections = sections_for(pages)
    assert sections[0].label == "forside og innhold"
    assert sections[1].label == "kapittel 1 Kapittel om tema 1"


def test_find_chapters_on_real_excerpt(pdf_pages):
    # The excerpt is TOC pages plus pages from chapter 3; no false chapter 1 from page numbers.
    assert all(number <= 3 for _, number, _ in find_chapters(pdf_pages))


def test_plan_chunks_single_and_split():
    letter = DocInput("D1", 1, "Høringsbrev", "horingsbrev", 1, {1: "brev " * 200})
    notat = DocInput("D2", 2, "Høringsnotat", "horingsnotat", 0, _synthetic_pages(6, 5))
    notat.page_count = len(notat.pages)
    extra = DocInput("D3", 3, "Vedlegg", "annet", 1, {1: "vedlegg " * 100})
    single = plan_chunks([letter, notat, extra], budget=10_000_000, skipped=[])
    assert len(single) == 1 and [p.doc.ref for p in single[0].parts] == ["D1", "D2", "D3"]

    budget = notat.size() // 2
    chunks = plan_chunks([letter, notat, extra], budget=budget, skipped=[])
    assert len(chunks) >= 2
    for chunk in chunks:
        assert chunk.parts[0].doc.ref == "D1"  # the letter is in every part
        assert sum(p.doc.size(p.pages) for p in chunk.parts) <= budget * 1.1
    covered = set()
    for chunk in chunks:
        for part in chunk.parts:
            if part.doc.ref == "D2":
                covered |= set(part.pages or notat.pages)
    assert covered == set(notat.pages)
    assert any("D3" in s for s in chunks[0].skipped)


def test_merge_ranges():
    assert merge_ranges([(5, 7), (1, 2), (3, 3), (10, 12), (11, 15)]) == [(1, 3), (5, 7), (10, 15)]


# -- schema / output -------------------------------------------------------


def _response(text: str, stop: str = "end_turn") -> LLMResponse:
    return LLMResponse(text, "m", stop, 10, 10, raw={})


def test_schema_is_api_compatible():
    schema = output_json_schema()
    stack = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            assert not {"minItems", "maxItems", "minLength", "title"} & node.keys()
            if node.get("type") == "object" and "properties" in node:
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def test_parse_output_validation():
    import json

    assert parse_output(_response(json.dumps(output()))).relevance == "hoy"
    with pytest.raises(Exception, match="JSON"):
        parse_output(_response("{not json"))
    with pytest.raises(Exception, match="max_tokens"):
        parse_output(_response(json.dumps(output()), stop="max_tokens"))
    with pytest.raises(Exception, match="skjemafeil"):
        parse_output(_response(json.dumps(output(sectors=[]))))
    with pytest.raises(Exception, match="skjemafeil"):
        parse_output(_response(json.dumps(output(relevance="kritisk"))))
    normative = output(summary="Forslaget gjelder registre. Datatilsynet bør gi innspill.")
    with pytest.raises(Exception, match="skjemafeil"):
        parse_output(_response(json.dumps(normative)))
    missing = output()
    del missing["privacy_section"]
    with pytest.raises(Exception, match="skjemafeil"):
        parse_output(_response(json.dumps(missing)))


def test_merge_outputs_takes_highest_relevance():
    low = AssessmentOutput.model_validate(output("lav", sectors=["annet"]))
    high = AssessmentOutput.model_validate(
        output("hoy", findings=[finding(QUOTE_P7, 7)], relevance_reason="Høy grunn.")
    )
    merged = merge_outputs([low, high])
    assert merged.relevance == "hoy"
    assert merged.relevance_reason == "Høy grunn."
    assert merged.summary == low.summary
    assert merged.sectors[0] == "justis_politi"
    assert len(merged.findings) == 1


def test_prompt_v1_renders():
    from horingsradar.config import REPO_ROOT

    prompt = load_prompt(REPO_ROOT / "prompts", "v1")
    assert "Ved tvil" in prompt.system or "i tvil" in prompt.system
    user = prompt.render_user(
        hearing={
            "title": "T",
            "department": "JD",
            "original_department": None,
            "published_date": "2026-01-01",
            "deadline": "2026-03-01",
            "status_raw": "På høring",
            "url": "u",
            "ingress": None,
        },
        chunk={"index": 1, "count": 2, "label": "D2 kapittel 1"},
        documents=[
            {
                "ref": "D1",
                "title": "Brev",
                "kind_label": "høringsbrev",
                "page_count": 1,
                "part": None,
                "text": "[[side 1]]\nx",
            }
        ],
        skipped=["D3: Vedlegg – ikke plass"],
    )
    assert "del 1 av 2" in user and "D1: Brev" in user and "D3: Vedlegg" in user


# -- end to end with stored documents --------------------------------------


@pytest.fixture
def stored(settings):
    """Hearing 3170511 with its letter and the PDF excerpt stored as documents."""
    from fakes import FakeSite, make_fetcher

    from horingsradar.sources.regjeringen.crawler import RegjeringenSource

    conn = db.connect(settings.db_path)
    conn.execute(
        "INSERT INTO hearing (id, url, title, tracked, needs_assessment, first_seen)"
        " VALUES (3170511, ?, 'x', 1, 1, '2026-10-14')",
        (hearing_url(3170511),),
    )
    RegjeringenSource(settings, conn, make_fetcher(settings, FakeSite())).refresh_hearing(3170511)
    yield conn
    conn.close()


def test_assess_verifies_quotes_and_corrects_pages(settings, stored):
    llm = FakeLLM(
        [
            output(
                findings=[
                    finding(QUOTE_P7, 7),
                    finding(QUOTE_LINEBREAK, 5),  # wrong page, should be corrected to 7
                    finding("Dette sitatet finnes ikke i dokumentet i det hele tatt.", 7),
                    finding(QUOTE_P7, 7, document="D9"),
                ],
                privacy_section={
                    "exists": True,
                    "document": "D2",
                    "chapter": "3 Rettslig rammeverk",
                    "pages": {"start": 6, "end": 9},
                },
            )
        ]
    )
    assessment_id = assess_hearing(settings, stored, 3170511, llm)
    row = stored.execute("SELECT * FROM assessment WHERE id = ?", (assessment_id,)).fetchone()
    assert row["status"] == "ok" and row["relevance"] == "hoy"
    assert row["model"] == "fake-model" and row["prompt_version"] == "v1"
    assert row["context"] == "drift" and row["backtest_run_id"] is None
    assert Path(row["raw_response_path"]).is_file()
    guide = db.loads(row["reading_guide"])
    assert guide["relevant_pages"] == 4 and guide["total_pages"] == 11
    assert db.loads(row["privacy_section"])["exists"] is True
    findings = db.findings_for(stored, assessment_id)
    assert [f["quote_verified"] for f in findings] == [1, 1, 0, 0]
    assert findings[1]["page"] == 7 and findings[1]["page_stated"] == 5
    assert findings[3]["document_id"] is None
    assert stored.execute("SELECT needs_assessment FROM hearing").fetchone()[0] == 0
    # The prompt contained page markers and document references
    user = llm.calls[0]["messages"][0]["content"]
    assert "## D1: Høringsbrev" in user and "## D2:" in user and "[[side 7]]" in user


def test_assess_retries_once_then_fails(settings, stored):
    llm = FakeLLM(["{broken", "also broken"])
    assessment_id = assess_hearing(settings, stored, 3170511, llm)
    row = stored.execute("SELECT * FROM assessment WHERE id = ?", (assessment_id,)).fetchone()
    assert row["status"] == "failed" and "JSON" in row["error"]
    assert row["relevance"] is None
    assert len(llm.calls) == 2
    assert stored.execute("SELECT needs_assessment FROM hearing").fetchone()[0] == 1


def test_assess_retry_succeeds(settings, stored):
    llm = FakeLLM(["{broken", output("middels")])
    assessment_id = assess_hearing(settings, stored, 3170511, llm)
    row = stored.execute("SELECT * FROM assessment WHERE id = ?", (assessment_id,)).fetchone()
    assert row["status"] == "ok" and row["relevance"] == "middels"
    assert row["input_tokens"] > 0 and row["cost_estimate"] > 0


def test_assess_api_error_marks_failed(settings, stored):
    llm = FakeLLM([LLMError("HTTP 401: invalid x-api-key", status=401)])
    assessment_id = assess_hearing(settings, stored, 3170511, llm)
    row = stored.execute("SELECT * FROM assessment WHERE id = ?", (assessment_id,)).fetchone()
    assert row["status"] == "failed" and "401" in row["error"]
    assert len(llm.calls) == 1


def test_assess_splits_large_hearing(settings, stored):
    settings.max_input_tokens = 9_000  # forces several parts for the 10-page excerpt
    llm = FakeLLM(default=output("lav", sectors=["annet"]))
    plan = plan_assessment(settings, stored, 3170511, llm)
    assert len(plan.chunks) > 1
    assert all("## D1: Høringsbrev" in c.user for c in plan.chunks)
    assessment_id = assess_hearing(settings, stored, 3170511, llm)
    row = stored.execute("SELECT * FROM assessment WHERE id = ?", (assessment_id,)).fetchone()
    assert row["chunk_count"] == len(plan.chunks) and row["status"] == "ok"
    assert len(llm.calls) == len(plan.chunks)


def test_find_chapters_ignores_numbered_questions():
    pages = _synthetic_pages(chapters=3, pages_per_chapter=2)
    # A summary page listing numbered questions must not be taken as chapters 2 and 3.
    pages[3] = pages[3] + "\n2 Hva er virkningene av forslaget?\n3 Hvilke tiltak anbefales?\n"
    assert [c[1] for c in find_chapters(pages)] == [1, 2, 3]
    assert [c[0] for c in find_chapters(pages)] == [3, 5, 7]
