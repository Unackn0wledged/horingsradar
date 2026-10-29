"""LLM assessment of one EU case: its explanatory memorandum against the relevance scale.

Same rules as the regjeringen.no radar: the model describes and never advises; invalid output
gets one retry, then the assessment is stored as failed; every quote is verified in code
against the memorandum pages, and an unverified quote is kept and flagged, never dropped or
corrected. The page is corrected only when the quote is found on another page.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from horingsradar import db
from horingsradar.eu import rules
from horingsradar.eu.config import EUSettings
from horingsradar.eu.schema import EUAssessmentOutput, eu_output_json_schema
from horingsradar.eu.text import Memorandum, memorandum
from horingsradar.extract import split_pages
from horingsradar.llm.client import LLMClient, LLMError, LLMResponse
from horingsradar.llm.prompt import Prompt, load_prompt
from horingsradar.quotes import normalize_pages, verify_quote

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 2  # one retry on invalid output
FAILED_ATTEMPTS_BEFORE_PAUSE = 2  # then the case waits until its text changes
DOC_TYPE_LABELS = {
    "PROP_REG": "Forslag til forordning",
    "PROP_DIR": "Forslag til direktiv",
    "PROP_DEC": "Forslag til beslutning",
}


class OutputError(Exception):
    pass


@dataclass
class EUPlan:
    case: sqlite3.Row
    memo: Memorandum
    prompt: Prompt
    params: dict
    model: str
    input_hash: str
    input_chars: int
    notes: list[str] = field(default_factory=list)

    @property
    def case_id(self) -> int:
        return int(self.case["id"])


def plan_assessment(
    settings: EUSettings,
    conn: sqlite3.Connection,
    case_id: int,
    client: LLMClient,
    prompt_version: str | None = None,
) -> EUPlan:
    case = conn.execute("SELECT * FROM eu_case WHERE id = ?", (case_id,)).fetchone()
    if case is None:
        raise ValueError(f"Ukjent sak {case_id}")
    doc = conn.execute("SELECT * FROM eu_document WHERE case_id = ?", (case_id,)).fetchone()
    if doc is None or doc["fetch_status"] != "ok":
        raise ValueError(f"{case['com_ref']}: ingen tekst å vurdere ({doc and doc['note']})")
    memo = memorandum(split_pages(Path(doc["text_path"]).read_text(encoding="utf-8")))
    ids = db.loads(case["eurovoc"], [])
    labels = {
        r["id"]: r["label"]
        for r in conn.execute(
            f"SELECT id, label FROM eu_eurovoc WHERE id IN ({', '.join('?' * len(ids))})", ids
        )
    }
    prompt = load_prompt(settings.prompts_dir, f"eu_{prompt_version or settings.eu_prompt_version}")
    user = prompt.render_user(
        case=dict(case),
        doc_type=DOC_TYPE_LABELS.get(case["doc_type"], case["doc_type"] or "ukjent"),
        eurovoc=[labels[i] for i in ids if labels.get(i)],
        memo=memo,
        page_count=doc["page_count"],
    )
    params = client.build_params(prompt.system, user, eu_output_json_schema())
    digest = hashlib.sha256(
        json.dumps([client.model, prompt.system, user], ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    notes = [n for n in (doc["note"] or "").split("; ") if n]
    return EUPlan(case, memo, prompt, params, client.model, digest, len(user), notes)


def parse_output(response: LLMResponse) -> EUAssessmentOutput:
    if response.stop_reason == "refusal":
        raise OutputError("modellen avviste forespørselen (refusal)")
    if response.stop_reason == "max_tokens":
        raise OutputError("svaret ble avkortet (max_tokens)")
    try:
        data = json.loads(response.text)
    except json.JSONDecodeError as exc:
        raise OutputError(f"ugyldig JSON: {exc}") from exc
    try:
        return EUAssessmentOutput.model_validate(data)
    except ValidationError as exc:
        raise OutputError(f"skjemafeil: {exc.error_count()} feil: {exc.errors()[:3]}") from exc


def execute(
    plan: EUPlan, client: LLMClient
) -> tuple[EUAssessmentOutput | None, list[LLMResponse], str | None]:
    responses: list[LLMResponse] = []
    error: str | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.complete(plan.params)
        except LLMError as exc:
            return None, responses, f"API-feil: {exc}"
        responses.append(response)
        try:
            return parse_output(response), responses, None
        except OutputError as exc:
            error = str(exc)
            log.warning("%s attempt %s: invalid output: %s", plan.case["com_ref"], attempt, exc)
    return None, responses, error


def _cost(settings: EUSettings, responses: list[LLMResponse]) -> float:
    total = sum(
        (
            r.input_tokens * settings.price_input_per_mtok
            + r.output_tokens * settings.price_output_per_mtok
        )
        / 1_000_000
        * (settings.batch_price_factor if r.batch else 1.0)
        for r in responses
        if not r.cached
    )
    return round(total, 6)


def _save_raw(
    settings: EUSettings, plan: EUPlan, responses: list[LLMResponse], error: str | None
) -> Path:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    path = settings.llm_raw_dir / "eu" / str(plan.case_id) / f"{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "case_id": plan.case_id,
        "com_ref": plan.case["com_ref"],
        "model": plan.model,
        "prompt_version": plan.prompt.version,
        "input_hash": plan.input_hash,
        "error": error,
        "responses": [{"cached": r.cached, "batch": r.batch, "message": r.raw} for r in responses],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def store_assessment(
    settings: EUSettings,
    conn: sqlite3.Connection,
    plan: EUPlan,
    output: EUAssessmentOutput | None,
    responses: list[LLMResponse],
    error: str | None,
) -> int:
    raw_path = _save_raw(settings, plan, responses, error)
    model = next((r.model for r in reversed(responses) if r.model), plan.model)
    common = (
        plan.case_id,
        db.now_iso(),
        model,
        plan.prompt.version,
        sum(r.input_tokens for r in responses if not r.cached),
        sum(r.output_tokens for r in responses if not r.cached),
        plan.input_chars,
        _cost(settings, responses),
        str(raw_path),
        plan.input_hash,
        db.dumps(plan.notes),
    )
    columns = (
        "case_id, created_at, model, prompt_version, input_tokens, output_tokens, input_chars,"
        " cost_estimate, raw_response_path, input_hash, notes"
    )
    marks = ", ".join("?" * len(common))
    if output is None:
        cur = conn.execute(
            f"INSERT INTO eu_assessment ({columns}, status, error) VALUES ({marks}, 'failed', ?)",
            (*common, error or "ukjent feil"),
        )
        log.error("Assessment of %s failed: %s", plan.case["com_ref"], error)
        return int(cur.lastrowid)

    relevance, rule = rules.final_relevance(output.relevance, rules.has_edps(conn, plan.case_id))
    cur = conn.execute(
        f"INSERT INTO eu_assessment ({columns}, status, llm_relevance, relevance, rule,"
        " relevance_reason, sectors, summary)"
        f" VALUES ({marks}, 'ok', ?, ?, ?, ?, ?, ?)",
        (
            *common,
            output.relevance,
            relevance,
            rule,
            output.relevance_reason,
            db.dumps(output.sectors),
            output.summary,
        ),
    )
    assessment_id = int(cur.lastrowid)
    pages = normalize_pages(plan.memo.pages)
    verified = 0
    for f in output.findings:
        check = verify_quote(f.quote, pages, f.page)
        page = check.page if check.verified and check.page else f.page
        if check.verified and page != f.page:
            log.info(
                "%s: quote on page %s, not %s; page corrected", plan.case["com_ref"], page, f.page
            )
        if not check.verified:
            log.warning(
                "%s: unverified quote on p. %s (score %.1f)",
                plan.case["com_ref"],
                f.page,
                check.score,
            )
        verified += int(check.verified)
        conn.execute(
            "INSERT INTO eu_finding (assessment_id, category, description, chapter, page,"
            " page_stated, quote, quote_verified, match_score) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                assessment_id,
                f.category,
                f.description,
                f.chapter,
                page,
                f.page,
                f.quote,
                int(check.verified),
                check.score,
            ),
        )
    conn.execute("UPDATE eu_case SET needs_assessment = 0 WHERE id = ?", (plan.case_id,))
    log.info(
        "%s assessed: %s%s, %s findings (%s verified quotes)",
        plan.case["com_ref"],
        output.relevance,
        f" (EDPS rule: {relevance})" if rule else "",
        len(output.findings),
        verified,
    )
    return assessment_id


def assess_case(
    settings: EUSettings,
    conn: sqlite3.Connection,
    case_id: int,
    client: LLMClient,
    prompt_version: str | None = None,
) -> int:
    """Assess one case synchronously. Returns the assessment id."""
    plan = plan_assessment(settings, conn, case_id, client, prompt_version)
    log.info(
        "Assessing %s with %s / prompt %s, memorandum pages %s-%s, ~%s chars",
        plan.case["com_ref"],
        plan.model,
        plan.prompt.version,
        plan.memo.first_page,
        plan.memo.last_page,
        plan.input_chars,
    )
    output, responses, error = execute(plan, client)
    return store_assessment(settings, conn, plan, output, responses, error)


def failed_attempts_since_change(conn: sqlite3.Connection, case_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM eu_assessment a JOIN eu_document d ON d.case_id = a.case_id"
        " WHERE a.case_id = ? AND a.status = 'failed'"
        " AND (d.last_changed IS NULL OR a.created_at >= d.last_changed)",
        (case_id,),
    ).fetchone()
    return int(row[0])


def pending_assessments(conn: sqlite3.Connection) -> list[int]:
    """Candidates waiting for an assessment that have their text, newest first."""
    rows = conn.execute(
        "SELECT c.id FROM eu_case c JOIN eu_document d ON d.case_id = c.id"
        " WHERE c.candidate = 1 AND c.needs_assessment = 1 AND d.fetch_status = 'ok'"
        " ORDER BY c.date_document DESC, c.id DESC"
    ).fetchall()
    return [
        r["id"]
        for r in rows
        if failed_attempts_since_change(conn, r["id"]) < FAILED_ATTEMPTS_BEFORE_PAUSE
    ]
