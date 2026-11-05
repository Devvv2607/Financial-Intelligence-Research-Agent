"""Risk-factor taxonomy tagging under a tight token budget (DECISIONS D-003).

- Latest 10-K: every risk factor is tagged by the FAST model, in batches, from its
  heading plus the first N words of its body.
- Prior 10-K: each risk factor is matched to the latest year's by embedding cosine
  similarity; a close match inherits the tag (same risk, reworded or unchanged), and only
  new/changed risks go to the LLM. This saves tokens *and* keeps year-over-year
  categories consistent instead of re-rolling LLM label noise.
- `headings_only=True` is the cheaper mode measured against full tagging on the sample.
"""

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, get_args

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel
from rapidfuzz import fuzz

from core.llm import GroqClient, InvalidOutputError, ResponseMode
from ingestion import prompts

logger = logging.getLogger(__name__)
# heading_start check: first words of the echo vs the risk's heading. Loose enough for
# small copying slips (punctuation, a dropped article), strict enough that another
# risk's heading fails.
ECHO_WORDS = 6  # the prompt asks for the first 6 words
ECHO_MIN_RATIO = 80

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
    # Echo of the risk's heading: lets us detect a tag attached to the wrong id. Matching
    # by id alone can't: a 25-risk batch gave 16 of 28 disagreements a summary of a
    # *different* risk, under a valid id (D-045).
    heading_start: str
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
    # The model never produced a valid category for this risk (after the capped retries);
    # it is tagged "other" so the run can continue, and flagged for a human to check.
    needs_review: bool = False


VALID_CATEGORIES: frozenset[str] = frozenset(get_args(RiskCategory))


def salvage_tags(raw: str, pending: dict[int, "RiskInput"]) -> dict[int, tuple[str, str]]:
    """Best-effort read of an invalid batch output: id -> (category, summary).

    Only ids asked for, once each, whose heading_start matches their risk are kept;
    categories outside the taxonomy come back as "" so the caller can flag them.
    """
    try:
        data: Any = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    out: dict[int, tuple[str, str]] = {}
    items = data.get("tags", []) if isinstance(data, dict) else []
    seen = [i.get("id") for i in items if isinstance(i, dict)]
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            continue
        rid = item["id"]
        if rid not in pending or seen.count(rid) > 1:
            continue
        if not echo_matches(str(item.get("heading_start") or ""), rid, pending):
            continue
        category = item.get("category")
        out[item["id"]] = (
            category if category in VALID_CATEGORIES else "",
            str(item.get("summary") or "").strip(),
        )
    return out


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
    mode: ResponseMode = "json_schema",
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
            try:
                out = await llm.structured(
                    "fast",
                    RiskTagBatch,
                    [
                        {"role": "system", "content": prompts.load("risk_tagging")},
                        {"role": "user", "content": listing},
                    ],
                    stage=stage,
                    cik=cik,
                    mode=mode,
                )
            except InvalidOutputError as exc:
                # Retries are spent. Keep what was valid; everything else becomes
                # "other" + needs_review. No further calls for this batch.
                salvaged = salvage_tags(exc.last_output, pending)
                for i, r in pending.items():
                    category, summary = salvaged.get(i, ("", ""))
                    results.append(
                        TagResult(
                            r.key, category or "other", summary, "llm", needs_review=not category
                        )
                    )
                pending = {}
                break
            problem = batch_problem(out.tags, pending)
            if problem is not None:
                # Reject the whole output (nothing from a confused batch is trusted) and
                # re-ask; after the last attempt the risks fall through to needs_review.
                logger.warning("tag batch rejected", extra={"cik": cik, "problem": problem})
                continue
            for tag in out.tags:
                r = pending.pop(tag.id)
                results.append(TagResult(r.key, tag.category, tag.summary.strip(), "llm"))
        for r in pending.values():  # attempts spent: keep the run going, flag for a human
            results.append(TagResult(r.key, "other", "", "llm", needs_review=True))
    return results


def _words(text: str, n: int) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower())[:n])


def batch_problem(tags: Sequence[RiskTag], pending: dict[int, RiskInput]) -> str | None:
    """Why this output can't be trusted, or None. Ids must be unique and among the risks
    asked for, and each tag's heading_start must match the heading of its id's risk.
    Missing ids are allowed: the next attempt asks for just those."""
    ids = [t.id for t in tags]
    if len(ids) != len(set(ids)):
        return "duplicate ids"
    unknown = set(ids) - set(pending)
    if unknown:
        return f"unknown ids {sorted(unknown)}"
    for t in tags:
        if not echo_matches(t.heading_start, t.id, pending):
            return f"id {t.id} heading_start doesn't match its risk"
    return None


def echo_matches(heading_start: str, rid: int, pending: dict[int, RiskInput]) -> bool:
    """The echo must resemble its own risk's heading, and no other risk's more: headings
    often share long prefixes ("We are subject to the risks of ..."), so a threshold alone
    would let a tag attached to a sibling risk through."""
    echo = _words(heading_start, ECHO_WORDS)
    scores = {i: fuzz.ratio(echo, _words(r.heading, ECHO_WORDS)) for i, r in pending.items()}
    return scores[rid] >= ECHO_MIN_RATIO and scores[rid] >= max(scores.values())


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
