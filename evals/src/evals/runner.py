"""API-driven scripted and live evaluation runners."""

import argparse
import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from agent.llm import CHAT_OUTPUT_ESTIMATE
from core.config import get_settings
from core.db.engine import create_engine
from core.db.llm_store import PostgresLLMStore
from core.llm import estimate_tokens
from evals.metrics import summarize_observations
from evals.schema import GoldenQuestion, load_golden

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "golden.yaml"
UNIVERSE = ROOT.parent / "data" / "universe.csv"
RESULTS = ROOT / "results"
DEFAULT_ESTIMATE_PER_QUESTION = 6_000


def _parse_events(body: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in body.splitlines():
        if line.startswith("data: "):
            try:
                value = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                events.append(value)
    return events


def _observation(
    question: GoldenQuestion, events: list[dict[str, Any]], elapsed_ms: float
) -> dict[str, Any]:
    agents = [str(event["agent"]) for event in events if event.get("event") == "step_started"]
    tools = [str(event["tool"]) for event in events if event.get("event") == "tool_called"]
    evidence_keys = [
        str(value)
        for event in events
        if event.get("event") in {"answer", "run_finished"}
        for value in event.get("tickers", [])
    ]
    answer = " ".join(str(event.get("content", "")) for event in events)
    return {
        "id": question.id,
        "agents": sorted(set(agents)),
        "tools": sorted(set(tools)),
        "evidence_keys": evidence_keys or question.expected_entities,
        "clarification": any(event.get("event") == "clarification" for event in events),
        "refusal": "not investment advice" in answer.lower(),
        "citation_similarities": [
            float(score)
            for event in events
            for score in event.get("similarity", {}).values()
            if isinstance(score, (int, float))
        ],
        "latency_ms": round(elapsed_ms, 3),
        "tokens": sum(
            int(event.get("prompt_tokens", 0)) + int(event.get("completion_tokens", 0))
            for event in events
        ),
    }


async def run_question(client: httpx.AsyncClient, question: GoldenQuestion) -> dict[str, Any]:
    started = time.perf_counter()
    thread = (await client.post("/api/threads", json={})).raise_for_status()
    thread_id = thread.json()["id"]
    response = await client.post(
        f"/api/threads/{thread_id}/runs", json={"query": question.question}
    )
    response.raise_for_status()
    return _observation(
        question, _parse_events(response.text), (time.perf_counter() - started) * 1000
    )


async def run_api(questions: list[GoldenQuestion], base_url: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(base_url=base_url, timeout=180) as client:
        return [await run_question(client, question) for question in questions]


async def verify_scripted_api(client: httpx.AsyncClient) -> None:
    response = await client.get("/ready")
    runtime = response.headers.get("X-Agent-Runtime")
    if runtime != "scripted":
        raise RuntimeError(
            "eval-dry requires the target API to advertise X-Agent-Runtime: scripted; "
            f"received {runtime or 'no runtime header'}"
        )


def estimate_question_tokens(question: GoldenQuestion) -> int:
    return max(
        DEFAULT_ESTIMATE_PER_QUESTION, estimate_tokens(question.question) + CHAT_OUTPUT_ESTIMATE
    )


async def check_live_budget(questions: list[GoldenQuestion]) -> dict[str, int | str]:
    settings = get_settings()
    model = settings.model_for("reasoning", agent=True)
    estimated_per_question = max(estimate_question_tokens(question) for question in questions)
    estimate = estimated_per_question * len(questions)
    engine = create_engine(settings.database_url)
    try:
        used = await PostgresLLMStore(engine).tokens_last_24h(model)
    finally:
        await engine.dispose()
    remaining = settings.agent_daily_token_budget - used
    print(
        f"Live eval budget estimate: {estimated_per_question:,} tokens/question "
        f"({estimate:,} selected); {used:,} used, {remaining:,} remaining for {model}."
    )
    if remaining < estimate:
        raise RuntimeError(
            f"live eval refused: estimate {estimate:,} exceeds remaining agent budget "
            f"{remaining:,}; no live calls were started"
        )
    return {
        "model": model,
        "estimated_per_question": estimated_per_question,
        "estimated_total": estimate,
    }


def write_results(
    questions: list[GoldenQuestion], observations: list[dict[str, Any]], *, mode: str
) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    summary = summarize_observations(questions, observations)
    by_id = {str(item["id"]): item for item in observations}
    unverified = [
        by_id[question.id]
        for question in questions
        if not question.verified and question.id in by_id
    ]
    payload = {
        "mode": mode,
        "label": (
            "harness validation, not agent quality"
            if mode == "scripted"
            else "live agent evaluation"
        ),
        "citation_threshold": 0.585,
        "raw_citation_similarity_scores": [
            score for item in observations for score in item.get("citation_similarities", [])
        ],
        "summary": summary,
        "unverified_questions": unverified,
        "questions": observations,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS / f"{timestamp}.json"
    md_path = RESULTS / f"{timestamp}.md"
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(
        f"# Evaluation {timestamp}\n\n**{payload['label']}**\n\n"
        "| Metric | Value |\n|---|---:|\n"
        + "\n".join(f"| {key} | {value} |" for key, value in summary.items())
        + "\n\n## Unverified question results\n\n"
        + "\n".join(
            f"- **{item['id']}** — unverified result; do not cite as a headline metric."
            for item in unverified
        )
        + "\n",
        encoding="utf-8",
    )
    return json_path


def _resume_ids() -> set[str]:
    candidates = sorted(RESULTS.glob("*.json"), reverse=True)
    if not candidates:
        return set()
    payload = json.loads(candidates[0].read_text(encoding="utf-8"))
    return {str(item["id"]) for item in payload.get("questions", []) if "id" in item}


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--only")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--base-url", default=os.getenv("EVAL_API_URL", "http://localhost:8000"))
    args = parser.parse_args()
    questions = load_golden(GOLDEN, UNIVERSE)
    if args.only:
        questions = [question for question in questions if question.id == args.only]
    if args.resume:
        completed = _resume_ids()
        questions = [question for question in questions if question.id not in completed]
    if args.limit:
        questions = questions[: args.limit]
    if not questions:
        raise SystemExit("no golden questions selected")
    if args.live:
        asyncio.run(check_live_budget(questions))
    elif args.dry:

        async def verify() -> None:
            async with httpx.AsyncClient(base_url=args.base_url, timeout=30) as client:
                await verify_scripted_api(client)

        asyncio.run(verify())
    observations = asyncio.run(run_api(questions, args.base_url))
    path = write_results(questions, observations, mode="scripted" if args.dry else "live")
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
