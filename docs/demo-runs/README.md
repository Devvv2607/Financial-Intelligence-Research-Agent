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

Data caveat for 2026-11-04: stored risk factors still had the "Table of Contents" split
from the first extraction (see the navigation-link entry under "Adaptations" in
`docs/DECISIONS.md`); the sample is re-extracted before the next runs.
