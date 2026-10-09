---
title: 'Smoke results'
campaign: smoke
created: '2026-10-06'
scored: false
---

# Smoke results

Harness validation only: not scored, not part of the codex campaign, not a claim. Authorized
scope (decided 2026-10-06): a Codex smoke of the control question and one lookup in three arms, one
repetition (6 runs), and one Claude Code control smoke. Exactly 7 runs were spawned, no reruns.

- Versions: Codex CLI 0.160.0 (`gpt-6.1-sol`, effort `medium`, tier `default`, ChatGPT login);
  Claude Code 2.1.289 (`claude-opus-5-5`, effort `medium`, subscription, `--restricted`);
  archivist-cli 0.2.32 (commit aeb43de); harness `archivist_bench` 1.0.0 (working tree of Mosaic's private
  repository).
- Commands: `run --agent codex --question CT01 --question LK07 --repetitions 1 --execute
  --max-runs 6`, then `run --agent claude_code --question CT01 --arms both --repetitions 1
  --execute --max-runs 1`; concurrency 1.
- Raw evidence (outside git): `<evidence>/smoke/runs/` and `ledger.jsonl`.
- Archivist quota used by the smoke: 2 calls (1,354 before, 1,356 after).

## Run ledger

| run | agent | arm | question | model / effort / tier | auth / billing | status | total tokens | uncached in | cached in | output | reasoning | model calls | tool calls | wall s | cost USD | MCP loaded | events |
|---|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---|---|
| 20261006T190441420669Z-codex.web.CT01.r1-a1 | codex | web | CT01 | gpt-6.1-sol / medium / default | chatgpt / chatgpt_plan_allowance | completed | 27635 | 15783 | 11776 | 76 | 0 | 2 | web.search 1 | 7.4 | 0.0435 | not configured | compactions 0; truncated calls 0; paged results 0 |
| 20261006T190448812123Z-codex.archivist.CT01.r1-a1 | codex | archivist | CT01 | gpt-6.1-sol / medium / default | chatgpt / chatgpt_plan_allowance | completed | 32800 | 11546 | 21120 | 134 | 18 | 3 | none | 11.7 | 0.0265 | yes | compactions 0; truncated calls 0; paged results 0 |
| 20261006T190500471870Z-codex.both.CT01.r1-a1 | codex | both | CT01 | gpt-6.1-sol / medium / default | chatgpt / chatgpt_plan_allowance | completed | 43435 | 17292 | 25984 | 159 | 0 | 3 | web.search 1 | 12.5 | 0.0488 | yes | compactions 0; truncated calls 0; paged results 0 |
| 20261006T190512932942Z-codex.web.LK07.r1-a1 | codex | web | LK07 | gpt-6.1-sol / medium / default | chatgpt / chatgpt_plan_allowance | completed | 56343 | 24522 | 31616 | 205 | 0 | 3 | web.search 2 | 13.2 | 0.0643 | not configured | compactions 0; truncated calls 0; paged results 0 |
| 20261006T190526154598Z-codex.archivist.LK07.r1-a1 | codex | archivist | LK07 | gpt-6.1-sol / medium / default | chatgpt / chatgpt_plan_allowance | completed | 79052 | 27104 | 51584 | 364 | 0 | 4 | mcp:archivist.read_passage 1, mcp:archivist.search 1 | 18.6 | 0.0630 | yes | compactions 0; truncated calls 0; paged results 0 |
| 20261006T190544746238Z-codex.both.LK07.r1-a1 | codex | both | LK07 | gpt-6.1-sol / medium / default | chatgpt / chatgpt_plan_allowance | completed | 59936 | 25895 | 33792 | 249 | 0 | 3 | web.search 2 | 15.7 | 0.0677 | yes | compactions 0; truncated calls 0; paged results 0 |
| 20261006T190822183994Z-claude_code.both.CT01.r1-a1 | claude_code | both | CT01 | claude-opus-5-5 / medium / default | subscription / claude_plan_allowance | completed | 7114 | 6897 | 0 | 217 | 103 | 1 | none | 3.9 | 0.0595 | yes | compactions 0; truncated calls 0; paged results 0 |
| 20261006T192414248932Z-codex.web.CT01.r1-a1 | codex | web | CT01 | gpt-6.1-sol / medium / default | chatgpt / chatgpt_plan_allowance | completed | 25536 | 14316 | 11136 | 84 | 0 | 2 | web.search 1 | 7.9 | 0.0406 | not configured | compactions 0; truncated calls 0; paged results 0 |
| 20261006T192422266935Z-codex.archivist.LK07.r1-a1 | codex | archivist | LK07 | gpt-6.1-sol / medium / default | chatgpt / chatgpt_plan_allowance | completed | 76482 | 18650 | 57472 | 360 | 0 | 4 | mcp:archivist.read_passage 1, mcp:archivist.search 1 | 17.2 | 0.0466 | yes | compactions 0; truncated calls 0; paged results 0 |

Re-verification after review (2026-10-06, required repair, not part of the authorized smoke
count above): review round 1 found Codex arms still had a shell, so every arm now sets
`shell_tool = false` and `unified_exec = false`. Two Codex runs proved the new config works live
(`run --question CT01 --arms web --max-runs 1` and `run --question LK07 --arms archivist
--max-runs 1`): both completed, the Archivist tools still ran through code mode, and LK07 matched
the key. Total live agent runs in the smoke: 9 (8 Codex, 1 Claude Code). Archivist quota after:
1,358 (2 more calls).



All 7 runs completed. The Archivist and both arms show the server loaded: Claude Code through
`system/init` (`mcp_servers: archivist connected`, 11 `mcp__archivist__*` tools beside
`WebSearch` and `WebFetch`, `apiKeySource: none`); Codex through its session tool catalog (each
archivist and both run listed `mcp__archivist__*` tools in a tool output; the web runs never did)
and, in `codex.archivist.LK07`, two completed Archivist tool calls. Costs are API list price
equivalents (Codex) and Claude Code's client estimate; neither was billed per token.

## Answers (links redacted)

LK07 key: revenues USD 12,559,938 thousand; operating income USD 4,192,610 thousand. All three
Codex arms reported both figures exactly. CT01 (17 x 23 = 391): all four runs answered 391.

### codex.web.CT01.r1

> 391. [Source](<url:math.tools>)

### codex.archivist.CT01.r1

> 391 (source: direct calculation, 17 × 23).

### codex.both.CT01.r1

> [391](<url:amsi.org.au>)

### codex.web.LK07.r1

> For the three months ended June 30, 2026, Netflix reported (in thousands of US dollars):
>
> - **Revenues:** $12,559,938
> - **Operating income:** $4,192,610
>
> Source: [Netflix’s Form 10-Q](<url:filing-source>), filed July 17, 2026.

### codex.archivist.LK07.r1

> For the **three months ended June 30, 2026**, Netflix reported (in **thousands of US dollars**):
>
> - **Revenues:** 12,559,938
> - **Operating income:** 4,192,610
>
> Source: [Netflix’s Form 10-Q, filed July 17, 2026—Consolidated Statements of Operations](<url:mosaic-finance.com>).

### codex.both.LK07.r1

> For the three months ended June 30, 2026, Netflix reported (in **thousands of US dollars**):
>
> - **Revenues:** $12,559,938
> - **Operating income:** $4,192,610
>
> Source: [Netflix Form 10-Q, filed July 17, 2026](<url:filing-source>).

### claude_code.both.CT01.r1

> **391**
>
> 17 × 23 = 17 × 20 + 17 × 3 = 340 + 51 = 391.
>
> I didn't use the research tools or cite a source. This is plain arithmetic, so it can be checked by working it out as shown above. The Mosaic filings database covers company filings, not math facts, so there was nothing relevant to cite.

## What the smoke changed in the harness

1. Codex 0.160.0 `exec` persists no MCP startup event (no `mcp_startup_*` record in the rollout,
   the `--json` stream or the home's log database). The first extraction therefore marked three
   archivist and both runs `invalid: arm_isolation (unseen)`. The harness now records in run
   evidence: the session tool catalog (`tool_catalog`), completed tool calls (`tool_call`), and,
   for every future Codex run, a preflight written just before the run (`codex mcp list --json`
   on the run's home plus an MCP stdio handshake in the run's environment). The six records were
   re-extracted with `archivist_bench extract`; `ledger.jsonl` keeps the original rows.
2. The `both` arm used web search, not Archivist, for the lookup (one smoke run; a behaviour to
   watch in the codex campaign, not a finding).
3. Fixed overhead differs widely: the control cost Codex 27.6K to 43.4K total tokens (2 to 3
   model calls) and Claude Code 7.1K (1 model call, 1 hour cache write).
4. Committed answers now mask filing source hosts as `<url:filing-source>`.

## Judge smoke (pre registration amendment 1, 2026-10-06)

Unscored: one live call per judge on one existing smoke answer, to prove both plan judges run
and parse. Command: `judge --evidence-dir <evidence>/smoke --run-id
20261006T192422266935Z-codex.archivist.LK07.r1-a1 --execute --max-calls 2` (harness 1.1.0
working tree). Raw evidence: `judge-ledger.jsonl`, `judge-logs/` and that run's
`judgement.json` in the smoke evidence directory.

| pass | CLI | model / effort | auth / billing | status | total tokens | input | output | reasoning | wall s | cost USD | notes |
|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---|
| A | Claude Code 2.1.289 | claude-opus-5-5 / medium | subscription / claude_plan_allowance | ok | 3408 | 3299 (3297 cache write) | 109 | 0 | 3.1 | 0.0286 (client estimate) | init tools `StructuredOutput` only, no MCP, `apiKeySource: none`; 2 turns; structured output; plan window five hour 32%, seven day 51% |
| B | Codex CLI 0.160.0 | gpt-6.1-sol / medium | chatgpt / chatgpt_plan_allowance | ok | 9354 | 9315 (0 cached) | 39 | 0 | 3.8 | 0.0190 (API list price equivalent) | ephemeral, read only, no shell, web search off; only message items; schema valid last message |

Both judges marked F1 (revenues) and F2 (operating income) `correct` and the answer complete:
no disputed fact, accuracy 1.0, completion 1.0, matching the key (section "Answers" above).
Codex warned that it could not create helper binaries under `/tmp` (its scratch home was
there); the executor now keeps the scratch home beside the call log.

Re-verification after the Codex review (required repair, 2026-10-06 21:08Z, same command with
`--run-id 20261006T190544746238Z-codex.both.LK07.r1-a1`, a second unscored answer whose source
link is a masked filing source): both calls ok, both judges marked both facts `correct` and the
answer complete; no Codex warning.

| pass | status | total tokens | input | output | wall s | cost USD | notes |
|---|---|---:|---:|---:|---:|---:|---|
| A | ok | 3351 | 3242 (2003 cache read, 1237 cache write) | 109 | 3.8 | 0.0125 (client estimate) | init `StructuredOutput` only, `apiKeySource: none`; plan window five hour 33%, seven day 51% |
| B | ok | 9479 | 9440 (0 cached) | 39 | 5.0 | 0.0193 (API list price equivalent) | ChatGPT login checked before start, `forced_login_method = "chatgpt"`; only message items |

Judge calls in the smoke: 4 (2 per judge), all on plan allowance; no Gemini or other metered call.
