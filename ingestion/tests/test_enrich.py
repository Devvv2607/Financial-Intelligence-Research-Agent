"""SIC sector rules and risk-tagging logic (scripted LLM, synthetic vectors)."""

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from core.llm import InvalidOutputError
from ingestion.enrich.risk_tagger import (
    RiskInput,
    TagResult,
    inherit_tags,
    tag_with_llm,
)
from ingestion.enrich.sector_rules import sic_to_sector


@pytest.mark.parametrize(
    ("sic", "sector"),
    [
        (3571, "Information Technology"),  # Apple: electronic computers
        (3674, "Information Technology"),  # semiconductors
        (6324, "Health Care"),  # UnitedHealth: medical service plans (specific override)
        (2834, "Health Care"),  # pharmaceutical preparations
        (2911, "Energy"),  # petroleum refining
        (6021, "Financials"),  # national commercial banks
        (3711, "Consumer Discretionary"),  # motor vehicles
        (3721, "Industrials"),  # aircraft
        (4922, "Energy"),  # gas pipelines, not Utilities
        (5331, "Consumer Staples"),  # Walmart/Costco, not Discretionary
        (1531, "Consumer Discretionary"),  # homebuilders, not Industrials
        (6798, "Real Estate"),  # REITs, not Financials
        (7370, "Information Technology"),  # Alphabet: a known SIC-vs-GICS conflict
        (7389, "Industrials"),  # Visa/Mastercard: a known SIC-vs-GICS conflict
    ],
)
def test_sic_mapping(sic: int, sector: str) -> None:
    assert sic_to_sector(sic) == sector


def test_unknown_sic() -> None:
    assert sic_to_sector(None) is None
    assert sic_to_sector(9999) is None


class ScriptedTagger:
    """Answers tag batches; can drop ids on the first call to test the re-ask."""

    def __init__(self, drop_first: set[int] | None = None) -> None:
        self.drop_first = drop_first or set()
        self.prompts: list[str] = []

    async def structured(
        self, slot: str, schema: type, messages: list[dict[str, str]], **kw: Any
    ) -> Any:
        listing = messages[-1]["content"]
        self.prompts.append(listing)
        ids = [
            int(line[1 : line.index("]")]) for line in listing.split("\n") if line.startswith("[")
        ]
        if len(self.prompts) == 1:
            ids = [i for i in ids if i not in self.drop_first]
        return schema.model_validate(
            {"tags": [{"id": i, "category": "regulatory", "summary": f"s{i}"} for i in ids]}
        )


RISKS = [RiskInput(f"k{i}", f"Heading {i}", "word " * 400) for i in range(5)]


async def test_tagging_batches_and_truncates_bodies() -> None:
    llm = ScriptedTagger()
    out = await tag_with_llm(
        llm,
        RISKS,
        cik=1,
        stage="risk_tagging",
        batch_size=2,  # type: ignore[arg-type]
        body_words=150,
    )
    assert [t.key for t in out] == [r.key for r in RISKS]
    assert len(llm.prompts) == 3  # 5 risks in batches of 2
    assert max(p.count("word") for p in llm.prompts) <= 2 * 150


async def test_headings_only_sends_no_body() -> None:
    llm = ScriptedTagger()
    await tag_with_llm(
        llm,
        RISKS[:2],
        cik=1,
        stage="x",
        batch_size=5,
        body_words=150,  # type: ignore[arg-type]
        headings_only=True,
    )
    assert "word" not in llm.prompts[0]


async def test_missing_ids_are_reasked_once() -> None:
    llm = ScriptedTagger(drop_first={1})
    out = await tag_with_llm(llm, RISKS[:3], cik=1, stage="x", batch_size=5, body_words=10)  # type: ignore[arg-type]
    assert sorted(t.key for t in out) == ["k0", "k1", "k2"]
    assert "[1]" in llm.prompts[1] and "[0]" not in llm.prompts[1]


def test_prior_year_inherits_only_above_threshold() -> None:
    latest = [
        TagResult("L0", "cybersecurity", "cyber", "llm"),
        TagResult("L1", "talent", "people", "llm"),
    ]
    latest_vecs = np.array([[1, 0, 0], [0, 1, 0]], dtype=np.float32)
    prior = [RiskInput("P0", "h", "b"), RiskInput("P1", "h", "b")]
    v = np.array([0.95, 0.312, 0], dtype=np.float32)
    prior_vecs = np.stack([v / np.linalg.norm(v), np.array([0, 0, 1], dtype=np.float32)])
    inherited, remaining = inherit_tags(prior, latest, prior_vecs, latest_vecs, threshold=0.9)
    assert [(t.key, t.category, t.source, t.inherited_from) for t in inherited] == [
        ("P0", "cybersecurity", "inherited", "L0")
    ]
    assert inherited[0].similarity == pytest.approx(0.95, abs=0.01)
    assert [r.key for r in remaining] == ["P1"]


def test_inherit_with_nothing_tagged_sends_everything_to_llm() -> None:
    prior = [RiskInput("P0", "h", "b")]
    inherited, remaining = inherit_tags(
        prior, [], np.zeros((1, 3), np.float32), np.zeros((0, 3), np.float32), 0.9
    )
    assert inherited == [] and remaining == prior


class AlwaysInvalid:
    """Retries already spent inside the client: it raises with the last raw output."""

    def __init__(self, last_output: str) -> None:
        self.last_output = last_output
        self.calls = 0

    async def structured(self, *a: Any, **kw: Any) -> Any:
        self.calls += 1
        raise InvalidOutputError("output failed validation", self.last_output)


async def test_invalid_batch_salvages_valid_tags_and_flags_the_rest() -> None:
    raw = (
        '{"tags": [{"id": 0, "category": "talent", "summary": "key people"},'
        ' {"id": 1, "category": "pandemic", "summary": "made-up category"}]}'
    )
    llm = AlwaysInvalid(raw)
    out = await tag_with_llm(llm, RISKS[:3], cik=1, stage="x", batch_size=5, body_words=10)  # type: ignore[arg-type]
    got = {t.key: (t.category, t.needs_review) for t in out}
    assert got == {"k0": ("talent", False), "k1": ("other", True), "k2": ("other", True)}
    assert llm.calls == 1  # no extra re-ask after the capped retries


async def test_unparseable_output_flags_whole_batch() -> None:
    out = await tag_with_llm(
        AlwaysInvalid("not json"),
        RISKS[:2],
        cik=1,
        stage="x",  # type: ignore[arg-type]
        batch_size=5,
        body_words=10,
    )
    assert all(t.category == "other" and t.needs_review for t in out)


def test_prompt_versions_come_from_headers() -> None:
    from ingestion import prompts

    assert prompts.version("risk_tagging") == "risk_tagging:v2"
    assert prompts.version("sector_classification").startswith("sector_classification:v")


def test_experiment_config_parsing() -> None:
    from ingestion.experiments import Config

    assert Config.parse("json_object:25") == Config("json_object", 25)
    with pytest.raises(ValueError):
        Config.parse("xml:5")


def test_review_sample_covers_rare_categories_and_spreads_companies() -> None:
    from ingestion.review import stratified_sample

    rows = [{"category": "regulatory", "ticker": f"T{i % 3}", "id": f"r{i}"} for i in range(40)]
    rows += [{"category": "ip", "ticker": "T9", "id": "ip1"}]
    picked = stratified_sample(rows, 10)
    assert len(picked) == 10 and "ip1" in [r["id"] for r in picked]
    assert {r["ticker"] for r in picked} >= {"T0", "T1", "T2"}


def test_review_scoring(tmp_path: Path) -> None:
    import csv

    from ingestion.review import COLUMNS, score_review_sheet

    path = tmp_path / "sheet.csv"
    rows = [
        ("a", "talent", "talent"),
        ("b", "regulatory", "geopolitical"),
        ("c", "ip", ""),
        ("d", "other", "nonsense"),
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for rid, model, mine in rows:
            w.writerow({"risk_id": rid, "model_category": model, "your_category": mine})
    s = score_review_sheet(path)
    assert (s.labelled, s.correct) == (2, 1)
    assert s.confusions == [(("geopolitical", "regulatory"), 1)]
    assert s.invalid_labels == ["d: 'nonsense'"]
