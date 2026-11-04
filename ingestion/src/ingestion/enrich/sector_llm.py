"""LLM sector signal: classify the Item 1 business description (BUILD_SPEC 3.4, signal 2)."""

from typing import Literal

from pydantic import BaseModel, Field

from core.llm import GroqClient
from ingestion import prompts

GicsSector = Literal[
    "Information Technology",
    "Communication Services",
    "Consumer Discretionary",
    "Consumer Staples",
    "Energy",
    "Financials",
    "Health Care",
    "Industrials",
    "Materials",
    "Real Estate",
    "Utilities",
]

# The opening of Item 1 states what the company does; more text costs tokens, not accuracy.
MAX_WORDS = 1_200


class SectorClassification(BaseModel):
    sector: GicsSector
    sub_industry: str
    confidence: float = Field(ge=0, le=1)
    rationale: str


async def classify_sector(
    llm: GroqClient, company_name: str, item_1_text: str, *, cik: int
) -> SectorClassification:
    excerpt = " ".join(item_1_text.split()[:MAX_WORDS])
    return await llm.structured(
        "fast",
        SectorClassification,
        [
            {"role": "system", "content": prompts.load("sector_classification")},
            {"role": "user", "content": f"Company: {company_name}\n\nItem 1. Business:\n{excerpt}"},
        ],
        stage="sector_llm",
        cik=cik,
    )
