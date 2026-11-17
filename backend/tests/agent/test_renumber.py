"""Citations are numbered 1, 2, 3... in order of first appearance."""

import re

from agent.nodes.answer import renumber_citations
from agent.state import Citation


def cite(n: int) -> Citation:
    return Citation(n=n, source_id=f"s{n}", title=f"Source {n}", url=None)


def test_numbers_follow_first_appearance_and_repeat_consistently() -> None:
    answer = "Export controls [4]. Supply [17][4]. Rates [2] [unverified]. Again [17]."
    text, cites, mapping = renumber_citations(answer, [cite(2), cite(4), cite(17)])
    assert text == "Export controls [1]. Supply [2][1]. Rates [3] [unverified]. Again [2]."
    assert mapping == {4: 1, 17: 2, 2: 3}
    assert [(c.n, c.source_id) for c in cites] == [(1, "s4"), (2, "s17"), (3, "s2")]


def test_already_ordered_answers_and_answers_without_citations_are_unchanged() -> None:
    assert renumber_citations("A [1]. B [2].", [cite(1), cite(2)])[0] == "A [1]. B [2]."
    assert renumber_citations("No sources needed.", []) == ("No sources needed.", [], {})


def test_markers_and_citation_list_agree() -> None:
    text, cites, _ = renumber_citations("x [9] y [3] z [9] w [5]", [cite(3), cite(5), cite(9)])
    seen = list(dict.fromkeys(int(n) for n in re.findall(r"\[(\d+)\]", text)))
    assert seen == [c.n for c in cites] == [1, 2, 3]
