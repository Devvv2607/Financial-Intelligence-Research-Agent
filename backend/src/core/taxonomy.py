"""The risk-factor taxonomy, shared by ingestion (tagging) and the agent (filters)."""

from typing import Literal, get_args

RiskCategory = Literal[
    "regulatory",
    "competition",
    "supply_chain",
    "cybersecurity",
    "macroeconomic",
    "geopolitical",
    "litigation",
    "technology",
    "talent",
    "financial_liquidity",
    "climate_esg",
    "customer_concentration",
    "ip",
    "other",
]
RISK_CATEGORIES: tuple[str, ...] = get_args(RiskCategory)
