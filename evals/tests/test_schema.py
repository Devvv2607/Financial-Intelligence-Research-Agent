from pathlib import Path

from evals.schema import load_golden

ROOT = Path(__file__).parents[1]


def test_golden_set_is_well_formed_and_uses_universe_tickers() -> None:
    questions = load_golden(ROOT / "golden.yaml", ROOT.parent / "data" / "universe.csv")
    assert len(questions) == 30
    assert len({question.id for question in questions}) == 30
