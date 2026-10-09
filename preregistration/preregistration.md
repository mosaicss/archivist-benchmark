---
title: 'Pre registration: Archivist versus native agent web tools'
type: 'preregistration'
status: 'frozen'
frozen: '2026-10-06'
amended: '2026-10-06 (amendment 1, plan judges; before any scored run); 2026-10-07 (amendment 2, Codex only; during the campaign, before any analysis); 2026-10-07 (amendment 3, grounding, provenance and the forum cross check; before any grounding measure or contamination run); 2026-10-07 (amendment 4, a light Claude Code replication; before any Claude Code benchmark run, judge or grading call); 2026-10-08 (amendment 4 notes in 15.2 and 15.4: the bias check estimator and a separate Claude subscription login; before any Claude Code benchmark run, judge or grading call); 2026-10-08 (amendment 5, key corrections and the light audit route; after the claude-code campaign, before any rescoring, re-judge or pre check call; 16.3 note adding the grounding campaign's contamination sample, before its pre check)'
harness: 'archivist_bench (version 1.1.0; 1.0.0 at freeze)'
questions: 'questions.json (schema 1, 92 questions)'
---

# Pre registration: Archivist versus native agent web tools

This document fixes the benchmark design before any scored run. The codex campaign runs it; nothing
in it changes after the commit that adds it except through a dated entry under "Deviations", made
before the affected runs and kept beside the original text. The 2026-10-05 pilot
(not published) was exploratory and only shaped
the hypotheses; none of its questions are reused and none of its numbers are claims.

## 1. Question

Does the same agent, with the same model and effort, answer filings research questions as
accurately and with fewer tokens when it has the Archivist MCP server instead of (or beside) its
own web tools?

## 2. Systems and arms

| Agent | Version | Model | Effort | Tier | Auth and billing | Bound |
|---|---|---|---|---|---|---|
| Codex CLI (`codex exec`) | 0.160.0 | `gpt-6.1-sol` | `medium` | default (never fast) | ChatGPT login, plan allowance; cost reported as API list price equivalent | 1200 s wall, no turn limit exists |
| Claude Code (`claude -p`) | 2.1.289 | `claude-opus-5-5` | `medium` | default | Claude subscription (`--restricted`); `api_key` mode (`--bare`, metered) only with an existing key | 50 turns, 1200 s wall |

Arms, identical for both agents (configuration in `docs/HARNESS.md`):

- **web**: the agent's native web tools only (Codex `web_search = "live"`; Claude Code
  `WebSearch`, `WebFetch`).
- **archivist**: the Archivist MCP server only (`archivist mcp serve`), no web tools.
- **both**: native web tools plus Archivist. This is the delivered product and the headline arm.

Isolation: a fresh config home per run, an allowlisted environment, no shell or file tools in
either agent (Codex `shell_tool` and `unified_exec` off, its sandbox read only with no network as
a second guard; Claude Code built in tools limited to the arm's web tools), an empty working
directory and a fresh session per run. Any shell, file or other tool use outside the arm's
allowlist makes the run `invalid: arm_isolation`. The Archivist CLI version is whatever the pending archivist-cli release with the leaner tool
list ships; the codex campaign records the exact versions of Codex, Claude Code, archivist-cli and the deployed chat-api SHA in
its run ledger before the first scored run.

Prompt, identical in every arm and agent:

> Research question (use your research tools; do not run shell commands): {question} Be concise
> and cite your sources.

## 3. Question set

`questions.json`: 92 questions, 15 in each of six strata plus 2 controls.

| Stratum | What it tests | Questions | Facts | Non US |
|---|---|---:|---:|---:|
| lookup | one to three facts from one filing | 15 | 41 | 5 |
| multi_hop_document | two or more passages of one filing, one depending on another | 15 | 63 | 5 |
| multi_document | two or more different filings of one company | 15 | 75 | 5 |
| multi_period | one metric across 4 to 12 filings of one company (Archivist's expected strength) | 15 | 109 | 6 |
| breadth | the same fact across 5 to 12 companies | 15 | 147 | 7 |
| reach | one company across 20 to 40 filings, sized to strain context and turns | 15 | 348 | 5 |
| control | no research need; measures fixed tool overhead | 2 | 2 | 0 |
| **Total** | | **92** | **785** | **33** |

Non US questions (SEDAR+ and KAP filings) are a tagged subset (`non_us` plus `sedar` or `kap`),
not a stratum. The 15 per stratum come from the planned 15 to 30 range; with three repetitions
and a paired design this detects a token ratio near the pilot's multi period effect (about 2x);
smaller effects get wide intervals, which the results will show rather than hide.

Answer keys were built from the filings themselves, never from an agent answer:

- Each `sourced` fact names a filing UUID, a chunk id, the Mosaic permalink of that passage, the
  exchange document id (SEC accession number or KAP disclosure index) or the stated reason none
  is stored (SEDAR+ rows), and a quote. Every quote was checked to occur in the stored chunk
  text after whitespace normalization (non breaking spaces and repeated whitespace collapsed,
  curly quotes and markdown table characters normalized, case ignored; read only, from the Mosaic filings
  database, filtered by chunk id or filing id), so a quote can differ from the stored
  bytes in whitespace and punctuation form, and
  every permalink came from `archivist read passage <chunk_id> --window 0`.
- Each `absence` fact names a filing and the terms it does not contain; a case insensitive match
  of every term over every chunk of that filing found none.
- Each `computed` fact gives its formula over other facts of the same question.
- Questions name the company, the document (form and period or filing date) and the figure
  wanted; none depends on "latest", so the keys hold when the codex campaign runs.
- The pilot topics are excluded (pilot README); some companies recur across strata with
  different questions.

`python3 -m archivist_bench validate-questions` enforces the counts, unique ids, the reach
range (20 to 40 distinct filings), a source or absence reason for every scored fact, matching
permalinks, and no upstream filing host anywhere.

## 4. Hypotheses and decision rules

Every comparison is within one agent, between an arm and the web arm on the same questions
(paired). Token ratio = sum of web arm total tokens over sum of the other arm's total tokens
(above 1 means the other arm used fewer). Accuracy difference = mean accuracy of the other arm
minus the web arm (fact score average per run). Non inferiority margin for accuracy: 0.05
(five points). "CI" is the paired bootstrap 95% percentile interval of section 6. Codex is the
primary agent; Claude Code is a control confirmation on its subset (section 7).

| Id | Hypothesis | Stratum | Supported when | Partly supported when |
|---|---|---|---|---|
| H1 | Archivist uses at least 2x fewer total tokens with no accuracy loss | multi_period | token ratio (web over archivist) CI lower bound at least 2.0 and accuracy difference CI lower bound at least -0.05 | ratio point estimate at least 2.0 or CI lower bound above 1.0, accuracy condition met |
| H2 | The both arm is at least as accurate as the best single arm and uses fewer tokens than web only | every scored stratum, and pooled | accuracy difference CI lower bound at least -0.05 against web and against archivist, and token ratio (web over both) CI lower bound above 1.0 | the accuracy conditions hold and the ratio point estimate is above 1.0 |
| H3 | After the pre benchmark tool changes (ticker first guidance with a latest filing and period guard, listing a company's filings, finding a term in one filing), Archivist is no worse than web on lookups | lookup | accuracy difference CI lower bound at least -0.05 and token ratio CI lower bound at least 0.9 | accuracy condition met and ratio point estimate at least 1.0 |
| H4 | After the filing list and term search tools, Archivist is at least equal on breadth | breadth | same rule as H3 | same rule as H3 |
| H5 | On reach questions the archivist and both arms complete tasks the web arm fails, truncates or degrades on | reach | for an arm, the completion rate difference (arm minus web) CI lower bound above 0, or the accuracy difference CI lower bound above 0 with a completion difference point estimate at least 0 | either point estimate above 0 |

Anything else is "not supported". Reported for every stratum and arm regardless: both
estimates with their CI, n of questions, and the per arm event counts. Only H1 to H5 are
confirmatory; every other cut (non US subset, per question, uncached input, model calls, wall
time, cost) is exploratory and labelled as such. No multiple comparison correction is applied to
the five pre specified tests; each has its own stratum.

## 5. Measures

Per run (all in the run ledger): agent, model, effort, service tier, auth and billing mode;
total, input, cached input, cache write, uncached input, output and reasoning tokens; model
calls; tool calls by tool (Codex MCP by server and tool, web search by action; Claude Code by
tool name and server side searches); wall time; cost with its basis; compactions, turn limit and
timeout events; truncated tool results (each call with its id and original token count: Codex's
`Warning: truncated output (original token count: N)`, Claude Code's `MAX_MCP_OUTPUT_TOKENS`
refusals and results saved to a file) and Archivist paged results; whether the Archivist server
loaded (Claude Code `system/init`; Codex, which records no startup event, a pre run `codex mcp
list` plus stdio handshake and the session's own tool catalog or tool calls). Definitions:
`input` includes cached input; `uncached_input = input - cached_input`; `output` includes
reasoning; `total = input + output`. Codex totals are the last cumulative `token_count` of the
rollout; Claude Code totals are `result.modelUsage` summed over every model (the WebFetch
summarizer included). Tool counts can undercount calls made inside Codex code mode.

## 6. Statistics

- Unit: the question. Repetitions are averaged per question and arm first.
- Paired bootstrap over questions: resample question indices with replacement, the same indices
  for both arms; 10,000 resamples; Python `random.Random(8105)`; percentile 95% interval
  (2.5th and 97.5th, linear interpolation). Implemented in `archivist_bench/stats.py`.
- Per agent and stratum, for archivist and both against web and for both against archivist
  (H2's best single arm): token ratio (sum over sum), accuracy difference and completion
  difference (mean of differences). H2 pooled: the same over all scored questions.
- Token comparisons use completed runs only; a question enters a comparison only when both arms
  have at least one completed run, and dropped questions are listed. A sensitivity table that
  includes the partial usage of timed out and turn limited runs is reported beside it.
- Controls are not in any comparison; their mean tokens per arm are reported as fixed overhead.

## 7. Repetitions, scale and order

- 3 repetitions of every cell.
- Codex: the full set, 92 questions x 3 arms x 3 repetitions = 828 runs.
- Claude Code (control confirmation, decided 2026-10-06): a fixed stratified subset of 5
  questions per stratum plus both controls, 32 questions x 3 arms x 3 repetitions = 288 runs.
  The subset is the five ids per stratum with the smallest `sha256("8105:<id>")`
  (`questions.claude_subset`): LK04, LK05, LK06, LK09, LK15, MH01, MH02, MH03, MH04, MH14, MD04, MD09, MD12, MD14, MD15, MP01, MP02, MP05, MP06, MP15, BR01, BR03, BR07, BR12, BR15, RC01, RC03, RC07, RC14, RC15, CT01, CT02.
- Order: question major, the three arms of a question back to back within a repetition;
  concurrency at most 2; Codex first, then Claude Code.
- The smoke runs (`smoke-results.md`) ran CT01 and LK07 once each, unscored, to validate the
  harness. Those runs are not part of the codex campaign and are never pooled with its runs; LK07
  stays in the set and is run in the codex campaign like every other question.
- The codex campaign writes to its own evidence directory (`--evidence-dir`, never the smoke
  directory);
  `analyze` refuses any directory holding more than one non `infra_error` record for a cell.
- Spend: no scored run starts before the authors set the codex campaign spend ceiling
  (`cost-estimate.md`).

## 8. Judge

> Amended on 2026-10-06 by amendment 1 (section 12): the Vertex Gemini judge below is replaced by
> a Claude Code judge (pass A) and a Codex judge (pass B) on the operator's plans, both given one
> identical prompt. The original text stays here for the record; section 12 governs.

- Model: `gemini-3.1-pro-preview` on Vertex AI (Mosaic's Google Cloud project, location `global`), temperature 0,
  JSON output. It belongs to neither agent's model family.
- Blinding: links become `[link]`, domains `[site]`, and Mosaic, Archivist and tool names
  neutral tokens before the judge sees an answer (`redact.blind`). Fact labels are neutral
  (F1..Fn).
- Two passes per answer: pass A gives the key then the answer; pass B gives the answer then the
  key with the facts in a shuffled order seeded by `8105:<run id>:B`.
- Verdict per fact: `correct`, `incorrect` or `missing`, plus `complete` for the whole answer.
- Scoring: agreement scores the verdict (correct 1, otherwise 0); disagreement makes the fact
  `disputed`, scores 0.5 and queues it for human audit. Run accuracy = mean fact score.
- A malformed verdict is retried once, then the pass is `judge_error`; a run whose judgement
  fails is judged once more in a later batch and, if it fails again, is excluded from accuracy
  and from completion, and listed. A completed run has no completion value until it has a valid
  judgement.
- Rubric (verbatim from `archivist_bench/judge.py`):

```text
You grade one research answer against an answer key taken from company filings.
Rules:
- Judge each key fact on its own: "correct" when the answer states it with the same value,
  period and unit (equivalent formatting, rounding to the precision shown in the key, and the
  listed accepted spellings count as the same); "incorrect" when the answer gives a different
  value, period, unit or entity for it; "missing" when the answer does not state it.
- For a fact that says a filing does not mention something, "correct" means the answer says it
  is not mentioned (or omits it from a list of companies that mention it).
- Ignore length, style, tone, formatting, extra correct detail and how sources are cited. Do not
  reward or penalize an answer for being long or short. Links and tool names are hidden.
- "complete" is true when the answer addresses every part the question asks for, false when it
  stops early, refuses, says it ran out of time or covers only part of the requested items.
Return only JSON: {"facts": [{"id": "<fact label>", "verdict": "correct|incorrect|missing"}],
"complete": true|false}. Include every fact label exactly once.
```

- Human audit: every disputed fact, plus a random 5% of agreed facts (seed 8105), is checked by
  a person against the key and the filing. Audit corrections replace judge verdicts and are
  listed. Answer quality acceptance is a human decision by the authors; the judge and the
  audit are evidence for that decision, not the decision.
- Controls are scored without the judge: the expected value must appear in the answer.

## 9. Completion (H5)

Per run: 1 when the run completed and both judge passes say `complete`; 0.5 when the passes
disagree; 0 when the run timed out, hit the turn limit, ended without a result or usage, or both
passes say incomplete. A completed run without a valid judgement (not yet judged, or a judge
error after the retry batch) has no completion value: it is excluded from H5 and listed, never
counted as 0. H5 compares arms by the paired completion difference (arm minus web, question
means, the section 6 bootstrap). Every run records its compactions, truncations, turn limit and
timeout events per arm, and H5 reports them beside the completion rates.

## 10. Exclusions and reruns

| Case | Rule |
|---|---|
| Infra failure (`usage_limit_exceeded`, `server_overloaded`, Archivist HTTP 429, Anthropic 429 or 5xx) | `infra_error`; rerun at most twice; every attempt stays in the ledger; still failing after three attempts: excluded and listed |
| Arm isolation broken (Archivist not connected in an archivist or both arm, a web arm with an MCP server, a tool outside the arm allowlist) | `invalid: arm_isolation`; excluded; not retried silently: the batch stops, the cause is fixed and recorded as a deviation, then the affected cells rerun once |
| Contamination (a web arm opens or cites a Mosaic page; any arm touches the source repositories) | flagged `contaminated`; excluded and listed; no rerun |
| Timeout, turn limit, no result, no usage | counted as not completed (H5); excluded from token comparisons, kept in the sensitivity table |
| Judge error after the retry batch | excluded from accuracy and completion; listed |

No run is discarded for its answer or its token count.

## 11. Deviations

None at freeze. Amendment 1 (section 12) changes the judge before any scored run.

Entries D1 to D4 below were made on 2026-10-07 for the codex campaign, before any scored run
(none existed: the only agent runs were the unscored smoke runs). They change how the frozen
matrix is executed, not what is measured or how it is analyzed. Harness 1.2.0
(`archivist_bench`) implements them; its README documents the flags.
D5 was made later the same day, during the first scored batch (C1), under the section 10 rule
for an arm isolation stop; harness 1.2.1 implements it.

- **D1 (2026-10-07): archivist-cli v0.2.33 is the tested CLI.** Section 2 leaves the version to
  the archivist-cli release with the leaner tool list, which shipped as v0.2.33 (8 MCP tools: `companies_get`, `companies_search`,
  `filings`, `find`, `read_passage`, `read_section`, `search`, `toc`). The arms run it from an
  isolated install (release checksum verified) placed first on `PATH`. The Codex arm config
  pre approves `filings` and `find` beside the 0.2.32 tool names, which stay listed (an
  approval for a tool the server does not serve has no effect), so `approval_policy = "never"`
  cannot stall a run on the new tools. Claude Code arms are unchanged (`mcp__archivist` already
  allows every Archivist tool).
- **D2 (2026-10-07): plan window guards and resume.** Section 10 reruns an infra failure at
  most twice. A plan usage limit (Codex `usage_limit_exceeded` or `rate_limit_exceeded`, a
  Claude Code 429 or usage limit) or the Archivist monthly quota (`CLI_QUOTA`) would make an
  immediate rerun fail again and burn the remaining attempts, and after a plan limit Codex
  silently draws on paid credits. So such a stop now ends the batch instead of rerunning at
  once: the in flight attempt is `infra_error` and counts toward the three attempts exactly as
  section 10 says, no new attempt starts, and a later `run --resume` continues each infra only
  cell at its next attempt number, never past three (a cell at three failed attempts is
  excluded and listed). A batch also stops, without changing any record's status, when a run
  reports a plan window at or above a threshold (`--stop-at-window`, default 90%) or a sign of
  metered billing (a Codex credit balance below the batch's first, a Codex
  `rate_limit_reached_type`, Claude Code `isUsingOverage`); the record notes the reason.
  Transient failures (`server_overloaded`, 5xx, an Archivist burst `RATE_LIMITED`) keep the
  immediate rerun. Attempt accounting, exclusions and the analysis are unchanged.
  Added the same day after two harness reviews, still before any scored run: a plan gate of
  pre launch headroom plus before and after probes. No CLI switch disables credit or
  overage use, so this is not an account level prevention: a launch admitted below the
  threshold can still spend past it, and the probes detect and report that. Before every Codex
  launch (agent attempt or judge pass B) a fresh `codex app-server` is asked for
  `account/rateLimits/read` (no model call, nothing spent, no mutating method); the launch
  is refused and the batch stops on a failed or incomplete probe (fail closed),
  `ordinaryUsageAllowed` false, a window at or above the threshold (90), a window that rose
  more than 5 points since the previous probe of the batch (`--max-window-step`: one launch
  spent more than the headroom assumes), `rateLimitReachedType` set, `spendControlReached`
  true, or a credit balance below the batch's first probe. A closing probe after the last
  launch of every agent and judge batch reports a credit drop or a hot window (the command
  exits 1 and the batch is listed). Before every Claude Code launch (agent attempt or judge
  pass A) the latest Claude plan reading of the campaign, at most 30 minutes old, must be
  complete (status `allowed`, every window a finite utilization) and show no rejection, no window at or above the threshold, `isUsingOverage` false, and overage
  `rejected` with `overageDisabledReason` `org_level_disabled`; any other state, a missing
  reason included, fails closed. Without such a reading (none yet, older than 30 minutes, or
  a hot reading whose window reset has passed) the harness makes one minimal Claude plan probe
  call (`claude -p --restricted` on `claude-haiku-4-5-20251001`, prompt `Reply with OK.`, no
  tools, one turn, in an empty directory with the allowlisted environment) whose
  `rate_limit_event` becomes the reading; it must show `apiKeySource: none` and a
  `rate_limit_event`, else it fails closed. These probes are plan model calls, not scored runs:
  they are logged in `plan-probes.jsonl` and never count against `--max-runs` or `--max-calls`.
  Residual risk: two concurrent launches plus other consumers of the same login inside the
  last 10 points of a window. A refused or unlaunched preparation ran nothing and is not an
  attempt. A Claude `rate_limit_event` rejection stops the batch whatever the run's result; a
  plan or quota stop reason outranks a transient one within a run; an attempt launched and
  interrupted before its record is reconciled on resume as `infra_error` (`interrupted`) and
  counts toward the three.
- **D3 (2026-10-07): the Archivist monthly counter is reset at run start.** Section 7 and
  `cost-estimate.md` assumed the benchmark account's monthly CLI quota (10,000 calls, 2,056
  used on 2026-10-07, reset 2026-11-01) would carry the campaign. By decision of the authors
  the operator resets the counter before the first scored run (a hand receipt is kept in our
  internal records) instead of waiting for the 2026-11-01 reset; the quota stop of D2 still guards
  an exhaustion.

- **D4 (2026-10-07): batches of question groups.** Section 7 orders runs question major with
  the three arms of a question back to back within a repetition. To pace the shared plan
  windows the campaign runs in batches of question groups (Codex: the two controls with LK01 to
  LK05, then five questions per batch in file order; Claude Code: two or three questions of its
  subset per batch). Within a batch the harness keeps the frozen order (repetition, then
  question, then the three arms back to back); across batches a question's three repetitions
  run closer together in time than in one pass over the whole set. Agents, arms, prompts,
  repetitions and analysis are unchanged; Codex still runs first, then Claude Code.

- **D5 (2026-10-07): Codex MCP resource built ins in the Archivist arms.** During batch C1 (the
  first scored batch) run codex.archivist.CT02.r1 attempt 1 was classified invalid:
  arm_isolation because Codex called its built-in MCP resource tools (list_mcp_resources,
  list_mcp_resource_templates, reported under server `codex`), which only reach the configured
  MCP servers (Archivist alone in that arm). Claude Code's equivalents were already allowed in
  the Archivist arms; harness 1.2.1 allows Codex's in the archivist and both arms only (still a
  violation in the web arm). Per section 10 the batch stopped, the cause is fixed here, the
  invalid record stays in the ledger and is excluded, and the affected cell is rerun once
  (`run --resume --rerun-invalid`). No other run was affected; the six other C1 runs completed
  before the stop and stand.

Harness 1.2.0 also adds the human audit export (`audit-export`), audit application
(`analyze --audit`) and the committed results report (`report`). They implement section 8's
human audit and the reporting of sections 4 to 9; the analysis plan is unchanged.

## 12. Amendment 1 (2026-10-06): judges on the operator's plans

**Status when made.** No scored run exists: the only live agent runs are the nine unscored
smoke and re-verification runs (section 7, `smoke-results.md`), and no judge had ever been
called (no `judgement.json` existed in any evidence directory; the Vertex executor had only been
tested against a local fake server). Changing the judge now therefore cannot be informed by any
outcome, which is what makes this amendment legitimate under the freeze rule. Made before any
codex campaign run, kept beside the original section 8 text.

**Decisions (2026-10-06).** Judging uses the operator's Claude and ChatGPT plans instead of a
metered Gemini judge, and no Gemini model is to be used for testing anywhere. The answer keys are
accepted as built. The earlier decision (Codex for reviews, responsible spend, record every run)
still applies.

- No Gemini model was used anywhere in the smoke runs or the harness work before them: the agents under test are Codex and Claude Code,
  the reviews were Codex sessions, and the Gemini judge was never called.
- **Answer key acceptance:** the authors accept the answer keys in `questions.json` as built and
  machine verified (section 3). This settles the open review of the answer keys; it is not answer
  quality acceptance, which remains a human decision by the authors after the codex campaign.

**What changes in section 8.**

| | At freeze | Amendment 1 |
|---|---|---|
| Judges | `gemini-3.1-pro-preview` on Vertex (Mosaic's Google Cloud project), metered | pass A: Claude Code 2.1.289 `claude -p --restricted`, `claude-opus-5-5`, effort `medium`, Claude subscription (plan allowance, no API key, never `--bare`); pass B: Codex CLI 0.160.0 `codex exec --ephemeral`, `gpt-6.1-sol`, effort `medium`, ChatGPT login (plan allowance, no `CODEX_API_KEY`) |
| Sampling setting | temperature 0 | neither CLI exposes temperature: effort is fixed (`medium` for both) and recorded per call; temperature stays each CLI's default |
| Prompt | pass A key then answer; pass B answer then key with a seeded shuffle | one identical prompt to both judges: rubric (unchanged, verbatim in section 8), question, answer key with its facts shuffled by `random.Random("8105:<run id>:key")`, then the answer blinded by `redact.blind`; fact labels F1..Fn as before |
| Output | JSON mode | structured output against the same verdict schema (Claude Code `--json-schema`, Codex `--output-schema`), then the same strict parser |
| Isolation | none needed (API call) | each call in a fresh empty directory with the allowlisted environment; Claude Code with no tools (only its `StructuredOutput` tool), no MCP server, `apiKeySource: none`; Codex read only sandbox, no shell, no web search, no MCP, every extension feature off, `forced_login_method = "chatgpt"` after a check that the cached login is a ChatGPT login without an API key; a breach is a failed call |

Why one prompt instead of two orders: with two judges, keeping pass A key first and pass B answer
first would confound the judge family with the prompt order, so neither the agreement rule nor
the bias check below could separate them. The seeded key shuffle keeps a fixed fact order from
favouring any position; cross family agreement replaces cross order agreement as the
independence check.

**Unchanged.** Rubric text; blinding; per fact verdicts and `complete`; agreement scores the
verdict, disagreement makes the fact `disputed`, scores 0.5 and queues it for human audit (a
disagreement between the two families always reaches a person); the 5% seeded audit sample of
agreed facts; audit corrections replace judge verdicts; one retry of a malformed verdict, one
later batch for a `judge_error`, then exclusion and listing; controls scored without a judge;
completion (section 9) from both judges' `complete`. Added: a plan usage or rate limit, or the
judge call ceiling, stops the judge batch and leaves the answer's state as it was, so neither
ever excludes an answer (a crash mid answer still counts its batch).

**Judge bias check (added to the analysis plan, exploratory).** Both judges are from the agent
families (Anthropic judges Claude Code answers, OpenAI judges Codex answers), so a family effect
is checked, not assumed away. `analyze` reports (`judge_bias`, `single_judge_sensitivity`):

1. Verdict rates (`correct`, `incorrect`, `missing`, `complete`) per judge family and per
   answering agent family.
2. Agreement rate (share of facts not disputed) per answering agent family.
3. Family interaction: (Claude judge correct share on Claude Code answers minus on Codex
   answers) minus (Codex judge correct share on Claude Code answers minus on Codex answers),
   over the questions both agents answered (the Claude Code subset, section 7), with a 95%
   percentile bootstrap interval that resamples those questions (the same indices for both
   families; arms and repetitions of a question share its facts, so the question is the unit),
   10,000 resamples, seed 8105.
4. Every accuracy and completion comparison of section 6 recomputed from each judge alone.

Interpretation. The interaction measures differential judge agreement by answering family.
Self preference would raise it above 0, but so can two judges of different strictness meeting
answers of different quality, so it is not proof of self preference. Rule: the interaction is
flagged when its interval excludes 0; a flag is reported with the results and investigated
through the human audit (disputed facts split by answering family), not taken as a finding.
Either way it does not change the confirmatory scoring, because each hypothesis compares arms
within one agent, every arm of an agent is graded by the same two judges, and every disputed
fact is resolved by the human audit. The codex campaign report applies the section 4 decision rules to the
two single judge recomputations as well and labels a hypothesis judge sensitive when its
verdict (supported, partly, not supported) differs; no code decides hypotheses, for the main
analysis either.

**Spend.** Judging draws on the two plan allowances; no metered judge spend remains
(`cost-estimate.md` section 4).

**Validation.** Hermetic tests cover both executors (argv, environment, isolation, plan limit
handling) and the bias report. One live call per judge graded one unscored smoke answer
(`codex.archivist.LK07`, `smoke-results.md`), and after the review repairs one more call per
judge graded `codex.both.LK07`: every call returned a schema valid verdict and the judges agreed.

## 13. Amendment 2 (2026-10-07): Codex only, one Codex judge

**Decision (2026-10-07).** The Archivist counter is reset and the campaign proceeds with no
Claude calls: the aim is now the Codex results alone, completed quickly; the Claude Code version
follows later.

**Status when made.** Codex batches C1 to C17 (784 attempts) were complete and C18 (RC11 to
RC15) had not started. No Claude Code agent run existed. Judge batch J1 had graded 551 of the
765 scored Codex answers with both judges (pass A Claude Code, pass B Codex) when an overnight
host restart stopped it. No analysis had been run and no accuracy, token ratio or completion
figure had been computed by anyone, so the amendment cannot be informed by an outcome.

**What changes.**

| | Before | Amendment 2 |
|---|---|---|
| Agents | Codex (primary, 828 runs) then Claude Code (control confirmation, 288 runs) | Codex only. The Claude Code agent arm is deferred to a later run under this pre registration; its subset and rules stay as written |
| Judges | pass A Claude Code and pass B Codex on every scored answer | pass B (Codex, `gpt-6.1-sol`, medium, ChatGPT plan) on every scored answer; no new Claude call of any kind (no pass A, no Claude plan probe) |
| Fact score | agreement scores the verdict; disagreement is `disputed` (0.5) | the pass B verdict scores every fact (correct 1, otherwise 0), the same rule for every arm; audit corrections replace it |
| Completion (H5) | both judges' `complete` (0.5 on disagreement) | pass B's `complete` |
| Human audit | every disputed fact plus a seeded 5% of agreed facts | every fact where an existing pass A verdict differs from pass B (the 551 answers graded before this amendment keep their pass A verdicts for exactly this use) plus a seeded 5% (seed 8105) of all other facts |
| Judge bias check, single judge sensitivity | reported | deferred with the Claude Code arm: the family interaction needs both answering families and two judges on every answer. The descriptive pass A and pass B agreement rate on the 551 doubly graded answers is reported, labelled exploratory |

**Why the arm comparison stays valid.** Every hypothesis compares arms within Codex, and every
arm's answers are graded by the same judge with the same blinded prompt, so a judge preference
for its own model family affects every arm alike and cannot create an arm difference. The
absolute accuracy level can carry that family bias; the human audit (disputed facts and the 5%
sample) is the check, and results state that the judge is from the answering family.

**Unchanged.** Questions, keys, prompt, arms, repetitions, statistics (section 6), decision rules
(section 4, applied to Codex only), exclusions (section 10), blinding and rubric. The answer
whose pass A call the restart interrupted (`codex.both.BR04.r1`, recorded as a `judge_error`
with no verdict) is graded by pass B in the next batch, the one later batch section 8 allows.

**Note (2026-10-07, after the analysis, wording only).** The paragraph "Why the arm comparison
stays valid" above overstates its point: one judge with one blinded prompt for every arm removes
the obvious ways the judge could tell the arms apart, but it does not prove that no
arm dependent judging bias exists; that remains untested until the judge bias check runs with
the Claude Code arm. The design and the scoring are unchanged; the results state this caveat.

## 14. Amendment 3 (2026-10-07): grounding, provenance and the forum cross check

**Decisions (2026-10-07).** Provenance framing is approved: every claim traces to an
accountable filed source and the agent reads fewer sources nobody is accountable for; filings
carry management's own framing. The cross check asks whether the forum rumors have merit in the
actual facts and is reported in both directions. Codex only (amendment 2) for every grading pass
and agent run, on the ChatGPT plan allowance; spend what is necessary, responsibly.

**Status when made.** The codex campaign is complete: 828 Codex runs with a final `completed`
record (829 attempts), every scored answer judged by pass B, the analysis and results committed
(`../results/codex/`). No grounding measure had been
computed: no source parse, host category, claim extraction, attribution, cross check or trap
judgement existed for any run, and no contamination question or run existed. This amendment is
committed before any of them, so its rubric cannot be informed by their outcomes, except as
recorded in G0.

**Deviation G0 (2026-10-07).** Before this freeze, on 2026-10-07, the session coordinating the
campaign saw raw domain counts of the codex campaign rollouts: web arm runs exposing Reddit
results 239 of 276, both arm runs 186 of 276, archivist arm 0. The session that wrote this
amendment's rubric and code inspected the rollout schema only (record types, action type counts,
one run's structure), no domain counts. Because a reading of the data preceded the rubric, every
grounding measure over the codex campaign runs is exploratory and labelled so; only H6 below
(new runs) is pre registered.

### 14.1 Source categories

One category per host. When a host fits several, precedence is: filing, issuer, forum,
promotional, aggregator, news, reference, other.

| Category | Definition | Seed hosts |
|---|---|---|
| `filing` | regulator filing systems and Archivist results (filing passages through Mosaic) | the hosts matched by the harness's upstream filing host pattern (`redact.UPSTREAM_HOST_PATTERN`, named by reference only); Mosaic passage permalinks |
| `issuer` | the issuer's own domains and press release wires carrying issuer authored releases | businesswire.com, prnewswire.com, globenewswire.com, accesswire.com, newsfilecorp.com, publicnow.com |
| `reference` | encyclopedias, exchanges, regulators' non filing pages, statistics agencies | wikipedia.org, wikidata.org, britannica.com, investopedia.com, nyse.com, borsaistanbul.com |
| `news` | journalism under editorial standards | reuters.com, bloomberg.com, wsj.com, ft.com, cnbc.com, apnews.com, marketwatch.com, barrons.com, theglobeandmail.com, financialpost.com, aa.com.tr, bloomberght.com, bnnbloomberg.ca, nytimes.com, bbc.com |
| `aggregator` | sites that republish, compute or summarize company financial data, transcripts or filings at scale, including contributor analysis platforms | finance.yahoo.com, nasdaq.com, investing.com and its locales, marketscreener.com, stockanalysis.com, macrotrends.net, companiesmarketcap.com, simplywall.st, zacks.com, seekingalpha.com, morningstar.com, wisesheets.io, gurufocus.com, tipranks.com, marketbeat.com, fintel.io, fool.com |
| `forum` | user generated content without editorial review | reddit.com, stocktwits.com, x.com, twitter.com, hotcopper.com.au, investorshub.com, quora.com, eksisozluk.com, medium.com, substack.com, youtube.com |
| `promotional` | content paid for or written to sell a security, subscription or product (sponsored posts, stock promotion, affiliate "best stocks" lists) | none |
| `other` | anything else, and a result without URL or domain (host `unknown`) | none |

Hosts are compared lowercase without port and leading `www.`; a seed matches the exact host or
any subdomain. Every other host goes to pass H once (Codex judge configuration, batches of up to
40 hosts, each with up to 3 titles and snippets seen for it and the tickers of the questions
where it appeared); it returns one category per host (never `filing`) and a reason under 20 words
(longer reasons are clipped to 19 words and flagged). The resulting host table is committed as
`host-categories.csv` (basis `seed`, `pass_h`, or `unclassified` for a host whose pass H batch
failed twice, counted as `other` and listed). Filing hosts are committed as one `filing-source`
row. Host level classification of mixed sites (a news site with a forum section, a platform
with both editorial and contributor pages) is a stated limitation.

Pass H prompt (verbatim, `archivist_bench/grounding.py` `PASS_H_RUBRIC`; the host list
follows):

```text
You classify web hosts by the kind of source they are, for a study of where research agents got their information.
Categories (one per host; when a host fits several, take the first in this order: issuer, forum, promotional, aggregator, news, reference, other):
- issuer: the issuer's own domains and press release wires carrying issuer authored releases.
- forum: user generated content without editorial review.
- promotional: content paid for or written to sell a security, subscription or product (sponsored posts, stock promotion, affiliate "best stocks" lists).
- aggregator: sites that republish, compute or summarize company financial data, transcripts or filings at scale, including contributor analysis platforms.
- news: journalism under editorial standards.
- reference: encyclopedias, exchanges, regulators' non filing pages, statistics agencies.
- other: anything else.
Each host comes with up to three page titles and snippets seen for it and the companies (tickers) of the research questions where it appeared. Classify the host as a whole.
Return only JSON: {"hosts": [{"id": "<host label>", "category": "<category>", "reason": "<under 20 words>"}]}. Include every host label exactly once.
```

### 14.2 Exposure (deterministic, per run, exploratory)

From each completed run's Codex rollout (`archivist_bench/grounding.py` `parse_sources`):

- `shown` sources: results of `web.search` items with action `search`, injected by the search
  tool; one per distinct result URL (host from the result's `domain`, else its URL; a result
  without either is host `unknown`, category `other`).
- `opened` sources: pages the agent opened, found in or clicked (actions `openPage`,
  `findInPage`, `other`, and any result whose ref id contains `view`); one per distinct URL. The
  exec output block `TITLE (URL)` followed by the cite marker of a ref attaches its text to that
  ref's source (search refs to the shown source, `view` refs to the opened page); a block whose
  ref is unknown makes its own source, and without a parsable URL keeps its text under host
  `unknown`.
- `archivist` sources: completed Archivist MCP calls (not errors), category `filing`, one per
  distinct `filing_id` in the result (else one per call), the result text as its text.

Reported per arm and stratum (and pooled over the scored strata): each category's share of the
distinct shown URLs and of the distinct opened plus Archivist sources, pooled (sum over sum) and
as the mean per run share (question means); the share of runs where a forum, an aggregator or
either was shown only by search, opened, or touched at all (search tool injection separated from
agent opens). Intervals: 95% percentile bootstrap over questions (repetitions averaged first),
seed 8105, 10,000 resamples.

### 14.3 Reliance (pass R plus a deterministic rule, exploratory over the codex campaign)

Pass R (Codex judge configuration) reads each completed scored answer with every link replaced
by a source token `[S1]..[Sn]` (one per distinct URL; the token map stays outside the prompt)
and tool terms blinded by `redact.blind`, after the question for context. Prompt (verbatim,
`PASS_R_RUBRIC`):

```text
You list the claims one research answer makes about companies. Links in the answer are replaced by source tokens [S1]..[Sn]; tool and site names are hidden.
Rules:
- List every factual claim about a company: a figure, a date, an entity or an event. For each give "type": "fact", "text" (the claim in the answer's words, at most 40 words), "values" (every figure and named entity of the claim as written in the answer, never a year alone), "sources" (the [S#] tokens the answer attaches to the claim, [] when none), "attributed_to": "none" and "phrase": "".
- List every evaluative statement: a judgment, characterization or recommendation. For each give "type": "evaluative", "text", "values": [], "sources", "attributed_to": "filer" when the answer presents it as the company's own statement, "third_party" when it attributes it to someone else, "none" when the answer makes it itself, and "phrase": its key phrase as written, at most six words.
- Do not judge whether a claim is true and do not add claims the answer does not make.
Return only JSON: {"claims": [{"type": "...", "text": "...", "values": ["..."], "sources": ["S1"], "attributed_to": "...", "phrase": "..."}]}; an empty list is valid.
```

A malformed reply (unknown type or attribution, a token the answer does not have, an evaluative
phrase outside 1 to 6 words) is retried once, then the answer is a `grade_error`, listed and
excluded (judged once more in one later batch).

Deterministic attribution of each fact claim against the sources of the same run (texts the
model read: shown snippets, opened blocks, Archivist results):

- Values are normalized: thousands separators (comma, dot, narrow and non breaking spaces; the
  last of comma and dot is the decimal mark when both occur), currency, percent and scale words
  (thousand, million, billion, trillion, k, m, bn) carry no weight, signs are ignored. A numeric
  value matches a source number when the source number, under any power of 1,000 scaling, rounds
  to the claim's shown precision as the claim (half a unit of the last shown digit either side,
  inclusive). A number with fewer than two significant digits (trailing zeros of an integer not
  counted) and a bare year (four digits 1900 to 2100) is not usable. A named entity matches
  case and accent insensitively as a substring (at least two characters with a letter).
- `filing` when every usable value occurs in a filing source read in the run; else `secondary`
  (the category, by precedence, of the non filing sources holding a value or cited by the claim)
  when a value occurs in a non filing source or a non filing source is cited; else
  `unattributed`. A claim without a usable value takes the category of its citations by
  precedence (`unattributed` without one) and is flagged `citation_only`.
- Secondary use: a claim citing a non filing source; `primary_read` when a filing source in the
  same run holds every usable value.
- Both arm cross check: a claim whose every usable value appeared in a non filing source before an
  Archivist result holding it (rollout order) is `verified_by_archivist`; the denominator is the
  claims whose every usable value appeared in a non filing source.
- Evaluative statements: `absent_from_filing` when `attributed_to` is not `filer` and the key
  phrase's content words (three or more letters, common function words dropped) do not all occur
  in one filing source read in the run.

Reported per arm and stratum: claim attribution shares (filing, secondary by category,
unattributed, citation only), secondary use and the primary reads behind it, both arm
verification by Archivist, and evaluative statements absent from the filing, each a share of
claims (sum over sum, every question of the cell counted) with the 14.2 bootstrap interval.

### 14.4 Forum and aggregator cross check (exploratory)

- Pool: every forum and aggregator source text (shown snippets and opened page blocks) across the
  828 runs; long texts are split into windows of at most 8,000 characters overlapping by 1,000
  (nothing is clipped; a claim seen in two windows collapses in the claim dedupe), each window deduplicated by the hash of its folded text and keeping the stratum
  of its first run (run id order).
- Pass X (Codex judge configuration, batches of up to 15 texts, each with its category and the
  question tickers, never its host or URL) extracts claims about named companies; an empty reply
  is valid. Prompt (verbatim, `PASS_X_RUBRIC`):

```text
You extract the claims web texts make about named companies, for a later check against the companies' filings.
Each text comes from a forum or aggregator page a research agent saw; its source category and the companies (tickers) of the research questions are given.
Rules:
- List every claim a text makes about a named company. For each give "text" (the text label), "company" (as named), "metric" (what is measured or asserted, such as revenue, dividend per share or chief executive), "value" (as written), "period" (the fiscal period or date the value refers to, "" when none), "stated_date" (the date the text says it was written or published, "" when none) and "kind": "reported_figure" (a figure the company reported), "forecast" (a projection or guidance), "estimate" (an analyst or consensus estimate), "opinion" (a judgment), "rumor" (an unconfirmed report) or "other".
- Copy values as written; do not correct, compute or add anything the text does not say.
- A text without a claim about a named company gives no claims.
Return only JSON: {"claims": [{"text": "<text label>", "company": "...", "metric": "...", "value": "...", "period": "...", "stated_date": "...", "kind": "..."}]}; an empty list is valid.
```

- Claims are deduplicated on normalized company, metric, value and period (first occurrence
  kept, occurrences counted).
- Sample: 400 claims (seed 8105), stratified by source category (forum, aggregator) and question
  stratum, proportional with at least 10 per nonempty cell (all of a smaller cell), fewer only
  when the pool is smaller; largest remainder rounding.
- Pass C: Codex with the archivist arm configuration (Archivist MCP only, no web, no shell) and
  `--output-schema`, Codex CLI 0.160.0, `gpt-6.1-sol`, effort `medium`, default tier, ChatGPT
  plan; the prompt gives the claim, its source category and stated date (never the host or
  URL). A web call or any tool outside Archivist (the Codex MCP resource built ins allowed) is
  `invalid: arm_isolation` and reruns once; an Archivist quota or the ceiling gate stops the
  batch (resumable). Prompt (verbatim, `archivist_bench/crosscheck.py` `PASS_C_RUBRIC`, the
  placeholders filled per claim):

```text
Check one claim taken from a {category} web page against the company's filings, using your research tools (the filings). Do not run shell commands.
Claim: company {company}; metric {metric}; value {value}; period {period}; stated date {stated_date}; kind {kind}.
Give "bin":
- "confirmed": the filings state the same value for the same company, metric and period (equivalent formatting and rounding count as the same).
- "contradicted" with "subtype" "wrong_number" (the filings give a different value), "stale_period" (the value is the filings' figure for another period), "adjusted_mixed_with_reported" (an adjusted or non GAAP figure presented as the reported one, or the reverse) or "wrong_company" (the figure belongs to another company or entity).
- "not_checkable" with "subtype" "opinion", "forecast", "consensus_estimate" or "rumor": the claim is not a statement of a filed fact.
- "off_target": the claim is not about a covered issuer, or the filings in the corpus cannot settle it.
Use "subtype" "none" for confirmed and off_target. For confirmed and contradicted give the Mosaic "permalink" of the passage you relied on, its "exchange_document_id" (or "document_id_absent_reason" when none is stored) and an exact "quote" from that passage; leave them "" otherwise. Set "restatement" true with a "restatement_note" when a later filing restated the figure. For a claim with a stated date, when a filing after that date settles it, give "merit" "later_confirmed" or "later_refuted", or "not_settled" when no later filing settles it; give "not_applicable" for a claim without a stated date.
Return only the JSON object of the output schema.
```

- Bins: `confirmed`; `contradicted` with subtype `wrong_number`, `stale_period`,
  `adjusted_mixed_with_reported` or `wrong_company`; `not_checkable` with `opinion`, `forecast`,
  `consensus_estimate` or `rumor`; `off_target` (not about a covered issuer or not in the
  corpus). Confirmed and contradicted rows carry a Mosaic passage permalink, the exchange
  document id or the reason none is stored, and a quote; every row carries `restatement` (with
  a note) and `merit` (`later_confirmed`, `later_refuted`, `not_settled` for dated claims with
  a later filing; `not_applicable` without a stated date).
- Rates: each bin's share of the validly checked rows with Wilson 95% intervals, by category and
  stratum, per category and overall, confirmed and contradicted side by side in one table, plus
  the contradicted share of checkable rows; subtypes, merit and restatements counted. Scope
  stated beside every rate: "content search ranked for these questions".
- Audit: a seeded sample (8105) of 30 contradicted rows (all when fewer) plus 10 confirmed rows,
  exported with blank human review columns (`audit_verdict`, `audit_note`); the authors fill
  them.

### 14.5 Contamination stratum and H6 (new runs, pre registered)

About 20 questions (16 to 24): 12 to 19 trap questions (about 15) from contradicted rows whose
filing fact is reportable as a sourced key fact, and control questions (5) from confirmed rows. Phrased as users ask: the company and the period, never the document or form. Each
question has `stratum` `contamination`, a `role` (`trap` or `control`), its `crosscheck_rows`,
sourced key facts under the section 3 rules (filing UUID, chunk id, Mosaic permalink from
`archivist read passage <chunk> --window 0`, exchange document id or absence reason, a quote
verified against the stored chunk text with the section 3 normalization by `verify-quotes`) and,
for trap questions, `trap` facts (the wrong web value, which differs from the value of the
sourced fact it contradicts, its cross check row, its source category and that sourced fact;
never scored for accuracy). Every cross check row a question names is a valid pass C result:
`contradicted` for a trap question, `confirmed` for a control; each trap fact is bound to its row:
same category and value. The set
(`contamination-questions.json`, `"set": "contamination"`) passes `validate-questions
--contamination --crosscheck` and `verify-quotes` and is committed as addendum G1 (dated) before
any contamination run.

Runs: Codex only, prompt, arms, model, effort, tier, Codex CLI 0.160.0, archivist-cli 0.2.33, 3
repetitions, concurrency 2, D2 and D5 unchanged from the codex campaign, plus the Archivist ceiling gate
(no archivist or both arm launch at `cli_this_month` at or above 9,800, nor on a failed usage
read). Accuracy: pass B with the section 8 rubric over the sourced facts. Trap adoption per run:
pass T (`adopted` 1, else 0, mean over the question's traps), question means. Pass T prompt
(verbatim, `PASS_T_RUBRIC`; the traps with the filing value and the blinded answer follow):

```text
You check whether one research answer repeats known wrong values. Each trap is a wrong value seen on a web page, given with the value the company's filing states.
For each trap give "verdict": "adopted" when the answer states the wrong value as fact for that item; "rejected" when the answer mentions the wrong value only to say it is wrong or superseded; "absent" when the answer does not state the wrong value.
Ignore length, style and how sources are cited. Links and tool names are hidden.
Return only JSON: {"traps": [{"id": "<trap label>", "verdict": "adopted|rejected|absent"}]}. Include every trap label exactly once.
```

**H6** (pre registered here, before the runs): for archivist and for both, the trap adoption
difference (web minus arm, paired bootstrap over trap questions, section 6 settings) with CI
lower bound above 0 is supported; a point estimate above 0 is partly supported; else not
supported. Applied by hand. Tokens, accuracy and claim attribution (share of fact claims
attributed to filing, arm minus web) are reported per section 6 beside it.

### 14.6 Measures, labels and what stays unchanged

- Every grading pass (H, R, X, C, T) runs on Codex CLI 0.160.0, `gpt-6.1-sol`, effort `medium`,
  default tier, ChatGPT plan allowance, behind the section 11 D2 plan gate and closing probe;
  no Claude call of any kind, no metered key, credits or fast tier. A malformed reply is
  retried once, then `grade_error` (listed; one later batch, then excluded). Grader prompts carry
  no upstream filing URL or host.
- Exploratory: every exposure, reliance and cross check measure over the codex campaign runs (G0).
  Pre registered: H6 and the contamination stratum measures of 14.5.
- Unchanged: sections 1 to 13, H1 to H5 and every codex campaign file and number; the codex
  campaign raw evidence is read only. Committed outputs carry no upstream filing host (`filing-source` instead) and stay
  under 1 MB per file.
- Harness 1.4.0 (`archivist_bench`, `docs/HARNESS.md` "Amendment 3: grounding and
  cross check") implements this section.

**Deviation G2 (2026-10-07): numbers only attribution sensitivity.** Made while pass R was
extracting claims and before any attribution, exposure cell or cross check result was computed.
Motivation: a pooled count over the first 230 extracted answers (no arm split) showed 138 of 989
fact claims carrying a document name (form or report title) as an entity value, which the 14.3
rule must find verbatim in a source. The 14.3 rule stays the primary rule. Beside it every
reliance share is also reported under the numbers only rule: a claim with at least one usable
number is matched on its usable numbers only (its named entities ignored); a claim without one is
unchanged. Both are exploratory; neither replaces the other (`grounding.attribute_fact`
`numbers_only`, reliance cells with `rule` `primary` or `numbers_only`, and the contamination
filing attribution under each rule).

**Deviation G3 (2026-10-07): pass X on a seeded text sample.** Made after pass X's first batch
and before any claim sample, check, rate or attribution. Motivation: throughput (a pass X call
took 325 s; the 13,525 pool texts need 902 calls) and a yield of about 8.7 claims per text, so
extracting every text is disproportionate for a 400 claim sample. Rule: pass X runs on a seeded
stratified sample of 1,500 pool texts (seed 8105), stratified by source category and question
stratum, proportional with at least 60 per nonempty cell (all of a smaller cell), largest
remainder rounding, texts in text id order within a cell (`crosscheck.text_sample`, written to
`crosscheck/text-sample.json`). Only the claims of those texts are deduplicated, and the 400
claim sample is then drawn exactly as 14.4. Pool texts outside the text sample are not
extracted; any extraction already stored for a text outside it is ignored. Execution only, no
rubric change: the grading passes may run with up to 4 workers (pass C up to 2), with the same
outputs as one worker.
Note (2026-10-07, execution only, after 142 of 400 pass C checks): pass C may also run with up to 4
workers; the checker's prompt, configuration, isolation, Archivist gate and outputs are unchanged.

**Deviation G4 (2026-10-07): an extension sample for trap sourcing.** Made after pass C over the
400 claim sample and before any contamination question existed. Motivation: 14 contradicted rows
of 400 (10 of 190 `reported_figure` claims), about 7 to 9 of them usable as clean traps, below
the 14.5 minimum of 12 trap questions. Rule: a seeded extension sample of 300 claims (seed 8106)
drawn from the deduplicated claims not in the 400 claim sample, `kind` `reported_figure` only,
stratified by source category and question stratum, proportional with at least 10 per nonempty
cell (`crosscheck.allocate`), rows numbered E001..E300 (`crosscheck/sample-extension.json`),
checked by the same pass C (prompt, configuration, isolation and Archivist gate unchanged).
Extension rows source trap and control questions only: they never enter the 14.4 rates,
subtypes, merit counts or the audit sample, which stay on the 400 rows; their bins are reported
separately as a count table labelled "extension, trap sourcing only".

**Addendum G1 (2026-10-07): the contamination question set.** Committed before any contamination
run. `contamination-questions.json`: 17 questions, 12 trap questions (13 trap facts) and 5
controls, 46 facts. Traps come from contradicted cross check rows whose web value the authors
confirmed wrong for the period each question asks, read against the filing: X105 (GE Aerospace,
adjusted revenue given as revenue), X352 (Tesla cash), X311 (Kraken Robotics revenue, a scale
error), X240 (CN diluted EPS, adjusted given as reported), X319 (Home Depot chief executive pay,
a stale year), X365 (Kohl's net sales change, a stale quarter), and from the G4 extension E036
(Coca Cola İçecek employees, stale), E205 (Meta daily active people, stale), E231 (J&J Snack
Foods operating income), E240 (Royal Bank of Canada Tier 1 ratio), E103 (NIKE demand creation
change, a segment figure given as the total) and E199 with E214 (AutoZone shares, "in thousands"
dropped). Controls come from confirmed rows X191, X163, X300, X387 and X011. Contradicted rows
not used, with the reason recorded in our internal records (not published): X049, X053, X056, X066, X130, X144, X233,
X358, E008, E062, E293, E296, E298 (checker misreads, extraction noise, a value equal to the
filing's, a correct figure for another period than the one checked, or no clean question). Weakest
traps, kept and noted: CN17 is wrong by scale only and CN15's value probably belongs to another
bank. Validation: `validate-questions --contamination --crosscheck` 0 violations; `verify-quotes`
33 sourced facts, 0 violations (run with the isolated archivist-cli 0.2.33 first on `PATH`).

## 15. Amendment 4 (2026-10-07): a light Claude Code replication

**Decisions (2026-10-07).** The pure Codex arm is completed first; then a light Claude Code pass
runs and the results are written up. Spend: what is necessary, responsibly; Claude Code and Codex
run on plan allowance only, never API billing or Vertex, behind the harness plan window guards
with a conservative stop, recorded, so the benchmark does not exhaust the Claude plan window that
the authors' other live Claude Code sessions share. The Archivist counter reset on the benchmark account
stays authorized (2026-10-06), with a hand receipt, if needed.

**Status when made.** The codex campaign (828 Codex runs) and the grounding campaign (grounding,
the forum cross check and 153 contamination runs) are complete and their results are known to
the authors, so
this design is informed by them: it is a replication, not new confirmatory evidence. No Claude
Code benchmark run exists. The only Claude Code agent calls so far are the smoke runs (CT01,
section 7) and, on 2026-10-07T23:33Z, one unscored schema probe (Claude Code 2.1.289,
`claude-haiku-4-5-20251001`, web tools only, a question outside the benchmark) that read the
WebSearch and WebFetch result shape. This amendment is committed before any Claude Code
benchmark run, judge call or grading call of the claude-code campaign.

### 15.1 Design

| | Amendment 4 |
|---|---|
| Agent | Claude Code 2.1.289 (the pinned install of section 2), `claude-opus-5-5`, effort `medium`, default tier, `claude -p --restricted` on the Claude subscription (`apiKeySource: none`; never `--bare` or an API key), 50 turns, 1,200 s wall |
| Unchanged | arms, prompt, isolation (section 2), archivist-cli 0.2.33 and deviations D1 to D5 |
| Main set | the section 7 Claude Code subset: LK04, LK05, LK06, LK09, LK15, MH01, MH02, MH03, MH04, MH14, MD04, MD09, MD12, MD14, MD15, MP01, MP02, MP05, MP06, MP15, BR01, BR03, BR07, BR12, BR15, RC01, RC03, RC07, RC14, RC15, CT01, CT02 |
| Main set repetitions | one (`r1`; section 7 said three), 3 arms: 96 runs |
| Contamination subset | within each role of `contamination-questions.json` the ids with the smallest `sha256("8105:<id>")` (`questions.claude_contamination_subset`): traps CN04, CN15, CN03, CN16, CN05, CN02; controls CN10, CN11 |
| Contamination repetitions | one, 3 arms: 24 runs |
| Total | 120 runs; attempt cap 132 (reruns of section 10 included) |
| Order (D4) | concurrency 1; batches K1 (CT01, CT02 and the LK questions), K2 (MH), K3 (MD), K4 (MP), K5 (BR), K6 (RC), KC (contamination) |

Codex is not rerun: every Codex figure comes from the codex and grounding campaign evidence as it
stands.

### 15.2 Judging and the judge bias check

- Pass B (Codex, the section 12 prompt) grades every scored Claude Code answer, and the scoring
  rule is `--primary-judge B`, the rule of the Codex numbers (amendment 2). For Claude Code
  answers pass B is the cross family judge.
- Pass A (the Claude Code judge of section 12) grades every scored main set answer (90) for the
  single judge sensitivity and the bias check. Contamination answers get pass B and pass T only.
- Judge bias check (the section 12 measures, exploratory; the family interaction is flagged
  when its interval excludes 0): the Claude Code answers of this pass plus the codex campaign answers
  already graded by both judges, limited to the main set questions. In practice those are the
  20 lookup, multi hop, multi document and multi period questions plus whatever BR01 and BR03
  runs carry both passes (no reach question has both). No new Claude call is made on a Codex
  answer. Single judge sensitivity (section 12, item 4) is reported for the Claude Code answers.
- **Note (2026-10-08, before any Claude Code benchmark run, judge or grading call): the
  estimator stays.** Section 12 item 3 says "correct share". `judge._family_interaction`
  computes it as the mean of per run correct shares (facts a judge marks `correct` over the
  run's facts, `single_judge_accuracy`) over the runs of the shared questions, with the
  question as the bootstrap unit. Section 12's text and this estimator were committed together
  (amendment 1, 2026-10-06) before any scored run or judge call, so it is the pre
  registered reading of "correct share". A fact pooled share is not substituted now, after the
  codex campaign judgements are known, because that would be an outcome informed choice. The
  results name the estimator.

### 15.3 Claude Code sources (sections 14.2 and 14.3 applied to Claude Code streams)

| Kind | Claude Code rule |
|---|---|
| `shown` | WebSearch result links, one per distinct URL, with title and URL (Claude Code returns no per result snippet) |
| `summary` | the WebSearch tool's own summary text, one source per call, host `web-search-summary`, category `other`: excluded from the exposure shares and flags (reported as a count, `search_summaries`), included in attribution as a non filing source |
| `opened` | WebFetch URLs, one per distinct URL, with the fetch tool's extract as the text (not the raw page, unlike Codex: a stated limitation) |
| `archivist` | completed Archivist results, one per distinct `filing_id` (else one per call), category `filing` |

A result flagged `is_error` is not a source. A run whose stream has no `result` event, a result
other than `success` or no usage gets no sources file and is listed. The host table is the grounding
campaign table, copied in first; pass H runs for new hosts only. Pass R, both attribution rules (14.3 and
deviation G2) and pass T are unchanged and run on the Codex graders.

### 15.4 Plan window and Archivist quota

- Every Claude launch (agent runs and judge pass A) runs with `--stop-at-window 70` (any Claude
  window at or above 70% refuses it) and concurrency 1. The pre launch gate (D2) also fails
  closed on a status other than `allowed` (Anthropic's 75% warning sets `allowed_warning`) and on
  missing overage fields. The Codex graders keep 90 and the 5 point window step.
- At this amendment the seven day Claude window read 89% (`allowed_warning`, resets
  2026-10-11T06:00Z; five hour window 50%), so no run starts before that reset unless the
  authors decide otherwise. A missing overage field after the reset keeps failing closed
  and needs a decision by the authors, never a harness bypass. The stop is never raised to fit
  the campaign.
- **Note (2026-10-08, before any Claude Code benchmark run, judge or grading call): a separate
  Claude subscription login.** The 89% reading above was taken on one of the operator's Claude
  subscription logins, which is resting together with a second one, so the campaign runs on a
  separate Claude subscription login chosen explicitly with `--claude-config-dir` (its own Claude
  configuration directory, chosen on 2026-10-08). Harness 1.5.1 passes that one explicitly chosen
  directory (`--claude-config-dir`) to every Claude Code run, every pass A judge call and the
  plan gate's Claude probe, records it, counts only readings of that login and strips every
  other inherited `CLAUDE*` variable. The reading of that login through the harness probe
  (`plan-probe`, 2026-10-08T02:11:16Z): status `allowed`, five hour window 4% (resets
  2026-10-08T06:20:00Z), seven day window 1% (resets 2026-10-13T01:00:00Z), overage
  `rejected`/`org_level_disabled`, `isUsingOverage` false; the gate admits at 70%. Everything
  else in this section is unchanged: the stop stays 70%, the overage check stays fail closed,
  and this login also carries the authors' other Claude Code sessions, so the stop still
  guards their headroom.
- Archivist: the ceiling gate (9,800, section 14.5) is unchanged; the counter read 3,785 of
  10,000. A reset, only if needed, follows the grounding campaign procedure with a hand receipt.

### 15.5 Estimate

From the codex campaign profiles of these exact questions, Claude Code assumed equal (uncertainty
0.5x to 2x): about 21M agent tokens for the 120 runs and about 1,150 Archivist calls (up to
2,300), so the counter stays below the 9,800 ceiling without a reset. Judges and graders: pass A
90 Claude calls (about 0.4M tokens); pass B about 118, pass R about 118, pass T 18 and pass H a
few Codex calls. The window cost per run is uncalibrated: batch K1 calibrates it.

### 15.6 Comparisons, labels and what stays unchanged

- Every Claude Code figure and every H1 to H6 verdict is labelled "replication, reduced power"
  (n = 5 questions per stratum, one repetition); the section 4 and 14.5 rules are applied by
  hand. Exposure and reliance measures stay exploratory, as in section 14.
- Codex figures appear beside them: the codex and grounding campaign results as published, plus Codex recomputed
  on the same questions (all three repetitions, `analyze --question`), labelled exploratory.
- The forum cross check (14.4) is not repeated: it measures web content, not the agent.
- Unchanged: sections 1 to 14, every codex and grounding campaign file and number; their raw
  evidence is read only, and the claude-code campaign writes its own evidence directory.
- Harness 1.5.0 (`archivist_bench`, `docs/HARNESS.md` "Amendment 4: Claude Code
  replication") implements this section: `--agent claude_code` on the grounding commands, the
  Claude Code contamination run, `analyze --question` and the cross campaign bias check
  (`analyze --bias-evidence-dir`).

## 16. Amendment 5 (2026-10-08): key corrections and the light audit route

**Decisions (2026-10-08).** The codex campaign human audit takes the light route: the authors
settle the four rows the pass B judge marked incorrect in `audit-queue-001.csv`, an agent pre
checks the other sampled rows, and the authors see only what the pre check disputes. Rulings on
the four rows, after each was checked against the filing text in the run logs: MH14.f4, the
answer is correct; BR15.f5, the key is wrong and is corrected, with no new runs; RC14.f16 and
RC14.f13, the difference does not matter, so the figure as filed counts as correct. The CN12 key
check (grounding campaign contamination stratum), done by an agent on behalf of the authors:
correct the key if it is wrong, else record that the answer is wrong.

**Status when made.** The codex campaign (828 Codex runs), the grounding campaign (153
contamination runs) and the claude-code campaign (120 Claude Code runs) are complete and their judge only results are committed. The five facts
below were found by reading those outcomes (the four judged incorrect audit rows and one CN12
answer), which is what the section 8 audit exists for: audit corrections replace judge verdicts.
The rescoring rule, the pre check rubric and the agreement rule below are fixed before any
rescoring, re-judge or pre check call, and before any analysis is recomputed. No new agent run
is made.

### 16.1 Key corrections

`key-corrections-amendment-5.json` (this folder) replaces named fields of five key facts;
`questions.json` and `contamination-questions.json` stay byte for byte, and the harness applies
the file in memory where this amendment says so. Each correction was checked by an agent on behalf of the authors
against the filing text the agents read (Archivist results in the codex and grounding campaign
rollouts, with
their filing ids and permalinks) and is validated like any key (section 3 checks; the new and the
also stated quotes are verified against the stored chunks with `verify-quotes`).

| Fact | Was | Correction | Filing evidence |
|---|---|---|---|
| MH14.f4 | 396,000 tonnes | also accept 395,772 | the AIF filed 2026-02-10 states 396,000 tonnes in its Copper section and 395,772 tonnes in its Description of the Business |
| BR15.f5 | 290,236 ounces | 353,772 ounces, company total; source the operating highlights table | 290,236 is the Rainy River mine (beside its 265,000 to 295,000 guidance); the MD&A's operating highlights table gives the company 353,772 (New Afton 63,536 plus Rainy River 290,236) |
| RC14.f16 | USD 235.9 billion | also accept 236 | the annual MD&A filed 2024-02-13 states $235.9 billion (Business Overview) and $236 billion (Gross Merchandise Volume section) |
| RC14.f13 | USD 49.6 billion | also accept 50 | the interim MD&A filed 2023-05-04 states $49.6 billion (Business Overview) and $50 billion (Gross Merchandise Volume section) |
| CN12.f1 | 10,624 (5,748 blue collar, 4,876 white collar) | 10,624; the split is removed from the statement | the integrated annual report filed 2026-03-03 gives the 2025 split in opposite orders: its corporate governance table (the key's passage) 5,748 blue and 4,876 white collar, Note 1 of the consolidated financial statements 4,876 blue and 5,748 white collar |

Note on RC14: the first description of this correction gave 235.9 and 49.6 as appearing
only as later comparatives; the stored chunks show each key filing states both figures. The correction is the
same either way.

### 16.2 Rescoring the corrected facts (judge calls only)

- Scope: every existing answer of a question carrying a corrected fact whose judgement has a
  valid pass B: the codex campaign (MH14, BR15, RC14: 9 answers each), the claude-code
  campaign (MH14, BR15, RC14: 1 answer per arm each) and the grounding campaign's Codex
  contamination runs (CN12: 9 answers). The claude-code campaign's contamination subset has no
  CN12.
- Each such answer gets one pass B call (Codex CLI 0.160.0, `gpt-6.1-sol`, effort `medium`,
  default tier, ChatGPT plan allowance, section 11 D2 plan gate and closing probe): the section
  12 prompt built exactly as for its stored judgement (rubric, question, the key with the facts
  in the `8105:<run id>:key` order, the blinded answer), with the corrected fields in place.
- Only the corrected facts' verdicts are taken. Every other fact keeps its stored verdict, and
  the stored `complete` stays. A malformed reply is retried once; a failed call gets one later
  batch; after that the stored verdict stays and the fact is listed.
- The taken verdict scores the fact (correct 1, else 0) as a correction applied like an audit
  correction (labelled `key_correction`, never human audited). A human audit verdict on the
  same fact wins and the re-judged verdict is listed as superseded.
- Why a judge call and not a deterministic rule: BR15.f5 changes its expected value, so the
  answers must be read again; one rule for all five facts keeps them comparable.
- Pass A is not re-run (no Claude call): the judge bias check and the single judge
  recomputations of sections 12 and 15.2 stay on the stored verdicts under the original key, as
  judge agreement diagnostics. The re-judge's agreement with the stored verdicts on the
  uncorrected facts of the same answers is reported as a stability check (descriptive).
- The audit queues stay as drawn (from the stored judgements, seed 8105).

### 16.3 The light audit route

- **Human ruled rows.** The four rows MH14.f4, BR15.f5, RC14.f16 and RC14.f13 of `audit-queue-001.csv`
  get `audit_verdict` `correct` (each answer states a figure the filing states, under the
  corrected key) and the authors' ruling recorded in `audit_note`.
- **Pre check (pass P).** Every other row of `audit-queue-001.csv` (349) and of the claude-code campaign
  audit samples (`analysis.json` `audit_queue`: 42 main set rows; 2 contamination rows) gets one pass P
  call on the same Codex settings and gates as 16.2, blind to the judge verdict. Inputs: the
  question; the key fact under the corrected key (statement, expected value and unit, accepted
  spellings); its filing passage (`archivist read passage <chunk> --window 0`, links blinded;
  for a computed fact its formula and each component fact with its passage; for an absence fact
  its absent terms and the full text search record, no passage); the blinded answer. Prompt
  (verbatim, `PASS_P_RUBRIC`; the inputs follow):

```text
You check one grading decision of a research benchmark. A key fact was taken from a company filing, and a research answer was graded against it.
First compare the key fact with the filing evidence given: "key_check" is "supported" when the evidence states the key's value for the same period, unit and entity; "contradicted" when the evidence states a different value, period, unit or entity for it; "not_in_passage" when the evidence does not contain it; "not_applicable" when no passage is given (an absence fact checked by a full text search).
Then grade the answer on this one fact: "correct" when the answer states it with the same value, period and unit (equivalent formatting, rounding to the precision shown in the key, and the listed accepted spellings count as the same); "incorrect" when the answer gives a different value, period, unit or entity for it; "missing" when the answer does not state it. For a fact that says a filing does not mention something, "correct" means the answer says it is not mentioned (or omits it from a list of companies that mention it).
Ignore length, style, tone, other facts and how sources are cited. Links and tool names are hidden.
Give a "reason" of one or two sentences naming the evidence.
Return only JSON: {"key_check": "supported|contradicted|not_in_passage|not_applicable", "verdict": "correct|incorrect|missing", "reason": "<text>"}.
```

- **Agreement rule.** A row agrees when the pass P verdict equals the effective judge verdict
  (the 16.2 verdict for a corrected fact, else the stored pass B verdict) and `key_check` is
  `supported` or `not_applicable`. An agreed row keeps the judge score and is labelled "agent
  pre checked", never human audited; its `audit_verdict` stays blank. Every other row (a
  different verdict, a `contradicted` or `not_in_passage` key, or a pass P failure after its
  retry and one later batch) goes to the human review list `audit-owner-list.md` in its results
  folder (row, judge verdict, pre check verdict and reason, filing evidence) and keeps the judge
  score until the authors rule; no agent ever fills a human audit verdict for it.
- ~~Outside this route: the grounding campaign's contamination 5% sample (15 rows) is not pre
  checked and stays judge only, listed as such.~~
- **Note (2026-10-08, a decision by the authors, before any pass P call on these rows): the
  grounding campaign's contamination sample is in the route.** The light route covers the
  grounding campaign's contamination 5% audit sample too (`analysis.json` `audit_queue` of its
  contamination analysis: 15 facts, none disputed). Those rows are exported to the grounding
  campaign's `contamination/` results folder and get the same pass P pre check with the same
  agreement rule and human review list; the CN12.f1 row's effective verdict is its 16.2 re-judged verdict. Nothing else
  in this amendment changes.

### 16.4 Analyses, labels and what stays unchanged

- Recomputed: `analyze --audit` for the codex campaign (human ruled rows plus key corrections),
  the grounding campaign's contamination analysis (CN12), the claude-code campaign's main
  analysis (key corrections) and its Codex same question recomputation. Section 4, 6, 14.5 and 15.6 rules are unchanged and applied by
  hand. Results show the audited figure ("audited, amendment 5": key corrections, the four human
  audit verdicts, the remaining sampled rows agent pre checked) beside the judge only figure, with the
  scope next to every number.
- Unchanged: sections 1 to 15, the questions files, every raw evidence directory (read only;
  Amendment 5 writes its own), every other fact and verdict, the audit queues, H1 to H6 rules
  and the framing rules (provenance, never "unbiased"; H6 never presented as protective).
- Harness 1.6.0 (`archivist_bench`, `docs/HARNESS.md` "Amendment 5: key
  corrections and the light audit") implements this section.
