"""Zero-token inspection of cached risk-tag responses."""

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from core.db.schema import llm_cache, risk_factors
from ingestion.enrich.risk_tagger import _words


async def replay_cached_tags(engine: AsyncEngine) -> dict[str, int]:
    """Count flagged rows with an unambiguous cached normalized-prefix echo.

    The response cache does not retain the original batch listing or CIK. Rows are
    credited only when the echo has exactly one matching flagged heading globally.
    """
    async with engine.connect() as conn:
        flagged = (
            await conn.execute(
                select(risk_factors.c.id, risk_factors.c.heading).where(
                    risk_factors.c.needs_review.is_(True)
                )
            )
        ).all()
        cached = (
            await conn.execute(
                select(llm_cache.c.response).where(llm_cache.c.response.has_key("tags"))
            )
        ).scalars()

        flagged_by_prefix: dict[str, list[str]] = defaultdict(list)
        for risk_id, heading in flagged:
            words = _words(heading, 6)
            for length in range(1, len(words.split()) + 1):
                flagged_by_prefix[" ".join(words.split()[:length])].append(risk_id)

        credited: set[str] = set()
        echo_count = 0
        for response in cached:
            tags: Any = response.get("tags") if isinstance(response, dict) else None
            if not isinstance(tags, list):
                continue
            for tag in tags:
                if not isinstance(tag, dict):
                    continue
                echo = _words(str(tag.get("heading_start") or ""), 6)
                if not echo:
                    continue
                echo_count += 1
                matches = flagged_by_prefix.get(echo, [])
                if len(matches) == 1:
                    credited.add(matches[0])

    return {
        "flagged_rows": len(flagged),
        "cached_echoes": echo_count,
        "would_validate": len(credited),
        "not_creditable": len(flagged) - len(credited),
    }
