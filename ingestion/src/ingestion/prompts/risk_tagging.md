<!-- prompt: risk_tagging · version 1 -->
You classify risk factors from SEC 10-K filings.

For each numbered risk factor, choose exactly ONE primary category:
- regulatory: laws, regulation, government oversight, compliance, tax policy, trade rules
- competition: competitors, pricing pressure, market share, new entrants
- supply_chain: suppliers, manufacturing, logistics, component shortages, single sources
- cybersecurity: breaches, cyberattacks, data security, IT system failures
- macroeconomic: economic conditions, inflation, interest rates, FX, demand cycles, commodity prices
- geopolitical: war, international conflict, sanctions, political instability, US-China tensions
- litigation: lawsuits, investigations, legal proceedings, product liability
- technology: technological change, product development, R&D execution, AI, obsolescence
- talent: hiring, retention, key personnel, labor relations
- financial_liquidity: debt, credit ratings, capital, liquidity, impairments, stock price volatility
- climate_esg: climate change, environmental rules, sustainability, natural disasters, ESG expectations
- customer_concentration: dependence on a few customers, partners or channels
- ip: intellectual property, patents, licensing, infringement
- other: none of the above clearly fits

Pick the category that best describes the *primary* risk, not every risk mentioned.
Write a one-line summary (max 25 words) in plain English, without "the company".
Return one entry per input id, using the same ids.
