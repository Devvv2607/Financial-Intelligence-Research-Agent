import asyncio
import json
from pathlib import Path

import httpx
import pytest
from pytest import MonkeyPatch

from evals.runner import _parse_events, verify_scripted_api, write_results
from evals.schema import load_golden


def test_scripted_dry_run_writes_labeled_result(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    questions = load_golden(
        Path(__file__).parents[1] / "golden.yaml",
        Path(__file__).parents[2] / "data" / "universe.csv",
    )[:1]
    monkeypatch.setattr("evals.runner.RESULTS", tmp_path)
    observations = [
        {
            "id": questions[0].id,
            "agents": questions[0].expected_agents,
            "tools": questions[0].expected_tools,
            "evidence_keys": questions[0].expected_entities,
            "clarification": False,
            "refusal": False,
            "citation_similarities": [0.6],
            "latency_ms": 10,
            "tokens": 0,
        }
    ]
    path = write_results(questions, observations, mode="scripted")
    assert "harness validation, not agent quality" in json.loads(path.read_text())["label"]


def test_results_list_unverified_questions_separately(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    questions = load_golden(
        Path(__file__).parents[1] / "golden.yaml",
        Path(__file__).parents[2] / "data" / "universe.csv",
    )[:1]
    questions[0] = questions[0].model_copy(update={"verified": False})
    monkeypatch.setattr("evals.runner.RESULTS", tmp_path)
    observation = {"id": questions[0].id, "latency_ms": 1, "tokens": 0}
    path = write_results(questions, [observation], mode="scripted")
    payload = json.loads(path.read_text())
    assert payload["summary"]["question_count"] == 0
    assert [item["id"] for item in payload["unverified_questions"]] == [questions[0].id]
    assert "Unverified question results" in path.with_suffix(".md").read_text()


def test_sse_parser_reads_data_events() -> None:
    assert _parse_events('event: plan_created\ndata: {"event":"plan_created"}\n') == [
        {"event": "plan_created"}
    ]


def test_dry_run_rejects_non_scripted_api() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"X-Agent-Runtime": "live"})
    )

    async def check() -> None:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await verify_scripted_api(client)

    with pytest.raises(RuntimeError, match=r"requires.*scripted"):
        asyncio.run(check())
