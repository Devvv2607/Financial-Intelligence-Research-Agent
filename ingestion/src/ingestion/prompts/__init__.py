"""Ingestion prompts (risk tagging, sector classification, section boundaries)."""

from core import prompts as _prompts


def load(name: str) -> str:
    return _prompts.load(__name__, name)


def version(name: str) -> str:
    return _prompts.version(__name__, name)
