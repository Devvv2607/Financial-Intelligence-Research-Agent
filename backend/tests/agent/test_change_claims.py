"""Change claims rest on the diff evidence (D-052), on the 2026-11-18 production drafts.

The Apple draft's three FY2025-comparison bullets ("whereas FY 2025's version did not
mention AI", "differing from the FY 2025 phrasing", "compared with the FY 2025 version")
cited the FY2025 wordings and failed the citation check (precision 0.786). Each such
clause must now either cite the risk's diff and pass, or be removed.
"""

import json
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from agent.citecheck import verify_citations
from agent.consistency import ConsistencyReport, check_answer, diff_links, relink_change_claims
from agent.tools.base import Evidence
from agent.tools.filings import word_diff

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "consistency"
MODELS = Path("data/models")
# Typography the model writes: narrow no-break space, right quote, non-breaking hyphen.
NNB, RSQ, NBH = chr(0x202F), chr(0x2019), chr(0x2011)


def load(name: str) -> tuple[str, list[Evidence]]:
    d = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return d["draft"], [Evidence.model_validate(s) for s in d["sources"]]


def bullet(text: str, needle: str) -> str:
    return next(line for line in text.splitlines() if needle in line)


# ---------------------------------------------------------------- the Apple draft
def test_contrast_clauses_cite_the_diff_or_are_removed() -> None:
    draft, sources = load("apple-risk-changes-2026-11-18")
    report = ConsistencyReport()
    fixed = relink_change_claims(draft, sources, report)

    # Backed by the diff: the defects diff adds a passage on artificial intelligence.
    defects = bullet(fixed, "defects")
    assert f"whereas FY{NNB}2025{RSQ}s version did not mention AI [14]" in defects
    assert sources[13].source_id.startswith("diff:")
    assert sources[13].linked_source_ids == [sources[4].source_id, sources[5].source_id]
    assert "[6]" not in defects  # no longer cites the FY2025 wording for the contrast

    # Not backed (no content, or a word no diff passage contains): removed, the claim
    # about FY 2026 and its citation kept.
    assert bullet(fixed, "proceedings").rstrip().endswith(f"updated in FY{NNB}2026[7].")
    assert bullet(fixed, "cybersecurity").rstrip().endswith(f"re{NBH}worded in FY{NNB}2026[9].")
    assert bullet(fixed, "developers").rstrip().endswith(f"restated in FY{NNB}2026[11].")
    assert "mentioned only components" not in fixed
    for gone in ("differing from the FY", f"compared with the FY{NNB}2025 version", "differs from"):
        assert gone not in fixed
    assert [f.detail for f in report.change_claims_recited] == ["diff [14]"]
    assert len(report.change_claims_removed) == 4
    assert fixed.count("\n") == draft.count("\n")  # no bullet lost


async def _changed_only(texts: list[str]) -> NDArray[np.float32]:
    """Claims mentioning lawsuits sit on one axis, everything else on another."""
    out = np.zeros((len(texts), 2), dtype=np.float32)
    for i, t in enumerate(texts):
        out[i, 0 if "lawsuit" in t.lower() else 1] = 1.0
    return out


async def test_a_change_claim_citing_the_risk_is_scored_against_its_diff() -> None:
    later = Evidence(
        source_id="rf:a",
        source_type="risk_factor",
        title="AAPL 10-K FY2026 risk factor: legal proceedings",
        snippet="The Company is subject to legal proceedings and government investigations.",
        compare_status="changed",
    )
    diff = Evidence(
        source_id="diff:a",
        source_type="risk_factor",
        title="AAPL risk factor change, FY2025 vs FY2026: legal proceedings",
        snippet="FY2026 adds: class-action and other lawsuits based on legal theories",
        compare_status="changed",
        linked_source_ids=["rf:a", "rf:b"],
    )
    sources = [later, diff]
    claim = "FY 2026 adds class-action lawsuits to the legal risk [1]."
    out, report = await verify_citations(claim, sources, _changed_only, links=diff_links(sources))
    assert out == "FY 2026 adds class-action lawsuits to the legal risk [2]."
    assert (report.supported, report.via_diff, report.precision) == (1, 1, 1.0)

    # Not a change claim: the diff link doesn't apply. (The ordinary reattach rule still
    # moves a citation to a clearly better source, and counts it as reattached, not
    # supported.)
    plain = "Apple faces class-action lawsuits [1]."
    out, report = await verify_citations(plain, sources, _changed_only, links=diff_links(sources))
    assert (report.via_diff, report.reattached, report.supported) == (0, 1, 0)


@pytest.mark.integration
@pytest.mark.skipif(not MODELS.is_dir(), reason="embedding model not cached in data/models")
async def test_with_the_real_model_the_draft_now_passes_the_gate() -> None:
    """The production embedding model, cached locally."""
    from core.config import Settings
    from core.embeddings import Embedder

    s = Settings(_env_file=None)
    embed = Embedder(s.embedding_model, MODELS).embed
    draft, sources = load("apple-risk-changes-2026-11-18")

    # Before: exactly the production result.
    _, before = await verify_citations(draft, sources, embed)
    assert (before.precision, before.unverified_claims) == (0.786, 3)

    report = ConsistencyReport()
    relinked = relink_change_claims(draft, sources, report)
    out, after = await verify_citations(relinked, sources, embed, links=diff_links(sources))
    out, _ = check_answer(out, sources)
    assert after.precision is not None and after.precision >= 0.8
    assert "[unverified]" not in out
    recited = next(c for c in after.checks if 14 in c.cited)
    assert recited.action == "supported" and recited.kept == [14]


# ---------------------------------------------------------------- the diff itself
def test_word_diff_reports_the_largest_removed_passage_too() -> None:
    before = "alpha beta gamma delta epsilon zeta eta theta"
    after = "alpha beta NEW1 NEW2 NEW3 epsilon zeta eta"
    d = word_diff(before, after)
    assert d.added_excerpt.startswith("NEW1 NEW2 NEW3")
    assert d.removed_excerpt.startswith("gamma delta")


# ---------------------------------------------------------------- Boeing 5.02 wording
def test_a_rewritten_8k_bullet_reads_once() -> None:
    draft, sources = load("boeing-8k-events-2026-11-18")
    fixed, report = check_answer(draft, sources)
    assert len(report.eight_k_rewrites) == 1
    line = bullet(fixed, "5.02")
    assert line == (
        "   * Item 5.02 (Departure or appointment of directors or officers; "
        "compensation arrangements) [3]."
    )
    assert "The 8-K filed" not in fixed  # the lead line already names the filing


def test_the_synthesizer_sees_each_changed_risk_with_its_diff() -> None:
    from agent.nodes.answer import comparison_section

    _, sources = load("apple-risk-changes-2026-11-18")
    ((name, text),) = comparison_section({"evidence": sources}, sources)  # type: ignore[typeddict-item]
    assert name == "comparison"
    defects = next(line for line in text.splitlines() if "[5]" in line)
    assert defects.startswith("- CHANGED [5]") and defects.endswith("; diff [14]")
    assert "risk factor change" not in text  # diffs aren't listed as risks of their own
