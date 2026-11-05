"""Split Item 1A into individual risk factors using emphasized headings.

Rules (validated on the sample 10-Ks; see tests):
- A block that is >= 80% bold/italic/underlined is a heading, and so is a block set in
  noticeably larger type than the section's body text (Intel: 12pt headings, 9pt body).
- Two headings in a row: if the first is short and has no closing punctuation it is a
  *subsection label* ("Risks Related to Our Business"); otherwise the two blocks are one
  heading split across lines and are merged.
- A paragraph that *starts* with an emphasized sentence followed by normal text is an
  "inline" heading (e.g. ExxonMobil's lead-ins): that sentence is the heading. Bulleted
  paragraphs are excluded: an emphasized lead-in on a bullet is a point *within* a risk.
- Text before the first heading (the section intro), the "summary of risk factors"
  bullet list many filers put first, and boilerplate lead-ins are not risk factors.
"""

import re
from collections import Counter
from dataclasses import dataclass, field

from ingestion.extract.html_clean import Block

MIN_BODY_CHARS = 120
MAX_HEADING_CHARS = 900
LARGER_FONT_PT = 1.5  # this much above the body font size reads as a heading
_LABEL_MAX_CHARS = 120
_BOILERPLATE_RE = re.compile(
    r"^(the following|the above summary|summary|risk factors? summary|summary of risk factors"
    r"|forward-looking|investing in our|an investment in"
    # Notices that some filers place inside Item 1A (UNH: "CAUTIONARY STATEMENTS").
    r"|cautionary (statements?|note|language)|special note|safe harbor"
    r"|(note|statement)s? (regarding|about|concerning) forward-looking)\b",
    re.I,
)
# Introductory sentences styled like a heading. With a short or summary body the segment
# is just the preamble (AMD); with a substantial body it is a real risk whose own heading
# was absorbed (Tesla), so it is kept under its body's first sentence.
_PREAMBLE_RE = re.compile(
    r"^(the risks (and uncertainties )?described (below|in this)|you should carefully consider"
    r"|in addition to the other information)",
    re.I,
)
PREAMBLE_BODY_MIN = 400
_FIRST_SENTENCE_RE = re.compile(r"^(.{20,240}?[.!?])(\s|$)", re.S)
_BULLET_RE = re.compile(r"^\s*[\u2022\u25cf\u25aa\u25e6\u00b7\uf0b7\-\u2013]")


@dataclass
class RiskFactor:
    heading: str
    body: str
    subsection: str | None
    ordinal: int = 0


@dataclass
class _Seg:
    heading: str
    subsection: str | None
    body: list[str] = field(default_factory=list)
    inline: bool = False  # heading came from a paragraph's emphasized lead-in


def _body_font_pt(blocks: list[Block]) -> float:
    """The font size most of the section's text is set in (weighted by characters)."""
    sizes: Counter[float] = Counter()
    for b in blocks:
        if b.font_pt:
            sizes[b.font_pt] += len(b.text)
    return sizes.most_common(1)[0][0] if sizes else 0.0


def _is_heading(b: Block, body_pt: float) -> bool:
    larger = bool(body_pt) and b.font_pt >= body_pt + LARGER_FONT_PT
    return (b.emphasized or larger) and 15 <= len(b.text) <= MAX_HEADING_CHARS


def _inline_heading(b: Block) -> tuple[str, str] | None:
    prefix = b.lead_emphasis
    if _BULLET_RE.match(b.text):
        return None
    # >= 12 chars: ExxonMobil-style lead-ins ("Economic conditions.") are short.
    if (
        12 <= len(prefix) <= 600
        and re.search(r"[.:]$", prefix)
        and len(b.text) - len(prefix) >= 100
    ):
        return prefix, b.text[len(prefix) :].strip()
    return None


def _looks_like_label(text: str) -> bool:
    return len(text) <= _LABEL_MAX_CHARS and not re.search(r"[.!?:]$", text)


def split_risk_factors(blocks: list[Block]) -> list[RiskFactor]:
    segs: list[_Seg] = []
    subsection: str | None = None
    body_pt = _body_font_pt(blocks)
    for b in blocks:
        if b.text.strip().lower() in {"risk factors", "item 1a. risk factors"}:
            continue
        if _is_heading(b, body_pt):
            last = segs[-1] if segs else None
            if last is not None and not last.body:
                if _looks_like_label(last.heading):
                    subsection = last.heading
                    segs[-1] = _Seg(b.text, subsection)
                else:
                    last.heading = f"{last.heading} {b.text}"
                continue
            segs.append(_Seg(b.text, subsection))
            continue
        inline = _inline_heading(b)
        if inline is not None:
            last = segs[-1] if segs else None
            if last is not None and not last.inline and _looks_like_label(last.heading):
                # A styled topic label ("Supply and Demand") followed by inline lead-in
                # headings is a subsection; any text under it is an intro, not a risk.
                subsection = last.heading
                segs.pop()
            segs.append(_Seg(inline[0], subsection, [inline[1]], inline=True))
            continue
        if segs:
            segs[-1].body.append(b.text)

    out: list[RiskFactor] = []
    for seg in segs:
        body = "\n\n".join(seg.body).strip()
        if len(body) < MIN_BODY_CHARS or _BOILERPLATE_RE.match(seg.heading):
            continue
        mostly_bullets = sum(bool(_BULLET_RE.match(p)) for p in seg.body) > len(seg.body) / 2
        if mostly_bullets and not out:
            # The bulleted risk *summary* sits before the first real risk factor. Later
            # risk factors may legitimately have bulleted bodies, so only skip up front.
            continue
        heading = seg.heading.strip()
        if _PREAMBLE_RE.match(heading):
            first = _FIRST_SENTENCE_RE.match(body)
            short = len(body) < PREAMBLE_BODY_MIN or body.lower().startswith("the following")
            if short or not first:
                continue
            heading = first.group(1)
        out.append(RiskFactor(heading, body, seg.subsection, ordinal=len(out)))
    return out
