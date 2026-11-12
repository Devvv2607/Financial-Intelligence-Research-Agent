"""Citation verification with a deterministic bag-of-words embedder (no model, no network)."""

import re
import zlib

import numpy as np
from numpy.typing import NDArray

from agent.citecheck import UNVERIFIED, extract_claims, verify_citations
from agent.tools.base import Evidence


async def bow_embed(texts: list[str]) -> NDArray[np.float32]:
    out = np.zeros((len(texts), 512), dtype=np.float32)
    for i, t in enumerate(texts):
        for w in re.findall(r"[a-z0-9]+", t.lower()):
            out[i, zlib.crc32(w.encode()) % 512] += 1
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    return out / np.where(norms == 0, 1, norms)


def src(title: str, snippet: str) -> Evidence:
    return Evidence(source_id=title, source_type="risk_factor", title=title, snippet=snippet)


SOURCES = [
    src(
        "INTC risk factor: competition",
        "We are in a highly competitive and rapidly changing industry.",
    ),
    src("INTC risk factor: capital returns", "We have ceased to return capital to stockholders."),
    src(
        "INTC risk factor: smart capital",
        "Our Smart Capital approach to capital spending involves risks.",
    ),
]


def test_claims_cover_sentences_cells_and_source_columns() -> None:
    answer = (
        "Intel says its industry is highly competitive [1]. It stopped buybacks [2].\n"
        "| Risk | Detail | Source |\n"
        "| Capital | We have ceased to return capital to stockholders | [2] |\n"
        "| Removed | None [3] | x |"
    )
    claims = extract_claims(answer)
    assert [c.numbers for c in claims] == [[1], [2], [2], [3]]
    assert claims[0].text == "Intel says its industry is highly competitive"
    # under 4 words: the whole line gives the context
    assert claims[1].text.startswith("Intel says") and claims[1].text.endswith("stopped buybacks")
    assert "ceased to return capital" in claims[2].text  # citation alone in a cell: the row
    assert claims[3].text == ""  # "None": too little to check


async def test_supported_citations_are_kept() -> None:
    answer = "We are in a highly competitive and rapidly changing industry [1]."
    out, report = await verify_citations(answer, SOURCES, bow_embed)
    assert out == answer
    assert report.summary()["precision"] == 1.0


async def test_wrong_citation_moves_to_the_clearly_matching_source() -> None:
    # demo 7: the "ceased to return capital" quote was cited to the Smart Capital risk
    answer = "Intel noted: we have ceased to return capital to stockholders [3]."
    out, report = await verify_citations(answer, SOURCES, bow_embed)
    assert out.endswith("stockholders [2].")
    assert (report.checked, report.supported, report.reattached) == (1, 0, 1)
    assert report.precision == 0.0


async def test_unmatched_claim_is_marked_unverified() -> None:
    answer = "Revenue grew forty percent on record datacenter demand [1]."
    out, report = await verify_citations(answer, SOURCES, bow_embed)
    assert f"demand {UNVERIFIED}." in out
    assert report.unverified_claims == 1 and report.dropped == 1


async def test_out_of_range_numbers_are_left_for_resolve_citations() -> None:
    answer = "We are in a highly competitive and rapidly changing industry [9]."
    out, report = await verify_citations(answer, SOURCES, bow_embed)
    assert out == answer and report.checked == 0
