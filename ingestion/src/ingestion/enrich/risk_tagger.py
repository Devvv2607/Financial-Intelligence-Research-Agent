"""Risk-factor taxonomy tagging under a tight token budget (DECISIONS D-003).

- Latest 10-K: every risk factor is tagged by the FAST model, in batches, from its
  heading plus the first N words of its body.
- Prior 10-K: each risk factor is matched to the latest year's by embedding cosine
  similarity; a close match inherits the tag (same risk, reworded or unchanged), and only
  new/changed risks go to the LLM. This saves tokens *and* keeps year-over-year
  categories consistent instead of re-rolling LLM label noise.
- `headings_only=True` is the cheaper mode measured against full tagging on the sample.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel

from core.llm import GroqClient
from ingestion import prompts

RiskCategory = Literal[
    "regulatory",
    "competition",
    "supply_chain",
    "cybersecurity",
    "macroeconomic",
    "geopolitical",
    "litigation",
    "technology",
    "talent",
    "financial_liquidity",
    "climate_esg",
    "customer_concentration",
    "ip",
    "other",
]


class RiskTag(BaseModel):
    id: int
    category: RiskCategory
    summary: str


class RiskTagBatch(BaseModel):
    tags: list[RiskTag]


@dataclass(frozen=True)
class RiskInput:
    key: str  # risk_factors.id
    heading: str
    body: str


@dataclass(frozen=True)
class TagResult:
    key: str
    category: str
    summary: str
    source: Literal["llm", "inherited"]
    inherited_from: str | None = None
    similarity: float | None = None


def _excerpt(r: RiskInput, body_words: int, headings_only: bool) -> str:
    if headings_only:
        return r.heading
    return f"{r.heading}\n{' '.join(r.body.split()[:body_words])}"


def similarity_text(r: RiskInput, body_words: int) -> str:
    """What we embed to compare risks across years: heading + opening of the body."""
    return _excerpt(r, body_words, headings_only=False)


async def tag_with_llm(
    llm: GroqClient,
    risks: Sequence[RiskInput],
    *,
    cik: int,
    stage: str,
    batch_size: int,
    body_words: int,
    headings_only: bool = False,
) -> list[TagResult]:
    results: list[TagResult] = []
    for start in range(0, len(risks), batch_size):
        batch = list(risks[start : start + batch_size])
        pending = dict(enumerate(batch))
        # A model can occasionally drop an id; re-ask once for just the missing ones.
        for _ in range(2):
            if not pending:
                break
            listing = "\n\n".join(
                f"[{i}] {_excerpt(r, body_words, headings_only)}" for i, r in pending.items()
            )
            out = await llm.structured(
                "fast",
                RiskTagBatch,
                [
                    {"role": "system", "content": prompts.load("risk_tagging")},
                    {"role": "user", "content": listing},
                ],
                stage=stage,
                cik=cik,
            )
            for tag in out.tags:
                if tag.id in pending:
                    r = pending.pop(tag.id)
                    results.append(TagResult(r.key, tag.category, tag.summary.strip(), "llm"))
    return results


def best_matches(
    prior: NDArray[np.float32], latest: NDArray[np.float32]
) -> tuple[NDArray[np.int64], NDArray[np.float32]]:
    """For each prior-year vector, the index and cosine similarity of its closest
    latest-year vector (vectors are L2-normalized, so dot product = cosine)."""
    sims = prior @ latest.T
    idx = sims.argmax(axis=1)
    return idx, sims[np.arange(len(prior)), idx]


def inherit_tags(
    prior: Sequence[RiskInput],
    latest_tags: Sequence[TagResult],
    prior_vecs: NDArray[np.float32],
    latest_vecs: NDArray[np.float32],
    threshold: float,
) -> tuple[list[TagResult], list[RiskInput]]:
    """Split prior-year risks into (inherited tags, risks that still need the LLM)."""
    if len(latest_tags) == 0 or len(prior) == 0:
        return [], list(prior)
    idx, sims = best_matches(prior_vecs, latest_vecs)
    inherited: list[TagResult] = []
    remaining: list[RiskInput] = []
    for r, j, sim in zip(prior, idx, sims, strict=True):
        if sim >= threshold:
            src = latest_tags[int(j)]
            inherited.append(
                TagResult(r.key, src.category, src.summary, "inherited", src.key, float(sim))
            )
        else:
            remaining.append(r)
    return inherited, remaining
