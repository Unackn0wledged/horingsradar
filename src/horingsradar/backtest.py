"""Backtest: does the model/prompt rank hearings like Datatilsynet's actual responses?

Only ever started manually (CLI or admin panel), always after a cost estimate and an explicit
confirmation. Never called from `run`, timers or cron. Assessments are stored with
context='backtest' and never reach the operational reports.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from horingsradar import db
from horingsradar.assess import (
    MAX_ATTEMPTS,
    AssessmentPlan,
    ChunkOutcome,
    OutputError,
    parse_output,
    plan_assessment,
    store_assessment,
)
from horingsradar.config import Settings
from horingsradar.http import Fetcher, FetchError
from horingsradar.jobs import JobHandle
from horingsradar.llm.client import LLMClient, LLMError, LLMResponse
from horingsradar.sources.regjeringen.crawler import RegjeringenSource

log = logging.getLogger(__name__)

SPLITS = {
    "dev": (date(2024, 1, 1), date(2024, 12, 31)),
    "test": (date(2025, 1, 1), date(2025, 12, 31)),
}
CLOSED_STATUSES = ("under_behandling", "ferdigbehandlet")
DEFAULT_OUTPUT_TOKENS_PER_CHUNK = 4000
MAX_BATCH_BYTES = 200 * 1024 * 1024
DISCOVERY_MAX_PAGES = 600


@dataclass
class Selection:
    split: str | None
    period_from: date
    period_to: date
    hearing_ids: list[int]


@dataclass
class Estimate:
    hearings: int
    requests: int
    input_tokens: int
    output_tokens: int
    cost: float
    batch: bool
    chars_per_token: float
    cached_requests: int = 0
    unavailable: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "hearings": self.hearings,
            "requests": self.requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost": round(self.cost, 2),
            "batch": self.batch,
            "chars_per_token": round(self.chars_per_token, 2),
            "cached_requests": self.cached_requests,
        }

    def describe(self, settings: Settings) -> str:
        def n(value: int) -> str:
            return f"{value:,}".replace(",", " ")

        return (
            f"Høringer: {self.hearings}\n"
            f"Forespørsler til modellen: {self.requests} ({self.cached_requests} fra cache)\n"
            f"Estimerte tokens: {n(self.input_tokens)} inn / {n(self.output_tokens)} ut\n"
            f"Modell: {settings.llm_model}, prompt {settings.prompt_version}"
            f"{', batch-API' if self.batch else ''}\n"
            f"Estimert kostnad: ${self.cost:.2f} (priser fra .env, estimat)"
        )


def resolve_period(
    split: str | None, period_from: date | None, period_to: date | None
) -> tuple[date, date]:
    if split:
        start, end = SPLITS[split]
    else:
        if not (period_from and period_to):
            raise ValueError("Use --split or both --from and --to")
        start, end = period_from, period_to
    return period_from or start, period_to or end


def _stable_order(hearing_id: int) -> str:
    return hashlib.sha256(f"backtest-{hearing_id}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# Prepare (no LLM cost)


def prepare(
    settings: Settings,
    conn: sqlite3.Connection,
    fetcher: Fetcher,
    split: str | None,
    period_from: date,
    period_to: date,
    limit: int | None,
    progress: Callable[..., None],
) -> Selection:
    source = RegjeringenSource(settings, conn, fetcher)
    log.info("Backtest discovery %s – %s", period_from, period_to)
    source.discover(
        since=period_from,
        until=period_to,
        tracked=False,
        max_pages=DISCOVERY_MAX_PAGES,
        progress=progress,
    )
    rows = conn.execute(
        "SELECT id FROM hearing WHERE published_date BETWEEN ? AND ? AND status IN (?, ?)",
        (period_from.isoformat(), period_to.isoformat(), *CLOSED_STATUSES),
    ).fetchall()
    ids = sorted((r["id"] for r in rows), key=_stable_order)
    if limit:
        ids = ids[:limit]
    ready: list[int] = []
    for i, hearing_id in enumerate(ids, start=1):
        progress(phase="prepare", done=i - 1, total=len(ids), hearing=hearing_id)
        row = conn.execute(
            "SELECT last_checked, dt_responded FROM hearing WHERE id = ?", (hearing_id,)
        ).fetchone()
        if row["last_checked"] is None or row["dt_responded"] is None:
            try:
                # Closed hearings do not change: cache their pages and files indefinitely.
                source.refresh_hearing(hearing_id, max_age=math.inf, fetch_responses=True)
            except FetchError as exc:
                log.warning("Backtest: could not fetch hearing %s: %s", hearing_id, exc)
                continue
        ready.append(hearing_id)
    log.info("Backtest selection: %s hearings ready", len(ready))
    return Selection(split, period_from, period_to, sorted(ready))


# ---------------------------------------------------------------------------
# Cache of LLM responses keyed by request parameters


def _cache_path(settings: Settings, params: dict[str, Any]) -> Path:
    key = hashlib.sha256(
        json.dumps(params, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return settings.llm_cache_dir / key[:2] / f"{key}.json"


def cache_get(settings: Settings, params: dict[str, Any]) -> LLMResponse | None:
    path = _cache_path(settings, params)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return LLMResponse(
        text=data["text"],
        model=data["model"],
        stop_reason=data["stop_reason"],
        input_tokens=data["input_tokens"],
        output_tokens=data["output_tokens"],
        raw=data["raw"],
        batch=data.get("batch", False),
        cached=True,
    )


def cache_put(settings: Settings, params: dict[str, Any], response: LLMResponse) -> None:
    path = _cache_path(settings, params)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "text": response.text,
        "model": response.model,
        "stop_reason": response.stop_reason,
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "raw": response.raw,
        "batch": response.batch,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Estimate


def calibrated_chars_per_token(settings: Settings, conn: sqlite3.Connection) -> float:
    row = conn.execute(
        "SELECT COUNT(*) AS n, SUM(input_chars) AS chars, SUM(input_tokens) AS tokens"
        " FROM assessment WHERE input_tokens > 0 AND input_chars > 0"
    ).fetchone()
    if row["n"] and row["n"] >= 5 and row["tokens"]:
        return row["chars"] / row["tokens"]
    return settings.chars_per_token


def average_output_tokens(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT SUM(output_tokens) AS t, SUM(chunk_count) AS c FROM assessment"
        " WHERE status = 'ok' AND output_tokens > 0"
    ).fetchone()
    if row["c"] and row["t"]:
        return int(row["t"] / row["c"])
    return DEFAULT_OUTPUT_TOKENS_PER_CHUNK


def estimate(
    settings: Settings, conn: sqlite3.Connection, plans: list[AssessmentPlan], batch: bool
) -> Estimate:
    cpt = calibrated_chars_per_token(settings, conn)
    per_chunk_out = average_output_tokens(conn)
    requests = cached = 0
    input_chars = 0
    for plan in plans:
        for chunk in plan.chunks:
            requests += 1
            if cache_get(settings, chunk.params):
                cached += 1
                continue
            input_chars += len(chunk.user) + len(plan.prompt.system)
    billable = requests - cached
    input_tokens = int(input_chars / cpt)
    output_tokens = billable * per_chunk_out
    cost = (
        input_tokens * settings.price_input_per_mtok
        + output_tokens * settings.price_output_per_mtok
    ) / 1_000_000
    if batch:
        cost *= settings.batch_price_factor
    return Estimate(len(plans), requests, input_tokens, output_tokens, cost, batch, cpt, cached)


# ---------------------------------------------------------------------------
# Execution


def _custom_id(plan: AssessmentPlan, chunk_index: int, attempt: int) -> str:
    return f"h{plan.hearing_id}-c{chunk_index}-a{attempt}"


def _wait_for_batch(
    client: LLMClient, batch_id: str, settings: Settings, progress: Callable[..., None]
) -> None:
    while True:
        status = client.get_batch(batch_id)
        progress(phase="batch", batch_id=batch_id, status=status.processing_status, **status.counts)
        if status.processing_status == "ended":
            return
        time.sleep(settings.backtest_poll_seconds)


def _run_batch(
    client: LLMClient,
    requests: list[tuple[str, dict[str, Any]]],
    settings: Settings,
    progress: Callable[..., None],
) -> dict[str, LLMResponse | LLMError]:
    results: dict[str, LLMResponse | LLMError] = {}
    group: list[tuple[str, dict[str, Any]]] = []
    size = 0
    groups: list[list[tuple[str, dict[str, Any]]]] = []
    for item in requests:
        item_size = len(json.dumps(item[1], ensure_ascii=False).encode())
        if group and size + item_size > MAX_BATCH_BYTES:
            groups.append(group)
            group, size = [], 0
        group.append(item)
        size += item_size
    if group:
        groups.append(group)
    for group in groups:
        batch_id = client.create_batch(group)
        log.info("Submitted batch %s with %s requests", batch_id, len(group))
        try:
            _wait_for_batch(client, batch_id, settings, progress)
        except BaseException:
            log.warning("Cancelling batch %s", batch_id)
            try:
                client.cancel_batch(batch_id)
            except LLMError as exc:
                log.error("Could not cancel batch %s: %s", batch_id, exc)
            raise
        results.update(dict(client.batch_results(batch_id)))
    return results


def execute(
    settings: Settings,
    plans: list[AssessmentPlan],
    client: LLMClient,
    use_batch: bool,
    progress: Callable[..., None],
) -> dict[int, list[ChunkOutcome]]:
    """Run all chunks with one retry for invalid output. Returns outcomes per hearing."""
    state: dict[tuple[int, int], dict[str, Any]] = {}
    for plan in plans:
        for chunk in plan.chunks:
            state[(plan.hearing_id, chunk.index)] = {
                "plan": plan,
                "chunk": chunk,
                "responses": [],
                "output": None,
                "error": None,
                "done": False,
            }

    for attempt in range(1, MAX_ATTEMPTS + 1):
        todo = [s for s in state.values() if not s["done"]]
        if not todo:
            break
        pending: list[tuple[str, dict[str, Any]]] = []
        lookup: dict[str, dict[str, Any]] = {}
        for s in todo:
            cached = cache_get(settings, s["chunk"].params) if attempt == 1 else None
            if cached is not None:
                _accept(s, cached, settings, store_cache=False)
                continue
            cid = _custom_id(s["plan"], s["chunk"].index, attempt)
            lookup[cid] = s
            pending.append((cid, s["chunk"].params))
        if not pending:
            continue
        log.info("Backtest attempt %s: %s requests", attempt, len(pending))
        if use_batch:
            results = _run_batch(client, pending, settings, progress)
        else:
            results = {}
            for i, (cid, params) in enumerate(pending, start=1):
                progress(phase="assess", done=i - 1, total=len(pending), attempt=attempt)
                try:
                    results[cid] = client.complete(params)
                except LLMError as exc:
                    results[cid] = exc
        for cid, s in lookup.items():
            result = results.get(cid, LLMError("missing from batch results", retryable=True))
            if isinstance(result, LLMError):
                s["error"] = f"API-feil: {result}"
                if not result.retryable:
                    s["done"] = True
                continue
            _accept(s, result, settings, store_cache=True)

    outcomes: dict[int, list[ChunkOutcome]] = {}
    for plan in plans:
        outcomes[plan.hearing_id] = [
            ChunkOutcome(
                state[(plan.hearing_id, c.index)]["chunk"],
                state[(plan.hearing_id, c.index)]["output"],
                state[(plan.hearing_id, c.index)]["responses"],
                state[(plan.hearing_id, c.index)]["error"],
            )
            for c in plan.chunks
        ]
    return outcomes


def _accept(
    s: dict[str, Any], response: LLMResponse, settings: Settings, store_cache: bool
) -> None:
    s["responses"].append(response)
    try:
        s["output"] = parse_output(response)
        s["error"] = None
        s["done"] = True
        if store_cache:
            cache_put(settings, s["chunk"].params, response)
    except OutputError as exc:
        s["error"] = str(exc)
        log.warning("Backtest %s: invalid output: %s", s["plan"].hearing_id, exc)


# ---------------------------------------------------------------------------
# Metrics


def compute_metrics(
    conn: sqlite3.Connection, run_id: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows = conn.execute(
        "SELECT h.*, a.id AS assessment_id, a.status AS a_status, a.relevance, a.reading_guide,"
        " a.input_tokens, a.output_tokens, a.cost_estimate"
        " FROM assessment a JOIN hearing h ON h.id = a.hearing_id"
        " WHERE a.backtest_run_id = ? ORDER BY h.published_date, h.id",
        (run_id,),
    ).fetchall()
    per_hearing: list[dict[str, Any]] = []
    levels = ["hoy", "middels", "lav", "ingen", "feilet"]
    confusion = {lvl: {"svarte": 0, "svarte_ikke": 0, "ukjent": 0} for lvl in levels}
    findings_total = findings_verified = 0
    guide_relevant = guide_total = 0
    for r in rows:
        level = r["relevance"] if r["a_status"] == "ok" else "feilet"
        truth = r["dt_responded"]
        column = "ukjent" if truth is None else ("svarte" if truth else "svarte_ikke")
        confusion[level][column] += 1
        f = conn.execute(
            "SELECT COUNT(*) AS n, SUM(quote_verified) AS v FROM finding WHERE assessment_id = ?",
            (r["assessment_id"],),
        ).fetchone()
        findings_total += f["n"] or 0
        findings_verified += f["v"] or 0
        guide = db.loads(r["reading_guide"], {}) or {}
        flagged = level in ("hoy", "middels")
        if flagged and guide.get("total_pages"):
            guide_relevant += guide.get("relevant_pages", 0)
            guide_total += guide["total_pages"]
        per_hearing.append(
            {
                "id": r["id"],
                "title": r["title"],
                "department": r["department"],
                "published": r["published_date"],
                "deadline": r["deadline"],
                "status": r["status"],
                "relevance": level,
                "flagged": int(flagged),
                "dt_responded": truth,
                "dt_response_no_comments": r["dt_response_no_comments"],
                "dt_on_address_list": r["dt_on_address_list"],
                "findings": f["n"] or 0,
                "verified_quotes": f["v"] or 0,
                "relevant_pages": guide.get("relevant_pages"),
                "total_pages": guide.get("total_pages"),
                "url": r["url"],
            }
        )
    assessed = [h for h in per_hearing if h["relevance"] != "feilet"]
    responded = [h for h in assessed if h["dt_responded"] == 1]
    recall = sum(h["flagged"] for h in responded) / len(responded) if responded else None
    flagged_share = sum(h["flagged"] for h in assessed) / len(assessed) if assessed else None
    distribution = Counter(h["relevance"] for h in per_hearing)
    metrics = {
        "hearings": len(per_hearing),
        "assessed": len(assessed),
        "failed": distribution.get("feilet", 0),
        "dt_responded": len(responded),
        "dt_responded_no_comments": sum(1 for h in responded if h["dt_response_no_comments"]),
        "recall_high_medium": recall,
        "flagged_share": flagged_share,
        "distribution": {lvl: distribution.get(lvl, 0) for lvl in levels},
        "confusion": confusion,
        "findings": findings_total,
        "verified_quote_share": findings_verified / findings_total if findings_total else None,
        "reading_guide_share": guide_relevant / guide_total if guide_total else None,
        "review_high_without_response": [
            h["id"] for h in assessed if h["relevance"] == "hoy" and h["dt_responded"] == 0
        ],
        "review_response_but_low": [
            h["id"] for h in responded if h["relevance"] in ("lav", "ingen")
        ],
    }
    return metrics, per_hearing


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{value * 100:.1f} %"


def write_results(
    out_dir: Path, run: sqlite3.Row, metrics: dict[str, Any], per_hearing: list[dict[str, Any]]
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    if per_hearing:
        with (out_dir / "results.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(per_hearing[0].keys()))
            writer.writeheader()
            writer.writerows(per_hearing)
    (out_dir / "summary.json").write_text(
        json.dumps({"run": dict(run), "metrics": metrics}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    titles = {h["id"]: h["title"] for h in per_hearing}
    lines = [
        f"# Backtest {run['id']} ({run['split'] or 'egendefinert'})",
        "",
        f"- Periode: {run['period_from']} – {run['period_to']}",
        f"- Modell: {run['model']}, prompt {run['prompt_version']}",
        f"- Høringer: {metrics['hearings']} (vurdert {metrics['assessed']}, feilet {metrics['failed']})",
        f"- Datatilsynet svarte på: {metrics['dt_responded']}"
        f" (herav «uten merknader»: {metrics['dt_responded_no_comments']})",
        f"- **Recall høy + middels:** {_pct(metrics['recall_high_medium'])}",
        f"- **Andel flagget (høy + middels):** {_pct(metrics['flagged_share'])}",
        f"- Verifiserte sitater: {_pct(metrics['verified_quote_share'])} av {metrics['findings']} funn",
        f"- Leseguide: {_pct(metrics['reading_guide_share'])} av sidene i flaggede høringer",
        f"- Tokens: {run['actual_input_tokens']} inn / {run['actual_output_tokens']} ut",
        "",
        "## Forvirringsmatrise",
        "",
        "| Relevans | Svarte | Svarte ikke | Ukjent |",
        "|---|---|---|---|",
    ]
    for level, cols in metrics["confusion"].items():
        lines.append(f"| {level} | {cols['svarte']} | {cols['svarte_ikke']} | {cols['ukjent']} |")
    lines += [
        "",
        "## Til manuell gjennomgang",
        "",
        "### Høy relevans, men Datatilsynet svarte ikke",
        "",
    ]
    lines += [f"- {i}: {titles.get(i, '')}" for i in metrics["review_high_without_response"]] or [
        "- ingen"
    ]
    lines += ["", "### Datatilsynet svarte, men lav eller ingen relevans", ""]
    lines += [f"- {i}: {titles.get(i, '')}" for i in metrics["review_response_but_low"]] or [
        "- ingen"
    ]
    lines += [
        "",
        "Fasiten er støyete: noen svar sendes utenom skjemaet, og at Datatilsynet ikke svarte kan"
        " skyldes kapasitet. Backtesten tester verktøyet, ikke Datatilsynet.",
    ]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Entry point used by the CLI


def run_backtest(
    settings: Settings,
    conn: sqlite3.Connection,
    job: JobHandle,
    client: LLMClient,
    split: str | None,
    period_from: date | None = None,
    period_to: date | None = None,
    limit: int | None = None,
    use_batch: bool = True,
    confirm: Callable[[Estimate], bool] | None = None,
    estimate_only: bool = False,
    fetcher: Fetcher | None = None,
) -> dict[str, Any]:
    start, end = resolve_period(split, period_from, period_to)
    own_fetcher = fetcher is None
    fetcher = fetcher or Fetcher(settings)
    try:
        selection = prepare(settings, conn, fetcher, split, start, end, limit, job.progress)
    finally:
        if own_fetcher:
            fetcher.close()
    plans = [plan_assessment(settings, conn, hid, client) for hid in selection.hearing_ids]
    est = estimate(settings, conn, plans, use_batch)
    job.summary.update(
        split=split,
        period_from=start.isoformat(),
        period_to=end.isoformat(),
        limit=limit,
        estimate=est.as_dict(),
    )
    job.save_summary()
    log.info("Backtest estimate:\n%s", est.describe(settings))
    if estimate_only:
        job.summary["estimate_only"] = True
        return job.summary
    if not plans:
        job.summary["result"] = "no hearings in selection"
        return job.summary
    if confirm is None or not confirm(est):
        job.summary["result"] = "not confirmed"
        log.info("Backtest not confirmed; nothing was sent to the model")
        return job.summary

    cur = conn.execute(
        "INSERT INTO backtest_run (created_at, split, period_from, period_to, model, prompt_version,"
        " hearing_count, estimated_cost, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running')",
        (
            db.now_iso(),
            split,
            start.isoformat(),
            end.isoformat(),
            client.model,
            plans[0].prompt.version,
            len(plans),
            round(est.cost, 4),
        ),
    )
    run_id = int(cur.lastrowid)
    job.summary["backtest_run_id"] = run_id
    job.save_summary()
    try:
        outcomes = execute(settings, plans, client, use_batch, job.progress)
    except BaseException:
        conn.execute("UPDATE backtest_run SET status = 'cancelled' WHERE id = ?", (run_id,))
        raise
    for plan in plans:
        store_assessment(
            settings,
            conn,
            plan,
            outcomes[plan.hearing_id],
            context="backtest",
            backtest_run_id=run_id,
        )
    totals = conn.execute(
        "SELECT SUM(input_tokens) AS i, SUM(output_tokens) AS o FROM assessment WHERE backtest_run_id = ?",
        (run_id,),
    ).fetchone()
    out_dir = settings.backtests_dir / str(run_id)
    conn.execute(
        "UPDATE backtest_run SET actual_input_tokens = ?, actual_output_tokens = ?, results_path = ?"
        " WHERE id = ?",
        (totals["i"] or 0, totals["o"] or 0, str(out_dir), run_id),
    )
    metrics, per_hearing = compute_metrics(conn, run_id)
    conn.execute(
        "UPDATE backtest_run SET status = 'ok', metrics = ? WHERE id = ?",
        (db.dumps(metrics), run_id),
    )
    run = conn.execute("SELECT * FROM backtest_run WHERE id = ?", (run_id,)).fetchone()
    write_results(out_dir, run, metrics, per_hearing)
    job.summary.update(
        recall=metrics["recall_high_medium"],
        flagged_share=metrics["flagged_share"],
        results_path=str(out_dir),
    )
    log.info(
        "Backtest %s done: %s",
        run_id,
        {k: metrics[k] for k in ("recall_high_medium", "flagged_share")},
    )
    return job.summary
