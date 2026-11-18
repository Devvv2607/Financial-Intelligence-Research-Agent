# Live demo runs

Unedited CLI transcripts (`python -m agent ask ...`) of the BUILD_SPEC §7 demo queries,
run live on gpt-oss-120b. One folder per day. Each transcript shows the plan, the mode of
each step (direct or agentic), the tool calls, the critic's verdict, the streamed answer,
tokens per node and the sources.

They are kept as evidence of how the agent behaved at that point. Later fixes are not
applied to earlier transcripts, and each fix is logged in `docs/DECISIONS.md`.

| Day | Demo | Transcript | Tokens | Notes |
|---|---|---|---|---|
| 2026-11-04 | 2 Apple risk changes | `demo2-apple-risk-changes.txt` | 7,163 | after the year-order / citation / fiscal-period fixes (D-035, D-036); the first, wrong run was not saved |
| 2026-11-04 | 1 NVIDIA vs AMD + news | `demo1-nvda-amd-run1.txt` | 11,801 | source cap used up by AMD: NVIDIA and news uncited (fixed, D-035) |
| 2026-11-04 | 1 (re-run) | `demo1-nvda-amd-run2.txt` | 6,712 | round-robin sources; analyzer/planner/critic served from the dev cache |
| 2026-11-04 | 4 Boeing 8-Ks | `demo4-boeing-8k.txt` | 9,565 | |
| 2026-11-04 | 5 "How risky is the bank?" | `demo5-bank-clarify-pause.txt`, `demo5-bank-resume.txt` | 14,107 | interrupt, then resume from a new process; the CLI crash on pause and the answer's own risk verdict were fixed afterwards |
| 2026-11-04 | 7 "Now do the same for Intel." | `demo7-intel-followup.txt` | 27,652 | follow-up in demo 2's thread; critic over-asked (2 replans, fixed D-036); one wrong citation |
| 2026-11-05 | 6 "Should I buy Tesla?" | `demo6-tesla-advice.txt` | 0 | guardrail, no LLM call |
| 2026-11-05 | 3 Healthcare revenue growth | `demo3-healthcare-growth.txt` | 15,680 | 6 steps (4 direct, 2 agentic after their dependencies); citation precision 1.0. UNH's "regulatory risk" is a forward-looking-statements notice extracted as a risk factor (planner didn't use the `category` filter) |
| 2026-11-05 | 2 Apple risk changes | `demo2-apple-risk-changes.txt` | 19,043 | correct; precision 1.0; planner went agentic and the specialist repeated an identical `search_filings` call 3x (vs 7,163 direct yesterday) |
| 2026-11-05 | 7 "Now do the same for Intel." | `demo7-intel-followup.txt` | 19,139 | follow-up in demo 2's thread; 1 replan; precision 0.727 (2 vague claims marked [unverified]). "New" list wrong: Intel FY2024 lost 7 risk factors in the re-extraction (open regression) |
| 2026-11-17 | 8 "Did the NVIDIA CEO resign?" | `demo8-nvda-ceo-resign.txt` (+ raw `.sse.txt`) | 6,256 | first run through the API, not the CLI; after the sample refresh (NVDA 8-K Item 5.02, 12 Nov). Correct, cited, states data date. Plan used news only, so the 8-K went uncited; cites an opinion letter and Breitbart; precision 0.571 (1 filler claim [unverified]) |
| 2026-11-17 | Replay: "Should I buy Tesla?" | `replay-tesla-advice.txt` | 0 | recorded for the demo chips (production API); guardrail; **saved** to `replays/` |
| 2026-11-17 | Replay: NVIDIA vs AMD risks | `replay-nvda-amd-risks.txt` | 13,627 | precision 1.0 (10/10); news freshness line correct; **saved**. Two "supports competition" news rows are loose (stock move, leadership scandal) |
| 2026-11-17 | Replay: Apple risk changes | `replay-apple-risk-changes-FAILED.txt` | 8,574 | precision 1.0 but **failed the gate, not saved**: calls defects/legal/cybersecurity risks "unchanged" ("identical language") although its own cited FY2025/FY2026 texts differ; the comparison said 20 materially changed |
| 2026-11-17 | Replay: healthcare growth + regulatory risks | `replay-healthcare-growth.txt` | 27,346 | precision 1.0 (17/17); growth math checked (UNH +11.8%, JNJ +6.0%); **saved** |
| 2026-11-17 | Replay: Boeing 8-Ks this year | `replay-boeing-8k-events-HELD.txt` | 7,942 | precision 1.0; freshness line correct; **held, not saved**: the 21 Aug 5.02 row restates the item title ("new directors/officers"); the filing appoints Ryan L. Shedd as SVP and Controller |
| 2026-11-17 | Replay: "How risky is the bank?" → JPMorgan | `replay-bank-clarification.txt` | 10,261 | clarification flow (BAC/JPM/C/WFC offered); precision 1.0 (13/13); **saved** |
| 2026-11-18 | Replay re-record: Apple risk changes | `2026-11-18/replay-apple-risk-changes-FAILED.txt` | 11,144 | after D-051: status_mismatches 0 and stated counts match the tool (1 new / 0 removed / 20 changed / 7 unchanged), but precision 0.786 (11/14): 3 FY2025-side claims ("did not mention AI", "differing from the FY 2025 phrasing") marked [unverified]. **Failed the gate, not saved** |
| 2026-11-18 | Replay re-record: Boeing 8-Ks this year | `2026-11-18/replay-boeing-8k-events.txt` | 8,707 | precision 1.0 (13/13); freshness correct; status_mismatches 0; eight_k_rewrites 1: the draft's 5.02 note "(board departures and officer appointments)" was replaced by the item's official name (D-051). **Saved** to `replays/` |

Data caveats:
- **2026-11-04:** the stored risk factors still had the "Table of Contents" split from
  the first extraction.
- **2026-11-05:** the sample was re-extracted and re-tagged (v2 taxonomy) before demos 3,
  2 and 7. That fixed the split, but Intel FY2024 lost its first 7 risk factors.

See the navigation-link entry under "Adaptations" in `docs/DECISIONS.md`.
