"""Versioned prompt files. The text is part of the LLM cache key, so editing a prompt
invalidates its cached responses automatically."""

from functools import cache
from importlib.resources import files


@cache
def load(name: str) -> str:
    return files(__package__).joinpath(f"{name}.md").read_text(encoding="utf-8").strip()
