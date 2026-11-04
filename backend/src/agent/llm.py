"""The agent's LLM layer: one interface for structured calls and tool-calling chat.

`AgentLLM` is what nodes depend on; tests inject a scripted fake, so the suite never
calls Groq. The live implementation (`GroqAgentLLM`) gives every call the same guards:
- the agent model only (AGENT_MODEL_*, gpt-oss-120b during the full run, D-027),
- reasoning effort per node (NODE_EFFORT),
- a budget check before each call (AGENT_DAILY_TOKEN_BUDGET, default 150K/day),
- a dev response cache keyed by model + effort + prompt (+ tools), so re-running a
  question while developing costs nothing,
- a ledger row per call with the node name and run id (tokens per node per run).

Structured nodes use our GroqClient (strict JSON schema, pacing, D-019). Tool loops and
the synthesizer use langchain-groq's ChatGroq, which LangGraph's `messages` stream mode
needs for token streaming; the wrapper adds the cache/budget/ledger around it.
"""

import hashlib
import json
import time
from typing import Any, Literal, Protocol

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.runnables import RunnableConfig
from langchain_groq import ChatGroq
from pydantic import BaseModel

from core.config import ModelSlot, Settings
from core.llm import CallRecord, DailyLimitError, GroqClient, LLMStore, estimate_tokens

Effort = Literal["low", "medium", "high"]

# Owner's setting: low for extraction/classification and tool loops, medium where the
# reasoning quality of the answer is decided. Measured on the first live runs (D-033).
NODE_EFFORT: dict[str, Effort] = {
    "query_analyzer": "low",
    "entity_resolver": "low",
    "specialist": "low",
    "planner": "medium",
    "critic": "medium",
    "synthesizer": "medium",
}
NODE_SLOT: dict[str, ModelSlot] = {
    "planner": "reasoning",
    "critic": "reasoning",
    "synthesizer": "reasoning",
}
CHAT_OUTPUT_ESTIMATE = 1_500  # tokens reserved for a chat reply when checking the budget


class AgentLLM(Protocol):
    async def structured[T: BaseModel](
        self, node: str, schema: type[T], messages: list[dict[str, str]], *, run_id: str | None
    ) -> T: ...

    async def chat(
        self,
        node: str,
        messages: list[BaseMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        run_id: str | None = None,
        stream: bool = False,
        config: RunnableConfig | None = None,
    ) -> AIMessage: ...


def _effort(node: str) -> Effort:
    return NODE_EFFORT.get(node, NODE_EFFORT["specialist"] if node.endswith("_agent") else "low")


def _serialize(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    return [
        {
            "type": m.type,
            "content": m.content,
            "tool_calls": getattr(m, "tool_calls", None) or None,
            "tool_call_id": getattr(m, "tool_call_id", None),
        }
        for m in messages
    ]


class GroqAgentLLM:
    def __init__(self, settings: Settings, store: LLMStore) -> None:
        self._s = settings
        self._store = store
        self._structured = GroqClient(settings, store, agent=True)
        self._chat_models: dict[tuple[str, str], ChatGroq] = {}

    async def aclose(self) -> None:
        await self._structured.aclose()

    async def structured[T: BaseModel](
        self, node: str, schema: type[T], messages: list[dict[str, str]], *, run_id: str | None
    ) -> T:
        return await self._structured.structured(
            NODE_SLOT.get(node, "fast"),
            schema,
            messages,
            stage=node,
            reasoning_effort=_effort(node),
            run_id=run_id,
        )

    def _model(self, slot: ModelSlot, effort: Effort) -> ChatGroq:
        name = self._s.model_for(slot, agent=True)
        if (name, effort) not in self._chat_models:
            self._chat_models[(name, effort)] = ChatGroq(
                model=name,
                api_key=self._s.groq_api_key,  # type: ignore[arg-type]
                reasoning_effort=effort,
                max_retries=6,  # 429s from the 8K TPM limit are retried with backoff
                timeout=self._s.groq_timeout_s,
            )
        return self._chat_models[(name, effort)]

    async def chat(
        self,
        node: str,
        messages: list[BaseMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        run_id: str | None = None,
        stream: bool = False,
        config: RunnableConfig | None = None,
    ) -> AIMessage:
        slot = NODE_SLOT.get(node, "fast")
        effort = _effort(node)
        model_name = self._s.model_for(slot, agent=True)
        key = hashlib.sha256(
            json.dumps([model_name, effort, _serialize(messages), tools], sort_keys=True,
                       default=str).encode()
        ).hexdigest()

        if self._s.agent_dev_cache:
            cached = await self._store.cache_get(key)
            if cached is not None:
                await self._store.record(CallRecord(
                    model_name, node, None, 0, 0, None, 0, True, True,
                    saved_tokens=cached.counted_tokens, run_id=run_id,
                ))
                return AIMessage(
                    content=cached.response["content"],
                    tool_calls=cached.response.get("tool_calls") or [],
                )

        estimate = sum(estimate_tokens(str(m.content)) for m in messages) + CHAT_OUTPUT_ESTIMATE
        used = await self._store.tokens_last_24h(model_name)
        if used + estimate > self._s.agent_daily_token_budget:
            raise DailyLimitError(
                f"{model_name}: {used:,} counted tokens in the last 24h; agent budget is "
                f"{self._s.agent_daily_token_budget:,}"
            )
        model = self._model(slot, effort)
        runnable = model.bind_tools(tools) if tools else model
        started = time.monotonic()
        if stream:
            msg: AIMessageChunk | None = None
            async for chunk in runnable.astream(messages, config=config):
                msg = chunk if msg is None else msg + chunk  # type: ignore[assignment]
            result = AIMessage(
                content=msg.content if msg else "",
                tool_calls=msg.tool_calls if msg else [],
                usage_metadata=msg.usage_metadata if msg else None,
                response_metadata=msg.response_metadata if msg else {},
            )
        else:
            result = await runnable.ainvoke(messages, config=config)  # type: ignore[assignment]
        latency_ms = int((time.monotonic() - started) * 1000)

        usage: dict[str, Any] = dict(result.usage_metadata or {})
        token_usage = (result.response_metadata or {}).get("token_usage") or {}
        cached_tokens = int(
            (usage.get("input_token_details") or {}).get("cache_read")
            or (token_usage.get("prompt_tokens_details") or {}).get("cached_tokens")
            or 0
        )
        record = CallRecord(
            model_name, node, None,
            int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)),
            (usage.get("output_token_details") or {}).get("reasoning"),
            latency_ms, False, True, cached_tokens=cached_tokens, run_id=run_id,
        )
        await self._store.record(record)
        if self._s.agent_dev_cache:
            await self._store.cache_put(
                key, model_name,
                {"content": result.content, "tool_calls": result.tool_calls},
                record.counted_tokens,
            )
        return AIMessage(content=result.content, tool_calls=result.tool_calls)
