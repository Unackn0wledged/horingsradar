"""The only module that talks to an LLM provider.

Anthropic Messages API and Message Batches API over httpx. Provider and model are selected with
LLM_PROVIDER and LLM_MODEL.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from horingsradar.config import Settings

log = logging.getLogger(__name__)

API_VERSION = "2023-06-01"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that accept `fallbacks: "default"` on the Claude API.
FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"}
RETRYABLE = {408, 409, 429, 500, 502, 503, 504, 529}
MAX_ATTEMPTS = 4


class LLMError(Exception):
    def __init__(self, message: str, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass
class LLMResponse:
    text: str
    model: str
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    raw: dict[str, Any] = field(repr=False)
    batch: bool = False
    cached: bool = False


@dataclass
class BatchStatus:
    id: str
    processing_status: str
    counts: dict[str, int]
    results_url: str | None


class LLMClient(Protocol):
    model: str

    def build_params(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]: ...

    def complete(self, params: dict[str, Any]) -> LLMResponse: ...

    def create_batch(self, requests: list[tuple[str, dict[str, Any]]]) -> str: ...

    def get_batch(self, batch_id: str) -> BatchStatus: ...

    def batch_results(self, batch_id: str) -> Iterator[tuple[str, LLMResponse | LLMError]]: ...

    def cancel_batch(self, batch_id: str) -> None: ...


def parse_message(data: dict[str, Any], batch: bool = False) -> LLMResponse:
    text = "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    )
    usage = data.get("usage") or {}
    input_tokens = (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
    )
    return LLMResponse(
        text=text,
        model=data.get("model", ""),
        stop_reason=data.get("stop_reason"),
        input_tokens=input_tokens,
        output_tokens=int(usage.get("output_tokens") or 0),
        raw=data,
        batch=batch,
    )


class AnthropicClient:
    def __init__(
        self,
        settings: Settings,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self.model = settings.llm_model
        self.base_url = settings.anthropic_base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=settings.llm_timeout_seconds)
        self.sleep = sleep
        self._fallback_rejected = False

    def _headers(self, beta: str | None = None) -> dict[str, str]:
        if not self.settings.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        headers = {
            "x-api-key": self.settings.anthropic_api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        if beta:
            headers["anthropic-beta"] = beta
        return headers

    def _send(
        self, method: str, url: str, *, body: dict | None = None, beta: str | None = None
    ) -> httpx.Response:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self.http.request(method, url, json=body, headers=self._headers(beta))
            except httpx.TransportError as exc:
                if attempt == MAX_ATTEMPTS:
                    raise LLMError(f"connection error: {exc}", retryable=True) from exc
                delay = 2**attempt
                log.warning("LLM connection error (%s/%s): %s", attempt, MAX_ATTEMPTS, exc)
            else:
                if response.status_code < 400:
                    return response
                message = self._error_message(response)
                if response.status_code not in RETRYABLE or attempt == MAX_ATTEMPTS:
                    raise LLMError(
                        f"HTTP {response.status_code}: {message}",
                        retryable=response.status_code in RETRYABLE,
                        status=response.status_code,
                    )
                retry_after = response.headers.get("retry-after")
                delay = float(retry_after) if retry_after else 2**attempt * 2
                log.warning(
                    "LLM HTTP %s (%s/%s), retrying in %.0fs: %s",
                    response.status_code,
                    attempt,
                    MAX_ATTEMPTS,
                    delay,
                    message,
                )
            self.sleep(min(delay, 120))
        raise LLMError("unreachable")  # pragma: no cover

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            return response.json().get("error", {}).get("message", response.text[:300])
        except ValueError:
            return response.text[:300]

    # -- messages ----------------------------------------------------------

    def build_params(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": self.settings.llm_max_output_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {
                "effort": self.settings.llm_effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        }

    def complete(self, params: dict[str, Any]) -> LLMResponse:
        url = f"{self.base_url}/v1/messages"
        use_fallback = (
            self.settings.llm_refusal_fallback
            and not self._fallback_rejected
            and params.get("model") in FALLBACK_MODELS
        )
        if use_fallback:
            try:
                response = self._send(
                    "POST", url, body={**params, "fallbacks": "default"}, beta=FALLBACK_BETA
                )
                return parse_message(response.json())
            except LLMError as exc:
                if exc.status != 400 or "fallback" not in str(exc).lower():
                    raise
                # The account/model does not accept server-side fallbacks: continue without.
                self._fallback_rejected = True
                log.warning("Server-side fallback rejected (%s); continuing without it", exc)
        response = self._send("POST", url, body=params)
        return parse_message(response.json())

    # -- batches -----------------------------------------------------------

    def create_batch(self, requests: list[tuple[str, dict[str, Any]]]) -> str:
        body = {"requests": [{"custom_id": cid, "params": params} for cid, params in requests]}
        response = self._send("POST", f"{self.base_url}/v1/messages/batches", body=body)
        return response.json()["id"]

    def get_batch(self, batch_id: str) -> BatchStatus:
        data = self._send("GET", f"{self.base_url}/v1/messages/batches/{batch_id}").json()
        return BatchStatus(
            id=data["id"],
            processing_status=data.get("processing_status", ""),
            counts=data.get("request_counts") or {},
            results_url=data.get("results_url"),
        )

    def batch_results(self, batch_id: str) -> Iterator[tuple[str, LLMResponse | LLMError]]:
        status = self.get_batch(batch_id)
        if not status.results_url:
            raise LLMError(f"batch {batch_id} has no results yet")
        response = self._send("GET", status.results_url)
        for line in response.text.splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            result = item.get("result") or {}
            kind = result.get("type")
            if kind == "succeeded":
                yield item["custom_id"], parse_message(result["message"], batch=True)
            else:
                error = result.get("error") or {}
                retryable = (
                    kind in ("expired", "canceled") or error.get("type") != "invalid_request"
                )
                yield (
                    item["custom_id"],
                    LLMError(f"batch item {kind}: {error}", retryable=retryable),
                )

    def cancel_batch(self, batch_id: str) -> None:
        self._send("POST", f"{self.base_url}/v1/messages/batches/{batch_id}/cancel")


def get_client(settings: Settings) -> LLMClient:
    if settings.llm_provider == "anthropic":
        return AnthropicClient(settings)
    raise LLMError(f"Unsupported LLM_PROVIDER: {settings.llm_provider}")
