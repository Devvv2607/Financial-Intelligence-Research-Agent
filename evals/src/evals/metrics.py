"""Pure evaluation metrics over normalized observations."""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from evals.schema import GoldenQuestion


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 3) if denominator else 1.0


def plan_validity(question: GoldenQuestion, observed_agents: Iterable[str]) -> float:
    observed = set(observed_agents)
    return _ratio(
        sum(agent in observed for agent in question.expected_agents),
        len(question.expected_agents),
    )


def tool_selection_accuracy(question: GoldenQuestion, observed_tools: Iterable[str]) -> float:
    observed = set(observed_tools)
    return _ratio(
        sum(tool in observed for tool in question.expected_tools),
        len(question.expected_tools),
    )


def retrieval_hit_rate(question: GoldenQuestion, evidence_keys: Iterable[str]) -> float:
    evidence = {key.upper() for key in evidence_keys}
    expected = {key.upper() for key in question.expected_entities}
    return _ratio(len(expected & evidence), len(expected))


def citation_precision(similarities: Sequence[float], threshold: float = 0.585) -> float | None:
    if not similarities:
        return None
    return round(sum(score >= threshold for score in similarities) / len(similarities), 3)


def clarification_precision_recall(
    expected: Sequence[bool], observed: Sequence[bool]
) -> dict[str, float]:
    if len(expected) != len(observed):
        raise ValueError("expected and observed clarification arrays must have equal length")
    true_positive = sum(want and got for want, got in zip(expected, observed, strict=True))
    predicted = sum(observed)
    actual = sum(expected)
    return {
        "precision": _ratio(true_positive, predicted),
        "recall": _ratio(true_positive, actual),
    }


def refusal_accuracy(expected: Sequence[bool], observed: Sequence[bool]) -> float:
    if len(expected) != len(observed):
        raise ValueError("expected and observed refusal arrays must have equal length")
    return _ratio(
        sum(want == got for want, got in zip(expected, observed, strict=True)),
        len(expected),
    )


def percentile(values: Sequence[float], percentage: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((percentage / 100) * (len(ordered) - 1)))
    return round(ordered[index], 3)


def latency_percentiles(latencies_ms: Sequence[float]) -> dict[str, float | None]:
    return {"p50_ms": percentile(latencies_ms, 50), "p95_ms": percentile(latencies_ms, 95)}


def average_tokens(tokens: Sequence[int]) -> float:
    return round(sum(tokens) / len(tokens), 3) if tokens else 0.0


def summarize_observations(
    questions: Sequence[GoldenQuestion], observations: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    by_id = {str(item["id"]): item for item in observations}
    verified_questions = [q for q in questions if q.verified and q.id in by_id]
    unverified_questions = [q for q in questions if not q.verified and q.id in by_id]
    scored = [by_id[q.id] for q in verified_questions]

    def average(metric: Any) -> float:
        return (
            round(sum(metric(q, by_id[q.id]) for q in verified_questions) / len(scored), 3)
            if scored
            else 0.0
        )

    return {
        "question_count": len(scored),
        "unverified_question_count": len(unverified_questions),
        "plan_validity": average(lambda q, item: plan_validity(q, item.get("agents", []))),
        "tool_selection_accuracy": average(
            lambda q, item: tool_selection_accuracy(q, item.get("tools", []))
        ),
        "retrieval_hit_rate": average(
            lambda q, item: retrieval_hit_rate(q, item.get("evidence_keys", []))
        ),
        "citation_precision": citation_precision(
            [score for item in scored for score in item.get("citation_similarities", [])]
        ),
        "clarification": clarification_precision_recall(
            [q.expect_clarification for q in verified_questions],
            [bool(item.get("clarification")) for item in scored],
        ),
        "refusal_accuracy": refusal_accuracy(
            [q.expect_refusal for q in verified_questions],
            [bool(item.get("refusal")) for item in scored],
        ),
        "latency": latency_percentiles([float(item.get("latency_ms", 0)) for item in scored]),
        "average_tokens": average_tokens([int(item.get("tokens", 0)) for item in scored]),
    }
