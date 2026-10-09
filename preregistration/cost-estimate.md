---
title: 'Codex campaign cost estimate (benchmark run)'
campaign: codex
created: '2026-10-06'
status: 'awaiting a decision by the authors (Archivist quota timing; agent billing mode confirmation)'
amended: '2026-10-06 (judges on plan allowance, pre registration amendment 1)'
---

# Codex campaign cost estimate

Estimate for the pre registered codex campaign matrix (`preregistration.md` section 7), produced
by `python3 -m archivist_bench estimate` from the measured smoke (`smoke-results.md`) and the
2026-10-05 pilot (an earlier exploratory run, not published).
It needs a decision by the authors (section 6) before any scored run. The profiles use the 7 run authorized smoke; the 2 later
re-verification runs (shell free Codex config, `smoke-results.md`) used 25,536 and 76,482 tokens
against 27,635 and 79,052 for the same cells and do not change the estimate materially.

## 1. Run counts

| Agent | Questions | Arms | Repetitions | Runs |
|---|---:|---:|---:|---:|
| Codex CLI 0.160.0, `gpt-6.1-sol`, medium | 92 (all) | 3 | 3 | 828 |
| Claude Code 2.1.289, `claude-opus-5-5`, medium | 32 (5 per stratum plus 2 controls) | 3 | 3 | 288 |
| **Total** | | | | **1,116** |

Infra reruns (at most two per run) come on top; the harness counts them against `--max-runs`.

## 2. Prices (read 2026-10-06)

| Item | Input | Cached input | Cache write | Output | Source |
|---|---:|---:|---:|---:|---|
| `gpt-6.1-sol` standard, USD per 1M tokens | 2.00 | 0.10 | 2.50 | 10.00 | developers.openai.com/api/docs/pricing; double above 272K input per request |
| `claude-opus-5-5`, USD per 1M tokens | 4.00 | 0.20 | 5.00 (5 min); 8.00 (1 h) | 20.00 | platform.claude.com pricing |
| Web search (OpenAI and Anthropic) | 10 USD per 1,000 calls | | | | vendor pricing pages |

Fast service tier: excluded (2x API price, 2.5x plan allowance).

## 3. Auth and billing mode

- Codex runs on the operator's ChatGPT login (plan type `pro` per the smoke rollouts' rate limit
  block). Runs draw on the plan allowance and are not billed per token; the USD below is an API
  list price equivalent. The smoke moved the weekly Codex window from 2% to 2% used (below its
  reporting granularity), so the plan's weekly window, not money, is the Codex constraint: a
  `usage_limit_exceeded` stops a batch and is recorded as `infra_error`. Switching to
  `CODEX_API_KEY` (metered OpenAI API) would make the USD real.
- Claude Code runs on the operator's Claude subscription (`--restricted`; `apiKeySource: none`
  in the smoke). Runs draw on the plan allowance; `total_cost_usd` is Claude Code's own
  estimate. No `ANTHROPIC_API_KEY` exists on the host, and the harness creates none. The smoke
  showed Claude Code writing its prompt cache with the 1 hour TTL (`ephemeral_1h_input_tokens`),
  priced at 2x input (8 USD per 1M for Opus), so cache writes cost more than the 5 minute
  price used for the profiles below.

## 4. Token profiles and cost

Basis of every row: `smoke` (measured 2026-10-06, n=1 per cell), `pilot` (2026-10-05, Codex,
n in brackets), or a stated proxy. Proxies: `multi_hop_document` uses the pilot's one document
NVIDIA question; `multi_document` the Aselsan disclosure aggregation; `breadth` the grocers and
TSMC questions; `reach` is twice the multi period profile (20 to 40 filings against 4 to 12);
`both` without a measurement takes the larger of web and Archivist field by field (an upper
bound). Claude Code has no research smoke: its rows assume Codex token profiles, except the one
cell measured on the same question (`control/both`, Claude used 0.16x Codex's tokens).

### codex (gpt-6.1-sol): 828 runs, 193.86 USD API list price equivalent, 3585 Archivist calls
| stratum | arm | runs | tokens per run | USD | Archivist calls | profile basis |
|---|---|---:|---:|---:|---:|---|
| breadth | web | 45 | 491,663 | 12.43 | 0 | pilot (n=3) |
| breadth | archivist | 45 | 513,358 | 11.21 | 420 | pilot (n=3) |
| breadth | both | 45 | 513,358 | 13.46 | 420 | max(web: pilot (n=3); archivist: pilot (n=3)) |
| control | web | 6 | 27,635 | 0.26 | 0 | smoke (n=1) |
| control | archivist | 6 | 32,800 | 0.16 | 0 | smoke (n=1) |
| control | both | 6 | 43,435 | 0.29 | 0 | smoke (n=1) |
| lookup | web | 45 | 56,343 | 2.89 | 0 | smoke (n=1) |
| lookup | archivist | 45 | 79,052 | 2.84 | 90 | smoke (n=1) |
| lookup | both | 45 | 59,936 | 3.04 | 0 | smoke (n=1) |
| multi_document | web | 45 | 568,692 | 13.11 | 0 | pilot (n=1) |
| multi_document | archivist | 45 | 383,979 | 9.02 | 270 | pilot (n=1) |
| multi_document | both | 45 | 569,322 | 13.40 | 270 | max(web: pilot (n=1); archivist: pilot (n=1)) |
| multi_hop_document | web | 45 | 120,394 | 4.61 | 0 | pilot (n=1) |
| multi_hop_document | archivist | 45 | 145,922 | 3.95 | 225 | pilot (n=1) |
| multi_hop_document | both | 45 | 151,305 | 4.89 | 225 | max(web: pilot (n=1); archivist: pilot (n=1)) |
| multi_period | web | 45 | 629,611 | 12.71 | 0 | pilot (n=6) |
| multi_period | archivist | 45 | 318,514 | 7.27 | 278 | pilot (n=6) |
| multi_period | both | 45 | 629,774 | 12.78 | 278 | max(web: pilot (n=6); archivist: pilot (n=6)) |
| reach | web | 45 | 1,259,221 | 25.42 | 0 | 2.0x multi_period [pilot (n=6)] |
| reach | archivist | 45 | 637,027 | 14.53 | 555 | 2.0x multi_period [pilot (n=6)] |
| reach | both | 45 | 1,259,547 | 25.57 | 555 | max(web: 2.0x multi_period [pilot (n=6)]; archivist: 2.0x multi_period [pilot (n=6)]) |

### claude_code (claude-opus-5-5): 288 runs, 122.58 USD API list price equivalent, 1195 Archivist calls
| stratum | arm | runs | tokens per run | USD | Archivist calls | profile basis |
|---|---|---:|---:|---:|---:|---|
| breadth | web | 15 | 491,663 | 7.53 | 0 | pilot (n=3); Claude assumed equal to Codex |
| breadth | archivist | 15 | 513,358 | 7.48 | 140 | pilot (n=3); Claude assumed equal to Codex |
| breadth | both | 15 | 513,358 | 8.23 | 140 | max(web: pilot (n=3); archivist: pilot (n=3)); Claude assumed equal to Codex |
| control | web | 6 | 27,635 | 0.46 | 0 | smoke (n=1); Claude assumed equal to Codex |
| control | archivist | 6 | 32,800 | 0.32 | 0 | smoke (n=1); Claude assumed equal to Codex |
| control | both | 6 | 7,114 | 0.09 | 0 | smoke (n=1) x measured Claude ratio 0.16 |
| lookup | web | 15 | 56,343 | 1.78 | 0 | smoke (n=1); Claude assumed equal to Codex |
| lookup | archivist | 15 | 79,052 | 1.89 | 30 | smoke (n=1); Claude assumed equal to Codex |
| lookup | both | 15 | 59,936 | 1.88 | 0 | smoke (n=1); Claude assumed equal to Codex |
| multi_document | web | 15 | 568,692 | 7.99 | 0 | pilot (n=1); Claude assumed equal to Codex |
| multi_document | archivist | 15 | 383,979 | 6.01 | 90 | pilot (n=1); Claude assumed equal to Codex |
| multi_document | both | 15 | 569,322 | 8.18 | 90 | max(web: pilot (n=1); archivist: pilot (n=1)); Claude assumed equal to Codex |
| multi_hop_document | web | 15 | 120,394 | 2.92 | 0 | pilot (n=1); Claude assumed equal to Codex |
| multi_hop_document | archivist | 15 | 145,922 | 2.64 | 75 | pilot (n=1); Claude assumed equal to Codex |
| multi_hop_document | both | 15 | 151,305 | 3.11 | 75 | max(web: pilot (n=1); archivist: pilot (n=1)); Claude assumed equal to Codex |
| multi_period | web | 15 | 629,611 | 7.90 | 0 | pilot (n=6); Claude assumed equal to Codex |
| multi_period | archivist | 15 | 318,514 | 4.84 | 92 | pilot (n=6); Claude assumed equal to Codex |
| multi_period | both | 15 | 629,774 | 7.95 | 92 | max(web: pilot (n=6); archivist: pilot (n=6)); Claude assumed equal to Codex |
| reach | web | 15 | 1,259,221 | 15.80 | 0 | 2.0x multi_period [pilot (n=6)]; Claude assumed equal to Codex |
| reach | archivist | 15 | 637,027 | 9.69 | 185 | 2.0x multi_period [pilot (n=6)]; Claude assumed equal to Codex |
| reach | both | 15 | 1,259,547 | 15.90 | 185 | max(web: 2.0x multi_period [pilot (n=6)]; archivist: 2.0x multi_period [pilot (n=6)]); Claude assumed equal to Codex |

Measured Claude to Codex token ratio (stratum/arm): {'control/both': 0.1638}

Summary (API list price equivalent, USD):

| Agent | web | archivist | both | Total | With 1.5x contingency |
|---|---:|---:|---:|---:|---:|
| Codex | 71.43 | 48.98 | 73.43 | 193.86 | 291 |
| Claude Code | 44.38 | 32.87 | 45.34 | 122.58 | 184 |
| **Total** | | | | **316.44** | **475** |

The largest share is `reach` (34% of each agent's total). Uncertainty is high: one smoke run
per cell, the pilot ran at the fast tier (token counts unaffected), the reach proxy is a guess
and Claude Code's research profile is assumed. A 1.5x contingency covers reruns and the reach
and Claude unknowns.

### Judges (amended 2026-10-06: plan allowance, no metered spend)

The Vertex judge and its spend line are removed (pre registration amendment 1). Pass A is Claude
Code on the Claude subscription, pass B is Codex on the ChatGPT login: one call per judge per
scored answer, 1,080 scored answers (Codex 810, Claude Code 270; controls are not judged), so
1,080 calls per judge in the ordinary case. Failure bound for counted batches: one retry within
a batch and one later batch for a `judge_error` allow up to 4 calls per judge per answer (4,320
per judge). A batch stopped by a plan limit or the call ceiling does not count and is redone in
full (a pass that already succeeded is called again), so each such stop adds up to 2 calls for
the answer it interrupted; `--max-calls` bounds every batch. Nothing is billed per token.

Token load per call: the measured fixed overhead of the live judge smoke (2026-10-06,
`codex.archivist.LK07`: Claude Code 3,408 total tokens, Codex 9,354) minus that prompt's payload
(about 505 tokens at 4 characters per token) gives about 2,800 (Claude Code) and 8,800 (Codex)
tokens of system prompt and schema per call. Payload per stratum = the actual judge prompt of
every committed question plus an answer of 1,200 characters (the pilot's median answer was
1,137, mean 1,242, max 3,011), 1,500 for multi period, 2,000 for breadth and 3,000 for reach;
output = 60 + 25 tokens per fact.

| Judge | Calls | Input tokens | Output tokens | Per call | API list price equivalent | Share of that plan's codex campaign agent load |
|---|---:|---:|---:|---:|---:|---|
| A Claude Code `claude-opus-5-5` | 1,080 | 4.32M | 0.30M | about 4,300 | about 41 USD (every input priced as a 1 hour cache write, an upper bound) | 3.7% of tokens (126M), 33% of list price equivalent (122 USD) |
| B Codex `gpt-6.1-sol` | 1,080 | 10.82M | 0.30M | about 10,300 | about 25 USD (no cache reads assumed, an upper bound) | 2.9% of tokens (378M), 13% of list price equivalent (193 USD) |

The Claude upper bound is loose: the second live judge call already read 2,003 of its 3,242
input tokens from cache (0.0125 USD against 0.0286 for the first).

Plan window impact (uncalibrated): the judges add about 3% to each plan's token load in the
ordinary case (up to about 12% at the 4 call failure bound). How much of a plan window that is
is not measured: the Codex smoke (6 runs, about 300K tokens) left the rounded weekly
reading at 2%, which bounds nothing for 11M tokens, and the Claude subscription login is shared
with the authors' other sessions (during the judge smoke the five hour window read 32% and the seven day
window 51%). Batch sizes should follow the observed window change of the first batch. Every Claude judge call
records the plan window utilization in `judge-ledger.jsonl`, and a rejected window stops the
batch without counting against any answer. Recommendation: judge after each agent's runs, in
batches sized to the remaining window, Codex judging first.

## 5. Archivist quota

Every `/research/*` call counts toward the 10,000 per month CLI quota (admin included). Used
this month: 1,358 (688 when the plan was drafted, plus 638 permalink reads for the answer keys, 4 smoke tool
calls and unrelated use); 8,642 remain until the reset on 2026-11-01.

| Agent | Archivist calls (estimate) |
|---|---:|
| Codex (archivist and both arms) | 3,585 |
| Claude Code (archivist and both arms) | 1,195 |
| **Total** | **4,780** |

Pilot tool counts undercount calls made inside Codex code mode loops, so the real figure may be
up to about 2x (9,560), which exceeds October's remainder. The 100 requests per 60 s burst limit
is not a constraint at concurrency 2.

## 6. Decisions needed

1. **Spend ceiling for the codex campaign.** Answered for the judges (decided 2026-10-06: use
   the operator's Claude and ChatGPT plans): judging uses the plan allowances, so the Vertex judge ceiling
   is withdrawn and no metered spend remains while both agents stay on plan allowance.
   Recommendation unchanged for the agents: cap at 1,116 runs plus at most 10% reruns
   (`--max-runs 1230`) and the judges at 2,160 calls plus retries (`--max-calls` per batch).
2. **Billing mode** (agents): keep both agents on plan allowance (recommended, and consistent
   with the decision to use the plans; to be confirmed for the agent runs) or authorize
   metered API keys (about 316 USD at list price, 475 with
   contingency), which would also make Claude Code's cost a Console figure rather than a client
   estimate.
3. **Archivist quota**: run the codex campaign after the 2026-11-01 reset (recommended: the whole run fits in
   one month's 10,000 even at 2x), or raise the admin CLI ceiling for this account.
