"""The API backends, against the request and response shapes in the providers' documentation. Error bodies are the
real ones both APIs returned for an invalid key (2026-10-22)."""
import json

import httpx
import pytest

from shortlist import scoring
from shortlist.config import Scoring
from shortlist.discover import AgentStep

OK_SCHEMA = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
ANTHROPIC_401 = {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"},
                 "request_id": "req_011CgH8hNbN9155FjYzyiVWV"}
OPENAI_401 = {"error": {"message": "Incorrect API key provided: invalid. You can find your API key at "
                                   "https://platform.openai.com/account/api-keys.",
                        "type": "invalid_request_error", "param": None, "code": "invalid_api_key"}}


def recorder(response):
    seen = []

    def handler(request):
        seen.append({"url": str(request.url), "headers": request.headers, "body": json.loads(request.content)})
        return response

    return seen, httpx.MockTransport(handler)


def test_strict_schema_closes_every_object_and_drops_unsupported_keywords():
    item = scoring.strict_schema(scoring.SCHEMA)["properties"]["results"]["items"]
    assert item["additionalProperties"] is False and item["required"] == list(item["properties"])
    assert item["properties"]["score"] == {"type": "integer"} and "title" not in item  # bounds are checked afterwards
    step = scoring.strict_schema(AgentStep.model_json_schema())
    assert "title" in step["properties"]  # a property called "title" is a name, not the keyword
    assert step["required"] == list(step["properties"]) and step["properties"]["board"] == {"type": "string"}


def test_anthropic_api_request_and_answer(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    seen, transport = recorder(httpx.Response(200, json={
        "type": "message", "role": "assistant", "stop_reason": "end_turn",
        "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": '{"ok": true}'}]}))
    assert scoring.anthropic_api("hi", OK_SCHEMA, Scoring(backend="anthropic_api"), transport=transport) == {"ok": True}
    [call] = seen
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert (call["headers"]["x-api-key"], call["headers"]["anthropic-version"]) == ("sk-test", "2023-06-01")
    body = call["body"]
    assert body["model"] == "claude-opus-5-5" and body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["output_config"] == {"format": {"type": "json_schema", "schema": scoring.strict_schema(OK_SCHEMA)}}
    # Refusal fallback (beta) for the models that support its "default" mode, and only for them
    assert call["headers"]["anthropic-beta"] == "server-side-fallback-2026-07-01" and body["fallbacks"] == "default"
    seen.clear()
    scoring.anthropic_api("hi", OK_SCHEMA, Scoring(backend="anthropic_api", model="claude-haiku-5-5", effort="low"),
                          transport=transport)
    assert "anthropic-beta" not in seen[0]["headers"] and "fallbacks" not in seen[0]["body"]
    assert seen[0]["body"]["output_config"]["effort"] == "low"


@pytest.mark.parametrize("response, message", [
    (httpx.Response(401, json=ANTHROPIC_401), "HTTP 401: invalid x-api-key"),
    (httpx.Response(200, json={"stop_reason": "refusal", "content": []}), "no answer (stop_reason=refusal)"),
    (httpx.Response(200, json={"stop_reason": "max_tokens", "content": [{"type": "text", "text": "{"}]}),
     "no answer (stop_reason=max_tokens)"),
])
def test_anthropic_api_failures_raise_without_the_key(monkeypatch, response, message):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-SECRET")
    with pytest.raises(RuntimeError) as err:
        scoring.anthropic_api("hi", OK_SCHEMA, Scoring(backend="anthropic_api"), transport=recorder(response)[1])
    assert message in str(err.value) and "SECRET" not in str(err.value)


def test_api_backends_need_their_key(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY is not set"):
        scoring.anthropic_api("hi", OK_SCHEMA, Scoring(backend="anthropic_api"))
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is not set"):
        scoring.openai_api("hi", OK_SCHEMA, Scoring(backend="openai_api", model="m"))


def completion(content, finish_reason="stop", refusal=None):
    return httpx.Response(200, json={"id": "x", "object": "chat.completion", "choices": [
        {"index": 0, "finish_reason": finish_reason,
         "message": {"role": "assistant", "content": content, "refusal": refusal}}]})


def test_openai_api_request_and_answer(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    seen, transport = recorder(completion('{"ok": true}'))
    cfg = Scoring(backend="openai_api", model="some-model")
    assert scoring.openai_api("hi", OK_SCHEMA, cfg, transport=transport) == {"ok": True}
    [call] = seen
    assert call["url"] == "https://api.openai.com/v1/chat/completions"
    assert call["headers"]["authorization"] == "Bearer sk-test"
    assert call["body"]["response_format"] == {"type": "json_schema", "json_schema": {
        "name": "answer", "schema": scoring.strict_schema(OK_SCHEMA), "strict": True}}


@pytest.mark.parametrize("response, message", [
    (httpx.Response(401, json=OPENAI_401), "HTTP 401: Incorrect API key provided"),
    (completion(None, refusal="I can't help with that."), "refusal=I can't help with that."),
    (completion('{"ok": tr', finish_reason="length"), "finish_reason=length"),
])
def test_openai_api_failures_raise(monkeypatch, response, message):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with pytest.raises(RuntimeError, match=message):
        scoring.openai_api("hi", OK_SCHEMA, Scoring(backend="openai_api", model="m"), transport=recorder(response)[1])
