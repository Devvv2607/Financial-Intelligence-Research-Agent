from evals.metrics import (
    citation_precision,
    clarification_precision_recall,
    latency_percentiles,
    plan_validity,
    refusal_accuracy,
    retrieval_hit_rate,
    tool_selection_accuracy,
)
from evals.schema import GoldenQuestion

QUESTION = GoldenQuestion(
    id="q",
    question="q",
    category="test",
    expected_entities=["AAPL"],
    expected_agents=["filings"],
    expected_tools=["get_risk_factors"],
    expect_clarification=False,
    expect_refusal=False,
    key_facts=["risk"],
    needs_full_data=False,
)


def test_pure_metrics() -> None:
    assert plan_validity(QUESTION, ["filings"]) == 1
    assert tool_selection_accuracy(QUESTION, ["get_risk_factors"]) == 1
    assert retrieval_hit_rate(QUESTION, ["AAPL"]) == 1
    assert citation_precision([0.585, 0.4]) == 0.5
    assert clarification_precision_recall([True, False], [True, True]) == {
        "precision": 0.5,
        "recall": 1.0,
    }
    assert refusal_accuracy([True, False], [True, True]) == 0.5
    assert latency_percentiles([10, 20, 100]) == {"p50_ms": 20, "p95_ms": 100}
