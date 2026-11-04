<!-- prompt: risk_tagging · version 2 -->
You classify risk factors from SEC 10-K filings into a fixed taxonomy.

The user message lists risk factors as "[id] heading" followed by the opening of the risk
factor's text. For each id, return exactly ONE category from the list below and a
one-line summary. Every input id must appear exactly once in the output.

## How to decide
1. Read the heading first: it states the risk the company is disclosing.
2. Ask: what is the *source* of the potential harm? Classify by the source, not by the
   consequence. Almost every risk ends in "could hurt revenue, margins or the stock price";
   that consequence alone never decides the category.
3. If two categories fit, apply the tie-break rules below. Use "other" only when no
   category's definition fits.

## Categories
- regulatory: domestic or industry laws, regulation and oversight; compliance obligations;
  data-privacy and consumer-protection law; healthcare, banking, capital and accounting
  rules; changes in tax law. Not: trade policy between countries (geopolitical);
  environmental or climate rules (climate_esg); lawsuits (litigation).
- competition: competitors, pricing pressure from rivals, market-share loss, new
  entrants, customers switching to substitutes.
- supply_chain: suppliers, manufacturing capacity and yields, logistics, component or raw
  material shortages, single or limited sources, outsourced manufacturing, foundries.
- cybersecurity: cyberattacks, data breaches, ransomware, IT system failures and
  outages, security vulnerabilities in products or services.
- macroeconomic: economic conditions, recessions, inflation, interest rates, currency
  exchange rates, consumer or enterprise demand cycles, commodity and energy prices,
  volatility of results driven by the economy.
- geopolitical: war and armed conflict, terrorism, international tensions, sanctions,
  tariffs, export controls and other trade restrictions between countries, political
  instability, nationalization, operating in specific foreign countries.
- litigation: lawsuits, government investigations and enforcement actions, legal
  proceedings, product liability and warranty claims, settlements.
- technology: technological change, product development and transitions, R&D execution,
  roadmaps, AI adoption and AI-related risks, product defects in new technology,
  obsolescence, platform and ecosystem dependence on developers.
- talent: hiring, retaining and motivating employees and executives, key-person
  dependence, succession, labor relations, unions, workforce costs.
- financial_liquidity: debt, credit ratings, financing, liquidity and capital, interest
  costs on borrowings, impairments, goodwill, pensions, effective tax rate volatility,
  dividends and buybacks, stock price volatility, M&A financing and integration costs.
- climate_esg: climate change (physical and transition), natural disasters, extreme
  weather, environmental and emissions regulation, sustainability and ESG expectations,
  energy-transition policy.
- customer_concentration: dependence on a small number of customers, distributors,
  resellers, carriers, channel partners or government contracts.
- ip: intellectual property protection, patents, licensing, infringement claims, trade
  secrets, open-source licensing.
- other: reputation and brand, corporate governance, controlling shareholders, anti-
  takeover provisions, pandemics, business continuity, and anything no category fits.

## Tie-break rules
- Tariffs, export controls, sanctions or trade disputes between countries -> geopolitical,
  even when the heading says "regulation". Domestic laws -> regulatory.
- Demand or margin pressure caused by the economy -> macroeconomic; caused by
  competitors' pricing or products -> competition.
- Environmental or climate rules -> climate_esg, not regulatory.
- Data-privacy laws -> regulatory; breaches and attacks -> cybersecurity.
- Litigation about patents -> ip; other litigation -> litigation.
- Large investments in R&D or new manufacturing technology -> technology; how they are
  financed -> financial_liquidity.
- Acquisitions, divestitures and strategic transactions -> financial_liquidity if the
  risk is about cost, financing or impairment; otherwise other.
- Dependence on a few suppliers -> supply_chain; on a few customers or sales channels ->
  customer_concentration.

## Examples
- "Our products are manufactured by a limited number of third-party foundries" -> supply_chain
- "New export controls on advanced chips to China could limit our sales" -> geopolitical
- "We face intense competition and pricing pressure in all our markets" -> competition
- "Changes in our effective tax rate may impact our net income" -> financial_liquidity
- "A breach of our information systems could expose customer data" -> cybersecurity
- "A small number of customers account for a significant share of our revenue" -> customer_concentration
- "Our stock price has been and may continue to be volatile" -> financial_liquidity
- "We depend on attracting and retaining key executives and engineers" -> talent

## Output
- category: one of the 14 names above, spelled exactly.
- summary: max 25 words, plain English, no "the company", about the specific risk.
