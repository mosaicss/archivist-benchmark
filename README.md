# Archivist benchmark: Codex and Claude Code, own web tools versus Archivist

A pre registered benchmark of how many tokens a coding agent spends answering questions about
company filings with its own web tools, with the Archivist MCP server (Mosaic's filing search), or
with both. Method: [`preregistration/preregistration.md`](preregistration/preregistration.md).

## Results

Average tokens consumed per question, by arm. In brackets: the change against the model's native
AI web tools. Medium effort, scored questions, completed runs.

<table>
<thead>
<tr><th rowspan="2">Model (agent)</th><th rowspan="2">Questions x runs</th><th colspan="3">Tokens consumed per question</th></tr>
<tr><th>Native AI web tools</th><th>Archivist alone</th><th>Both (web tools and Archivist)</th></tr>
</thead>
<tbody>
<tr><td>Sol 6.1 (Codex CLI 0.160.0)</td><td>90 x 3</td><td>302,665</td><td>156,457 (48% reduction / 1.93x less)</td><td>186,831 (38% reduction / 1.62x less)</td></tr>
<tr><td>Opus 5.5 (Claude Code 2.1.289)</td><td>30 x 1</td><td>317,363</td><td>97,404 (69% reduction / 3.26x less)</td><td>88,805 (72% reduction / 3.57x less)</td></tr>
</tbody>
</table>

95% bootstrap intervals for the x factors: Sol 6.1 1.93x (1.60 to 2.32) and 1.62x (1.35 to 1.93);
Opus 5.5 3.26x (2.13 to 4.80) and 3.57x (2.44 to 5.03).

- **Accuracy held.** Near the ceiling in every arm, and no pooled difference against native web
  tools outside the pre registered 0.05 margin (details in the results files).
- **Provenance.** With Archivist alone, every source the model read was a filing and no answer
  cited anything else.

### By question type

Opus 5.5 (Claude Code 2.1.289), 5 questions per type x 1 run:

<table>
<thead>
<tr><th rowspan="2">Question type</th><th colspan="3">Tokens consumed per question</th></tr>
<tr><th>Native AI web tools</th><th>Archivist alone</th><th>Both (web tools and Archivist)</th></tr>
</thead>
<tbody>
<tr><td>Lookup: 1 to 3 facts from one filing</td><td>74,245</td><td>29,290 (61% reduction / 2.53x less)</td><td>24,175 (67% reduction / 3.07x less)</td></tr>
<tr><td>Multi hop within one filing</td><td>439,856</td><td>49,193 (89% reduction / 8.94x less)</td><td>61,356 (86% reduction / 7.17x less)</td></tr>
<tr><td>Several filings of one company</td><td>157,965</td><td>39,330 (75% reduction / 4.02x less)</td><td>40,609 (74% reduction / 3.89x less)</td></tr>
<tr><td>One metric across 5 to 10 filings</td><td>333,619</td><td>125,283 (62% reduction / 2.66x less)</td><td>102,617 (69% reduction / 3.25x less)</td></tr>
<tr><td>Breadth: the same fact across 7 to 10 companies</td><td>510,694</td><td>97,058 (81% reduction / 5.26x less)</td><td>103,917 (80% reduction / 4.91x less)</td></tr>
<tr><td>Reach: one company across 20 to 26 filings</td><td>387,797</td><td>244,272 (37% reduction / 1.59x less)</td><td>200,156 (48% reduction / 1.94x less)</td></tr>
</tbody>
</table>

Sol 6.1 (Codex CLI 0.160.0), 15 questions per type x 3 runs:

<table>
<thead>
<tr><th rowspan="2">Question type</th><th colspan="3">Tokens consumed per question</th></tr>
<tr><th>Native AI web tools</th><th>Archivist alone</th><th>Both (web tools and Archivist)</th></tr>
</thead>
<tbody>
<tr><td>Lookup: 1 to 3 facts from one filing</td><td>63,928</td><td>82,067 (28% increase / 1.28x more)</td><td>73,822 (15% increase / 1.15x more)</td></tr>
<tr><td>Multi hop within one filing</td><td>157,191</td><td>88,387 (44% reduction / 1.78x less)</td><td>104,432 (34% reduction / 1.51x less)</td></tr>
<tr><td>Several filings of one company</td><td>109,105</td><td>88,918 (19% reduction / 1.23x less)</td><td>119,330 (9% increase / 1.09x more)</td></tr>
<tr><td>One metric across 5 to 10 filings</td><td>225,155</td><td>184,633 (18% reduction / 1.22x less)</td><td>218,214 (3% reduction / 1.03x less)</td></tr>
<tr><td>Breadth: the same fact across 7 to 10 companies</td><td>598,310</td><td>207,516 (65% reduction / 2.88x less)</td><td>264,723 (56% reduction / 2.26x less)</td></tr>
<tr><td>Reach: one company across 20 to 26 filings</td><td>662,303</td><td>287,218 (57% reduction / 2.31x less)</td><td>340,464 (49% reduction / 1.95x less)</td></tr>
</tbody>
</table>

For Sol 6.1 the largest savings are on breadth and reach; on single lookups it spent more
tokens with Archivist than with its native web tools.

Full tables, per stratum results, hypotheses and limits: [`results/codex/results.md`](results/codex/results.md),
[`results/grounding/grounding.md`](results/grounding/grounding.md),
[`results/claude-code/replication.md`](results/claude-code/replication.md).

## What is in this repository

- `archivist_bench/`: the harness (Python 3.12+, standard library only); reference in
  [`docs/HARNESS.md`](docs/HARNESS.md); hermetic tests in `tests/`.
- `preregistration/`: the pre registration, the 92 question set with answer keys, the
  contamination questions and the amendment 5 key corrections.
- `results/`: per campaign analysis JSON, CSV tables, answers and redacted run logs (codex,
  grounding, claude-code). [`REDACTIONS.md`](REDACTIONS.md) lists how this public edition differs
  from the internal record.

## Reproduce

1. Install the pinned CLIs (Codex CLI 0.160.0, Claude Code 2.1.289, archivist-cli 0.2.33) and
   sign in on plans: ChatGPT for Codex, a Claude subscription for Claude Code, no API keys.
2. Check offline: `python -m pytest tests -q`, `python3 -m archivist_bench validate-questions
   --questions preregistration/questions.json`, then a dry run of the plan.
3. Run, judge, analyze and report each campaign in batches that fit your plan windows.
4. Compare your tables with `results/`.

Every command, expected output and what cannot be reproduced exactly: [`RUNBOOK.md`](RUNBOOK.md).
To measure tokens and sources on your own questions (no answer keys needed): [`RUNBOOK.md` section 12](RUNBOOK.md#12-run-it-on-your-own-questions).

## Archivist access

- Mosaic's MCP and CLI plan is USD 8 per month with a 30 day free trial; the free trial covers
  the usage required.
- Fair use is 10,000 Archivist CLI calls per month; the free plan's 20 lifetime calls are not
  enough.
- Calls per campaign, from the run ledgers: codex 9,291; grounding cross check and contamination
  about 3,785; claude-code 834. The trial covers one campaign; running every campaign in the
  same month can exceed the monthly ceiling.

## License

Code: Apache License 2.0 ([`LICENSE`](LICENSE)). Data and text: CC BY 4.0
([`LICENSE-DATA`](LICENSE-DATA)). Copyright 2026 Mosaic Finance; see [`NOTICE`](NOTICE).
