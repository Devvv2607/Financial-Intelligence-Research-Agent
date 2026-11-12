from pathlib import Path

from evals.schema import load_golden

ROOT = Path(__file__).parents[1]


def test_golden_set_is_well_formed_and_uses_universe_tickers() -> None:
    questions = load_golden(ROOT / "golden.yaml", ROOT.parent / "data" / "universe.csv")
    assert len(questions) == 30
    assert len({question.id for question in questions}) == 30


def test_uncovered_questions_use_tickers_absent_from_universe() -> None:
    universe = {
        row.split(",", 1)[0].strip().upper()
        for row in (ROOT.parent / "data" / "universe.csv").read_text().splitlines()[1:]
        if row.strip()
    }
    questions = load_golden(ROOT / "golden.yaml", ROOT.parent / "data" / "universe.csv")
    for question in questions:
        if question.category == "uncovered_company":
            assert question.needs_full_data
            assert set(question.expected_entities).isdisjoint(universe)
