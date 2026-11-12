"""Scripted stand-ins for the LLM and the tool executor: no network, no tokens.

Used by the test suite and by the API's scripted runtime (zero-token browser E2E,
api/scripted.py). Scripts answer per graph node, from a queue or from a function of the
messages. Streaming goes through LangChain's GenericFakeChatModel, so LangGraph's
"messages" stream mode sees real token chunks, exactly as with ChatGroq.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import BaseModel

from agent.tools.base import Evidence, ToolError, ToolResult

Script = list[Any] | Callable[[Sequence[Any]], Any]
ToolHandler = Callable[[dict[str, Any]], Awaitable[ToolResult]]


class ScriptedLLM:
    """Implements `agent.llm.AgentLLM`. Records every call as (node, messages)."""

    def __init__(
        self,
        structured: dict[str, Script] | None = None,
        chat: dict[str, Script] | None = None,
        *,
        token_delay: float = 0.0,  # seconds between streamed chunks (E2E realism)
    ) -> None:
        self._structured = structured or {}
        self._chat = chat or {}
        self._token_delay = token_delay
        self.calls: list[tuple[str, Any]] = []

    @staticmethod
    def _next(script: Script, messages: Sequence[Any]) -> Any:
        if callable(script):
            return script(messages)
        if not script:
            raise AssertionError("script exhausted")
        return script.pop(0) if len(script) > 1 else script[0]

    async def structured(
        self,
        node: str,
        schema: type[BaseModel],
        messages: list[dict[str, str]],
        *,
        run_id: str | None,
    ) -> Any:
        self.calls.append((node, messages))
        if node not in self._structured:
            raise AssertionError(f"unexpected structured call for {node}")
        value = self._next(self._structured[node], messages)
        return value if isinstance(value, BaseModel) else schema.model_validate(value)

    async def chat(
        self,
        node: str,
        messages: Sequence[BaseMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        run_id: str | None = None,
        stream: bool = False,
        config: Any = None,
    ) -> AIMessage:
        self.calls.append((node, list(messages)))
        if node not in self._chat:
            raise AssertionError(f"unexpected chat call for {node}")
        value = self._next(self._chat[node], messages)
        reply = value if isinstance(value, AIMessage) else AIMessage(content=str(value))
        if stream and config is not None and not reply.tool_calls:
            model = GenericFakeChatModel(messages=iter([reply]))
            parts = []
            async for chunk in model.astream(list(messages), config=config):
                parts.append(str(chunk.content))
                if self._token_delay:
                    await asyncio.sleep(self._token_delay)
            return AIMessage(content="".join(parts))
        return reply

    def count(self, node: str) -> int:
        return sum(1 for n, _ in self.calls if n == node)


class ScriptedTools:
    """Tool executor with per-tool handlers; records (name, args, start, end)."""

    def __init__(self, handlers: dict[str, ToolHandler], delay: float = 0.0) -> None:
        self.handlers = handlers
        self.delay = delay
        self.log: list[tuple[str, dict[str, Any], float, float]] = []

    async def __call__(self, name: str, args: dict[str, Any]) -> ToolResult:
        start = time.monotonic()
        await asyncio.sleep(self.delay)
        handler = self.handlers.get(name)
        result = (
            await handler(args)
            if handler
            else ToolResult(
                tool=name, ok=False, error=ToolError(code="not_found", message="no handler")
            )
        )
        self.log.append((name, args, start, time.monotonic()))
        return result


def ok(tool: str, data: dict[str, Any], evidence: list[Evidence]) -> ToolHandler:
    async def handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult(tool=tool, ok=True, data=data, evidence=evidence)

    return handler


def failing(tool: str) -> ToolHandler:
    async def handler(args: dict[str, Any]) -> ToolResult:
        return ToolResult(tool=tool, ok=False, error=ToolError(code="upstream", message="boom"))

    return handler
