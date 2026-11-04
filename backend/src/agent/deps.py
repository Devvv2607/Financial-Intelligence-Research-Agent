"""What the graph needs from the outside world, injected so tests can fake all of it.

Nodes never import clients directly: the LLM, tool execution, the company list and the
clock come from `AgentDeps`. The test suite builds the same graph with scripted fakes
(zero Groq calls); the CLI/API build it with live implementations.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from agent.llm import AgentLLM
from agent.tools.base import ToolResult
from agent.tools.common import Company

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[ToolResult]]
RunRecorder = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class AgentDeps:
    llm: AgentLLM
    call_tool: ToolExecutor
    list_companies: Callable[[], Awaitable[list[Company]]]
    today: Callable[[], date] = date.today
    record_run: RunRecorder | None = None
    _companies: list[Company] | None = field(default=None, repr=False)

    async def companies(self) -> list[Company]:
        """The ingested companies (whatever is in the database), cached per process."""
        if self._companies is None:
            self._companies = await self.list_companies()
        return self._companies
