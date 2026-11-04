"""Locate Item 1 (Business) and Item 1A (Risk Factors) in a 10-K.

Primary method (regex): every "Item 1A" occurrence is a candidate; the first is
usually the table of contents. We pick the candidate whose span to the next item
heading (1B / 1C / 2) is longest (BUILD_SPEC 3.4).
Fallback (LLM): if no candidate yields a plausible span, we show the model a short
numbered list of heading-like lines and ask which ones start/end the section.
"""

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from core.llm import GroqClient
from ingestion import prompts
from ingestion.extract.html_clean import Block

SectionName = Literal["item_1", "item_1a"]
_ITEM_RE = re.compile(r"^item\s*(\d{1,2}[a-c]?)\s*(?:[\.:\-\u2013\u2014]|\b)", re.I)
MIN_SECTION_CHARS = {"item_1": 2_000, "item_1a": 5_000}
# Section -> (its own item code, the item codes that can follow it)
_BOUNDS: dict[SectionName, tuple[str, set[str]]] = {
    "item_1": ("1", {"1a", "1b", "1c", "2"}),
    "item_1a": ("1a", {"1b", "1c", "2"}),
}


@dataclass(frozen=True)
class Section:
    name: SectionName
    start: int  # block index of the heading
    end: int  # exclusive block index of the next item heading
    method: Literal["regex", "llm"]

    def body(self, blocks: list[Block]) -> list[Block]:
        return blocks[self.start + 1 : self.end]


def item_code(text: str) -> str | None:
    m = _ITEM_RE.match(text)
    return m.group(1).lower() if m else None


def _span_chars(blocks: list[Block], start: int, end: int) -> int:
    return sum(len(b.text) for b in blocks[start:end])


def find_section_regex(blocks: list[Block], name: SectionName) -> Section | None:
    own, following = _BOUNDS[name]
    codes = [item_code(b.text) for b in blocks]
    best: Section | None = None
    best_len = 0
    for i, code in enumerate(codes):
        if code != own:
            continue
        end = next((j for j in range(i + 1, len(blocks)) if codes[j] in following), None)
        if end is None:
            continue
        length = _span_chars(blocks, i + 1, end)
        if length > best_len:
            best, best_len = Section(name, i, end, "regex"), length
    if best is None or best_len < MIN_SECTION_CHARS[name]:
        return None
    return best


_COVER_RE = re.compile(
    r"(indicate by check mark|commission file number|securities registered pursuant|"
    r"documents incorporated by reference|forward-looking statements?|table of contents|"
    r"aggregate market value|exact name of registrant|incorporated by reference|"
    r"cross-reference index|presentation of content in our form 10-k)",
    re.I,
)


_BULLET_START_RE = re.compile(r"^\s*[\u2022\u25aa\u25cf\u00b7\uf0b7]")


def business_overview(blocks: list[Block], max_words: int = 1_200) -> str:
    """Opening business description for filers with no single Item 1 (e.g. Intel).

    Takes substantive paragraphs (>= 200 chars) from the start of the document, skipping
    the cover page, table of contents and forward-looking-statements boilerplate, until
    `max_words`. Used only for sector classification and indexing, never as "Item 1".
    """
    words: list[str] = []
    for b in blocks:
        if (
            len(b.text) < 200
            or _COVER_RE.search(b.text[:300])
            or item_code(b.text)
            or _BULLET_START_RE.match(b.text)  # forward-looking-statement bullet lists
        ):
            continue
        words.extend(b.text.split())
        if len(words) >= max_words:
            break
    return " ".join(words[:max_words])


class _Boundaries(BaseModel):
    start_line: int = Field(description="Line number of the section's own heading, or -1")
    end_line: int = Field(description="Line number of the next section's heading, or -1")


_KEYWORDS = re.compile(
    r"\b(item|risk|business|unresolved|cybersecurity|properties|legal proceedings|part)\b", re.I
)
_TITLES: dict[SectionName, str] = {"item_1": "Business", "item_1a": "Risk Factors"}


def fallback_candidates(blocks: list[Block], limit: int = 300) -> list[tuple[int, str]]:
    """Short lines that could be section headings, deduplicated.

    Filers without "Item" headings mark sections with large type (Intel's are 18pt and
    not bold), so: >= 13pt text, or lines naming a 10-K section. Measured on the sample
    this is ~60-120 lines (~1K tokens); including every bold short line was ~700 lines.
    """
    seen: set[str] = set()
    out: list[tuple[int, str]] = []
    for i, b in enumerate(blocks):
        if len(b.text) > 120 or b.text in seen:
            continue
        if b.font_pt >= 13 or _KEYWORDS.search(b.text):
            seen.add(b.text)
            out.append((i, b.text[:100]))
    return out[:limit]


async def find_section_llm(
    blocks: list[Block], name: SectionName, llm: GroqClient, *, cik: int | None
) -> Section | None:
    """Ask the fast model to pick boundaries from short heading-like lines only.

    Sending only short lines that contain section keywords keeps this to a few hundred
    tokens instead of the whole filing.
    """
    candidates = fallback_candidates(blocks)
    if not candidates:
        return None
    listing = "\n".join(f"{i}: {text}" for i, text in candidates)
    result = await llm.structured(
        "fast",
        _Boundaries,
        [
            {"role": "system", "content": prompts.load("section_boundaries")},
            {
                "role": "user",
                "content": (
                    f"Which line is the heading that starts the '{_TITLES[name]}' section body, "
                    f"and which line starts the section after it?\n\n{listing}"
                ),
            },
        ],
        stage="extract_fallback",
        cik=cik,
    )
    valid = {i for i, _ in candidates}
    if result.start_line not in valid or result.end_line not in valid:
        return None
    if result.end_line <= result.start_line:
        return None
    if _span_chars(blocks, result.start_line + 1, result.end_line) < MIN_SECTION_CHARS[name]:
        return None
    return Section(name, result.start_line, result.end_line, "llm")
