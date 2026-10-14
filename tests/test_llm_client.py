"""AnthropicClient against a mocked HTTP API (no network, no real key)."""

import json

import httpx
import pytest

from horingsradar.llm.client import FALLBACK_BETA, AnthropicClient, LLMError
from horingsradar.llm.schema import output_json_schema


def message(text="{}", stop="end_turn", model="claude-sonnet-5-5"):
    return {
        "id": "msg_1",
        "type": "message",
        "model": model,
        "stop_reason": stop,
        "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": text}],
        "usage": {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 0},
    }


def client_with(settings, handler):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return AnthropicClient(settings, http=http, sleep=lambda s: None)


def test_request_shape_and_response_parsing(settings):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=message('{"a": 1}'))

    client = client_with(settings, handler)
    params = client.build_params("system", "user", output_json_schema())
    response = client.complete(params)

    request = seen[0]
    body = json.loads(request.content)
    assert request.url.path == "/v1/messages"
    assert request.headers["x-api-key"] == "test-key"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.headers["anthropic-beta"] == FALLBACK_BETA
    assert body["model"] == "claude-sonnet-5-5"
    assert body["fallbacks"] == "default"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["effort"] == "medium"
    assert "thinking" not in body and "temperature" not in body
    assert body["messages"] == [{"role": "user", "content": "user"}]
    assert response.text == '{"a": 1}'  # thinking block ignored, text block kept
    assert (response.input_tokens, response.output_tokens) == (1200, 300)


def test_fallback_rejected_is_dropped_once(settings):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "fallbacks" in body:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "type": "invalid_request_error",
                        "message": "fallbacks: not supported",
                    }
                },
            )
        return httpx.Response(200, json=message())

    client = client_with(settings, handler)
    params = client.build_params("s", "u", {})
    client.complete(params)
    client.complete(params)
    assert ["fallbacks" in b for b in bodies] == [True, False, False]


def test_no_fallback_for_other_models(settings):
    settings.llm_model = "claude-haiku-5-5"
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=message())

    client = client_with(settings, handler)
    client.complete(client.build_params("s", "u", {}))
    assert "fallbacks" not in json.loads(seen[0].content)
    assert "anthropic-beta" not in seen[0].headers


def test_retries_overload_then_succeeds(settings):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(529, json={"error": {"message": "Overloaded"}})
        return httpx.Response(200, json=message())

    client = client_with(settings, handler)
    client.complete(client.build_params("s", "u", {}))
    assert calls["n"] == 3


def test_auth_error_is_not_retried(settings):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401, json={"error": {"message": "invalid x-api-key"}})

    client = client_with(settings, handler)
    with pytest.raises(LLMError) as err:
        client.complete(client.build_params("s", "u", {}))
    assert err.value.status == 401 and not err.value.retryable
    assert calls["n"] == 1


def test_missing_api_key(settings):
    settings.anthropic_api_key = ""
    client = client_with(settings, lambda r: httpx.Response(200, json=message()))
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        client.complete(client.build_params("s", "u", {}))


def test_batch_roundtrip(settings):
    results = "\n".join(
        json.dumps(line)
        for line in [
            {
                "custom_id": "h1-c1-a1",
                "result": {"type": "succeeded", "message": message('{"ok": 1}')},
            },
            {
                "custom_id": "h2-c1-a1",
                "result": {"type": "errored", "error": {"type": "invalid_request"}},
            },
            {"custom_id": "h3-c1-a1", "result": {"type": "expired"}},
        ]
    )
    created = []

    def handler(request):
        path = request.url.path
        if request.method == "POST" and path == "/v1/messages/batches":
            created.append(json.loads(request.content))
            return httpx.Response(
                200, json={"id": "msgbatch_1", "processing_status": "in_progress"}
            )
        if path == "/v1/messages/batches/msgbatch_1":
            return httpx.Response(
                200,
                json={
                    "id": "msgbatch_1",
                    "processing_status": "ended",
                    "request_counts": {"succeeded": 1, "errored": 1, "expired": 1},
                    "results_url": "https://api.anthropic.com/v1/messages/batches/msgbatch_1/results",
                },
            )
        if path.endswith("/results"):
            return httpx.Response(200, text=results)
        if path.endswith("/cancel"):
            return httpx.Response(200, json={"id": "msgbatch_1", "processing_status": "canceling"})
        return httpx.Response(404)

    client = client_with(settings, handler)
    params = client.build_params("s", "u", {})
    batch_id = client.create_batch([("h1-c1-a1", params)])
    assert batch_id == "msgbatch_1"
    assert "fallbacks" not in created[0]["requests"][0]["params"]  # not allowed on batches
    assert client.get_batch(batch_id).processing_status == "ended"
    out = dict(client.batch_results(batch_id))
    assert out["h1-c1-a1"].text == '{"ok": 1}' and out["h1-c1-a1"].batch
    assert isinstance(out["h2-c1-a1"], LLMError) and not out["h2-c1-a1"].retryable
    assert isinstance(out["h3-c1-a1"], LLMError) and out["h3-c1-a1"].retryable
    client.cancel_batch(batch_id)
