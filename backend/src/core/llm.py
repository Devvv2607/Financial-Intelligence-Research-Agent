"""Groq structured-output client with caching, pacing and token accounting.

One entry point, `GroqClient.structured(...)`, returns a validated Pydantic object.
It is used by ingestion now and by the agent's own nodes later (the agent's tool
loops use langchain-groq; see DECISIONS D-019).

Why talk to Groq's OpenAI-compatible HTTP API directly here (not langchain-groq):
the free tier forces us to pace on Groq's `x-ratelimit-*` headers, distinguish a
per-minute 429 (wait) from a per-day 429 (stop, resume tomorrow), and record exact
`usage` per call — including `cached_tokens`, which Groq does not count toward rate
limits. Those details are awkward to reach through the LangChain wrapper.
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal, NamedTuple, Protocol

import httpx
from pydantic import BaseModel, ValidationError

from core.config import ModelSlot, Settings

logger = logging.getLogger(__name__)

# Total attempts per structured call when the output breaks the schema (first try +
# one retry). Kept low on purpose: each retry spends daily quota.
MAX_VALIDATION_ATTEMPTS = 2
MAX_RATE_LIMIT_RETRIES = 8

# json_schema: Groq enforces the schema server-side (rejects with HTTP 400 on violation).
# json_object: Groq only guarantees valid JSON; the schema goes in the system prompt and is
# validated locally, so there are no server-side rejections. Prompt-cache hits were
# hit-and-miss in both modes when probed (DECISIONS D-024), so the mode is selectable and
# chosen from measurements.
ResponseMode = Literal["json_schema", "json_object"]


class DailyLimitError(RuntimeError):
    """The model's per-day budget (ours or Groq's) is exhausted; resume after the reset."""


class SchemaRejectedError(RuntimeError):
    """Groq's server-side strict-mode check rejected the generation (HTTP 400,
    `json_validate_failed`). Retryable like a local validation error."""

    def __init__(self, message: str, failed_generation: str) -> None:
        super().__init__(message)
        self.failed_generation = failed_generation


class LLMError(RuntimeError):
    """Non-retryable failure (bad request, auth, validation still failing after retries)."""


class InvalidOutputError(LLMError):
    """Output still broke the schema after MAX_VALIDATION_ATTEMPTS. Carries the last
    raw output so callers can salvage the valid parts instead of retrying again."""

    def __init__(self, message: str, last_output: str) -> None:
        super().__init__(message)
        self.last_output = last_output


@dataclass(frozen=True)
class CallRecord:
    model: str
    stage: str
    cik: int | None
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int | None
    latency_ms: int
    cache_hit: bool  # our response cache (no request sent), not Groq's prompt cache
    ok: bool
    error: str | None = None
    cached_tokens: int = 0  # Groq prompt-cache hits inside prompt_tokens
    saved_tokens: int = 0  # on a response-cache hit: counted tokens the original call cost
    run_id: str | None = None

    @property
    def counted_tokens(self) -> int:
        """Tokens that count toward Groq rate limits (cached prompt tokens don't)."""
        return self.prompt_tokens - self.cached_tokens + self.completion_tokens


class CachedResponse(NamedTuple):
    response: dict[str, Any]
    counted_tokens: int


class LLMStore(Protocol):
    """Persistence for the response cache and the call ledger (Postgres in prod)."""

    async def cache_get(self, key: str) -> CachedResponse | None: ...
    async def cache_put(
        self, key: str, model: str, response: dict[str, Any], counted_tokens: int
    ) -> None: ...
    async def record(self, call: CallRecord) -> None: ...
    async def tokens_last_24h(self, model: str) -> int: ...


@dataclass
class InMemoryLLMStore:
    """Test/dev store. Same contract as the Postgres store."""

    cache: dict[str, CachedResponse] = field(default_factory=dict)
    calls: list[CallRecord] = field(default_factory=list)

    async def cache_get(self, key: str) -> CachedResponse | None:
        return self.cache.get(key)

    async def cache_put(
        self, key: str, model: str, response: dict[str, Any], counted_tokens: int
    ) -> None:
        self.cache[key] = CachedResponse(response, counted_tokens)

    async def record(self, call: CallRecord) -> None:
        self.calls.append(call)

    async def tokens_last_24h(self, model: str) -> int:
        return sum(c.counted_tokens for c in self.calls if c.model == model)


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic JSON schema rewritten for Groq/OpenAI strict mode.

    Strict mode requires every object to list all properties as required and set
    `additionalProperties: false`, and we inline `$defs` so no `$ref` resolution is needed.
    """
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def fix(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return fix(defs[node["$ref"].split("/")[-1]])
            out = {k: fix(v) for k, v in node.items() if k not in ("title", "default")}
            if out.get("type") == "object" and "properties" in out:
                out["required"] = list(out["properties"])
                out["additionalProperties"] = False
            return out
        if isinstance(node, list):
            return [fix(v) for v in node]
        return node

    result: dict[str, Any] = fix(schema)
    return result


def _parse_duration_s(value: str | None) -> float | None:
    """Groq reset headers look like '7.66s', '1m26.4s', '120ms'."""
    if not value:
        return None
    total, matched = 0.0, False
    for num, unit in re.findall(r"([\d.]+)(ms|h|m|s)", value):
        matched = True
        total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total if matched else None


def _error_body(resp: httpx.Response) -> dict[str, Any]:
    try:
        err = resp.json().get("error", {})
    except ValueError:
        return {}
    return err if isinstance(err, dict) else {}


def _with_schema_instructions(
    messages: list[dict[str, str]], json_schema: dict[str, Any]
) -> list[dict[str, str]]:
    """json_object mode: append the schema to the (static) system message, so it stays part
    of the cacheable prefix and the variable user content stays last."""
    note = (
        "\n\nRespond with a single JSON object that conforms to this JSON Schema:\n"
        + json.dumps(json_schema, separators=(",", ":"))
    )
    if messages and messages[0]["role"] == "system":
        return [{"role": "system", "content": messages[0]["content"] + note}, *messages[1:]]
    return [{"role": "system", "content": note.strip()}, *messages]


def estimate_tokens(text: str) -> int:
    # ~4 chars/token for English prose; only used to decide whether to wait.
    return len(text) // 4 + 1


class GroqClient:
    def __init__(
        self,
        settings: Settings,
        store: LLMStore,
        http: httpx.AsyncClient | None = None,
        sleep: Any = asyncio.sleep,
        *,
        agent: bool = False,
        run_id: str | None = None,
    ) -> None:
        if settings.groq_api_key is None:
            raise LLMError("GROQ_API_KEY is not set")
        self._settings = settings
        self._store = store
        self._http = http or httpx.AsyncClient(timeout=settings.groq_timeout_s)
        self._sleep = sleep
        # Agent/dev clients resolve to their own models so they never spend the
        # ingestion model's daily quota (owner's plan for the full run).
        self._agent = agent
        self.run_id = run_id
        # One in-flight request per model: with an 8K TPM budget, parallel calls would
        # only race each other into 429s.
        self._locks: dict[str, asyncio.Lock] = {}
        # Pacing state from the last response's headers, per model.
        self._remaining_tokens: dict[str, int] = {}
        self._tokens_reset_at: dict[str, float] = {}
        self._last_request_at: dict[str, float] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    def model_for(self, slot: ModelSlot) -> str:
        return self._settings.model_for(slot, agent=self._agent)

    def _record(self, model: str, stage: str, cik: int | None, **kw: Any) -> CallRecord:
        base: dict[str, Any] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": None,
            "latency_ms": 0,
            "cache_hit": False,
            "ok": True,
        }
        return CallRecord(model=model, stage=stage, cik=cik, run_id=self.run_id, **(base | kw))

    async def structured[T: BaseModel](
        self,
        slot: ModelSlot,
        schema: type[T],
        messages: list[dict[str, str]],
        *,
        stage: str,
        cik: int | None = None,
        reasoning_effort: str = "low",
        max_completion_tokens: int = 4096,
        mode: ResponseMode = "json_schema",
    ) -> T:
        model = self.model_for(slot)
        json_schema = strict_json_schema(schema)
        if mode == "json_object":
            messages = _with_schema_instructions(messages, json_schema)
        key = hashlib.sha256(
            json.dumps(
                [model, schema.__name__, json_schema, messages, reasoning_effort, mode],
                sort_keys=True,
            ).encode()
        ).hexdigest()

        cached = await self._store.cache_get(key)
        if cached is not None:
            await self._store.record(
                self._record(model, stage, cik, cache_hit=True, saved_tokens=cached.counted_tokens)
            )
            return schema.model_validate(cached.response)

        # Static content (system prompt, schema) first and the variable content last, so
        # consecutive calls share an exact prefix that Groq's prompt cache can reuse.
        response_format: dict[str, Any] = (
            {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "strict": True, "schema": json_schema},
            }
            if mode == "json_schema"
            else {"type": "json_object"}
        )
        body: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "response_format": response_format,
            "reasoning_effort": reasoning_effort,
            "max_completion_tokens": max_completion_tokens,
        }
        lock = self._locks.setdefault(model, asyncio.Lock())
        async with lock:
            for attempt in range(MAX_VALIDATION_ATTEMPTS):
                try:
                    content, counted = await self._complete(model, body, stage=stage, cik=cik)
                    parsed = schema.model_validate_json(content)
                except (ValidationError, SchemaRejectedError) as exc:
                    if isinstance(exc, SchemaRejectedError):
                        content = exc.failed_generation
                    if attempt == MAX_VALIDATION_ATTEMPTS - 1:
                        raise InvalidOutputError(
                            f"output failed validation: {exc}", content
                        ) from exc
                    # Show the model its own output and the error, then retry.
                    body["messages"] = [
                        *messages,
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": f"That output was invalid: {exc}. Return corrected JSON.",
                        },
                    ]
                    continue
                await self._store.cache_put(key, model, parsed.model_dump(mode="json"), counted)
                return parsed
        raise AssertionError("unreachable")

    async def _complete(
        self, model: str, body: dict[str, Any], *, stage: str, cik: int | None
    ) -> tuple[str, int]:
        prompt_text = "".join(m["content"] for m in body["messages"])
        estimate = estimate_tokens(prompt_text) + body["max_completion_tokens"] // 4
        await self._check_daily_budget(model, estimate)

        for _ in range(MAX_RATE_LIMIT_RETRIES):
            await self._pace(model, estimate)
            started = time.monotonic()
            self._last_request_at[model] = started
            resp = await self._http.post(
                f"{self._settings.groq_base_url}/chat/completions",
                json=body,
                headers={
                    "Authorization": f"Bearer {self._settings.groq_api_key.get_secret_value()}"  # type: ignore[union-attr]
                },
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            self._update_limits(model, resp.headers)

            if resp.status_code == 429:
                message = resp.text
                if "per day" in message.lower() or "(tpd)" in message.lower():
                    await self._store.record(
                        self._record(
                            model, stage, cik, latency_ms=latency_ms, ok=False, error="TPD"
                        )
                    )
                    raise DailyLimitError(f"{model}: Groq daily limit reached")
                wait = float(resp.headers.get("retry-after", "0") or 0) or 5.0
                logger.info("groq 429, waiting", extra={"model": model, "wait_s": wait})
                await self._sleep(wait)
                continue
            if resp.status_code >= 500:
                await self._sleep(5.0)
                continue
            error = _error_body(resp)
            if resp.status_code == 400 and error.get("code") == "json_validate_failed":
                # No `usage` comes back on a 400, so these tokens can't be measured; the
                # ledger flags them so reports can count them instead of hiding them.
                await self._store.record(
                    self._record(
                        model, stage, cik, latency_ms=latency_ms, ok=False, error="schema_rejected"
                    )
                )
                raise SchemaRejectedError(
                    str(error.get("message", ""))[:500], str(error.get("failed_generation", ""))
                )
            if resp.status_code != 200:
                await self._store.record(
                    self._record(
                        model,
                        stage,
                        cik,
                        latency_ms=latency_ms,
                        ok=False,
                        error=f"HTTP {resp.status_code}",
                    )
                )
                raise LLMError(f"Groq HTTP {resp.status_code}: {resp.text[:500]}")

            data = resp.json()
            usage = data.get("usage", {})
            prompt_details = usage.get("prompt_tokens_details") or {}
            completion_details = usage.get("completion_tokens_details") or {}
            record = self._record(
                model,
                stage,
                cik,
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                reasoning_tokens=completion_details.get("reasoning_tokens"),
                cached_tokens=int(prompt_details.get("cached_tokens") or 0),
                latency_ms=latency_ms,
            )
            await self._store.record(record)
            content: str = data["choices"][0]["message"]["content"] or ""
            return content, record.counted_tokens
        raise LLMError(f"{model}: still rate limited after {MAX_RATE_LIMIT_RETRIES} retries")

    async def _check_daily_budget(self, model: str, estimate: int) -> None:
        """Stop before our own daily budget (set below Groq's limit to leave headroom)."""
        budget = self._settings.groq_daily_token_budget
        used = await self._store.tokens_last_24h(model)
        if used + estimate > budget:
            raise DailyLimitError(
                f"{model}: {used:,} counted tokens used in the last 24h; budget is {budget:,}"
            )

    async def _pace(self, model: str, estimate: int) -> None:
        now = time.monotonic()
        # Requests per minute: keep a minimum gap between requests.
        gap = 60.0 / self._settings.groq_requests_per_minute
        last = self._last_request_at.get(model)
        if last is not None and now - last < gap:
            await self._sleep(gap - (now - last))
        # Tokens per minute: if the last response said we don't have room, wait for reset.
        remaining = self._remaining_tokens.get(model)
        reset_at = self._tokens_reset_at.get(model, 0.0)
        if remaining is not None and remaining < estimate and reset_at > time.monotonic():
            await self._sleep(reset_at - time.monotonic())

    def _update_limits(self, model: str, headers: httpx.Headers) -> None:
        remaining = headers.get("x-ratelimit-remaining-tokens")
        if remaining is not None and remaining.isdigit():
            self._remaining_tokens[model] = int(remaining)
        reset = _parse_duration_s(headers.get("x-ratelimit-reset-tokens"))
        if reset is not None:
            self._tokens_reset_at[model] = time.monotonic() + reset
