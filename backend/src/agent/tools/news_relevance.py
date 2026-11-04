"""Relevance for news hits: an entity gate plus an absolute, explainable score.

Why not Weaviate's hybrid score: its default relative-score fusion min-max normalises the
BM25 and vector scores *within each query's result list* and averages them, so an article
that is top on one signal and absent from the other scores exactly 0.5 whatever its real
relevance. That is fine for ordering one list, useless as a threshold.

Our score, per article (all parts are returned with the hit):
    score = (0.75 * semantic + 0.25 * keyword) * source_weight
- semantic: cosine similarity of the query and article embeddings (bge, title +
  description), an absolute number comparable across queries;
- keyword: share of the query's content words found in the title or description;
- source_weight: 0.8 for aggregators that re-post others' headlines (Biztoc), 1.0 otherwise.
Gate and floor, applied before ranking:
- entity gate: the company's name or ticker appears in the title, or (for non-aggregator
  outlets) in the description. Aggregators list trending tickers in descriptions, so for
  them only the title counts;
- non-news sources (package indexes, deal sites) are excluded;
- relevance floor on *semantic* similarity (MIN_SEMANTIC). On stored NVDA/TSLA articles
  the relevant hits scored 0.61-0.63 and the irrelevant ones 0.44-0.56; the combined
  score separated them less cleanly, so it is used for ranking only (DECISIONS D-032).
"""

import re
from dataclasses import dataclass

from core.newsapi import short_company_name

SEMANTIC_WEIGHT = 0.75
KEYWORD_WEIGHT = 0.25
MIN_SEMANTIC = 0.58
AGGREGATOR_WEIGHTS: dict[str, float] = {"biztoc.com": 0.8}
NON_NEWS_SOURCES = frozenset({"pypi.org", "dealnews.com", "slickdeals.net"})
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "by", "from",
        "at", "as", "is", "are", "was", "were", "be", "been", "it", "its", "this", "that",
        "these", "those", "about", "into", "over", "after", "before", "than", "then",
        "their", "there", "what", "which", "who", "how", "why", "when", "news", "latest",
        "recent", "company", "companies",
    }
)  # fmt: skip


@dataclass(frozen=True)
class Relevance:
    score: float
    semantic: float
    keyword: float
    source_weight: float
    matched_on: str  # e.g. "title:name", "description:ticker"


# First words too generic to identify a company on their own ("General" Motors/Electric).
_GENERIC_FIRST_WORDS = frozenset(
    {
        "general", "american", "united", "international", "national", "first", "bank",
        "public", "southern", "eli", "the", "new", "global", "royal", "western", "eastern",
        "advanced", "home", "realty", "applied", "analog", "digital", "health", "energy",
        "financial", "capital", "texas", "pacific", "central", "consolidated", "coca",
        "motor", "electric", "data", "air", "waste", "union", "marathon", "valero",
    }
)  # fmt: skip


def name_aliases(sec_name: str) -> list[str]:
    """How news refers to a company: its short name, plus the first word when that word
    is distinctive ("Ford Motor" -> "Ford", "Jpmorgan Chase" -> "Jpmorgan")."""
    short = short_company_name(sec_name)
    aliases = [short] if short else []
    first = short.split()[0] if short else ""
    if " " in short and len(first) >= 4 and first.lower() not in _GENERIC_FIRST_WORDS:
        aliases.append(first)
    return aliases


def _alias_pattern(alias: str) -> str:
    # Whole words; a space in the name also matches a hyphen ("Coca Cola" ~ "Coca-Cola").
    return r"\b" + r"[\s-]+".join(re.escape(w) for w in alias.split()) + r"\b"


def mentions_company(text: str, name: str, ticker: str) -> str | None:
    """'name' if the company name (or a distinctive alias) appears as whole words, 'ticker'
    if the ticker does (3+ letters, or $-prefixed), else None."""
    if any(re.search(_alias_pattern(a), text, re.I) for a in name_aliases(name)):
        return "name"
    if (len(ticker) >= 3 and re.search(rf"\b{re.escape(ticker)}\b", text)) or re.search(
        rf"\${re.escape(ticker)}\b", text
    ):
        return "ticker"
    return None


def _words(text: str) -> set[str]:
    return {w.rstrip("s") for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w not in _STOPWORDS}


def keyword_coverage(query: str, text: str) -> float:
    wanted = _words(query)
    return len(wanted & _words(text)) / len(wanted) if wanted else 0.0


def source_weight(source_name: str | None) -> float | None:
    """None = not a news source (excluded)."""
    key = (source_name or "").strip().lower()
    if key in NON_NEWS_SOURCES:
        return None
    return AGGREGATOR_WEIGHTS.get(key, 1.0)


def relevance(
    query: str,
    title: str,
    description: str | None,
    source_name: str | None,
    semantic: float,
    company_name: str,
    ticker: str,
) -> Relevance | None:
    """None when the article fails the entity gate, isn't a news source, or falls below
    the semantic floor."""
    weight = source_weight(source_name)
    if weight is None or semantic < MIN_SEMANTIC:
        return None
    in_title = mentions_company(title, company_name, ticker)
    in_description = (
        mentions_company(description or "", company_name, ticker) if weight == 1.0 else None
    )
    if in_title is None and in_description is None:
        return None
    matched = f"title:{in_title}" if in_title else f"description:{in_description}"
    text = f"{title}. {description or ''}"
    keyword = keyword_coverage(query, text)
    score = (SEMANTIC_WEIGHT * semantic + KEYWORD_WEIGHT * keyword) * weight
    return Relevance(round(score, 3), round(semantic, 3), round(keyword, 3), weight, matched)
