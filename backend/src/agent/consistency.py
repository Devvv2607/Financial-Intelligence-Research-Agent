"""Deterministic checks of the answer against what the tools decided (D-051).

The citation check (citecheck.py) asks whether a cited source is *about* the claim. It
can't see a claim that cites the right source but says the wrong thing about it: on
2026-11-17 the Apple answer called reworded risks "unchanged ... identical language"
while citing both wordings, and the Boeing answer turned the title of 8-K Item 5.02 into
an event ("departure of certain officers and the appointment of new directors") that the
filing doesn't report. Both scored precision 1.0. Two rules close that gap:

1. Risk-factor status. compare_risk_factors labels every risk new / removed / changed /
   unchanged (Evidence.compare_status). A sentence, bullet or table row that asserts a
   status ("unchanged", "identical", "new risk", "removed", "reworded", ...) while every
   label it cites says otherwise is removed. A list item with no status word of its own
   inherits its lead line's ("remained unchanged, including:"). A lead line whose items
   were all removed goes too.
2. 8-K wording. A claim that cites only 8-Ks may use event words (appointed, departure,
   director, credit agreement, dividend, ...) only if the filing excerpt contains them;
   the item's official description is allowed when quoted as the item's name. Otherwise
   the claim is replaced by the item codes and official descriptions.

Both are counted in the run's citation_check and logged. Word lists, not a model: the
point is a check that behaves the same way every time and can be read in one screen.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from agent.tools.base import CompareStatus, Evidence
from agent.tools.eight_k_items import ITEM_DESCRIPTIONS

_NUM_RE = re.compile(r"\[(\d{1,3})\]")
# Unicode hyphens and dashes (U+2010-U+2015) and the minus sign, as ASCII "-".
_HYPHENS = re.compile("[" + chr(0x2010) + "-" + chr(0x2015) + chr(0x2212) + "]")
_LIST_ITEM = re.compile(r"^(\s*)(?:[-*•+]|\d+[.)])\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“(*\[])")
_MARKDOWN = re.compile(r"[*_`#>]+")

_RISK = r"risks?(?:[ -]factors?)?"
STATUS_PATTERNS: dict[CompareStatus, re.Pattern[str]] = {
    "unchanged": re.compile(
        r"\b(?:unchanged|identical|verbatim|word[- ]for[- ]word"
        r"|(?:the )?same (?:language|wording|text)"
        r"|not (?:been )?(?:changed|modified|revised|reworded|updated|amended)"
        r"|remain(?:ed|s)? (?:the same|as before)"
        r"|no (?:material |substantive |wording )?changes?)\b",
        re.I,
    ),
    "new": re.compile(
        rf"\b(?:new(?:ly added)? {_RISK}|added (?:a |one |two |three |\d+ )?(?:new )?{_RISK}"
        rf"|{_RISK} (?:was|were|is|are|has been|have been) (?:newly )?added"
        rf"|introduced (?:a |one )?(?:new )?{_RISK}|first appear(?:s|ed))\b",
        re.I,
    ),
    "removed": re.compile(
        r"\b(?:removed|dropped|deleted|eliminated"
        r"|no longer (?:included|disclosed|listed|appears?|present))\b",
        re.I,
    ),
    "changed": re.compile(
        r"\b(?:changed|re-?worded|revised|rewritten|re-?written|updated|expanded|broadened"
        r"|narrowed|modified|amended|re-?categori[sz]ed|tightened|softened|strengthened)\b",
        re.I,
    ),
}

# Words that describe what an 8-K reports. Matched as word prefixes ("appoint" covers
# appointed/appointment), in the claim and in the filing excerpt.
EVENT_TERMS = (
    "appoint", "depart", "resign", "retire", "elect", "terminat", "dismiss", "fired",
    "hire", "successor", "succeed", "director", "officer", "chief executive", "ceo",
    "cfo", "coo", "president", "chair", "controller", "treasurer", "compensat", "bonus",
    "severance", "salary", "award", "agreement", "credit", "loan", "debt", "acqui",
    "merger", "dividend", "repurchase", "buyback", "layoff", "restructur", "impairment",
    "settlement", "lawsuit", "guidance", "bankrupt", "restat", "cybersecurity", "breach",
)  # fmt: skip
_TERMS_RE = re.compile(r"\b(" + "|".join(re.escape(t) for t in EVENT_TERMS) + r")", re.I)
# The item headings inside an excerpt ("Item 5.02. Departure of Directors or Certain
# Officers; ..."): they name the item, they don't report what happened.
_ITEM_HEADING = re.compile(r"\bItem\s+\d\.\d{2}\.?\s+[A-Z][^.]{0,300}?\.(?=\s|$)")
_ITEM_CODE = re.compile(r"\b(?:Items?\s+)?\d\.\d{2}\b", re.I)


@dataclass
class Finding:
    text: str
    detail: str


@dataclass
class ConsistencyReport:
    status_mismatches: list[Finding] = field(default_factory=list)
    eight_k_rewrites: list[Finding] = field(default_factory=list)
    checked: bool = False  # the answer cited a labeled risk or an 8-K

    def summary(self) -> dict[str, int]:
        return {
            "status_mismatches": len(self.status_mismatches),
            "eight_k_rewrites": len(self.eight_k_rewrites),
        }


def asserted_statuses(text: str) -> set[CompareStatus]:
    t = _HYPHENS.sub("-", text)
    found: set[CompareStatus] = set()
    m = STATUS_PATTERNS["unchanged"]
    if m.search(t):
        found.add("unchanged")
        t = m.sub(" ", t)  # "not changed" must not also count as "changed"
    for status in ("new", "removed", "changed"):
        if STATUS_PATTERNS[status].search(t):
            found.add(status)
    return found


def _cited(text: str, sources: list[Evidence]) -> list[Evidence]:
    nums = dict.fromkeys(int(n) for n in _NUM_RE.findall(text))
    return [sources[n - 1] for n in nums if 1 <= n <= len(sources)]


def _plain(text: str) -> str:
    return " ".join(_MARKDOWN.sub(" ", _NUM_RE.sub(" ", text)).split())


def _status_conflict(
    text: str, inherited: set[CompareStatus], sources: list[Evidence]
) -> str | None:
    labels = {e.compare_status for e in _cited(text, sources) if e.compare_status}
    if not labels:
        return None
    said = asserted_statuses(text) or inherited
    if said and not said & labels:
        return f"says {'/'.join(sorted(said))}; the comparison labels it {'/'.join(sorted(labels))}"
    return None


def _sentences(line: str) -> list[tuple[int, int]]:
    spans, start = [], 0
    for m in _SENTENCE_END.finditer(line):
        spans.append((start, m.start()))
        start = m.end()
    return [*spans, (start, len(line))]


def check_comparison_statuses(
    answer: str, sources: list[Evidence], report: ConsistencyReport
) -> str:
    if not any(e.compare_status for e in sources):
        return answer
    report.checked = True
    lines = answer.split("\n")
    drop: set[int] = set()
    lead: tuple[int, int, set[CompareStatus]] | None = None  # line, indent (-1: not a list item)
    children: dict[int, list[int]] = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        item = _LIST_ITEM.match(line)
        indent = len(item.group(1)) if item else -1
        under = lead is not None and item is not None and indent > lead[1]
        inherited = lead[2] if lead is not None and under else set()
        if lead is not None and under and _NUM_RE.search(line):
            children.setdefault(lead[0], []).append(i)
        if not under:
            lead = None
        whole = stripped.startswith("|") or item is not None  # a row or a bullet is one unit
        units = [(0, len(line))] if whole else _sentences(line)
        kept = []
        for a, b in units:
            conflict = _status_conflict(line[a:b], inherited, sources)
            if conflict:
                report.status_mismatches.append(Finding(_plain(line[a:b]), conflict))
            else:
                kept.append(line[a:b].strip())
        if len(kept) < len(units):
            if whole or not any(kept):
                drop.add(i)
            else:
                lines[i] = " ".join(kept)
        if _plain(stripped).endswith(":") and not _NUM_RE.search(line):
            lead = (i, indent, asserted_statuses(line))
    for lead_line, kids in children.items():
        if all(k in drop for k in kids):
            drop.add(lead_line)  # "remained unchanged, including:" with nothing left under it
    return "\n".join(line for i, line in enumerate(lines) if i not in drop)


def _grounding(e: Evidence) -> str:
    return _ITEM_HEADING.sub(" ", e.snippet)


def _items_of(e: Evidence) -> list[tuple[str, str]]:
    """(code, official description) of an 8-K source, from its title."""
    _, _, items = e.title.partition(": ")
    out = []
    for part in items.split("; "):
        code = part.split(" ", 1)[0]
        if code in ITEM_DESCRIPTIONS:
            out.append((code, ITEM_DESCRIPTIONS[code]))
    return out


def _day(d: date | datetime | None) -> str:
    return f"{d.day} {d:%b %Y}" if d else "on its filing date"


def _unsupported_terms(text: str, cited: list[Evidence]) -> list[str]:
    claim = _HYPHENS.sub("-", _plain(text))
    for e in cited:  # the official item names may be quoted as names
        for _, desc in _items_of(e):
            claim = re.sub(re.escape(desc), " ", claim, flags=re.I)
    claim = _ITEM_CODE.sub(" ", claim)
    ground = " ".join(_grounding(e) for e in cited)
    terms = dict.fromkeys(m.group(1).lower() for m in _TERMS_RE.finditer(claim))
    return [t for t in terms if not re.search(rf"\b{re.escape(t)}", ground, re.I)]


def _item_wording(cited: list[Evidence], cites: str, sentence: bool) -> str:
    items = [f"Item {code} ({desc})" for e in cited for code, desc in _items_of(e)]
    names = "; ".join(dict.fromkeys(items)) or "its items"
    if sentence:
        return f"The 8-K filed {_day(cited[0].filed_or_published)} reports {names} {cites}."
    return f"Reported under {names} {cites}"


def check_8k_descriptions(answer: str, sources: list[Evidence], report: ConsistencyReport) -> str:
    if not any(e.source_type == "8k" for e in sources):
        return answer
    report.checked = True
    out = []
    for line in answer.split("\n"):
        if line.strip().startswith("|"):  # table: check each cell
            cells = line.split("|")
            out.append("|".join(_fix_8k_unit(c, sources, report, sentence=False) for c in cells))
            continue
        item = _LIST_ITEM.match(line)
        prefix, body = (item.group(0), line[item.end() :]) if item else ("", line)
        spans = [(0, len(body))] if item else _sentences(body)
        parts = [_fix_8k_unit(body[a:b], sources, report, sentence=True) for a, b in spans]
        out.append(prefix + " ".join(p.strip() if len(parts) > 1 else p for p in parts))
    return "\n".join(out)


def _fix_8k_unit(
    unit: str, sources: list[Evidence], report: ConsistencyReport, *, sentence: bool
) -> str:
    cited = _cited(unit, sources)
    if not cited or any(e.source_type != "8k" for e in cited):
        return unit
    missing = _unsupported_terms(unit, cited)
    if not missing:
        return unit
    cites = "".join(f"[{n}]" for n in dict.fromkeys(_NUM_RE.findall(unit)))
    report.eight_k_rewrites.append(
        Finding(_plain(unit), "not in the filing excerpt: " + ", ".join(missing))
    )
    lead = unit[: len(unit) - len(unit.lstrip())]
    trail = unit[len(unit.rstrip()) :]
    return lead + _item_wording(cited, cites, sentence) + trail


def check_answer(answer: str, sources: list[Evidence]) -> tuple[str, ConsistencyReport]:
    """`sources[n-1]` is source [n] (the synthesizer's numbering)."""
    report = ConsistencyReport()
    answer = check_comparison_statuses(answer, sources, report)
    answer = check_8k_descriptions(answer, sources, report)
    return answer, report
