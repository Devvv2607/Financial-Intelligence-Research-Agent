"""Versioned prompt files, shared by ingestion and the agent.

Prompts live as `<package>/<name>.md` and start with `<!-- prompt: <name> · version <N> -->`.
The text is part of the LLM cache key, so editing a prompt invalidates its cached
responses; outputs that matter record `version()` so stale ones can be redone.
"""

import re
from functools import cache
from importlib.resources import files

_VERSION_RE = re.compile(r"<!--\s*prompt:\s*([\w_]+)\s*·\s*version\s*(\d+)\s*-->")


@cache
def load(package: str, name: str) -> str:
    return files(package).joinpath(f"{name}.md").read_text(encoding="utf-8").strip()


@cache
def version(package: str, name: str) -> str:
    """e.g. 'risk_tagging:v2' — the label stored next to outputs of this prompt."""
    m = _VERSION_RE.search(load(package, name))
    if m is None or m.group(1) != name:
        raise ValueError(f"prompt {name!r} in {package} has no valid version header")
    return f"{name}:v{m.group(2)}"
