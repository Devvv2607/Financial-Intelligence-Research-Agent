"""GroqClient: strict schema, caching, validation retry, rate limits, daily budget."""

import json
from typing import Any, Literal

import httpx
import pytest
import respx
from pydantic import BaseModel, SecretStr

from core.config import Settings
from core.llm import (
    DailyLimitError,
    GroqClient,
    InMemoryLLMStore,
    InvalidOutputError,
    LLMError,
    _parse_duration_s,
    strict_json_schema,
)

# Groq is served by each test's own respx mock (canned responses, no network).
pytestmark = pytest.mark.groq_mocked

URL = "https://api.groq.com/openai/v1/chat/completions"


class Tag(BaseModel):
    id: int
    category: Literal["a", "b"]


class Batch(BaseModel):
    tags: list[Tag]


def _settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "groq_api_key": SecretStr("gsk_test"),
        "groq_model_fast": "fast-model",
        "groq_model_reasoning": "big-model",
    }
    return Settings(**(base | kw))


def _ok(
    content: Any,
    prompt: int = 100,
    completion: int = 20,
    reasoning: int = 5,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(
        200,
        headers=headers or {},
        json={
            "choices": [{"message": {"content": json.dumps(content)}}],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "completion_tokens_details": {"reasoning_tokens": reasoning},
            },
        },
    )


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)


def _client(store: InMemoryLLMStore, sleeps: Sleeps, **kw: Any) -> GroqClient:
    return GroqClient(_settings(**kw), store, http=httpx.AsyncClient(), sleep=sleeps)


MSG = [{"role": "user", "content": "tag these"}]


def test_strict_schema_inlines_refs_and_requires_everything() -> None:
    schema = strict_json_schema(Batch)
    assert "$defs" not in json.dumps(schema)
    item = schema["properties"]["tags"]["items"]
    assert item["required"] == ["id", "category"]
    assert item["additionalProperties"] is False
    assert schema["additionalProperties"] is False


@respx.mock
async def test_structured_call_records_usage_and_caches() -> None:
    route = respx.post(URL).mock(return_value=_ok({"tags": [{"id": 0, "category": "a"}]}))
    store, sleeps = InMemoryLLMStore(), Sleeps()
    llm = _client(store, sleeps)
    first = await llm.structured("fast", Batch, MSG, stage="risk_tagging", cik=1)
    second = await llm.structured("fast", Batch, MSG, stage="risk_tagging", cik=1)
    assert first == second == Batch(tags=[Tag(id=0, category="a")])
    assert route.call_count == 1
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "fast-model"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["reasoning_effort"] == "low"
    live, hit = store.calls
    assert (live.prompt_tokens, live.completion_tokens, live.reasoning_tokens) == (100, 20, 5)
    assert hit.cache_hit and hit.prompt_tokens == 0


@respx.mock
async def test_validation_error_is_retried_with_feedback() -> None:
    route = respx.post(URL).mock(
        side_effect=[
            _ok({"tags": [{"id": 0, "category": "zzz"}]}),
            _ok({"tags": [{"id": 0, "category": "b"}]}),
        ]
    )
    out = await _client(InMemoryLLMStore(), Sleeps()).structured("fast", Batch, MSG, stage="t")
    assert out.tags[0].category == "b"
    retry_messages = json.loads(route.calls[1].request.content)["messages"]
    assert "invalid" in retry_messages[-1]["content"]


@respx.mock
async def test_validation_stops_after_two_attempts_and_returns_last_output() -> None:
    route = respx.post(URL).mock(return_value=_ok({"tags": [{"id": 0, "category": "zzz"}]}))
    with pytest.raises(InvalidOutputError, match="validation") as info:
        await _client(InMemoryLLMStore(), Sleeps()).structured("fast", Batch, MSG, stage="t")
    assert route.call_count == 2  # capped: first try + one retry, never more
    assert json.loads(info.value.last_output) == {"tags": [{"id": 0, "category": "zzz"}]}


@respx.mock
async def test_per_minute_429_waits_retry_after_then_succeeds() -> None:
    respx.post(URL).mock(
        side_effect=[
            httpx.Response(
                429,
                headers={"retry-after": "12"},
                json={"error": {"message": "Rate limit reached for tokens per minute (TPM)"}},
            ),
            _ok({"tags": []}),
        ]
    )
    sleeps = Sleeps()
    await _client(InMemoryLLMStore(), sleeps).structured("fast", Batch, MSG, stage="t")
    assert 12.0 in sleeps.calls


@respx.mock
async def test_per_day_429_raises_daily_limit() -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            429,
            json={
                "error": {"message": "Rate limit reached ... on tokens per day (TPD): Limit 200000"}
            },
        )
    )
    with pytest.raises(DailyLimitError):
        await _client(InMemoryLLMStore(), Sleeps()).structured("fast", Batch, MSG, stage="t")


async def test_own_daily_budget_stops_before_calling() -> None:
    store = InMemoryLLMStore()
    from core.llm import CallRecord

    await store.record(CallRecord("fast-model", "t", None, 150_000, 49_990, None, 1, False, True))
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(URL)
        with pytest.raises(DailyLimitError, match="budget"):
            await _client(store, Sleeps()).structured("fast", Batch, MSG, stage="t")
        assert route.call_count == 0


@respx.mock
async def test_paces_on_remaining_token_headers() -> None:
    respx.post(URL).mock(
        return_value=_ok(
            {"tags": []},
            headers={"x-ratelimit-remaining-tokens": "10", "x-ratelimit-reset-tokens": "7.5s"},
        )
    )
    sleeps = Sleeps()
    llm = _client(sleeps=sleeps, store=InMemoryLLMStore())
    await llm.structured("fast", Batch, MSG, stage="t")
    await llm.structured("fast", Batch, [{"role": "user", "content": "other"}], stage="t")
    assert any(6.0 < s <= 7.5 for s in sleeps.calls)


def test_parse_duration() -> None:
    assert _parse_duration_s("7.66s") == pytest.approx(7.66)
    assert _parse_duration_s("1m26.4s") == pytest.approx(86.4)
    assert _parse_duration_s("120ms") == pytest.approx(0.12)
    assert _parse_duration_s(None) is None


@respx.mock
async def test_server_side_schema_rejection_is_retried_with_feedback() -> None:
    rejected = httpx.Response(
        400,
        json={
            "error": {
                "message": "Generated JSON does not match the expected schema.",
                "type": "invalid_request_error",
                "code": "json_validate_failed",
                "failed_generation": '{"tags": [{"id": 0, "category": "zzz"}]}',
            }
        },
    )
    route = respx.post(URL).mock(
        side_effect=[rejected, _ok({"tags": [{"id": 0, "category": "a"}]})]
    )
    store = InMemoryLLMStore()
    out = await _client(store, Sleeps()).structured("fast", Batch, MSG, stage="t")
    assert out.tags[0].category == "a"
    feedback = json.loads(route.calls[1].request.content)["messages"]
    assert feedback[-2] == {
        "role": "assistant",
        "content": '{"tags": [{"id": 0, "category": "zzz"}]}',
    }
    assert [c.error for c in store.calls] == ["schema_rejected", None]


@respx.mock
async def test_other_400s_are_not_retried() -> None:
    route = respx.post(URL).mock(
        return_value=httpx.Response(
            400, json={"error": {"message": "bad model", "code": "model_not_found"}}
        )
    )
    with pytest.raises(LLMError, match="400"):
        await _client(InMemoryLLMStore(), Sleeps()).structured("fast", Batch, MSG, stage="t")
    assert route.call_count == 1


def _ok_cached(content: Any, prompt: int, cached: int, completion: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": json.dumps(content)}}],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "prompt_tokens_details": {"cached_tokens": cached},
            },
        },
    )


@respx.mock
async def test_groq_cached_tokens_are_recorded_and_not_counted() -> None:
    respx.post(URL).mock(
        return_value=_ok_cached({"tags": []}, prompt=1500, cached=1024, completion=100)
    )
    store = InMemoryLLMStore()
    llm = GroqClient(_settings(), store, http=httpx.AsyncClient(), sleep=Sleeps(), run_id="r1")
    await llm.structured("fast", Batch, MSG, stage="t")
    call = store.calls[0]
    assert (call.cached_tokens, call.counted_tokens, call.run_id) == (1024, 576, "r1")
    assert await store.tokens_last_24h("fast-model") == 576


@respx.mock
async def test_response_cache_hit_records_what_it_saved() -> None:
    respx.post(URL).mock(
        return_value=_ok_cached({"tags": []}, prompt=1500, cached=1000, completion=100)
    )
    store = InMemoryLLMStore()
    llm = _client(store, Sleeps())
    await llm.structured("fast", Batch, MSG, stage="t")
    await llm.structured("fast", Batch, MSG, stage="t")
    hit = store.calls[1]
    assert hit.cache_hit and hit.saved_tokens == 600 and hit.counted_tokens == 0


async def test_daily_budget_setting_is_the_stop_line() -> None:
    from core.llm import CallRecord

    store = InMemoryLLMStore()
    await store.record(CallRecord("fast-model", "t", None, 1000, 0, None, 1, False, True))
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(URL)
        with pytest.raises(DailyLimitError, match="budget is 1,200"):
            await _client(store, Sleeps(), groq_daily_token_budget=1_200).structured(
                "fast", Batch, MSG, stage="t"
            )
        assert route.call_count == 0


@respx.mock
async def test_json_object_mode_puts_schema_in_static_system_prompt() -> None:
    route = respx.post(URL).mock(return_value=_ok({"tags": [{"id": 0, "category": "a"}]}))
    msgs = [{"role": "system", "content": "static rules"}, {"role": "user", "content": "variable"}]
    await _client(InMemoryLLMStore(), Sleeps()).structured(
        "fast", Batch, msgs, stage="t", mode="json_object"
    )
    body = json.loads(route.calls[0].request.content)
    assert body["response_format"] == {"type": "json_object"}
    system, user = body["messages"]
    assert (
        system["content"].startswith("static rules")
        and '"additionalProperties":false' in system["content"]
    )
    assert user == {"role": "user", "content": "variable"}  # variable content stays last


@respx.mock
async def test_agent_client_uses_agent_models() -> None:
    route = respx.post(URL).mock(return_value=_ok({"tags": []}))
    llm = GroqClient(
        _settings(agent_model_fast="agent-model"),
        InMemoryLLMStore(),
        http=httpx.AsyncClient(),
        sleep=Sleeps(),
        agent=True,
    )
    await llm.structured("fast", Batch, MSG, stage="t")
    assert json.loads(route.calls[0].request.content)["model"] == "agent-model"
