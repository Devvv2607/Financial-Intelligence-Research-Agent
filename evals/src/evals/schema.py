"""Golden-set loading and validation."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class GoldenQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    category: str = Field(min_length=1)
    expected_entities: list[str]
    expected_agents: list[str]
    expected_tools: list[str]
    expect_clarification: bool
    expect_refusal: bool
    key_facts: list[str] = Field(min_length=1)
    needs_full_data: bool
    verified: bool

    @model_validator(mode="after")
    def normalize_tickers(self) -> "GoldenQuestion":
        self.expected_entities = [ticker.upper() for ticker in self.expected_entities]
        return self


def load_golden(path: Path, universe_path: Path) -> list[GoldenQuestion]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("golden.yaml must contain a list")
    questions = [GoldenQuestion.model_validate(item) for item in raw]
    ids = [question.id for question in questions]
    if len(ids) != len(set(ids)):
        raise ValueError("golden question ids must be unique")
    universe = {
        row.split(",", 1)[0].strip().upper()
        for row in universe_path.read_text(encoding="utf-8").splitlines()[1:]
        if row.strip()
    }
    invalid = sorted(
        {ticker for q in questions for ticker in q.expected_entities if ticker not in universe}
    )
    if invalid:
        raise ValueError(f"golden questions contain unknown universe tickers: {', '.join(invalid)}")
    return questions


def load_golden_mapping(path: Path, universe_path: Path) -> dict[str, GoldenQuestion]:
    return {question.id: question for question in load_golden(path, universe_path)}


def validate_golden_item(item: Any) -> GoldenQuestion:
    return GoldenQuestion.model_validate(item)
