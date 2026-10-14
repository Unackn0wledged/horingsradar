"""LLM assessment of one hearing: input building, chapter chunking, validation, quote checks.

The plan/execute/store split lets backtest send the same requests through the batch API.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from horingsradar import db
from horingsradar.chunking import (
    Section,
    merge_ranges,
    sections_for,
    split_section,
    summary_section,
)
from horingsradar.config import Settings
from horingsradar.extract import marker, split_pages
from horingsradar.llm.client import LLMClient, LLMError, LLMResponse
from horingsradar.llm.prompt import Prompt, load_prompt
from horingsradar.llm.schema import RELEVANCE_ORDER, AssessmentOutput, output_json_schema
from horingsradar.quotes import QuoteCheck, normalize_pages, verify_quote

log = logging.getLogger(__name__)

KIND_LABELS = {
    "horingsbrev": "høringsbrev",
    "horingsnotat": "høringsnotat",
    "lovutkast": "lov- eller forskriftsutkast",
    "annet": "vedlegg",
}
KIND_PRIORITY = {"horingsbrev": 0, "horingsnotat": 1, "lovutkast": 2, "annet": 3}
PAGE_OVERHEAD = 16  # marker + newlines per page
PROMPT_RESERVE_TOKENS = 3000
MAX_ATTEMPTS = 2  # one retry on invalid output
FAILED_ATTEMPTS_BEFORE_PAUSE = 2


class OutputError(Exception):
    pass


@dataclass
class DocInput:
    ref: str
    document_id: int
    title: str
    kind: str
    page_count: int
    pages: dict[int, str]

    def size(self, pages: list[int] | None = None) -> int:
        selected = self.pages if pages is None else {p: self.pages.get(p, "") for p in pages}
        return sum(len(text) + PAGE_OVERHEAD for text in selected.values())

    def render(self, pages: list[int] | None = None) -> str:
        numbers = sorted(self.pages) if pages is None else pages
        return "\n".join(f"{marker(n)}\n{self.pages.get(n, '').strip()}\n" for n in numbers)


@dataclass
class ChunkPart:
    doc: DocInput
    pages: list[int] | None = None  # None = whole document

    @property
    def part_label(self) -> str | None:
        if self.pages is None:
            return None
        ranges = merge_ranges([(p, p) for p in self.pages])
        return ", ".join(f"s. {a}–{b}" if a != b else f"s. {a}" for a, b in ranges)


@dataclass
class Chunk:
    index: int
    count: int
    label: str
    parts: list[ChunkPart]
    skipped: list[str]
    user: str = ""
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def custom_id_suffix(self) -> str:
        return f"c{self.index}"


@dataclass
class AssessmentPlan:
    hearing: dict[str, Any]
    docs: list[DocInput]
    chunks: list[Chunk]
    prompt: Prompt
    model: str
    notes: list[str]

    @property
    def hearing_id(self) -> int:
        return int(self.hearing["id"])

    @property
    def input_chars(self) -> int:
        return sum(len(c.user) + len(self.prompt.system) for c in self.chunks)

    @property
    def input_hash(self) -> str:
        payload = json.dumps([c.params for c in self.chunks], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class ChunkOutcome:
    chunk: Chunk
    output: AssessmentOutput | None
    responses: list[LLMResponse]
    error: str | None = None


# ---------------------------------------------------------------------------
# Input


def load_documents(conn: sqlite3.Connection, hearing_id: int) -> tuple[list[DocInput], list[str]]:
    rows = sorted(
        db.documents_for(conn, hearing_id),
        key=lambda r: (KIND_PRIORITY.get(r["kind"], 9), r["position"], r["id"]),
    )
    docs: list[DocInput] = []
    skipped: list[str] = []
    for n, row in enumerate(rows, start=1):
        ref = f"D{n}"
        path = Path(row["text_path"]) if row["text_path"] else None
        if row["fetch_status"] != "ok" or path is None or not path.is_file():
            reason = row["note"] or f"ikke tilgjengelig ({row['fetch_status']})"
            skipped.append(f"{ref}: {row['title']} – {reason}")
            continue
        pages = split_pages(path.read_text(encoding="utf-8"))
        docs.append(
            DocInput(
                ref,
                row["id"],
                row["title"] or ref,
                row["kind"],
                row["page_count"] or len(pages),
                pages,
            )
        )
    return docs, skipped


def budget_chars(settings: Settings, prompt: Prompt) -> int:
    reserve = PROMPT_RESERVE_TOKENS + int(len(prompt.system) / settings.chars_per_token)
    return max(10_000, int((settings.max_input_tokens - reserve) * settings.chars_per_token))


def plan_chunks(docs: list[DocInput], budget: int, skipped: list[str]) -> list[Chunk]:
    letters = [d for d in docs if d.kind == "horingsbrev"]
    primary = [d for d in docs if d.kind in ("horingsnotat", "lovutkast")]
    secondary = [d for d in docs if d.kind not in ("horingsbrev", "horingsnotat", "lovutkast")]
    base_size = sum(d.size() for d in letters)
    if base_size > budget * 0.3:  # unusually long letter: treat it like any other document
        primary = letters + primary
        letters, base_size = [], 0
    primary_size = sum(d.size() for d in primary)

    if base_size + primary_size <= budget:
        parts = [ChunkPart(d) for d in letters + primary]
        room = budget - base_size - primary_size
        extra_skipped = list(skipped)
        for d in secondary:
            if d.size() <= room:
                parts.append(ChunkPart(d))
                room -= d.size()
            else:
                extra_skipped.append(f"{d.ref}: {d.title} – ikke plass innenfor budsjettet")
        return [Chunk(1, 1, "hele høringen", parts, extra_skipped)]

    # Too large: split primary documents per chapter. Every chunk gets the letter and,
    # when found, the summary/introduction chapter of the main document.
    context_parts: list[ChunkPart] = [ChunkPart(d) for d in letters]
    context_size = base_size
    units: list[tuple[DocInput, Section]] = []
    for d in primary:
        if d.size() <= budget * 0.25:
            units.append((d, Section(min(d.pages), max(d.pages), "hele dokumentet")))
            continue
        sections = sections_for(d.pages)
        intro = summary_section(sections) if d.kind == "horingsnotat" else None
        if (
            intro
            and d.size(list(intro.pages)) <= budget * 0.15
            and not any(p.doc is d for p in context_parts)
        ):
            context_parts.append(ChunkPart(d, list(intro.pages)))
            context_size += d.size(list(intro.pages))
        units.extend((d, s) for s in sections)

    available = max(budget - context_size, budget // 4)
    expanded: list[tuple[DocInput, Section]] = []
    for d, s in units:
        if d.size(list(s.pages)) > available:
            expanded.extend((d, part) for part in split_section(s, d.pages, available))
        else:
            expanded.append((d, s))

    groups: list[list[tuple[DocInput, Section]]] = [[]]
    used = 0
    for d, s in expanded:
        size = d.size(list(s.pages))
        if groups[-1] and used + size > available:
            groups.append([])
            used = 0
        groups[-1].append((d, s))
        used += size

    chunk_skipped = list(skipped) + [
        f"{d.ref}: {d.title} – ikke plass når høringen deles opp" for d in secondary
    ]
    chunks: list[Chunk] = []
    for index, group in enumerate(groups, start=1):
        by_doc: dict[str, tuple[DocInput, set[int]]] = {}
        for p in context_parts:
            pages = set(p.doc.pages) if p.pages is None else set(p.pages)
            by_doc[p.doc.ref] = (p.doc, pages)
        labels: list[str] = []
        for d, s in group:
            entry = by_doc.setdefault(d.ref, (d, set()))
            entry[1].update(s.pages)
            labels.append(f"{d.ref} {s.label}")
        parts = []
        for d, pages in sorted(by_doc.values(), key=lambda e: docs.index(e[0])):
            whole = pages >= set(d.pages)
            parts.append(ChunkPart(d, None if whole else sorted(pages)))
        label = labels[0] if len(labels) == 1 else f"{labels[0]} til {labels[-1]}"
        chunks.append(Chunk(index, len(groups), label, parts, chunk_skipped))
    return chunks


def plan_assessment(
    settings: Settings,
    conn: sqlite3.Connection,
    hearing_id: int,
    client: LLMClient,
    prompt_version: str | None = None,
) -> AssessmentPlan:
    row = conn.execute("SELECT * FROM hearing WHERE id = ?", (hearing_id,)).fetchone()
    if row is None:
        raise KeyError(f"Unknown hearing {hearing_id}")
    hearing = dict(row)
    prompt = load_prompt(settings.prompts_dir, prompt_version or settings.prompt_version)
    docs, skipped = load_documents(conn, hearing_id)
    chunks = plan_chunks(docs, budget_chars(settings, prompt), skipped)
    schema = output_json_schema()
    for chunk in chunks:
        chunk.user = prompt.render_user(
            hearing=hearing,
            chunk={"index": chunk.index, "count": chunk.count, "label": chunk.label},
            documents=[
                {
                    "ref": p.doc.ref,
                    "title": p.doc.title,
                    "kind_label": KIND_LABELS.get(p.doc.kind, p.doc.kind),
                    "page_count": p.doc.page_count,
                    "part": p.part_label,
                    "text": p.doc.render(p.pages),
                }
                for p in chunk.parts
            ],
            skipped=chunk.skipped,
        )
        chunk.params = client.build_params(prompt.system, chunk.user, schema)
    notes = list(skipped)
    if len(chunks) > 1:
        notes.append(f"Høringen ble delt i {len(chunks)} deler for vurdering.")
    for chunk in chunks:
        for item in chunk.skipped:
            if item not in notes:
                notes.append(item)
    return AssessmentPlan(hearing, docs, chunks, prompt, client.model, notes)


# ---------------------------------------------------------------------------
# Output


def parse_output(response: LLMResponse) -> AssessmentOutput:
    if response.stop_reason == "refusal":
        raise OutputError("modellen avviste forespørselen (refusal)")
    if response.stop_reason == "max_tokens":
        raise OutputError("svaret ble avkortet (max_tokens)")
    try:
        data = json.loads(response.text)
    except json.JSONDecodeError as exc:
        raise OutputError(f"ugyldig JSON: {exc}") from exc
    try:
        return AssessmentOutput.model_validate(data)
    except ValidationError as exc:
        raise OutputError(f"skjemafeil: {exc.error_count()} feil: {exc.errors()[:3]}") from exc


def execute_sync(plan: AssessmentPlan, client: LLMClient) -> list[ChunkOutcome]:
    outcomes: list[ChunkOutcome] = []
    for chunk in plan.chunks:
        responses: list[LLMResponse] = []
        output: AssessmentOutput | None = None
        error: str | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = client.complete(chunk.params)
            except LLMError as exc:
                error = f"API-feil: {exc}"
                log.error("Hearing %s chunk %s: %s", plan.hearing_id, chunk.index, error)
                break
            responses.append(response)
            try:
                output = parse_output(response)
                error = None
                break
            except OutputError as exc:
                error = str(exc)
                log.warning(
                    "Hearing %s chunk %s attempt %s: invalid output: %s",
                    plan.hearing_id,
                    chunk.index,
                    attempt,
                    exc,
                )
        outcomes.append(ChunkOutcome(chunk, output, responses, error))
        if output is None:
            break  # the assessment has failed; do not spend more on the remaining chunks
    return outcomes


def merge_outputs(outputs: list[AssessmentOutput]) -> AssessmentOutput:
    if len(outputs) == 1:
        return outputs[0]
    top = max(outputs, key=lambda o: RELEVANCE_ORDER[o.relevance])
    ordered = [top] + [o for o in outputs if o is not top]
    sectors: list[str] = []
    for o in ordered:
        for s in o.sectors:
            if s not in sectors:
                sectors.append(s)
    findings, seen = [], set()
    for o in outputs:
        for f in o.findings:
            key = (f.document, " ".join(f.quote.split()).lower())
            if key not in seen:
                seen.add(key)
                findings.append(f)
    guide: dict[str, list[tuple[int, int]]] = {}
    for o in outputs:
        for entry in o.reading_guide:
            guide.setdefault(entry.document, []).extend((r.start, r.end) for r in entry.pages)
    privacy = next((o.privacy_section for o in outputs if o.privacy_section.exists), None)
    return AssessmentOutput.model_validate(
        {
            "relevance": top.relevance,
            "relevance_reason": top.relevance_reason,
            "sectors": sectors[:3],
            "summary": outputs[0].summary,
            "findings": [f.model_dump() for f in findings],
            "reading_guide": [
                {"document": ref, "pages": [{"start": a, "end": b} for a, b in merge_ranges(r)]}
                for ref, r in guide.items()
            ],
            "privacy_section": (privacy or outputs[0].privacy_section).model_dump(),
        }
    )


def _cost(settings: Settings, responses: list[LLMResponse]) -> float:
    total = 0.0
    for r in responses:
        if r.cached:
            continue
        cost = (
            r.input_tokens * settings.price_input_per_mtok
            + r.output_tokens * settings.price_output_per_mtok
        ) / 1_000_000
        total += cost * (settings.batch_price_factor if r.batch else 1.0)
    return round(total, 6)


def _save_raw(
    settings: Settings, plan: AssessmentPlan, outcomes: list[ChunkOutcome], context: str
) -> Path:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    path = settings.llm_raw_dir / str(plan.hearing_id) / f"{stamp}-{context}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "hearing_id": plan.hearing_id,
        "model": plan.model,
        "prompt_version": plan.prompt.version,
        "input_hash": plan.input_hash,
        "chunks": [
            {
                "index": o.chunk.index,
                "label": o.chunk.label,
                "error": o.error,
                "responses": [
                    {"cached": r.cached, "batch": r.batch, "message": r.raw} for r in o.responses
                ],
            }
            for o in outcomes
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _reading_guide_json(plan: AssessmentPlan, output: AssessmentOutput) -> dict[str, Any]:
    by_ref = {d.ref: d for d in plan.docs}
    documents = []
    relevant = 0
    for entry in output.reading_guide:
        doc = by_ref.get(entry.document)
        limit = doc.page_count if doc else None
        ranges = merge_ranges(
            [
                (r.start, min(r.end, limit) if limit else r.end)
                for r in entry.pages
                if not limit or r.start <= limit
            ]
        )
        pages = sum(b - a + 1 for a, b in ranges)
        relevant += pages
        documents.append(
            {
                "ref": entry.document,
                "document_id": doc.document_id if doc else None,
                "title": doc.title if doc else entry.document,
                "page_count": doc.page_count if doc else None,
                "ranges": ranges,
                "relevant_pages": pages,
            }
        )
    total = sum(d.page_count for d in plan.docs)
    return {"documents": documents, "relevant_pages": relevant, "total_pages": total}


def _privacy_json(plan: AssessmentPlan, output: AssessmentOutput) -> dict[str, Any]:
    p = output.privacy_section
    doc = next((d for d in plan.docs if d.ref == p.document), None)
    return {
        "exists": p.exists,
        "document_ref": p.document,
        "document_id": doc.document_id if doc else None,
        "document_title": doc.title if doc else None,
        "chapter": p.chapter,
        "pages": [p.pages.start, p.pages.end] if p.pages else None,
    }


def store_assessment(
    settings: Settings,
    conn: sqlite3.Connection,
    plan: AssessmentPlan,
    outcomes: list[ChunkOutcome],
    context: str = "drift",
    backtest_run_id: int | None = None,
) -> int:
    responses = [r for o in outcomes for r in o.responses]
    raw_path = _save_raw(settings, plan, outcomes, context)
    failed = len(outcomes) < len(plan.chunks) or any(o.output is None for o in outcomes)
    model = next((r.model for r in reversed(responses) if r.model), plan.model)
    common = (
        plan.hearing_id,
        db.now_iso(),
        model,
        plan.prompt.version,
        context,
        backtest_run_id,
        sum(r.input_tokens for r in responses if not r.cached),
        sum(r.output_tokens for r in responses if not r.cached),
        plan.input_chars,
        len(plan.chunks),
        int(any(r.batch for r in responses)),
        _cost(settings, responses),
        str(raw_path),
        plan.input_hash,
        db.dumps(plan.notes),
    )
    columns = (
        "hearing_id, created_at, model, prompt_version, context, backtest_run_id, input_tokens,"
        " output_tokens, input_chars, chunk_count, batch, cost_estimate, raw_response_path,"
        " input_hash, notes"
    )
    if failed:
        error = next((o.error for o in outcomes if o.error), "ukjent feil")
        cur = conn.execute(
            f"INSERT INTO assessment ({columns}, status, error)"
            f" VALUES ({', '.join('?' * len(common))}, 'failed', ?)",
            (*common, error),
        )
        log.error("Assessment of %s failed: %s", plan.hearing_id, error)
        return int(cur.lastrowid)

    output = merge_outputs([o.output for o in outcomes if o.output is not None])
    cur = conn.execute(
        f"INSERT INTO assessment ({columns}, status, relevance, relevance_reason, sectors, summary,"
        " privacy_section, reading_guide)"
        f" VALUES ({', '.join('?' * len(common))}, 'ok', ?, ?, ?, ?, ?, ?)",
        (
            *common,
            output.relevance,
            output.relevance_reason,
            db.dumps(output.sectors),
            output.summary,
            db.dumps(_privacy_json(plan, output)),
            db.dumps(_reading_guide_json(plan, output)),
        ),
    )
    assessment_id = int(cur.lastrowid)
    by_ref = {d.ref: d for d in plan.docs}
    normalized: dict[str, dict[int, str]] = {}
    verified = 0
    for f in output.findings:
        doc = by_ref.get(f.document)
        if doc is None:
            check = QuoteCheck(False, 0.0, f.page)
            log.warning(
                "Hearing %s: finding refers to unknown document %s", plan.hearing_id, f.document
            )
        else:
            if doc.ref not in normalized:
                normalized[doc.ref] = normalize_pages(doc.pages)
            check = verify_quote(f.quote, normalized[doc.ref], f.page)
        page = check.page if check.verified and check.page else f.page
        if check.verified and page != f.page:
            log.info(
                "Hearing %s: quote found on page %s, not %s as stated (%s); page corrected",
                plan.hearing_id,
                page,
                f.page,
                f.document,
            )
        if not check.verified:
            log.warning(
                "Hearing %s: unverified quote in %s p. %s (score %.1f)",
                plan.hearing_id,
                f.document,
                f.page,
                check.score,
            )
        verified += int(check.verified)
        conn.execute(
            "INSERT INTO finding (assessment_id, document_id, document_ref, category, description,"
            " chapter, page, page_stated, quote, quote_verified, match_score)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                assessment_id,
                doc.document_id if doc else None,
                f.document,
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
    if context == "drift":
        conn.execute("UPDATE hearing SET needs_assessment = 0 WHERE id = ?", (plan.hearing_id,))
    log.info(
        "Hearing %s assessed: %s, %s findings (%s verified quotes)",
        plan.hearing_id,
        output.relevance,
        len(output.findings),
        verified,
    )
    return assessment_id


def assess_hearing(
    settings: Settings,
    conn: sqlite3.Connection,
    hearing_id: int,
    client: LLMClient,
    prompt_version: str | None = None,
) -> int:
    """Synchronous drift assessment of one hearing. Returns the assessment id."""
    plan = plan_assessment(settings, conn, hearing_id, client, prompt_version)
    log.info(
        "Assessing %s with %s / prompt %s in %s chunk(s), ~%s chars",
        hearing_id,
        plan.model,
        plan.prompt.version,
        len(plan.chunks),
        plan.input_chars,
    )
    outcomes = execute_sync(plan, client)
    return store_assessment(settings, conn, plan, outcomes, context="drift")


def failed_attempts_since_change(conn: sqlite3.Connection, hearing_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM assessment a JOIN hearing h ON h.id = a.hearing_id"
        " WHERE a.hearing_id = ? AND a.context = 'drift' AND a.status = 'failed'"
        " AND (h.content_changed_at IS NULL OR a.created_at >= h.content_changed_at)",
        (hearing_id,),
    ).fetchone()
    return int(row["n"])
