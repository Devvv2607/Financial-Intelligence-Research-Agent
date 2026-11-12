import json
from pathlib import Path

from pytest import MonkeyPatch

from evals.runner import _parse_events, write_results
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


def test_sse_parser_reads_data_events() -> None:
    assert _parse_events('event: plan_created\ndata: {"event":"plan_created"}\n') == [
        {"event": "plan_created"}
    ]
