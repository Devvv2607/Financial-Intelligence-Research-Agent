"""Filing HTML -> ordered text blocks that remember their emphasis.

Risk-factor headings are marked by *styling* (bold/italic/underline), not by tags,
and filers express that differently: `<b>`, `<strong>`, or inline CSS such as
`font-weight:700` on a `<span>` (Workiva/DFIN inline-XBRL output). So we keep, per
block, how many characters were bold/italic/underlined and the leading bold run.
"""

import re
from collections import Counter
from dataclasses import dataclass

from lxml import etree, html

BLOCK_TAGS = {
    "div", "p", "li", "td", "th", "tr", "table", "ul", "ol", "h1", "h2", "h3", "h4", "h5",
    "h6", "body", "center", "blockquote", "section", "article", "br", "hr",
}  # fmt: skip
SKIP_TAGS = {"script", "style", "head", "title", "noscript"}
_BOLD_RE = re.compile(r"font-weight\s*:\s*(bold|bolder|[6-9]00)", re.I)
_NOT_BOLD_RE = re.compile(r"font-weight\s*:\s*(normal|lighter|[1-5]00)", re.I)
_ITALIC_RE = re.compile(r"font-style\s*:\s*(italic|oblique)", re.I)
_NOT_ITALIC_RE = re.compile(r"font-style\s*:\s*normal", re.I)
_UNDERLINE_RE = re.compile(r"text-decoration[^;]*underline", re.I)
_HIDDEN_RE = re.compile(r"display\s*:\s*none", re.I)
_FONT_SIZE_RE = re.compile(r"font-size\s*:\s*([\d.]+)\s*(pt|px)", re.I)
_XML_DECL_RE = re.compile(r"^\s*<\?xml[^>]*\?>", re.I)
_WS_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"^[a-z]+$")
_WORD_BOUNDARIES = frozenset(
    [
        "a",
        "an",
        "are",
        "and",
        "as",
        "at",
        "be",
        "by",
        "company",
        "ensure",
        "financial",
        "for",
        "from",
        "in",
        "of",
        "risk",
        "results",
        "subject",
        "the",
        "to",
        "we",
        "with",
    ]
)
_WORD_CONTINUATIONS = frozenset(
    [
        "able",
        "acial",
        "al",
        "ance",
        "ancial",
        "ary",
        "cial",
        "ed",
        "ence",
        "ent",
        "er",
        "es",
        "ful",
        "ial",
        "ing",
        "ion",
        "ional",
        "ity",
        "less",
        "ly",
        "ment",
        "ness",
        "ous",
        "s",
        "t",
        "tion",
        "ual",
    ]
)


@dataclass(frozen=True)
class Style:
    bold: bool = False
    italic: bool = False
    underline: bool = False
    font_pt: float = 0.0

    @property
    def emphasized(self) -> bool:
        return self.bold or self.italic or self.underline


@dataclass
class Block:
    text: str
    bold_chars: int
    italic_chars: int
    underline_chars: int
    lead_emphasis: str  # leading run of emphasized text (for "inline" headings)
    font_pt: float  # largest font size in the block (0 if unknown)

    @property
    def emphasized(self) -> bool:
        n = max(len(self.text.replace(" ", "")), 1)
        return max(self.bold_chars, self.italic_chars, self.underline_chars) / n >= 0.8


def _style_for(el: etree._Element, parent: Style) -> Style:
    tag = el.tag.lower() if isinstance(el.tag, str) else ""
    css = el.get("style", "") or ""
    bold = parent.bold or tag in ("b", "strong") or tag.startswith("h")
    if _BOLD_RE.search(css):
        bold = True
    elif _NOT_BOLD_RE.search(css):
        bold = False
    italic = parent.italic or tag in ("i", "em")
    if _ITALIC_RE.search(css):
        italic = True
    elif _NOT_ITALIC_RE.search(css):
        italic = False
    underline = parent.underline or tag == "u" or bool(_UNDERLINE_RE.search(css))
    font_pt = parent.font_pt
    if m := _FONT_SIZE_RE.search(css):
        size = float(m.group(1))
        font_pt = size if m.group(2).lower() == "pt" else size * 0.75
    return Style(bold, italic, underline, font_pt)


class _Builder:
    def __init__(self) -> None:
        self.blocks: list[Block] = []
        self._runs: list[tuple[str, Style]] = []

    def add(self, text: str | None, style: Style) -> None:
        if text:
            self._runs.append((text, style))

    @staticmethod
    def _needs_space(previous: str, current: str) -> bool:
        if not previous or not current or previous[-1].isspace() or current[0].isspace():
            return False
        if not (previous[-1].isalnum() and current[0].isalnum()):
            return False
        if previous[-1].islower() and current[0].isupper():
            return True
        left_match = re.search(r"[a-z]+$", previous.lower())
        right_match = re.match(r"[a-z]+", current.lower())
        if left_match is None or right_match is None:
            return False
        left, right = left_match.group(), right_match.group()
        return (
            bool(_WORD_RE.fullmatch(left) and _WORD_RE.fullmatch(right))
            and left in _WORD_BOUNDARIES
            and right not in _WORD_CONTINUATIONS
        )

    def flush(self) -> None:
        if not self._runs:
            return
        text, bold, italic, underline, prefix, in_prefix = "", 0, 0, 0, "", True
        font_pt = 0.0
        for raw, style in self._runs:
            chunk = raw.replace("\xa0", " ")
            if text and self._needs_space(text, chunk):
                text += " "
            text += chunk
            visible = len(chunk.replace(" ", "").replace("\n", ""))
            bold += visible if style.bold else 0
            italic += visible if style.italic else 0
            underline += visible if style.underline else 0
            if visible:
                font_pt = max(font_pt, style.font_pt)
            if in_prefix and (style.emphasized or not chunk.strip()):
                prefix += chunk
            elif chunk.strip():
                in_prefix = False
        self._runs = []
        clean = _WS_RE.sub(" ", text).strip()
        if clean:
            self.blocks.append(
                Block(clean, bold, italic, underline, _WS_RE.sub(" ", prefix).strip(), font_pt)
            )


def _walk(el: etree._Element, style: Style, out: _Builder) -> None:
    tag = el.tag.lower() if isinstance(el.tag, str) else ""
    if not tag or tag in SKIP_TAGS or _HIDDEN_RE.search(el.get("style", "") or ""):
        out.add(el.tail, style)  # tail belongs to the parent's flow
        return
    own = _style_for(el, style)
    is_block = tag in BLOCK_TAGS or tag.split(":")[-1] == "header"
    if is_block:
        out.flush()
    out.add(el.text, own)
    for child in el:
        _walk(child, own, out)
    if is_block:
        out.flush()
    out.add(el.tail, style)


def decode_filing(content: bytes) -> str:
    """SEC documents are mostly UTF-8; older ones are Windows-1252. Never guess silently."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("cp1252", errors="replace")
    # lxml rejects str input that carries an XML encoding declaration (inline XBRL has one).
    return _XML_DECL_RE.sub("", text, count=1)


def html_to_blocks(content: bytes | str) -> list[Block]:
    root = html.fromstring(decode_filing(content) if isinstance(content, bytes) else content)
    builder = _Builder()
    _walk(root, Style(), builder)
    builder.flush()
    return drop_page_furniture(builder.blocks)


# Page-navigation links. Dropped regardless of styling: several filers (NVIDIA, AMD,
# Tesla, Boeing, UnitedHealth, Intel) underline them, which made them look like headings
# and split risk factors at every page break.
_NAV_RE = re.compile(
    r"(table of contents|back to (the )?(table of )?contents|return to (the )?table of contents"
    r"|go to (the )?table of contents|index to (consolidated )?financial statements)",
    re.I,
)


def drop_page_furniture(blocks: list[Block]) -> list[Block]:
    """Remove page numbers and repeated page headers/footers.

    Digits are masked so each page's copy of "Apple Inc. | 2025 Form 10-K | 12" counts
    as the same line. See `_is_furniture` for which repeated lines are kept.
    """

    def key(b: Block) -> str:
        return re.sub(r"\d+", "#", b.text.lower())

    counts = Counter(key(b) for b in blocks if len(b.text) < 100)
    return [
        b
        for b in blocks
        if not re.fullmatch(r"[\divxlc\-\u2013\u2014 .|]+", b.text.lower())  # page numbers, rules
        and not _NAV_RE.fullmatch(b.text.strip())
        and not (len(b.text) < 100 and counts[key(b)] >= 3 and _is_furniture(b))
    ]


def _is_furniture(b: Block) -> bool:
    # A repeated line is page furniture if it carries a page number, or if it is plain
    # small text. Emphasized/large repeated lines may be a real section heading that the
    # filer also uses as a running header (navigation links are handled by _NAV_RE).
    return bool(re.search(r"\d", b.text)) or not (b.emphasized or b.font_pt >= 12)


def blocks_text(blocks: list[Block]) -> str:
    return "\n\n".join(b.text for b in blocks)
