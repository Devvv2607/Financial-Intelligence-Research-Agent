"""Versioned prompt files. The text is part of the LLM cache key, so editing a prompt
invalidates its cached responses automatically. Each file starts with a header
`<!-- prompt: <name> · version <N> -->`; tags record that version so stale ones can be
redone without wiping anything."""

import re
from functools import cache
from importlib.resources import files

_VERSION_RE = re.compile(r"<!--\s*prompt:\s*([\w_]+)\s*·\s*version\s*(\d+)\s*-->")


@cache
def load(name: str) -> str:
    return files(__package__).joinpath(f"{name}.md").read_text(encoding="utf-8").strip()


@cache
def version(name: str) -> str:
    """'risk_tagging:v2' — the label stored next to every tag produced by this prompt."""
    m = _VERSION_RE.search(load(name))
    if m is None or m.group(1) != name:
        raise ValueError(f"prompt {name!r} has no valid version header")
    return f"{name}:v{m.group(2)}"
