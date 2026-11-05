"""Record SSE event sequences from the scripted runtime for the frontend's reducer tests.

    uv run python scripts/gen_frontend_fixtures.py

Runs the real API app with AGENT_RUNTIME=scripted (zero tokens) and writes
frontend/lib/__fixtures__/<scenario>.json: [[id, event], ...] per stream. Re-run when the
event contract changes (ids, run ids and durations differ between runs; tests don't
depend on them).
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from api import scripted
from api.main import create_app
from core.config import Settings

OUT = Path(__file__).resolve().parents[1] / "frontend" / "lib" / "__fixtures__"


def parse(text: str) -> list[list[Any]]:
    out = []
    for block in text.split("\n\n"):
        f = dict(x.split(": ", 1) for x in block.splitlines() if x and not x.startswith(":"))
        if f:
            out.append([int(f["id"]), json.loads(f["data"])])
    return out


async def main() -> None:
    scripted.TOOL_DELAY_S = scripted.TOKEN_DELAY_S = 0.0
    settings = Settings(
        _env_file=None,
        app_env="test",
        agent_runtime="scripted",
        groq_api_key=SecretStr("unused"),
        api_runs_per_minute=1000,
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    OUT.mkdir(parents=True, exist_ok=True)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://x", timeout=60) as c,
    ):

        async def run(q: str) -> tuple[str, list[list[Any]]]:
            t = (await c.post("/api/threads", json={})).json()["id"]
            return t, parse((await c.post(f"/api/threads/{t}/runs", json={"query": q})).text)

        _, compare = await run(
            "Compare the top risk factors NVIDIA and AMD disclosed in their latest 10-Ks. "
            "Does recent news support any of them?"
        )
        _, apple = await run("How did Apple's risk factors change between its last two 10-Ks?")
        t, bank = await run("How risky is the bank?")
        resume = await c.post(f"/api/threads/{t}/resume", json={"answer": "JPMorgan Chase (JPM)"})
        for name, events in {
            "compare": compare,
            "apple-replan": apple,
            "bank-paused": bank,
            "bank-resumed": parse(resume.text),
        }.items():
            (OUT / f"{name}.json").write_text(json.dumps(events, indent=1) + "\n", encoding="utf-8")
            print(f"{name}: {len(events)} events")


asyncio.run(main())
