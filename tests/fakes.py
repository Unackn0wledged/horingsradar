"""Test doubles: a fake regjeringen.no served from fixtures and a scripted LLM client."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
from conftest import REGJERINGEN

from horingsradar.llm.client import BatchStatus, LLMError, LLMResponse

KNOWN_HEARINGS = {
    3177107,
    3176758,
    3170511,
    3118997,
    3116453,
    3117588,
    3017719,
    3014731,
    3116210,
    3119016,
    3171614,
}


def _small_pdf() -> bytes:
    import pymupdf

    with pymupdf.open(REGJERINGEN / "notat_3170511_excerpt.pdf") as doc:
        doc.select([0, 6])
        return doc.tobytes()


@dataclass
class FakeSite:
    """Serves fixture files for regjeringen.no URLs and records requests."""

    list_pages: dict[int, str] = field(
        default_factory=lambda: {
            1: "list_page1.html",
            2: "list_page2.html",
            3: "list_page15_2025.html",
        }
    )
    overrides: dict[str, tuple[int, bytes, dict[str, str]]] = field(default_factory=dict)
    pdf_bytes: bytes = field(
        default_factory=lambda: (REGJERINGEN / "notat_3170511_excerpt.pdf").read_bytes()
    )
    small_pdf: bytes = field(default_factory=lambda: _small_pdf())
    requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url in self.overrides:
            status, body, headers = self.overrides[url]
            return httpx.Response(status, content=body, headers=headers)
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, content=(REGJERINGEN / "robots.txt").read_bytes())
        if path == "/no/dokument/hoyringar/id1763/":
            page = int(request.url.params.get("page", "1"))
            name = self.list_pages.get(page)
            if name is None:
                return httpx.Response(200, content=b"<html><body></body></html>")
            return self._html(name)
        if path.endswith(".pdf"):
            etag = '"fixture-etag"'
            if request.headers.get("if-none-match") == etag:
                return httpx.Response(304, headers={"etag": etag})
            # The real excerpt for 3170511; a two-page cut of it for every other hearing.
            body = self.pdf_bytes if "kriminformasjonsforskriften" in path else self.small_pdf
            return httpx.Response(
                200, content=body, headers={"content-type": "application/pdf", "etag": etag}
            )
        if "/id" in path:
            hearing_id = int(path.rstrip("/").rsplit("/id", 1)[1])
            if request.url.params.get("showSvar"):
                name = f"svar_{hearing_id}.html"
                if not (REGJERINGEN / name).exists():
                    name = "svar_3116453.html"
                return self._html(name)
            name = (
                f"hearing_{hearing_id}.html"
                if hearing_id in KNOWN_HEARINGS
                else "hearing_3177107.html"
            )
            return self._html(name)
        return httpx.Response(404)

    @staticmethod
    def _html(name: str) -> httpx.Response:
        return httpx.Response(
            200,
            content=(REGJERINGEN / name).read_bytes(),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler), follow_redirects=True)


def make_fetcher(settings, site: FakeSite):
    from horingsradar.http import Fetcher

    return Fetcher(settings, client=site.client(), sleep=lambda s: None)


def output(
    relevance: str = "hoy",
    findings: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    data = {
        "relevance": relevance,
        "relevance_reason": "Forslaget gir hjemmel for deling av taushetsbelagte opplysninger.",
        "sectors": ["justis_politi", "finans_skatt"],
        "summary": "Departementet foreslår en ny forskrift om informasjonsdeling. "
        "Forskriften regulerer behandling av personopplysninger i arbeidet mot økonomisk kriminalitet.",
        "findings": findings if findings is not None else [],
        "reading_guide": [{"document": "D2", "pages": [{"start": 6, "end": 9}]}],
        "privacy_section": {"exists": False, "document": None, "chapter": None, "pages": None},
    }
    data.update(overrides)
    return data


def finding(quote: str, page: int, document: str = "D2", **overrides: Any) -> dict[str, Any]:
    data = {
        "category": "behandlingsgrunnlag_hjemmel",
        "description": "Forslaget beskriver kravene til rettsgrunnlag for behandlingen.",
        "document": document,
        "chapter": "3.1 Krav til rettsgrunnlag",
        "page": page,
        "quote": quote,
    }
    data.update(overrides)
    return data


class FakeLLM:
    """Returns scripted responses. Each script item is a dict (JSON output), a str (raw text),
    an LLMError, or a callable(params) -> one of those."""

    model = "fake-model"

    def __init__(self, script: list[Any] | None = None, default: Any = None):
        self.script = list(script or [])
        self.default = default if default is not None else output()
        self.calls: list[dict[str, Any]] = []
        self.batches: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        self.cancelled: list[str] = []

    def build_params(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": 1000,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }

    def _respond(self, params: dict[str, Any], batch: bool = False) -> LLMResponse:
        item: Any = self.script.pop(0) if self.script else self.default
        if callable(item):
            item = item(params)
        if isinstance(item, LLMError):
            raise item
        text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        user = params["messages"][0]["content"]
        return LLMResponse(
            text=text,
            model=self.model,
            stop_reason="end_turn",
            input_tokens=len(user) // 3,
            output_tokens=len(text) // 3,
            raw={"content": [{"type": "text", "text": text}], "model": self.model},
            batch=batch,
        )

    def complete(self, params: dict[str, Any]) -> LLMResponse:
        self.calls.append(params)
        return self._respond(params)

    def create_batch(self, requests: list[tuple[str, dict[str, Any]]]) -> str:
        batch_id = f"batch-{len(self.batches) + 1}"
        self.batches[batch_id] = requests
        return batch_id

    def get_batch(self, batch_id: str) -> BatchStatus:
        return BatchStatus(batch_id, "ended", {"succeeded": len(self.batches[batch_id])}, "url")

    def batch_results(self, batch_id: str) -> Iterator[tuple[str, LLMResponse | LLMError]]:
        for custom_id, params in self.batches[batch_id]:
            self.calls.append(params)
            try:
                yield custom_id, self._respond(params, batch=True)
            except LLMError as exc:
                yield custom_id, exc

    def cancel_batch(self, batch_id: str) -> None:
        self.cancelled.append(batch_id)


def by_hearing(mapping: dict[int, Any], default: Any) -> Callable[[dict[str, Any]], Any]:
    """Script item that picks the output based on the hearing id in the prompt."""

    def pick(params: dict[str, Any]) -> Any:
        user = params["messages"][0]["content"]
        for hearing_id, value in mapping.items():
            if f"id{hearing_id}" in user:
                return value
        return default

    return pick
