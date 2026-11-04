"""Rule-based sector signal: SIC code -> GICS-style sector (BUILD_SPEC 3.4, signal 1).

SIC (1987) predates GICS, so some mappings are inherently lossy: e.g. 7370 (computer
services) covers both Alphabet (GICS: Communication Services) and software firms, and
7389 (business services) covers Visa and Mastercard (GICS: Financials). We deliberately
keep the rules simple and *don't* special-case companies: those disagreements are what
the LLM signal and the `sector_conflict` flag are for.
"""

Sector = str

# Specific codes, checked before ranges (where SIC's grouping crosses GICS sectors).
_SPECIFIC: dict[int, Sector] = {
    1531: "Consumer Discretionary",  # operative builders (homebuilders)
    2833: "Health Care",
    2834: "Health Care",
    2835: "Health Care",
    2836: "Health Care",
    2840: "Consumer Staples",  # soap, detergents, cleaning
    2842: "Consumer Staples",
    2844: "Consumer Staples",  # perfumes, cosmetics, toiletries
    3021: "Consumer Discretionary",  # rubber & plastics footwear
    3630: "Consumer Discretionary",  # household appliances
    3711: "Consumer Discretionary",  # motor vehicles
    3714: "Consumer Discretionary",  # motor vehicle parts
    3812: "Industrials",  # defense electronics / navigation
    3826: "Health Care",  # laboratory analytical instruments
    4922: "Energy",  # natural gas transmission (pipelines)
    4923: "Energy",
    4953: "Industrials",  # refuse systems
    5122: "Health Care",  # drug wholesalers
    5141: "Consumer Staples",  # grocery wholesalers
    5331: "Consumer Staples",  # variety stores (Walmart, Costco)
    5399: "Consumer Staples",
    5912: "Consumer Staples",  # drug stores
    6324: "Health Care",  # hospital & medical service plans (managed care)
    6798: "Real Estate",  # REITs
    7311: "Communication Services",  # advertising agencies
    8731: "Health Care",  # commercial physical & biological research
}

# (low, high, sector) inclusive ranges, first match wins.
_RANGES: list[tuple[int, int, Sector]] = [
    (100, 999, "Consumer Staples"),
    (1000, 1099, "Materials"),
    (1200, 1399, "Energy"),
    (1400, 1499, "Materials"),
    (1500, 1799, "Industrials"),
    (2000, 2199, "Consumer Staples"),
    (2200, 2399, "Consumer Discretionary"),
    (2400, 2499, "Materials"),
    (2500, 2599, "Consumer Discretionary"),
    (2600, 2699, "Materials"),
    (2700, 2799, "Communication Services"),
    (2800, 2899, "Materials"),
    (2900, 2999, "Energy"),
    (3000, 3399, "Materials"),
    (3400, 3569, "Industrials"),
    (3570, 3579, "Information Technology"),
    (3580, 3599, "Industrials"),
    (3600, 3689, "Information Technology"),
    (3690, 3699, "Industrials"),
    (3700, 3799, "Industrials"),
    (3800, 3839, "Information Technology"),
    (3840, 3859, "Health Care"),
    (3860, 3899, "Information Technology"),
    (3900, 3999, "Consumer Discretionary"),
    (4000, 4799, "Industrials"),
    (4800, 4899, "Communication Services"),
    (4900, 4999, "Utilities"),
    (5000, 5099, "Industrials"),
    (5100, 5139, "Industrials"),
    (5140, 5149, "Consumer Staples"),
    (5150, 5169, "Industrials"),
    (5170, 5179, "Energy"),
    (5180, 5199, "Industrials"),
    (5400, 5499, "Consumer Staples"),
    (5200, 5999, "Consumer Discretionary"),
    (6500, 6553, "Real Estate"),
    (6000, 6799, "Financials"),
    (7000, 7299, "Consumer Discretionary"),
    (7370, 7379, "Information Technology"),
    (7300, 7399, "Industrials"),
    (7500, 7599, "Consumer Discretionary"),
    (7800, 7899, "Communication Services"),
    (7900, 7999, "Consumer Discretionary"),
    (8000, 8099, "Health Care"),
    (8200, 8299, "Consumer Discretionary"),
    (8700, 8799, "Industrials"),
]


def sic_to_sector(sic: int | None) -> Sector | None:
    if sic is None:
        return None
    if sic in _SPECIFIC:
        return _SPECIFIC[sic]
    for low, high, sector in _RANGES:
        if low <= sic <= high:
            return sector
    return None
