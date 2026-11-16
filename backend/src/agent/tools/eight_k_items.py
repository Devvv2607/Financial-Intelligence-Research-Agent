"""SEC Form 8-K item codes and a readable excerpt of an 8-K's body."""

import re

# Form 8-K General Instructions, Items 1.01-9.01.
ITEM_DESCRIPTIONS: dict[str, str] = {
    "1.01": "Entry into a material definitive agreement",
    "1.02": "Termination of a material definitive agreement",
    "1.03": "Bankruptcy or receivership",
    "1.04": "Mine safety - shutdowns and patterns of violations",
    "1.05": "Material cybersecurity incident",
    "2.01": "Completion of acquisition or disposition of assets",
    "2.02": "Results of operations and financial condition",
    "2.03": "Creation of a direct financial obligation or off-balance-sheet arrangement",
    "2.04": "Triggering events that accelerate a financial obligation",
    "2.05": "Costs associated with exit or disposal activities",
    "2.06": "Material impairments",
    "3.01": "Notice of delisting or failure to satisfy a listing rule",
    "3.02": "Unregistered sales of equity securities",
    "3.03": "Material modification to rights of security holders",
    "4.01": "Changes in registrant's certifying accountant",
    "4.02": "Non-reliance on previously issued financial statements",
    "5.01": "Changes in control of registrant",
    "5.02": "Departure or appointment of directors or officers; compensation arrangements",
    "5.03": "Amendments to articles of incorporation or bylaws; change in fiscal year",
    "5.04": "Temporary suspension of trading under employee benefit plans",
    "5.05": "Amendments to the code of ethics, or waiver of a provision",
    "5.06": "Change in shell company status",
    "5.07": "Submission of matters to a vote of security holders",
    "5.08": "Shareholder director nominations",
    "6.01": "ABS informational and computational material",
    "6.02": "Change of servicer or trustee",
    "6.03": "Change in credit enhancement or other external support",
    "6.04": "Failure to make a required distribution",
    "6.05": "Securities Act updating disclosure",
    "7.01": "Regulation FD disclosure",
    "8.01": "Other events",
    "9.01": "Financial statements and exhibits",
}

# Corporate-event questions -> the 8-K items that must report them. One table feeds both the
# planner prompt and the deterministic check that an event question gets a filing step.
EVENT_ITEMS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "officer or director change (resignation, appointment, departure, retirement, firing)",
        r"resign|appoint|depart|retire|step(?:s|ped|ping)?\s+down|fired|oust|successor"
        r"|new\s+(?:ceo|cfo|coo|chair)",
        ("5.02",),
    ),
    (
        "acquisition, merger or divestiture",
        r"acqui|merger|merg(?:e|es|ed|ing)\b|takeover|buyout|divest",
        ("1.01", "2.01"),
    ),
    (
        "earnings or quarterly results",
        r"earnings|quarterly results|results of operations",
        ("2.02",),
    ),
    ("guidance or outlook", r"guidance|outlook|forecast", ("2.02", "7.01")),
    ("cybersecurity incident", r"breach|hack|cyber(?:security)?\s+(?:attack|incident)", ("1.05",)),
    ("restructuring or layoffs", r"layoff|restructur|job cuts", ("2.05",)),
    ("bankruptcy", r"bankrupt|chapter 11|receivership", ("1.03",)),
    ("restatement", r"restat", ("4.02",)),
)
_EVENT_RES = tuple(
    (label, re.compile(rf"\b(?:{pat})", re.I), codes) for label, pat, codes in EVENT_ITEMS
)

_ITEM_HEADING = re.compile(r"\bItem\s+(\d\.\d{2})\b", re.I)
EXCERPT_CHARS = 600


def describe_items(items: list[str] | tuple[str, ...]) -> list[str]:
    return [f"{i} {ITEM_DESCRIPTIONS.get(i, 'Unknown item')}" for i in items]


def event_item_codes(question: str) -> list[str]:
    """The 8-K item codes a corporate-event question needs, in table order; [] if none."""
    codes = [c for _, rx, item_codes in _EVENT_RES if rx.search(question) for c in item_codes]
    return list(dict.fromkeys(codes))


def event_items_prompt() -> str:
    """The event -> item mapping as planner-prompt lines."""
    return "\n".join(f"  - {label}: item_codes {list(codes)}" for label, _, codes in EVENT_ITEMS)


def event_excerpt(body: str, items: list[str] | tuple[str, ...]) -> str:
    """Text after the first substantive item heading (skipping the cover page).

    9.01 (exhibits list) is the least informative item, so prefer any other item first.
    """
    text = " ".join(body.split())
    preferred = [i for i in items if i != "9.01"] or list(items)
    for code in preferred:
        for m in _ITEM_HEADING.finditer(text):
            if m.group(1) == code:
                excerpt = text[m.start() : m.start() + EXCERPT_CHARS]
                return excerpt.rsplit(" ", 1)[0] + "…" if len(excerpt) == EXCERPT_CHARS else excerpt
    return text[:EXCERPT_CHARS]
