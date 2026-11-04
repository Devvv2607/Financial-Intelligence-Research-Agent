"""Item 1 / Item 1A extraction and risk-factor splitting.

Fixtures are real 10-K primary documents (public SEC filings, gzipped) chosen for their
different layouts:
- Apple FY2026: Workiva inline XBRL, bold risk headings under subsection labels
- ExxonMobil FY2025: bold *lead-in* sentences inside paragraphs, under topic labels
- JPMorgan FY2025: bank 10-K (12 MB), bulleted risk summary before the risk factors
- Intel FY2025: no "Item" headings at all (cross-reference index), so regex must fail
  and the LLM fallback is exercised with a scripted model
Synthetic blocks below pin down each splitting rule in isolation.
"""

import gzip
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from ingestion.extract.html_clean import Block, html_to_blocks
from ingestion.extract.risk_splitter import split_risk_factors
from ingestion.extract.sections import (
    fallback_candidates,
    find_section_llm,
    find_section_regex,
)

FIXTURES = Path(__file__).parent / "fixtures" / "10k"


@cache
def blocks(name: str) -> list[Block]:
    return html_to_blocks(gzip.open(FIXTURES / f"{name}.htm.gz").read())


@pytest.mark.parametrize(
    ("doc", "min_1a_chars", "first_heading", "count"),
    [
        ("aapl-20260926", 80_000, "The Company\u2019s operations and performance depend", 28),
        ("xom-20251231", 30_000, "Economic conditions.", 22),
        ("jpm-20251231", 100_000, "JPMorganChase faces significant legal risks", 41),
    ],
)
def test_regex_extraction_and_split_on_real_10ks(
    doc: str, min_1a_chars: int, first_heading: str, count: int
) -> None:
    b = blocks(doc)
    item_1, item_1a = find_section_regex(b, "item_1"), find_section_regex(b, "item_1a")
    assert item_1 is not None and item_1a is not None
    assert item_1.end <= item_1a.start  # Business comes before Risk Factors
    assert sum(len(x.text) for x in item_1a.body(b)) >= min_1a_chars
    rfs = split_risk_factors(item_1a.body(b))
    assert len(rfs) == count  # pinned to catch regressions in the splitter
    assert rfs[0].heading.startswith(first_heading)
    assert all(len(r.body) >= 120 for r in rfs)


def test_table_of_contents_is_not_chosen() -> None:
    long_body = [Block("Risk text. " * 60, 0, 0, 0, "", 10.0) for _ in range(12)]
    blocks_ = [
        Block("Item 1A. Risk Factors", 0, 0, 0, "", 10.0),  # table of contents
        Block("Item 1B. Unresolved Staff Comments", 0, 0, 0, "", 10.0),
        Block("Item 2. Properties", 0, 0, 0, "", 10.0),
        Block("Item 1A. Risk Factors", 0, 0, 0, "", 10.0),  # the real section
        *long_body,
        Block("Item 1B. Unresolved Staff Comments", 0, 0, 0, "", 10.0),
    ]
    s = find_section_regex(blocks_, "item_1a")
    assert s is not None and (s.start, s.end) == (3, 16)


def test_subsections_and_boilerplate() -> None:
    b = blocks("jpm-20251231")
    s = find_section_regex(b, "item_1a")
    assert s is not None
    headings = [r.heading for r in split_risk_factors(s.body(b))]
    assert not any(h.lower().startswith(("summary", "the following")) for h in headings)
    xom = blocks("xom-20251231")
    xs = find_section_regex(xom, "item_1a")
    assert xs is not None
    rfs = split_risk_factors(xs.body(xom))
    assert rfs[0].subsection == "Supply and Demand"
    assert "Supply and Demand" not in [r.heading for r in rfs]


def test_text_is_decoded_not_mojibake() -> None:
    text = " ".join(x.text for x in blocks("jpm-20251231")[:400])
    assert "\ufffd" not in text
    assert "JPMorganChase\u2019s" in text


class ScriptedLLM:
    """Stands in for GroqClient.structured: returns a fixed answer, records the prompt."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    async def structured(
        self, slot: str, schema: type, messages: list[dict[str, str]], **kw: Any
    ) -> Any:
        self.calls.append({"slot": slot, "messages": messages, **kw})
        return schema.model_validate(self.answer)


async def test_llm_fallback_for_filing_without_item_headings() -> None:
    b = blocks("intc-20251227")
    assert find_section_regex(b, "item_1a") is None
    candidates = dict(fallback_candidates(b))
    start = next(i for i, t in candidates.items() if t == "Risk Factors")
    end = next(i for i, t in candidates.items() if t == "Other Key Information")
    llm = ScriptedLLM({"start_line": start, "end_line": end})
    section = await find_section_llm(b, "item_1a", llm, cik=50863)  # type: ignore[arg-type]
    assert section is not None and section.method == "llm"
    assert len(split_risk_factors(section.body(b))) >= 20
    # Only short heading-like lines are sent (~1K tokens), never the whole filing.
    assert len(llm.calls[0]["messages"][1]["content"]) < 8_000
    assert llm.calls[0]["stage"] == "extract_fallback"


@pytest.mark.parametrize(
    "answer", [{"start_line": -1, "end_line": -1}, {"start_line": 900, "end_line": 10}]
)
async def test_llm_fallback_rejects_invalid_boundaries(answer: dict[str, int]) -> None:
    section = await find_section_llm(blocks("intc-20251227"), "item_1a", ScriptedLLM(answer), cik=1)  # type: ignore[arg-type]
    assert section is None


# ---- synthetic blocks: one rule at a time --------------------------------------
def B(text: str, *, bold: bool = False, lead: str = "") -> Block:  # noqa: N802
    n = len(text.replace(" ", ""))
    return Block(
        text, n if bold else len(lead.replace(" ", "")), 0, 0, lead or (text if bold else ""), 10.0
    )


BODY = "This risk could materially and adversely affect our business, results and outlook. " * 3


def test_label_followed_by_heading_becomes_subsection() -> None:
    rfs = split_risk_factors(
        [
            B("Risks Related to Our Operations", bold=True),
            B("We depend on a small number of suppliers for key components.", bold=True),
            B(BODY),
        ]
    )
    assert len(rfs) == 1
    assert rfs[0].subsection == "Risks Related to Our Operations"


def test_heading_split_across_blocks_is_merged() -> None:
    rfs = split_risk_factors(
        [
            B(
                "Our international operations expose us to risks including changes in "
                "trade policy,",
                bold=True,
            ),
            B("tariffs and export controls.", bold=True),
            B(BODY),
        ]
    )
    assert rfs[0].heading.endswith("tariffs and export controls.")


def test_inline_lead_in_heading() -> None:
    lead = "Cybersecurity incidents."
    rfs = split_risk_factors([B(f"{lead} {BODY}", lead=lead)])
    assert rfs[0].heading == lead
    assert rfs[0].body.startswith("This risk")


def test_intro_and_bullet_summary_are_skipped() -> None:
    rfs = split_risk_factors(
        [
            B("Summary of Risk Factors", bold=True),
            B("• supply risks " + BODY),
            B("• competition risks " + BODY),
            B("We face intense competition in all of our markets.", bold=True),
            B(BODY),
            B("Our debt agreements contain restrictive covenants.", bold=True),
            B("• limits on liens " + BODY),
            B("• limits on dividends " + BODY),
        ]
    )
    assert [r.heading for r in rfs] == [
        "We face intense competition in all of our markets.",
        "Our debt agreements contain restrictive covenants.",  # bulleted body kept
    ]


def test_page_furniture_removed_but_repeated_section_title_kept() -> None:
    page = "<p><b>Risk Factors</b></p><p>Acme Corp | 2025 Form 10-K | {n}</p><p>{n}</p>"
    html = "<html><body>" + "".join(page.format(n=i) for i in range(3, 8)) + "</body></html>"
    texts = [b.text for b in html_to_blocks(html)]
    assert texts.count("Risk Factors") == 5
    assert not any("Form 10-K" in t or t.isdigit() for t in texts)


def test_inline_css_bold_and_hidden_elements() -> None:
    html = (
        '<div style="display:none">hidden xbrl header</div>'
        '<div><span style="font-weight:700">Bold heading text here</span></div>'
        '<div><span style="font-weight:bold">Lead.</span>'
        '<span style="font-weight:400"> rest</span></div>'
    )
    b = html_to_blocks(html)
    assert [x.text for x in b] == ["Bold heading text here", "Lead. rest"]
    assert b[0].emphasized and not b[1].emphasized
    assert b[1].lead_emphasis == "Lead."
