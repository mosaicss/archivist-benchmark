# Archivist benchmark harness (version 1.3.0 for the codex campaign, 1.4.0 for grounding, 1.5.0 and 1.5.1 for claude-code, 1.6.0 for the light audit (amendment 5))

A reproducible harness for the pre registered comparison of an agent's own web tools with the
Archivist MCP server. It drives Codex CLI (`codex exec`) and Claude Code headless
(`claude -p`) in three isolated arms each, extracts exact usage and events, runs a blinded cross
family judge (Claude Code and Codex on the operator's plans) and computes paired bootstrap intervals and cost. Version 1.2.0 (the codex campaign) adds the plan window and
metered fallback guards, the pre launch plan gate, `run --resume`, the human audit export,
audit application and the committed results report. Version 1.3.0 adds the amendment 2 Codex
only mode: `judge --passes B` and `analyze --primary-judge B` (see "Amendment 2 mode"). Version
1.4.0 (grounding) adds the grounding, provenance and forum cross check layers, the
contamination stratum and the Archivist quota gate (see "Amendment 3: grounding and cross
check"). Version 1.5.0 (claude-code) adds the Claude Code replication: the grounding commands
read Claude Code streams (`--agent claude_code`), Claude Code runs the contamination subset,
`analyze --question` recomputes a question subset and `analyze --bias-evidence-dir` runs the
cross campaign judge bias check (see "Amendment 4: Claude Code replication"). Version 1.6.0
adds the amendment 5 key corrections, the pass B re-judge of corrected facts and the light
audit pre check (see "Amendment 5: key corrections and the light audit"). Python 3.12
standard library only.

> Public copy. This is the harness reference, with paths rewritten for this repository (see
> `REDACTIONS.md`). [`RUNBOOK.md`](../RUNBOOK.md) gives the step by step reproduction; the
> command blocks below record how each amendment's procedure was run. Run every command from the
> repository root. `$EVIDENCE` stands for an evidence directory of your choice outside the
> repository (one subdirectory per campaign: `smoke`, `codex`, `grounding`, `claude-code`), and
> `$EVIDENCE/tools` for wherever you installed the pinned CLIs.

The pre registration (questions, answer keys, hypotheses, rubric, statistics) is
`preregistration/`.

```bash
# from the repository root (no PYTHONPATH needed)
Q=preregistration/questions.json
python3 -m archivist_bench validate-questions --questions "$Q"
python3 -m archivist_bench plan --questions "$Q"                 # whole matrix, nothing runs
python3 -m archivist_bench run --questions "$Q" --agent codex --question CT01   # dry run
EV=$EVIDENCE/codex       # one evidence directory per campaign
python3 -m archivist_bench run --questions "$Q" --agent codex --question CT01 --repetitions 1 \
  --execute --max-runs 3 --evidence-dir "$EV"                     # spawns 3 runs (3 arms)
python3 -m archivist_bench analyze --format ledger --evidence-dir "$EV"   # run ledger
python3 -m archivist_bench judge --questions "$Q" --evidence-dir "$EV"    # dry run of the judge
# Pass A (Claude Code) needs the Claude account directory: --claude-config-dir is required
# whenever an executed judge runs pass A (see "Amendment 4"), never read from the environment.
python3 -m archivist_bench judge --questions "$Q" --evidence-dir "$EV" --execute --max-calls 2 \
  --run-id <run id> --claude-config-dir <Claude account dir>      # one answer, both judges
python3 -m archivist_bench analyze --questions "$Q" --evidence-dir "$EV"  # bootstrap, completion
python3 -m archivist_bench estimate --questions "$Q" --evidence-dir $EVIDENCE/smoke \
  --pilot <pilot results.json, not published>
```

## Safety rails

- `run` and `judge` are dry runs unless `--execute` is passed. An executed run needs
  `--max-runs` and refuses a larger plan; every attempt, reruns included, counts against it. The
  judge needs `--max-calls` the same way.
- Concurrency defaults to 1 and is capped at 2. Transient infra failures (`server_overloaded`,
  `internal_server_error`, Anthropic 5xx/529, an Archivist burst `RATE_LIMITED`) rerun at once,
  at most twice; every attempt is a ledger row. A plan usage limit or the Archivist quota is
  `infra_error` too but stops the batch instead (see Batch stops).
- The fast service tier is refused. Claude Code `api_key` mode (`--bare`) needs an existing
  `ANTHROPIC_API_KEY`; no key or account is created.
- Children get an allowlisted environment: `PATH`, `HOME`, `USER`, `LOGNAME`, locale, `TZ`,
  `TMPDIR`, `TERM`, TLS roots and XDG dirs, plus the harness's own switches. Nothing named
  `CLAUDE*`, `CODEX_*`, `ANTHROPIC_*`, `OPENAI_*`, `HERDR*`, `ARCHIVIST_*`, `GOOGLE_*` or
  `GCLOUD*` is inherited. `command.json` in each run directory lists the variables a run got
  (key values never).

## Plan gate, batch stops and resume (version 1.2.0)

Agents and judges run on plan allowance only. No CLI switch disables credit or overage use,
so the harness cannot prevent paid fallback at the account level; the control is pre
launch headroom (no launch above the threshold, default 90) plus probes before every
launch and after every batch, which detect and report any spend past it. Residual risk: two
concurrent launches plus other consumers of the same login inside the last 10 points of a
window.

**Pre launch plan gate** (`probe.py`, pre registration section 11, D2):

- Codex (every agent attempt and every judge pass B call): a fresh `codex app-server` in the
  launch's environment (its `CODEX_HOME` with the auth symlink) is asked over stdio for
  `account/rateLimits/read` (`initialize`, `initialized`, then the read; no model call, nothing
  spent). No other method is ever sent, never `account/rateLimitResetCredit/consume`. The
  launch is refused and the batch stops on a failed, timed out or incomplete probe (fail
  closed: `ordinaryUsageAllowed` must be a boolean, `rateLimits.primary.usedPercent` a finite
  number, `credits.hasCredits` a boolean and, when true, `balance` a finite number),
  `ordinaryUsageAllowed` false, a window `usedPercent` at or above the threshold, a window that
  rose more than `--max-window-step` points (default 5) since the previous probe of the batch,
  `rateLimitReachedType` set, `spendControlReached` true, or a credit balance below the batch
  baseline (the first probe's balance). The probe is saved as `plan-probe.json` in the run
  directory (or judge call directory) without `accountId`, reset credit ids or any other
  account identifier. A refused agent launch leaves `dispatch-refused.json`; it ran nothing
  and is not an attempt.
- Closing probe: after the last launch of every agent batch (`closing-probe.json` in the
  evidence directory) and every judge batch (`closing-probe.json` in
  `judge-logs/closing-<time>-B/`), one more Codex probe; a credit drop, a window at or above
  the threshold or a step past `--max-window-step` is printed, listed with the batch and makes
  the command exit 1. Every closing probe is also appended to `plan-probes.jsonl`.
- Claude Code (every agent attempt and every judge pass A call): the latest Claude plan
  reading of the campaign (agent records, judge call metas, `plan-probes.jsonl`), at most 30
  minutes old (`observed_at`), refuses the launch when its status is `rejected`, a
  utilization is at or above the threshold, `isUsingOverage` is true, or the overage state is
  anything but `overageStatus: rejected` with `overageDisabledReason: org_level_disabled` (a
  missing reason or `fetch_error` fails closed). The reading must also be complete: `status`
  `allowed`, at least one window with every utilization finite, and `isUsingOverage`
  explicitly false; an incomplete reading is replaced by a fresh probe and fails closed if the
  probe is incomplete too. Staleness after a reset looks only at the blocking windows' reset
  times. With no such reading (none yet, older than 30
  minutes, or a hot or rejected reading whose windows have all reset since) the gate makes one
  Claude plan probe call: `claude -p --restricted --output-format stream-json --verbose --model
  claude-haiku-4-5-20251001 --tools "" --strict-mcp-config --max-turns 1
  --no-session-persistence --disable-slash-commands`, prompt `Reply with OK.` on stdin, in a
  fresh empty directory with the allowlisted environment plus
  `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` and `DISABLE_AUTOUPDATER=1`. Its `rate_limit_event`
  becomes the reading; without `apiKeySource: none` or without a `rate_limit_event` it fails
  closed. Each probe is appended to `plan-probes.jsonl` (time, status, windows with
  utilization and `resetsAt`, overage fields, usage). It is a plan model call, not a scored
  run: it never counts against `--max-runs` or `--max-calls`.
- The thresholds are `run --stop-at-window PCT` and `judge --stop-at-window PCT` (1 to 100,
  default 90) and `--max-window-step POINTS` (default 5) on both. A refused judge call stops the judge batch
  like a plan limit (the answer's state is restored).
- Admission is atomic with the batch stop: right before the agent process starts, the budget
  is rechecked under its lock; if another worker stopped the batch meanwhile, nothing is
  launched, the reservation is released and the prepared directory is marked
  `not_dispatched` (evidence only, never an attempt, a ledger row or a cell record).
  `dispatched_at` is written into `meta.json` at that moment. A blocking probe releases its
  reservation and latches the batch stop under the same lock before any refusal evidence is
  written, so no other worker is admitted after it. A worker that raises latches the batch
stop itself before the exception propagates, so no queued cell is admitted after any worker
fails, whatever order the futures are collected in. The closing probe runs in guaranteed
  cleanup: on an exception or interrupt the harness stops admission, settles the in flight
  attempts, runs the closing probe and re-raises the original exception (agent and judge
  batches alike).

**Post run guards.** `run --execute` also starts no new attempt (the in flight attempt of a
concurrency 2 batch finishes normally) when a finished attempt shows:

| Stop | Signal | Record |
|---|---|---|
| Plan usage limit | Codex `usage_limit_exceeded` or `rate_limit_exceeded` (structured `codex_error_info` or the text "usage limit" or "Rate limit reached", in the rollout or in `exec --json` stdout); Claude Code `api_error_status` 429 or usage or rate limit text | `infra_error` (`usage_limit_exceeded`, `usage_limit:429`, ...); counts toward the three attempts; no immediate rerun |
| Claude rejection | a `rate_limit_event` with `status: rejected`, whatever the result subtype | the run keeps its classification (a completed answer stays completed), note `plan_rejected_guard` |
| Archivist quota | an Archivist tool error carrying `CLI_QUOTA` | `infra_error` (`archivist_quota`); a burst `RATE_LIMITED` stays `archivist_429` and reruns at once |
| Plan window | any window of the record's `plan_window` at or above the threshold | note `plan_window_guard: ...` |
| Metered fallback | the run's minimum Codex credit balance below the batch baseline or its own first balance, a Codex `rate_limit_reached_type` seen in the run, or Claude Code `isUsingOverage` true | note `metered_fallback_guard: ...` |

A plan or quota stop reason wins and is sticky within a run: a later transient reason never
replaces it, and it replaces an earlier transient one (both agents).

`plan_window` (in `record.json`, the ledger and `runs.csv`) is what the run saw: for Codex the
rollout `token_count.rate_limits` (primary and secondary `used_percent` and `window_minutes`,
`credits.has_credits` and the first, minimum and last balance as `balance_first`,
`balance_min` and `balance`, `plan_type`, `rate_limit_reached_type` and
`rate_limit_reached_seen`), for Claude Code the last `rate_limit_event` (`status`,
`unifiedWindows` utilization as a fraction, `isUsingOverage` and `rejected_seen`, both kept true
once seen, `overageStatus`). A run without such data has `plan_window: null` and never stops a
batch on it. Guard notes are kept in `meta.json`, so `extract` keeps them. After a stop, stderr
names the run and the reason, the cells without a record are listed and `run` exits 1. Other
sessions share the Codex login, so the operator also compares the balance across batches.

**Resume.** `run --execute` refuses an evidence directory that already holds records for
planned cells unless `--resume` is passed, and refuses duplicate `--question` ids or duplicate
planned run keys before any launch. A run directory whose `meta.json` has `dispatched_at`
but no `record.json` is an interrupted attempt: `--resume` first writes `reconciled:
interrupted` into its `meta.json` (so `extract` keeps it `infra_error`/`interrupted` whatever
raw output exists) and a record and a ledger row (`infra_error`, reason `interrupted`, the
attempt counts). Reconciliation keeps whatever telemetry the run left (usage, plan window,
credits, guard notes) and forces only the classification; its credits are compared with the
saved pre launch probe and the run's first balance, and any sign is noted
(`metered_fallback_guard`) and printed. A prepared directory without `dispatched_at` never
ran: it is marked `not_dispatched` and kept as evidence only, and `extract` refuses it
(`meta_version` 2 directories) even before resume. Then (the ledger plus each run's own `record.json`): cells with a non
`infra_error` record are skipped; infra only cells continue at the next attempt number, never
past attempt 3; cells without a record run; a cell at three `infra_error` attempts is listed as
excluded. A dry run with `--resume` prints the same summary.

**One time rerun of invalid cells** (pre registration section 10: the batch stops, the cause is
fixed and recorded as a deviation, then the affected cells rerun once): `run --resume
--rerun-invalid` gives a cell whose only non `infra_error` records are `invalid` one rerun
sequence: attempts numbered after its last, up to three when the earlier ones end
`infra_error` (a plan or quota stop ends the batch as usual, and a later `--resume
--rerun-invalid` continues the same sequence). A second `invalid` within the sequence, or a
sequence of three `infra_error` attempts, excludes and lists the cell; a cell is never given a
second sequence, and a cell with a final record is never rerun. `analyze` and `report` accept an invalid record plus its
rerun (invalid records are excluded statuses, listed under `excluded_runs`). Each run's
`meta.json` records `classified_by` (the harness version), `classified_status` and
`classified_reason`; `extract` keeps an `invalid` status recorded under an earlier rule set
(a legacy record is stamped when first met), so a fix applies to new runs only and the evidence
stays as it was classified. Never resume while another batch
runs in the same evidence directory (its in flight attempts would look interrupted).
`--max-runs` counts the attempts of one invocation; the campaign cap (1,230 attempts) is kept by
the operator across batches from the ledger.

## Campaign procedure (codex)

```bash
# from the repository root (no PYTHONPATH needed)
Q=preregistration/questions.json
EV=$EVIDENCE/codex/campaign
OUT=repro/codex      # a fresh folder; results/ is the published record, read only
export PATH=$EVIDENCE/tools/archivist-0.2.33/bin:$PATH  # tested CLI first
python3 -m archivist_bench run --questions "$Q" --agent codex --evidence-dir "$EV" --resume  # dry
python3 -m archivist_bench run --questions "$Q" --agent codex --evidence-dir "$EV" --execute \
  --resume --max-runs <batch> --concurrency 2 [--question <id> ...]   # repeat until done
# --max-runs must cover every run the selection plans (it refuses a larger plan, it does not
# truncate): narrow the batch with --question (9 runs per question) or --arms
python3 -m archivist_bench run --questions "$Q" --agent claude_code ...  # then Claude Code
# Both passes: pass A needs the Claude account directory (--claude-config-dir is required
# whenever an executed judge runs pass A)
python3 -m archivist_bench judge --questions "$Q" --evidence-dir "$EV" --execute \
  --max-calls <n> [--stop-at-window 90] --claude-config-dir <Claude account dir>
python3 -m archivist_bench analyze --questions "$Q" --evidence-dir "$EV" > analysis.json
python3 -m archivist_bench audit-export --questions "$Q" --evidence-dir "$EV" --out "$OUT"
# Before the human audit fills verdicts, report the unaudited analysis:
python3 -m archivist_bench report --questions "$Q" --evidence-dir "$EV" \
  --analysis analysis.json --out "$OUT"
# Once the human audit filled audit_verdict in the shards, apply them and report the audited analysis:
python3 -m archivist_bench analyze --questions "$Q" --evidence-dir "$EV" \
  --audit "$OUT" > analysis-audited.json
python3 -m archivist_bench report --questions "$Q" --evidence-dir "$EV" \
  --analysis analysis-audited.json --audit "$OUT" --out "$OUT"
```

Batches are paced by the plan windows: pick `--max-runs` (and `--question` lists) that fit the
remaining window, let the gate and the guards stop the batch, wait, then `--resume`. Judge
Codex answers first, then Claude Code answers, in batches that fit the windows (`--run-id`
narrows a batch).

**Audit queue.** `audit-export --out DIR` writes the queue in deterministic shards
`audit-queue-001.csv` and `audit-queue-001.md`, `-002`, ... (whole runs per shard, run id
order, each file under 900 KB) plus `audit-index.md` listing the shards with their runs,
disputed, sampled and total facts. The queue is every disputed fact plus the seeded 5% sample
of agreed facts (`judge.audit_queue`; run it after all judging, since the sample is drawn from
every judgement), each with the question, fact statement, expected value and unit, accepted
spellings, fact kind, Mosaic permalink, exchange document id or the reason none is stored,
absence terms or formula, the key quote, the combined and both judge verdicts, the link
redacted answer (`answers/<run_id>.md`, linked rather than repeated per row so the files stay
under the 1 MB commit limit) and blank `audit_verdict` and `audit_note`. An unknown fact id
fails the export naming it, and an export never overwrites a shard that holds human audit verdicts.
The human auditor fills `audit_verdict` with `correct`, `incorrect` or `missing`; `analyze --audit`
takes several CSV files and directories (every `audit-queue-*.csv` inside), scores those facts
1 or 0, recomputes the run accuracy and lists every change under `audit_corrections` (judge
verdict and score, audit verdict and score, run accuracy judged and audited). Validation is all
or nothing across the shards: an invalid verdict, an unknown run or fact, or conflicting
duplicates (also across files) apply nothing and exit nonzero; blank rows keep the judge
score. The single judge recomputations stay each judge's own.

**Evidence fingerprint.** `analyze` writes `harness_version`, `questions_sha256`,
`audit_files` (name and sha256 of each audit file it applied) and `evidence_fingerprint`:
sha256 over, in sorted order, every run's `record.json`, `judgement.json`, `answer.md` (the
one canonical answer, also the one control scoring and export read; a record `answer_file`
pointing elsewhere is never followed and is listed under `answer_file_mismatches`) and
raw log source (rollout or stream), the questions file bytes and the current bytes of each
audit file. `report` takes the same `--audit` files, verifies them against `audit_files`,
recomputes the fingerprint from the current files and refuses a mismatch (a resumed batch, a
new judgement, an edited answer, log, question set or audit verdict), an analysis without a
fingerprint or without comparisons, and an evidence directory with duplicate cells: rerun
`analyze`, then `report`. The billing evidence (`plan-probes.jsonl`, the agent and judge
`closing-probe.json` files, `dispatch-refused.json` files and `judge-ledger.jsonl`) is part of
the fingerprint, and `analyze` writes `billing_warnings`: closing probe credit drops, hot
windows and window steps, refused launches, failed Claude probes, record guard notes and
judge calls refused by the plan gate or the metered fallback guard.

`report --analysis JSON --out DIR` (with `--questions` and `--evidence-dir`) writes, reapplying
the analysis' own `audit_corrections`:

| File | Content |
|---|---|
| `runs.csv` | every attempt: identity, status and reason, tokens, model and tool calls, events, wall time, cost and basis, MCP loaded, versions, plan window columns, `metered_signs` (billing signs, credit drops included), `guard_notes` (every guard note), accuracy, completion, judge status, answer and log paths |
| `cells.csv` | per agent, stratum and arm: questions, runs, completed runs, excluded attempts, question mean accuracy, completed run tokens and completion |
| `comparisons.csv` | every comparison of the analysis (main, partial usage token sensitivity, single judge A and B) with estimate, 95% interval, n, resamples, seed and dropped questions |
| `multi_period_tokens_by_filings.csv` | per multi period question, agent and arm: distinct key filings, runs, completed reps, mean tokens, mean accuracy |
| `analysis.json` | the analyze output used |
| `answers/<run_id>.md` | each answer, link redacted |
| `plan-probes.csv` | the campaign's billing evidence, redacted: every Claude probe and every agent and judge closing probe (time, kind, source, ok or error, status, windows, credit balance, overage fields, warning) |
| `logs/<run_id>.jsonl.gz`, `logs/manifest.csv` | each run's extraction source (Codex rollout, Claude Code stream), scrubbed (below), every string over 4,000 characters clipped with its original length and sha256, gzipped without a timestamp; the manifest holds each raw file's path, bytes and sha256 |

Log scrubbing normalizes each key (lowercase, `_` and `-` removed, so `organizationId`,
`org-id`, `ORG_ID` and `accountid` compare alike) and masks the values of keys ending in token
(so usage counts like `input_tokens` stay) or holding secret, password, passwd, apikey,
authorization, cookie, session, accountid, userid, organizationid, orgid or email, plus the
account keys, whatever the value's type; and in text `Bearer <x>`, `sk-...`, `ak_...`, `mst_...`, JWT like
`eyJ....`, `access_token=`, `refresh_token=` and `id_token=` values, `Bearer` tokens and
`Basic` credentials, whole `Authorization` and `Proxy-Authorization` header values whatever
the scheme (Digest parameters included: to the end of the line, or of the quoted or JSON
string value), sensitive `key=value`, `key: value` and `"key": value` assignments in prose (a
quoted value to its matching delimiter, escapes honoured), emails and links (`redact_links`).
Strings that are JSON themselves, and JSON objects embedded in prose, are parsed and scrubbed
inside.

Every text file is whitespace tidy (the repository fixers leave it alone) and the output is
deterministic. Generated free text (answers, every CSV cell, the audit Markdown, scrubbed
before rendering so masks stay standalone) passes the same credential scrubber for token and
key patterns, bearer and basic credentials, authorization headers and credential key
assignments (also inside embedded JSON). In free text the credential keys are only these exact
singular keys, normalized: token, access_token, refresh_token, id_token, api_key, apikey,
password, passwd, secret, client_secret, authorization, cookie, set_cookie, plus the same
keys behind a prefix joined by `-` or `_` in one token without spaces (`X-API-Key`,
`auth_token`, `x-auth-token`, `client-secret`, `db_password`), also as keys of embedded JSON;
filing prose like "Trade secrets:", "Tokens:", "passwords:" or "API keys:" is untouched; JSON
and logs keep the full key set. One rule masks and checks a `Bearer` value in free text: no
spaces, and 8 or more characters with a digit or symbol, or 12 or more characters with at
least two uppercase letters after the first and at least two lowercase letters (title case
words such as "Bearer Certificates" or "Bearer Bonds" stay). Emails and identifiers in
answers stay, since filings quote investor relations contacts. Markdown validation treats
`<br>` and `|` as value boundaries.
`report` and `audit-export` check every file of the output folder, kept and hand edited files
included (gzip logs decompressed), for upstream filing hosts, credential patterns (and emails
in logs), any sensitive value still unmasked (JSON documents walked with every sensitive key;
CSV cell by cell and Markdown or other text by line with the credential keys, authorization
headers and embedded JSON objects) and the 1 MB commit limit, and exit nonzero naming the file
and row or line. Hand edited audit files are never rewritten. No code decides a
hypothesis: `results.md` applies the section 4 rules by hand.

## Amendment 2 mode: Codex only, pass B scoring (version 1.3.0)

Pre registration section 13 (amendment 2, 2026-10-07): the campaign finishes
with the Codex agent only and no Claude call of any kind; the pass B (Codex) verdict scores
every answer. The Claude Code agent arm, the cross family judge, the judge bias check and the
single judge sensitivity are deferred to a later run. The default of every command stays the
amendment 1 behaviour.

```bash
python3 -m archivist_bench judge --questions "$Q" --evidence-dir "$EV" --passes B   # dry run
python3 -m archivist_bench judge --questions "$Q" --evidence-dir "$EV" --passes B --execute \
  --max-calls <n> [--stop-at-window 90]
python3 -m archivist_bench analyze --questions "$Q" --evidence-dir "$EV" \
  --primary-judge B > analysis.json
python3 -m archivist_bench audit-export --questions "$Q" --evidence-dir "$EV" \
  --analysis analysis.json --out "$OUT"                 # takes the analysis' rule (B)
python3 -m archivist_bench report --questions "$Q" --evidence-dir "$EV" \
  --analysis analysis.json --out "$OUT"                 # uses the analysis' rule
# Once the human audit filled audit_verdict:
python3 -m archivist_bench analyze --questions "$Q" --evidence-dir "$EV" --primary-judge B \
  --audit "$OUT" > analysis-audited.json
python3 -m archivist_bench report --questions "$Q" --evidence-dir "$EV" \
  --analysis analysis-audited.json --audit "$OUT" --out "$OUT"
```

- `judge --passes B` (choices `A`, `B`; default both). Without pass A no Claude executor is
  created, no Claude plan reading is looked up and no Claude probe is called: the batch's plan
  gate is built with Claude disabled (`PlanGate(allow_claude=False)`), so any Claude check
  refuses and makes no call. The Codex login check, the pre launch Codex plan gate and the
  closing probe run as before. An answer whose judgement already has a valid pass B (no error,
  a verdict per fact, `complete` a boolean) is skipped; otherwise pass B runs and is merged
  into the judgement, keeping any existing pass A untouched (also in the pending record a crash
  leaves). A judge_error counts its batches as before: the one left by an interrupted batch
  (no passes) is judged in its second and last batch. A plan limit or the call ceiling restores
  the prior judgement. With both passes the amendment 1 behaviour is unchanged.
- Each judgement records `passes_requested`, `passes_run`, `passes_kept` and its `rule`:
  `combined` when both passes are valid (agreement scores the verdict, disagreement is
  `disputed` at 0.5), else the one valid pass (`B`: its verdict scores every fact, nothing is
  disputed).
- `analyze --primary-judge B` (default `combined`): each fact scores 1 when pass B says
  `correct`, else 0; run accuracy is their mean; completion is pass B's `complete`; the
  judgement's top level status does not matter, only a valid pass B. Audit corrections replace
  pass B verdicts. `audit_queue` is every disputed fact (a fact whose existing pass A verdict
  differs from pass B) plus a seeded 5% (seed 8105) of every other fact with a pass B verdict.
  `judge_bias` and `single_judge_sensitivity` are `{"deferred": "<reason>"}`, and
  `pass_agreement` (labelled exploratory) reports the answers and facts graded by both passes,
  the fact agreement share, the `complete` agreement share and the verdict cross table (rows
  pass A, columns pass B). The analysis records `primary_judge`. Under `combined` a judgement
  scored by pass B alone has no valid judgement (`no_valid_combined_judgement`).
- The audit CSV gains a `primary_judge` column; `judge_verdict` is the scoring verdict of that
  rule (pass B's under `B`). `audit-export --primary-judge` (default: the `--analysis` rule,
  else `combined`) refuses a rule other than the `--analysis` one, and with `--analysis` refuses
  a queue that differs from the analysis' `audit_queue` (changed evidence: rerun `analyze`).
  `analyze --audit` refuses audit files exported under another rule (no column: `combined`).
- `report` uses the analysis' rule (an analysis without `primary_judge` is `combined`), refuses
  a different `--primary-judge` and an output folder whose audit shards were exported under
  another rule. `cells.csv` and `comparisons.csv` are written as before and cover only the
  agents present (Codex); a deferred section adds no comparison rows.

## Amendment 3: grounding and cross check (version 1.4.0)

Pre registration section 14 (amendment 3, 2026-10-07) fixes the rubric; G0 makes every measure
over the codex campaign runs exploratory, and H6 (contamination stratum) is the only new confirmatory
test. Modules: `grounding.py` (source parsing, host categories, exposure, values and claim
attribution, passes H, R, X and T with strict parsers, the shared `Grader`) and `crosscheck.py`
(pool, dedupe, seeded stratified sample, the pass C checker, rates and the audit sample).

Every new command takes `--out` (the grounding state, `$EVIDENCE/grounding`); those
that read a campaign take `--evidence-dir` as read only input and refuse an `--out` inside it.
Grading passes are dry runs unless `--execute` with `--max-calls` (refused when smaller than the
planned calls), take `--stop-at-window` (90) and `--max-window-step` (5), and resume by skipping
valid outputs (an item with a `grade_error` is graded in one more batch, then excluded and
listed). An item starts only with room for both of its attempts under `--max-calls`, so its one
retry happens in the same batch; a malformed reply is a `malformed` ledger row. A completed run
without a non empty `answer.md` is listed and not graded (exit 1); `grounding-sources` writes
nothing for a run whose rollout is missing, empty, or lacks `task_complete` or usage (exit 1),
and resumes only past files marked `"complete": true`. They run on the Codex judge configuration (`judge.JUDGES["B"]`: `gpt-6.1-sol`,
`medium`, ChatGPT login checked first, `PlanGate(allow_claude=False)`, a pre launch probe per
call and a closing probe per batch); no Claude call of any kind. Each call has a `started` and
an outcome row in `<out>/grader-ledger.jsonl` and its logs under `<out>/logs/<pass>/<item>/`;
closing probes go to `<out>/plan-probes.jsonl`. `judge.CliExecutor` takes a per call output
schema (`__call__(prompt, schema=...)`); the verdict schema stays the default.

The full command sequence (exposure, passes H, R, X and C, the cross check audit, the contamination
stratum, analysis and report) is in [`RUNBOOK.md`](../RUNBOOK.md), section 6.

| Command | Reads | Writes (under `--out` unless noted) |
|---|---|---|
| `grounding-sources` | completed Codex runs' rollouts and answers | `sources/<run_id>.json` (shown, opened and Archivist sources with the text read, answer links), `hosts.json` (host inventory: up to 3 titles and snippets, tickers, runs) |
| `grounding-hosts` (pass H) | `hosts.json` | `host-table.json`, `pass-h/<batch>.json` |
| `grounding-claims` (pass R) | answers, questions | `claims/<run_id>.json` (claims, token map kept outside the prompt) |
| `crosscheck-extract` (pass X) | sources, host table | `crosscheck/pool.json` (texts split into windows of at most 8,000 characters overlapping by 1,000, `window` and `parent_text_id`), `crosscheck/text-sample.json` (G3), `crosscheck/extract.json`, `crosscheck/claims.json` |
| `crosscheck-sample` | the extraction (refused while texts are pending) | `crosscheck/sample.json` (refuses to replace a sample that has checks) |
| `crosscheck-check` (pass C) | the sample | `crosscheck/checks/<row_id>.json`, the checker's Codex home and rollout under `logs/C/`, `archivist-usage.jsonl` |
| `crosscheck-audit` | checks (refused while rows are pending) | `--results`/`crosscheck-audit.csv` (blank auditor columns; never overwrites a file with a human audit verdict or note) |
| `trap-judge` (pass T) | contamination answers and questions | `traps/<run_id>.json` |
| `grounding-analyze` | all of the above | `analysis-grounding.json`, `grounding-rows.json` (with an inputs fingerprint) |
| `grounding-report` | the analysis (refused when the inputs fingerprint changed) | `--results`: `host-categories.csv`, `exposure-runs.csv`, `exposure-cells.csv` (exposure and reliance cells), `claims-NNN.csv` shards, `crosscheck-sample.csv`, `analysis-grounding.json`, `contamination/claims-NNN.csv`; then `report.check_files` over the whole folder |
| `verify-quotes` | a question set's sourced facts | `passages/<chunk_id>.json` (one `archivist read passage <chunk> --window 0` per uncached chunk) |

- **Sources.** `shown`: search results injected by the search tool (one per distinct URL; host
  from `domain`, else the URL, else `unknown`). `opened`: `openPage`, `findInPage` and `other`
  (click) actions and results whose ref id contains `view`. Exec output blocks (`TITLE (URL)`,
  then the cite marker) attach their text to the source of their ref. `archivist`: completed
  Archivist calls, one source per `filing_id`. Filing hosts match `redact.UPSTREAM_HOST_PATTERN`.
- **Attribution.** Numbers match under any power of 1,000 scaling at the claim's shown precision
  (`grounding.number_matches`); numbers with fewer than two significant digits and bare years
  are not usable; entities match folded substrings. Labels `filing`, `secondary` (category by
  precedence) and `unattributed`, flags `citation_only`, `secondary_use`, `primary_read`,
  `verified_by_archivist`, and `absent_from_filing` for evaluative statements.
  Deviation G2: each fact claim also carries a `numbers_only` attribution (usable numbers only
  when it has one, else the primary result), reported beside the primary rule (`rule` column).
- **Text sample (deviation G3).** `crosscheck-extract` builds the whole pool, then writes
  `crosscheck/text-sample.json`: a seeded (8105) stratified sample of 1,500 pool texts (source
  category and question stratum, at least 60 per nonempty cell, largest remainder) and runs
  pass X on those texts only; `crosscheck-sample` deduplicates only their claims. A text sample
  whose texts already have extractions is never replaced unless identical; an extraction stored
  for a text outside the sample is ignored.
- **Extension sample (deviation G4).** `crosscheck-sample --extension` writes
  `crosscheck/sample-extension.json`: 300 `reported_figure` claims outside the 400 claim sample
  (seed 8106, at least 10 per nonempty cell, rows E001..E300; never replaced once it has
  checks); `crosscheck-check --extension` checks them into `crosscheck/checks/E*.json` with the
  same pass C, resume, concurrency and gates. Rates, subtypes, merit, the audit sample and
  `crosscheck-sample.csv` stay on the X rows; the extension's bins are counted separately
  (`crosscheck_extension` in the analysis, `crosscheck-extension-bins.csv`, labelled "extension,
  trap sourcing only"). `validate-questions --contamination --crosscheck` binds trap and control
  rows from either sample.
- **Concurrency (execution only).** `--concurrency N` on `grounding-hosts`, `grounding-claims`,
  `crosscheck-extract`, `trap-judge` and `crosscheck-check` (1 to 4), default 1:
  one executor per worker behind one shared plan gate (and the shared Archivist gate), every
  shared write, ledger row and `--max-calls` reservation under a lock; items are admitted in
  order, each reserving its two calls and waiting for a release when the room is short (the
  ceiling stops the batch only when nothing is outstanding), so the items done match one
  worker; a stop (plan gate, Archivist gate, quota, call ceiling) latches at detection and
  admits no new item while items in flight finish and are saved; one closing probe after every worker settled. Output files are the same as with
  one worker (shared tables are written in sorted key order).
- **Cross check.** Pass C runs `codex exec` with the archivist arm config plus
  `--output-schema`; any web call or non Archivist tool (MCP resource built ins allowed) is
  `invalid: arm_isolation` after one rerun; an Archivist `CLI_QUOTA` or the ceiling gate stops
  the batch. Confirmed and contradicted verdicts need a Mosaic passage permalink, a quote and an
  exchange document id or absence reason. Rates use Wilson 95% intervals, scope "content search
  ranked for these questions".
- **Archivist quota gate.** `run` and `crosscheck-check` read `archivist usage --format json`
  (`usage.cli_this_month`, the read is not counted) before each launch that can call
  Archivist (archivist and both arms, every checker call) and stop the batch at or above
  `--archivist-ceiling` (default 9,800) or on a failed read (`archivist-gate-refused.json` in
  the refused run directory; readings in `archivist-usage.jsonl`). A counter reset follows the
  authors' authorization with a hand receipt (pre registration section 14).
- **Contamination stratum.** `validate-questions --contamination` checks the section 14 rules
  (`"set": "contamination"`, 16 to 24 questions, 12 to 19 trap questions and exactly 5
  controls, `role`, `crosscheck_rows`, no document or form words, the section 3 per fact checks,
  trap facts with a wrong value that differs from the sourced fact named by `contradicts`,
  `crosscheck_row` and `source_category`); with `--crosscheck <grounding out>` every
  `crosscheck_rows` id must be a pass C result with status ok, `contradicted` for trap
  questions and `confirmed` for controls, and each trap fact is bound to its row (one of the
  question's rows, in `crosscheck/sample.json`, `contradicted`, same category and value). `run` validates a set by its own `set` field and runs
  a contamination set with `--agent codex` on the ChatGPT login only; `judge` takes it with
  `--passes B` only (else exit 2). Trap facts are never judged for accuracy (`judge.judged_facts`).
  `verify-quotes` checks every quote against the stored passage with the section 3
  normalization, and the passage's filing, permalink and exchange document id; it reads the
  Archivist counter before every uncached passage read (readings in `archivist-usage.jsonl`).
- **Write guard.** No write destination (`--out`, `--results`) may be or lie inside a read
  only input (`--evidence-dir`, `--contamination-evidence-dir`) or any campaign evidence
  directory (`ledger.jsonl` beside `runs/`); refused before anything is written.

## Amendment 4: Claude Code replication (versions 1.5.0 and 1.5.1)

Pre registration section 15 (amendment 4, 2026-10-07) fixes a reduced Claude Code design before
any run: the section 7 Claude Code subset (32 questions) at one repetition plus a seeded
contamination subset (traps CN04, CN15, CN03, CN16, CN05, CN02; controls CN10, CN11), three arms,
120 runs, every figure labelled "replication, reduced power". The default of every command is
unchanged (`--agent` defaults to `codex`; the Codex grounding outputs are byte identical to
1.4.0 apart from the version stamp).

- **Claude Code sources** (`grounding.parse_claude_sources`, section 15.3). From the
  stream-json transcript: WebSearch results give one `shown` source per distinct link URL
  (title, host from the URL, no snippet) and one `summary` source per call (host
  `web-search-summary`, category `other` by the seed table, never sent to pass H) with the
  tool's own summary text, read from the event's structured `tool_use_result` (`{"query",
  "results": [{"content": [{"title", "url"}]}, "<summary>"]}`) when the event holds exactly
  one tool result (its entries name the server tool id, never the call id),
  else from the `Links: [...]` JSON and the text after it (an unparsable text gives no link
  sources and the whole text is the summary; Claude Code's trailing `REMINDER:` is dropped).
  WebFetch results give one `opened` source per distinct input `url` with the fetch tool's
  extract as the text. `mcp__archivist__*` results give `archivist` sources by `filing_id`
  (else one per call). An `is_error` result is not a source.
- **Incomplete streams** (`grounding.stream_problem`): no events, no `result` event, a result
  other than `success`, or no usage: the run is listed, no sources file is written, the
  command exits 1, and a later run picks it up once the stream is restored.
- **Exposure.** `summary` sources enter no shown or opened share and no flag; each Claude Code
  run counts them as `search_summaries` (an `exposure-runs.csv` column after
  `archivist_sources`, and a `mean_run_count` cell per arm and stratum). In attribution a
  summary is a non filing source (`other`).
- **`--agent {codex,claude_code}`** (default `codex`) on `grounding-sources`,
  `grounding-claims`, `trap-judge`, `grounding-analyze` and `grounding-report`: only that
  agent's completed runs are read (also in the contamination section). Claude Code outputs are
  tagged: sources files carry `agent` and `stream_sha256`, the analysis and its contamination
  section carry `agent: claude_code` and the replication label. `grounding-report` refuses an
  analysis of another agent.
- **Claude Code contamination runs.** `run --questions contamination-questions.json --agent
  claude_code` plans the section 15 subset (`questions.claude_contamination_subset`; the six
  traps and two controls with the smallest `sha256("8105:<id>")` per role) unless `--question`
  names others; it runs on the subscription only. `--agent all` (the default) or
  `--claude-auth api_key` exit 2 before any preparation. `judge` still takes a contamination
  set with `--passes B` only.
- **`analyze --question ID`** (repeatable): rows, judgements, the audit queue, pass agreement,
  exclusions and comparisons of those questions only, recorded as `question_filter`; an unknown
  id exits 2. The evidence fingerprint still covers the whole directory, and `report` refuses
  such an analysis (it is an exploratory recomputation; keep its JSON).
- **`analyze --primary-judge B --bias-evidence-dir DIR`**: `judge_bias` is
  `judge.bias_report` over this evidence's judgements plus DIR's (read only), limited to the
  question ids present in this evidence's runs; `single_judge_sensitivity` is computed for this
  evidence. The analysis records `bias_evidence_fingerprint` (the DIR evidence fingerprint with
  the same questions file) and `bias_evidence` (runs, judged runs, runs graded by both judges,
  questions, agents), never a path. DIR without run records, DIR equal to `--evidence-dir`, or
  a rule other than `B` exit 2.
- **`report --bias-evidence-dir DIR`**: required when the analysis has a
  `bias_evidence_fingerprint`; the recomputed fingerprint must match (a changed judgement,
  answer, record or log of DIR refuses the report). Given without such an analysis it is
  refused too.
- **The Claude account, `--claude-config-dir DIR` (version 1.5.1).** Every Claude call runs on
  one explicitly chosen account: the campaign's account is the one named in pre registration
  section 15.4. The option exists on `run`, `plan`, `judge` and `plan-probe`; it is never read
  from the environment, must name an existing directory and is stored as its absolute resolved
  path. It is required (exit 2 before any run directory, probe or call) for `run --execute`
  with a Claude Code run in the plan, `judge --execute` with pass A, and `plan-probe`; dry runs
  and Codex only commands ignore it. No `CLAUDE*` variable is inherited from the parent: a
  Claude child gets only the harness's own switches plus this explicit `CLAUDE_CONFIG_DIR`
  (`claude_code.account_extras`); the parent's value is never copied. The agent runs, the
  judge pass A calls and the plan gate's Claude probe all build their environment from the
  same chosen value, so the probe reads the account the calls use. A gate whose probe
  environment names no account refuses without starting a
  process (and so refuses the launch). Claude runs record `claude_config_dir` in `record.json`
  (and their `command.json` environment), judge pass A calls in their `command.json` and their
  judge ledger meta, and every Claude probe row in `plan-probes.jsonl`; Codex runs, Codex judge
  calls and Codex probe rows are unchanged. Only readings tagged with the chosen account count
  for the gate (`probe.latest_claude_window(evidence, account)`): readings made before 1.5.1
  (the default account) or on another account are ignored, and the gate probes afresh.
- **`plan-probe --claude-config-dir DIR --evidence-dir DIR [--stop-at-window PCT]`** (default
  90): exactly one Claude plan probe call on that account, appended to
  `<evidence>/plan-probes.jsonl`; prints the reading (status, windows with resets, overage
  fields, `claude_config_dir`) and the gate's refusal reasons at PCT from that reading, without
  a second probe. Exit 0 when the reading was taken and admits, 1 when the probe failed or the
  gate refuses, 2 on invalid arguments. It never runs an agent, judge or Archivist call.

Campaign sequence (claude-code). Claude launches (agent runs, judge pass A) use
`--stop-at-window 70` and concurrency 1; the Codex graders keep 90 and the 5 point step. The
gate also refuses a Claude status other than `allowed` and missing overage fields; nothing
bypasses it.

The command sequence (dry runs, the plan probe, the batches, judging, grounding, analysis and
report, and the Codex same question recomputation) is in [`RUNBOOK.md`](../RUNBOOK.md), section 7.

## Amendment 5: key corrections and the light audit (version 1.6.0)

Pre registration section 16 (amendment 5, 2026-10-08) fixes, before any re-judge, pre check or
recomputed analysis: five key corrections (`key-corrections-amendment-5.json` beside the
questions; the frozen question files stay byte for byte), a pass B re-judge of the answers
carrying a corrected fact, and the light audit route (the authors settle the four disputed rows
of `audit-queue-001.csv` by human audit, pass P pre checks every other sampled row, human review
sees only what pass P disputes). No agent run is made. The default of every command is unchanged: without the
new flags the outputs are byte identical to 1.5.1 apart from the version stamp.

- **Key corrections, `--key-corrections FILE`** on `validate-questions`, `verify-quotes`,
  `rejudge-keys` and `audit-precheck`. `questions.apply_key_corrections` returns a copy of the
  loaded set with the named fields of each correction replaced (`statement`, `value`, `unit`,
  `accept`, `source` only; every other field and fact unchanged). Only corrections whose `set`
  is the loaded file's set apply: `contamination` for a file with `"set": "contamination"`, else
  `main`. An unknown question or fact (of the applicable corrections), another field, a fact
  corrected twice, an unknown set or a malformed `also_stated` entry exits 2 and applies
  nothing. `validate-questions` checks the corrected set (a line names what applied);
  `verify-quotes` checks the corrected facts and every `also_stated` quote of an applied
  correction against its own chunk (fetched once and cached like any passage).
- **`rejudge-keys`** (section 16.2, judge calls only): every completed, uncontaminated run of a
  question with an applied correction whose judgement has a valid pass B gets one pass B call
  (Codex judge configuration, `judge.VERDICT_SCHEMA`) on `judge.build_prompt(corrected
  question, answer, run id)`: the stored judgement's prompt with only the corrected fields
  changed (same `8105:<run id>:key` order, same blinding). The section 11 D2 plan gate, the call
  ceiling (`--max-calls`), `--concurrency` 1 to 4 and the closing probe are those of the
  grading passes; the calls are logged in `OUT/grader-ledger.jsonl` and `OUT/logs/B/`. Each run
  gets `OUT/key-rejudge/<run id>.json`: status, batches, calls (raw replies), `prompt_sha256`,
  `corrections_sha256` (the applied corrections' set, question, fact and fields) and
  `corrections_file_sha256`, all re-judged `verdicts`, `taken` (the corrected facts only),
  `stored` (the stored pass B verdicts) and `uncorrected_agreement` (re-judge versus stored on
  the other facts, a descriptive stability check). The stored `complete` stays. A malformed
  reply is retried once; a failed answer is `grade_error` and gets one later batch, then it is
  listed and its stored verdict stays. Resume skips `ok` records; records made under other
  corrections refuse the command (use a new `--out`). `--out` (and `--export`) may not be or lie
  inside `--evidence-dir` or any campaign directory.
- **The rescore CSV, `rejudge-keys --export CSV`** (no call): one row per run and corrected
  fact in the audit columns plus `correction_source` `key_correction`: `primary_judge` `B`,
  `judge_verdict` the stored pass B verdict, `audit_verdict` the taken verdict, `audit_note`
  naming amendment 5 and the correction; a summary (changed, unchanged, stability share) goes to
  stderr. It is refused (exit 1, nothing written) while an eligible run is neither re-judged
  `ok` nor failed after its two batches; finally failed facts are not exported and are listed.
- **`correction_source`** in audit files: blank or `owner` is a human audit verdict,
  `key_correction` a rescore row. `analyze --audit` and `report --audit` take the rescore CSV as
  a file beside the human audit shards (folders still glob `audit-queue-*.csv` only; the
  fingerprint and `audit_files` cover it). Key correction rows apply like audit rows; a human audit
  row on the same run and fact wins and the key row is listed under `superseded_corrections`
  (with the human audit verdict); every `audit_corrections` entry carries `source`. Conflicting rows
  of the same source are refused as before. `superseded_corrections` appears only with
  `--audit`. The rescore CSV may sit in the results folder (`key-rescore-amendment-5.csv`):
  `report.check_files` accepts it and `report` never treats it as an audit shard.
- **`--contamination-audit CSV ...`** on `grounding-analyze` and `grounding-report`: the
  contamination per arm accuracy and comparisons use `analysis_rows(..., corrections, "B")`
  with these files' corrections (validated against the contamination judgements under the
  pass B rule; an invalid file exits 1); the inputs fingerprint covers the files and the
  contamination section records `audit_files`, `audit_corrections` and
  `superseded_corrections`. `grounding-report` needs the same files.
- **`audit-precheck`** (pass P, section 16.3): every row of the given audit queues
  (`--audit-queue`, files or folders) without a human `audit_verdict` gets one Codex call,
  blind to the judge verdict, with `precheck.PASS_P_RUBRIC` (verbatim from section 16.3) and the
  inputs it names: the question; the key fact under the corrected key (statement, expected value
  and unit, accepted spellings); its passage (`archivist read passage <chunk> --window 0`
  through the verify-quotes reader and cache `OUT/passages/<chunk>.json`, behind the Archivist
  quota gate, `--archivist-ceiling` 9,800, readings in `OUT/archivist-usage.jsonl`; every
  passage is read before the first call); for a computed fact its formula and each component
  fact with its passage; for an absence fact its absent terms and the full text search record,
  no passage; the answer. Passage text and answer pass through `redact.blind`, and the prompt
  through `grounding.safe_prompt` (no upstream link or host). The reply schema is
  `{"key_check", "verdict", "reason"}` with fixed enums; the parser is strict (exactly those
  keys, a non empty reason, `not_applicable` exactly when no passage was given). Each row's
  record is `OUT/precheck/<run id>__<fact id>.json`; resume skips `ok` rows; a malformed reply
  is retried once, a failed row gets one later batch, then it is disputed as `pre check failed`.
  The effective judge verdict is the `--key-rescore` verdict of a corrected fact (source
  `key_rejudge`), else the row's `judge_verdict` (`stored_judge`). A row agrees when the pass P
  verdict equals it and `key_check` is `supported` or `not_applicable`. Once every row is final,
  `--results DIR` gets `audit-precheck.csv` (selection, run id, agent, arm, stratum, question,
  fact, effective judge verdict and source, pre check verdict, key check, reason, agree, status)
  and, only when a row disputes, `audit-owner-list.md` (row, judge verdict, pre check verdict and
  reason, filing evidence: Mosaic permalink and key quote); `report.check_files` runs on both.
  A dry run (no `--execute`) prints counts and makes no call and no Archivist read. No agent
  ever fills a human audit verdict: agreed rows are "agent pre checked", never human audited.

Operation (pre registration section 16 and the codex campaign follow-up): the order below completes it.
Before any re-report the judge only `analysis.json` and `comparisons.csv` of each results folder
are kept as `analysis-judge-only.json` and `comparisons-judge-only.csv`.

The command sequence (validation, re-judge and rescore export, the human audit rows, the audit
exports (`audit-export`), pass P, the judge only copies, the audited analyses and reports, and the audited Codex
same question recomputation) is in [`RUNBOOK.md`](../RUNBOOK.md), section 8.

## Arm isolation

| | web | archivist | both |
|---|---|---|---|
| Codex `web_search` | `live` | `disabled` | `live` |
| Codex MCP | none | `archivist mcp serve` | `archivist mcp serve` |
| Claude Code `--tools` | `WebSearch,WebFetch` | none | `WebSearch,WebFetch` |
| Claude Code MCP (`--strict-mcp-config`) | none | `archivist` | `archivist` |

Codex: each run gets a fresh `CODEX_HOME` holding the arm's rendered `config.toml` and a symlink
to `~/.codex/auth.json` (token refresh writes through). `approval_policy = "never"`, every
Archivist tool pre approved (archivist-cli v0.2.33's `filings` and `find` beside the 0.2.32
names; pre registration section 11, D1), and these features off: apps, plugins, remote plugins, browser use,
computer use, in app browser, multi agent, image generation, goals, hooks, tool suggestions,
skill search, the sleep tool, the shell tool and unified exec. No arm of either agent has a
shell or file tool (the read only, no network sandbox stays as a second guard); any shell, file
change or other extension tool use is an isolation violation. Codex has no turn
limit; the wall timeout (1200 s) is the bound.

Claude Code: `--output-format stream-json --verbose --model --effort --max-turns 50
--strict-mcp-config --mcp-config --permission-mode dontAsk --no-session-persistence
--disable-slash-commands --tools --allowedTools`, with `ENABLE_TOOL_SEARCH=false` (MCP
definitions load up front, like Codex), `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` and
`DISABLE_AUTOUPDATER=1`. The prompt goes on stdin.

MCP loaded check. Claude Code reports it in `system/init` (`mcp_servers[].status ==
"connected"`, tools in `tools`). Codex 0.160.0 `exec` persists no MCP startup event, so the
harness records three kinds of evidence per run (`notes` shows which): `preflight`, written to
`mcp-preflight.json` just before the run without a model call (`codex mcp list --json` against
the run's `CODEX_HOME`, then an MCP stdio handshake, `initialize` and `tools/list`, in the run's
environment); `tool_catalog`, a tool output in the session that lists `mcp__archivist__*` tools;
`tool_call`, a completed `McpToolCall`. A failure reported by the run overrides the preflight.

Codex's built in MCP resource tools (`list_mcp_resources`, `list_mcp_resource_templates`,
`read_mcp_resource`, reported as `McpToolCall` items of the pseudo server `codex`) only reach
the configured MCP servers, like Claude Code's `ListMcpResourcesTool` and
`ReadMcpResourceTool`: they are allowed in the archivist and both arms and stay violations in
the web arm (pre registration section 11, D5; harness 1.2.1). The pseudo server seen only
through them is not a loaded server; any other `codex.*` tool or other server is a violation.
Their calls are still counted (`mcp:codex.*`) as resource listings, never as Archivist
`/research` calls in the cost and quota estimate.

A run is `invalid: arm_isolation` (excluded, not retried) when an Archivist or both arm did not
connect the `archivist` server, a web arm loaded any MCP server, or a tool outside the arm's
allowlist was offered (Claude Code `system/init.tools`) or used. A web arm run that opens or
cites a Mosaic page, or any run that touches the source repositories, is flagged `contaminated`
and excluded.

## Auth and billing modes

| Agent | Mode | How | Billing | Cost in the ledger |
|---|---|---|---|---|
| Codex | `chatgpt` (default) | ChatGPT login (`~/.codex/auth.json`) | plan allowance | API list price equivalent |
| Codex | `api_key` | existing `CODEX_API_KEY` | OpenAI API, metered | API list price |
| Claude Code | `subscription` (default) | Claude login, `--restricted` | plan allowance | `total_cost_usd` (client estimate) |
| Claude Code | `api_key` | existing `ANTHROPIC_API_KEY`, `--bare` | Anthropic Console, metered | `total_cost_usd` (client estimate), cross check with Console |

## Judges

Pre registration section 8 as amended (amendment 1, 2026-10-06): two judges from the two model
families grade every scored answer from one identical prompt (`judge.build_prompt`: rubric,
question, answer key with its facts in a seeded shuffled order, then the answer blinded by
`redact.blind`). Both run on the operator's plans; there is no metered judge and no Vertex path.

| Pass | CLI | Model / effort | Auth and billing | Isolation |
|---|---|---|---|---|
| A | `claude -p --restricted` | `claude-opus-5-5` / `medium` | Claude subscription, plan allowance (never `--bare`, no API key) | `--tools ""`, `--strict-mcp-config` with no config, `--permission-mode dontAsk`, `--no-session-persistence`, `--disable-slash-commands`, `--json-schema`; init must show only `StructuredOutput`, no MCP server and `apiKeySource: none` |
| B | `codex exec --ephemeral` | `gpt-6.1-sol` / `medium` | ChatGPT login, plan allowance (no `CODEX_API_KEY`; the cached login must be `auth_mode: chatgpt` with no API key, checked before Codex starts, then `forced_login_method = "chatgpt"`) | fresh `CODEX_HOME` (judge config, auth symlink), `-s read-only`, `web_search = "disabled"`, no MCP, shell and every extension feature off, `--output-schema`; any item other than a message or reasoning is a violation |

Neither CLI exposes a sampling temperature; the fixed and recorded setting is the effort.
Each call runs in a fresh empty directory with the allowlisted environment; the prompt goes on
stdin. A malformed verdict or failed call is retried once, then the pass is a `judge_error`
(judged once more in a later batch, then excluded). A plan usage limit or rate limit
(`JudgeInfraError`, also read from Claude Code's `api_error_status` and `errors`) or the
`--max-calls` ceiling stops the batch and restores the answer's prior judgement, so neither
counts toward exclusion; a crash keeps the pending batch. The Codex login is checked before
the first call of a batch (and again per call); a wrong login stops the command with nothing
called. Disagreement between the judges makes a fact `disputed` (score 0.5,
human audit). `analyze` adds `judge_bias` (verdict and agreement rates by judge family and by
the answering agent's family, plus the family interaction on the shared questions with a
question cluster bootstrap) and `single_judge_sensitivity` (every accuracy and completion
comparison recomputed from pass A alone and pass B alone).

## Measurement

- Codex: the rollout JSONL in the run's `CODEX_HOME/sessions`. Totals are the last cumulative
  `token_count.info.total_token_usage` (never a sum); model calls are `token_usage_record`
  events; tools come from `item_completed` items (`McpToolCall` by server and tool; `Extension`
  `web.search` by action `search`, `openPage`, `findInPage`); compactions are `compacted`
  records; model and effort come from `turn_context`. Missing usage marks the run
  `incomplete: no_usage`.
- Claude Code: the stream-json transcript. Model calls are distinct `message.id`s; token totals
  are `result.modelUsage` summed over models (the WebFetch summarizer included), else
  `result.usage`; `total_cost_usd` and `num_turns` come from `result`; `compact_boundary` events
  are compactions; `error_max_turns` is `turn_limit`; no `result` is `incomplete: no_result`.
- Both: `input` includes cached input; `uncached_input = input - cached_input` (cache writes are
  part of it); `output` includes reasoning; `total = input + output`. Tool counts can
  undercount calls made inside Codex code mode loops.
- Truncation: a tool result the agent client cut before the model saw it. Codex marks it in
  the `custom_tool_call_output` or `function_call_output` text as `Warning: truncated output
  (original token count: N)`; Claude Code reports a result over `MAX_MCP_OUTPUT_TOKENS` or a
  result saved to a file. Each one is recorded in `truncated_calls` (call id, tool, original
  token count) and counted in `truncations`; the ledger shows them beside compactions, turn
  limit and timeout, and `analyze` sums them per arm. Archivist's own paging (`"truncated":
  true` with a `next_cursor`) is counted separately as `paged_results`.
- Every Codex arm config sets `model_reasoning_effort` explicitly (the host default is
  `xhigh`; the benchmark uses `medium`), and the command repeats it with `-c`.

## Evidence

Raw evidence stays outside git, one directory per campaign: the smoke runs use
`$EVIDENCE/smoke/`, and the codex campaign uses `$EVIDENCE/codex/campaign`. `run --execute`,
`judge` and `analyze` take no default (`--evidence-dir` is required; dry runs only display the
default under `MOSAIC_EVIDENCE_ROOT`), and `analyze` refuses a directory with more than one non
`infra_error` record for the same cell. `runs/<run id>/` holds `meta.json`,
`command.json`, the Codex home or MCP config, `stdout.jsonl`, `stderr.log`, `answer.md`,
`record.json` and `judgement.json`; `ledger.jsonl` has one row per attempt. `judge-ledger.jsonl`
has a `started` row before each judge call and an outcome row after it (pass, CLI, model,
effort, billing mode, usage, cost equivalent, Claude plan window utilization, status) and `judge-logs/<run id>/batch-<n>/<pass>-<k>/` keeps
each call's argv, stdout and stderr (a call directory is never reused). Committed results
carry metrics, link redacted answers and the redacted, clipped logs of `report` only
(`redact.redact_links`).

## Tests

`python -m pytest tests -q` from the repository root
(hermetic: synthetic rollouts and streams in `tests/fixtures/archivist_bench/`,
fake judge CLI processes, no agent or network).
