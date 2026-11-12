"""Citation verification: does each cited source actually support the claim citing it?

finalize already guarantees that every [n] exists. This checks that it fits: the claim
(the text a citation group closes) is embedded with the local model and compared with
each cited source's title + snippet. A citation below the threshold is moved to the best
matching source if one clearly matches; otherwise it's dropped, and a claim left without any
citation is marked [unverified]. Precision = supported / checked citations, per run.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from agent.tools.base import Evidence

Embed = Callable[[list[str]], Awaitable[NDArray[np.float32]]]

# Calibrated on the 68 (claim, cited source) pairs of the 2026-11-04 demo runs (D-038):
# the one confirmed wrong citation scored 0.567, the lowest correct one 0.604. Moving a
# citation needs a clear match: correct quote-to-source pairs scored 0.745-0.915, and the
# wrong citation's best alternative (0.614) was an unrelated chunk.
SUPPORT_THRESHOLD = 0.585
REATTACH_THRESHOLD = 0.75
UNVERIFIED = "[unverified]"
MIN_CLAIM_WORDS = 4

_GROUP_RE = re.compile(r"(?:\[\d{1,3}\])+")
_NUM_RE = re.compile(r"\[(\d{1,3})\]")
# A claim starts after a sentence end, a table cell / line break, or the previous group.
_BOUNDARY_RE = re.compile(r"(?:[.!?]\s+(?=[A-Z\"“(*])|\||\n|<br>)")
_MARKDOWN_RE = re.compile(r"[*_`#>|]+|^\s*[-•]\s*", re.MULTILINE)


@dataclass
class Claim:
    start: int  # span of the citation group in the answer
    end: int
    text: str
    numbers: list[int]


@dataclass
class CitationCheck:
    claim: str
    cited: list[int]
    similarity: dict[int, float]
    kept: list[int]
    action: Literal["supported", "reattached", "partly_dropped", "unverified"]


@dataclass
class CitationReport:
    checked: int = 0
    supported: int = 0
    reattached: int = 0
    dropped: int = 0
    unverified_claims: int = 0
    checks: list[CitationCheck] = field(default_factory=list)

    @property
    def precision(self) -> float | None:
        return round(self.supported / self.checked, 3) if self.checked else None

    def summary(self) -> dict[str, float | int | None]:
        return {
            "checked": self.checked,
            "supported": self.supported,
            "reattached": self.reattached,
            "dropped": self.dropped,
            "unverified_claims": self.unverified_claims,
            "precision": self.precision,
        }


def _clean(text: str) -> str:
    return " ".join(_MARKDOWN_RE.sub(" ", _GROUP_RE.sub(" ", text)).split())


def extract_claims(answer: str) -> list[Claim]:
    claims: list[Claim] = []
    prev_end = 0
    for m in _GROUP_RE.finditer(answer):
        before = answer[prev_end : m.start()]
        cut = max((b.end() for b in _BOUNDARY_RE.finditer(before)), default=0)
        text = _clean(before[cut:])
        if len(text.split()) < MIN_CLAIM_WORDS:
            # e.g. "... FY 2024[4] & FY 2025[3]": use the whole cell or line instead, and for
            # a citation alone in a table's "Source" column, the rest of the row.
            cell_start = max(answer.rfind("\n", 0, m.start()), answer.rfind("|", 0, m.start()))
            text = _clean(answer[cell_start + 1 : m.start()])
            if len(text.split()) < MIN_CLAIM_WORDS:
                line_start = answer.rfind("\n", 0, m.start()) + 1
                line_end = answer.find("\n", m.end())
                text = _clean(answer[line_start : line_end if line_end >= 0 else len(answer)])
            if len(text.split()) < MIN_CLAIM_WORDS:
                text = ""  # e.g. "None [1]" in a table: too little to check
        numbers = [int(n) for n in _NUM_RE.findall(m.group(0))]
        claims.append(Claim(m.start(), m.end(), text, list(dict.fromkeys(numbers))))
        prev_end = m.end()
    return claims


def source_text(e: Evidence) -> str:
    return f"{e.title}\n{e.snippet}"


async def verify_citations(
    answer: str,
    sources: list[Evidence],
    embed: Embed,
    threshold: float = SUPPORT_THRESHOLD,
) -> tuple[str, CitationReport]:
    """`sources[n-1]` is source [n]. Returns the corrected answer and the report."""
    report = CitationReport()
    claims = [c for c in extract_claims(answer) if c.text]
    if not claims or not sources:
        return answer, report
    vectors = await embed([c.text for c in claims] + [source_text(e) for e in sources])
    claim_vecs, source_vecs = vectors[: len(claims)], vectors[len(claims) :]
    sims = claim_vecs @ source_vecs.T  # normalized vectors: cosine similarity
    out = answer
    for i, c in sorted(enumerate(claims), key=lambda ic: -ic[1].start):  # edit right to left
        valid = [n for n in c.numbers if 1 <= n <= len(sources)]
        if not valid:
            continue  # numbers that aren't sources: resolve_citations removes them
        sim = {n: round(float(sims[i, n - 1]), 3) for n in valid}
        kept = [n for n in valid if sim[n] >= threshold]
        report.checked += len(valid)
        report.supported += len(kept)
        action: Literal["supported", "reattached", "partly_dropped", "unverified"] = "supported"
        if kept and len(kept) < len(valid):
            action = "partly_dropped"
            report.dropped += len(valid) - len(kept)
        elif not kept:
            best = int(np.argmax(sims[i])) + 1
            if float(sims[i, best - 1]) >= REATTACH_THRESHOLD:
                kept, action = [best], "reattached"
                sim[best] = round(float(sims[i, best - 1]), 3)
                report.reattached += 1
            else:
                action = "unverified"
                report.dropped += len(valid)
                report.unverified_claims += 1
        replacement = "".join(f"[{n}]" for n in kept) if kept else UNVERIFIED
        out = out[: c.start] + replacement + out[c.end :]
        report.checks.append(CitationCheck(c.text, valid, sim, kept, action))
    report.checks.reverse()
    return out, report
