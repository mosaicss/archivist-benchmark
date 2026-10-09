# Runbook: reproducing the Archivist benchmark

Step by step commands for every campaign in this repository. [`docs/HARNESS.md`](docs/HARNESS.md)
is the harness reference (safety rails, plan gate, every flag and file); this runbook is the
order in which to use it. Run every command from the repository root.

Method in brief: each question runs in three isolated arms (`web`, the agent's own web tools
only; `archivist`, the Archivist MCP server only; `both`). Every answer is graded by a blinded
judge against answer keys taken from the filings, and arms are compared with paired bootstrap 95%
intervals over questions (10,000 resamples, seed 8105). The pre registration
(`preregistration/preregistration.md`) fixes the questions, keys, hypotheses, rubric and
statistics, and its dated amendments record every later change.

Campaigns, by results folder:

| Folder | What it is | Pre registration |
|---|---|---|
| `results/codex/` | the Codex campaign: 92 questions (90 scored, 2 controls), 3 arms, 3 repetitions, 828 runs | sections 1 to 13, amendments 1 and 2 |
| `results/grounding/` | grounding, provenance, the forum cross check and the contamination stratum over the codex runs | section 14, amendment 3 |
| `results/claude-code/` | the reduced Claude Code replication: 32 questions and 8 contamination questions, 1 repetition, 120 runs | section 15, amendment 4 |
| all three | key corrections and the light audit | section 16, amendment 5 |

## 1. Requirements and pinned installs

| Item | Version used | Notes |
|---|---|---|
| OS | Linux x86_64 | the campaigns ran on Linux amd64 |
| Python | 3.12 or newer | standard library only; tests checked with CPython 3.12.14 and 3.13.5 |
| pytest | 8 or newer | tests only |
| Codex CLI | 0.160.0 | npm `@openai/codex`; model `gpt-6.1-sol` (Sol 6.1), effort `medium` |
| Claude Code | 2.1.289 | npm `@anthropic-ai/claude-code`; model `claude-opus-5-5` (Opus 5.5), effort `medium` |
| archivist-cli | 0.2.33 | release of `mosaicss/archivist`; 8 MCP tools |
| Node.js and npm | any current release | only to install the two agent CLIs |

The models and efforts are fixed in `archivist_bench/model.py` (agents) and
`archivist_bench/judge.py` (judges). Newer CLI versions change tool behaviour and token
accounting, so pin the versions above.

```bash
export EVIDENCE=~/archivist-bench-evidence     # raw evidence, outside this repository
T="$EVIDENCE/tools"
npm install --prefix "$T/codex-0.160.0" @openai/codex@0.160.0
npm install --prefix "$T/claude-code-2.1.289" @anthropic-ai/claude-code@2.1.289

# archivist-cli 0.2.33 from its release, checksum verified
mkdir -p "$T/archivist-0.2.33/bin" && cd "$T/archivist-0.2.33"
REL=https://github.com/mosaicss/archivist/releases/download/v0.2.33
curl -fsSLO "$REL/archivist_v0.2.33_linux_amd64.tar.gz"
curl -fsSLO "$REL/archivist_v0.2.33_SHA256SUMS"
sha256sum --check --ignore-missing archivist_v0.2.33_SHA256SUMS
# expected for linux_amd64: 11074be926836bb781d8be987dced6249f99eb79c8f31e45e53b71ebf8db6055
tar -xzf archivist_v0.2.33_linux_amd64.tar.gz -C bin archivist
cd -
# (alternative: go install github.com/mosaicss/archivist/cmd/archivist@v0.2.33)

export PATH="$T/archivist-0.2.33/bin:$T/codex-0.160.0/node_modules/.bin:$T/claude-code-2.1.289/node_modules/.bin:$PATH"
codex --version; claude --version; archivist version
```

The harness passes child processes an allowlisted environment (no `CLAUDE*`, `CODEX_*`,
`ANTHROPIC_*`, `OPENAI_*`, `ARCHIVIST_*` or `GOOGLE_*` variable is inherited), so every tool must
find its sign in under `HOME`, not in environment variables.

## 2. Sign in: plans, no API keys

Every agent run and every judge call ran on plan allowance, with no API key. What the harness
checks differs by path, as listed below; only the judge and grading paths inspect the Codex login
fields.

- **Codex: ChatGPT plan.** `codex login`, sign in with ChatGPT. What each path checks:
  - Agent runs (`run`, default `--codex-auth chatgpt`): the login file `~/.codex/auth.json` must
    exist; each run gets a fresh `CODEX_HOME` with a link to it, and no `CODEX_*` or `OPENAI_*`
    variable reaches the agent. The file's contents are not inspected here, so make sure the
    cached login is your ChatGPT login and not an API key login.
  - Judge pass B and every Codex grading pass (H, R, X, C, T, P and `rejudge-keys`): before the
    first call and again before every call, the login file must have `auth_mode: chatgpt` and no
    API key value, or the command stops with nothing called; the judge configuration also sets
    `forced_login_method = "chatgpt"`.
  - Both paths: before each launch the harness reads the plan's rate limit windows (no model
    call) and stops a batch at 90% of a window (`--stop-at-window`).
- **Claude Code: Claude subscription.** Choose a directory for the Claude login the benchmark
  uses (for example `CA=~/claude-bench-login`), start `claude` once with the environment variable
  `CLAUDE_CONFIG_DIR` set to it and sign in with your subscription (`/login`, not an Anthropic
  Console key). Pass the same directory as `--claude-config-dir "$CA"`; it is never read from the
  environment. Runs must report `apiKeySource: none`; the plan gate refuses a launch when overage
  is in use or not disabled.
- **Archivist: a Mosaic account with the MCP and CLI plan** (USD 8 per month; the 30 day free
  trial covers the usage required). Create a CLI API key on your Mosaic
  account page and store it with `archivist auth login --token "<your CLI API key>"`. Fair use is
  10,000 CLI calls per month; the free plan's 20 lifetime calls are not enough. Calls used: codex
  9,291, grounding cross check and contamination about 3,785, claude-code 834. The harness reads
  `archivist usage` before every launch that can call Archivist and stops the batch at
  `--archivist-ceiling` (default 9,800; set it below your monthly allowance).

## 3. Offline checks

```bash
python3 -m venv .venv && .venv/bin/pip install 'pytest>=8'
.venv/bin/python -m pytest tests -q          # 338 passed
python3 -m archivist_bench --help
P=preregistration
python3 -m archivist_bench validate-questions --questions $P/questions.json
python3 -m archivist_bench validate-questions --questions $P/questions.json \
  --key-corrections $P/key-corrections-amendment-5.json
python3 -m archivist_bench validate-questions --contamination \
  --questions $P/contamination-questions.json --key-corrections $P/key-corrections-amendment-5.json
python3 -m archivist_bench plan --questions $P/questions.json --agent codex      # 828 runs
python3 -m archivist_bench run --questions $P/questions.json --agent codex --question CT01
```

`validate-questions` prints `92 questions, 785 facts, 33 tagged non_us, 0 violations` and the 32
question Claude Code subset; with the key corrections it names the five corrected facts. `run`
without `--execute` is a dry run and launches nothing.

## 4. Conventions for every campaign

- Raw evidence goes to `$EVIDENCE/<campaign>/`; results folders you produce go to
  `repro/<campaign>/`. The `results/` folders are the published record: treat them as read only
  and never point `--out` or `--results` at them.
- `run`, `judge` and every grading pass are dry runs unless `--execute` is given with a ceiling
  (`--max-runs`, `--max-calls`). Start every step dry.
- `--max-runs` is a ceiling, not a batch size: `run --execute` refuses a plan with more pending
  runs than `--max-runs` instead of truncating it, and reruns of transient failures count against
  it. Make a batch smaller with `--question` (repeatable) or `--arms`, and give `--max-runs` the
  batch's planned runs plus room for reruns.
- Pace batches to your plan windows: a gate stops the batch, you wait for the window to reset,
  then continue with `--resume` (agent runs) or by rerunning the same command (grading passes skip
  valid outputs).

## 5. Codex campaign (amendments 1 and 2)

Amendment 2 finished the campaign with Codex only and the Codex judge (pass B) scoring every
answer; the committed results use that rule.

```bash
P=preregistration; Q=$P/questions.json
EV=$EVIDENCE/codex/campaign
OUT6=repro/codex
python3 -m archivist_bench run --questions "$Q" --agent codex --evidence-dir "$EV" --resume  # dry: 828 runs
# a batch of questions: 9 runs each (3 arms x 3 repetitions), here 2 questions = 18 runs
python3 -m archivist_bench run --questions "$Q" --agent codex --evidence-dir "$EV" --execute \
  --resume --question LK01 --question LK02 --max-runs 22 --concurrency 2
# repeat with the next questions until no cell is missing, or run the whole plan at once:
python3 -m archivist_bench run --questions "$Q" --agent codex --evidence-dir "$EV" --execute \
  --resume --max-runs 911 --concurrency 2                      # 828 runs plus 10% for reruns
python3 -m archivist_bench analyze --format ledger --evidence-dir "$EV"   # run ledger
python3 -m archivist_bench judge --questions "$Q" --evidence-dir "$EV" --passes B          # dry
python3 -m archivist_bench judge --questions "$Q" --evidence-dir "$EV" --passes B --execute \
  --max-calls <n>
python3 -m archivist_bench analyze --questions "$Q" --evidence-dir "$EV" --primary-judge B \
  > "$EVIDENCE/codex/analysis.json"
python3 -m archivist_bench audit-export --questions "$Q" --evidence-dir "$EV" \
  --analysis "$EVIDENCE/codex/analysis.json" --out "$OUT6"
python3 -m archivist_bench report --questions "$Q" --evidence-dir "$EV" \
  --analysis "$EVIDENCE/codex/analysis.json" --out "$OUT6"
```

The amendment 1 design (two judges, Claude Code pass A plus Codex pass B, `combined` rule) is
`judge` without `--passes` (it then needs `--claude-config-dir`) and `analyze` without
`--primary-judge`; see `docs/HARNESS.md`, "Campaign procedure" and "Amendment 2 mode".

## 6. Grounding, cross check and contamination (amendment 3)

Reads the codex evidence read only. Every grading pass is a Codex call on the ChatGPT plan.

```bash
P=preregistration; Q=$P/questions.json; CQ=$P/contamination-questions.json
EV=$EVIDENCE/codex/campaign            # codex evidence, read only
OUT=$EVIDENCE/grounding                # grounding state
CEV=$OUT/contamination-campaign        # contamination runs
RES=repro/grounding
python3 -m archivist_bench grounding-sources --evidence-dir "$EV" --out "$OUT" --questions "$Q"
python3 -m archivist_bench grounding-hosts --out "$OUT" --execute --max-calls <n>
python3 -m archivist_bench grounding-claims --evidence-dir "$EV" --out "$OUT" --questions "$Q" \
  --execute --max-calls <n>
python3 -m archivist_bench crosscheck-extract --evidence-dir "$EV" --out "$OUT" --execute --max-calls <n>
python3 -m archivist_bench crosscheck-sample --out "$OUT"                  # 400 claims, seed 8105
python3 -m archivist_bench crosscheck-sample --out "$OUT" --extension      # deviation G4, 300 more
python3 -m archivist_bench crosscheck-check --out "$OUT" --execute --max-calls <n>
python3 -m archivist_bench crosscheck-check --out "$OUT" --extension --execute --max-calls <n>
python3 -m archivist_bench crosscheck-audit --out "$OUT" --results "$RES"
# By hand: check each row of $RES/crosscheck-audit.csv (30 contradicted rows, all when fewer,
# plus 10 confirmed) against the filing (permalink, exchange document id and quote are in the
# row) and fill audit_verdict: `correct` when the checker's bin and subtype stand, else
# `incorrect` with "corrected to <bin>/<subtype>" and the evidence in audit_note. In the
# published record an agent did this on behalf of the authors. No command reads the filled
# file: the audited rates in results/grounding/grounding.md section 3 were computed by hand from
# it (for example audited contradicted claims = rows still contradicted / 400 sampled = 2.5%).
# Contamination stratum: the committed contamination-questions.json was built from the cross
# check (addendum G1); --crosscheck binds it to your own cross check results
python3 -m archivist_bench validate-questions --contamination --crosscheck "$OUT" --questions "$CQ"
python3 -m archivist_bench verify-quotes --questions "$CQ" --out "$OUT"
python3 -m archivist_bench run --questions "$CQ" --agent codex --evidence-dir "$CEV" --execute \
  --resume --max-runs 169 --concurrency 2           # 153 runs (17 x 3 arms x 3) plus 10%
# (or batches: --question CN01 --question CN02 --max-runs 20, then the next questions)
python3 -m archivist_bench judge --questions "$CQ" --evidence-dir "$CEV" --passes B --execute \
  --max-calls <n>
python3 -m archivist_bench trap-judge --evidence-dir "$CEV" --out "$OUT" --questions "$CQ" \
  --execute --max-calls <n>
python3 -m archivist_bench grounding-sources --evidence-dir "$CEV" --out "$OUT" --questions "$CQ"
python3 -m archivist_bench grounding-hosts --out "$OUT" --execute --max-calls <n>   # new hosts
python3 -m archivist_bench grounding-claims --evidence-dir "$CEV" --out "$OUT" --questions "$CQ" \
  --execute --max-calls <n>
python3 -m archivist_bench analyze --questions "$CQ" --evidence-dir "$CEV" --primary-judge B \
  > "$OUT/contamination-analysis.json"
python3 -m archivist_bench report --questions "$CQ" --evidence-dir "$CEV" \
  --analysis "$OUT/contamination-analysis.json" --out "$RES/contamination"
python3 -m archivist_bench grounding-analyze --evidence-dir "$EV" --out "$OUT" \
  --contamination-evidence-dir "$CEV" --contamination-questions "$CQ"
python3 -m archivist_bench grounding-report --evidence-dir "$EV" --out "$OUT" --results "$RES" \
  --contamination-evidence-dir "$CEV" --contamination-questions "$CQ"
```

Your cross check sample is drawn from your own runs, so `validate-questions --crosscheck`
against your own state can fail for the committed contamination set; the contamination runs
themselves only need `$CQ`.

## 7. Claude Code replication (amendment 4)

The section 7 Claude Code subset (32 questions) at one repetition plus 8 contamination questions,
three arms, 120 runs. Claude launches stop at 70% of a plan window and run one at a time.

```bash
P=preregistration; Q=$P/questions.json; CQ=$P/contamination-questions.json
S=$EVIDENCE/claude-code; EV=$S/campaign; CEV=$S/contamination-campaign; G=$S/grounding
CODEX_EV=$EVIDENCE/codex/campaign; CODEX_CEV=$EVIDENCE/grounding/contamination-campaign  # read only
RES=repro/claude-code
CA=~/claude-bench-login                       # the Claude login directory from section 2
CC="--agent claude_code --repetitions 1 --stop-at-window 70 --concurrency 1 --claude-config-dir $CA"
claude --version; codex --version; archivist version; archivist usage
python3 -m archivist_bench run --questions "$Q" $CC                    # dry run: 96 runs
python3 -m archivist_bench run --questions "$CQ" $CC                   # dry run: 24 runs
python3 -m archivist_bench plan-probe --claude-config-dir "$CA" --evidence-dir "$EV" \
  --stop-at-window 70                                  # one probe call; exit 0 admits
# batches of questions, 3 runs each at one repetition; for example the first one (21 runs)
K1="--question CT01 --question CT02 --question LK04 --question LK05 --question LK06 --question LK09 --question LK15"
python3 -m archivist_bench run --questions "$Q" $CC $K1 --evidence-dir "$EV" --execute \
  --resume --max-runs 23
# then the other subset questions in batches, or the rest at once (96 runs in all)
python3 -m archivist_bench run --questions "$Q" $CC --evidence-dir "$EV" --execute \
  --resume --max-runs 106
python3 -m archivist_bench run --questions "$CQ" $CC --evidence-dir "$CEV" --execute \
  --resume --max-runs 27                               # 24 runs plus 10%
python3 -m archivist_bench judge --questions "$Q" --evidence-dir "$EV" --execute \
  --max-calls <n> --stop-at-window 70 --claude-config-dir "$CA"   # passes A and B
python3 -m archivist_bench judge --questions "$CQ" --evidence-dir "$CEV" --passes B --execute \
  --max-calls <n>
python3 -m archivist_bench trap-judge --evidence-dir "$CEV" --out "$G" --questions "$CQ" \
  --agent claude_code --execute --max-calls <n>
mkdir -p "$G" && cp "$EVIDENCE/grounding/host-table.json" "$G/"   # the grounding host table first
python3 -m archivist_bench grounding-sources --evidence-dir "$EV" --out "$G" --questions "$Q" \
  --agent claude_code
python3 -m archivist_bench grounding-sources --evidence-dir "$CEV" --out "$G" --questions "$CQ" \
  --agent claude_code
python3 -m archivist_bench grounding-hosts --out "$G" --execute --max-calls <n>
python3 -m archivist_bench grounding-claims --evidence-dir "$EV" --out "$G" --questions "$Q" \
  --agent claude_code --execute --max-calls <n>
python3 -m archivist_bench grounding-claims --evidence-dir "$CEV" --out "$G" --questions "$CQ" \
  --agent claude_code --execute --max-calls <n>
python3 -m archivist_bench grounding-analyze --evidence-dir "$EV" --out "$G" --agent claude_code \
  --contamination-evidence-dir "$CEV" --contamination-questions "$CQ"
python3 -m archivist_bench grounding-report --evidence-dir "$EV" --out "$G" --agent claude_code \
  --contamination-evidence-dir "$CEV" --contamination-questions "$CQ" --results "$RES"
# Judge bias check input: Codex answers graded by both judges. The published check used the 186
# Codex runs of the 22 shared questions that pass A had graded under amendment 1, before
# amendment 2 stopped Claude judging. The selector below matches all 198 Codex runs of those
# questions (22 x 3 arms x 3 repetitions), so grading them all with pass A is an expanded
# reconstruction, not the published sample. Which 186 runs carried a pass A verdict is recorded
# only in the unpublished judgements (results/codex/audit-queue-001.csv shows judge_A_verdict
# for its sampled facts only), so the published selection is not recoverable from this
# repository. Without pass A on any Codex answer the check reports no family interaction.
SHARED="LK04 LK05 LK06 LK09 LK15 MH01 MH02 MH03 MH04 MH14 MD04 MD09 MD12 MD14 MD15 MP01 MP02
  MP05 MP06 MP15 BR01 BR03"
RUNS=$(for q in $SHARED; do ls "$CODEX_EV/runs" | grep -E "^[0-9TZ]+-codex\.[a-z]+\.$q\.r[0-9]+-a[0-9]+$"; done)
python3 -m archivist_bench judge --questions "$Q" --evidence-dir "$CODEX_EV" --passes A \
  $(for r in $RUNS; do printf -- '--run-id %s ' "$r"; done)                         # dry run
python3 -m archivist_bench judge --questions "$Q" --evidence-dir "$CODEX_EV" --passes A \
  --execute --max-calls <n> --stop-at-window 70 --claude-config-dir "$CA" \
  $(for r in $RUNS; do printf -- '--run-id %s ' "$r"; done)
python3 -m archivist_bench analyze --questions "$Q" --evidence-dir "$EV" --primary-judge B \
  --bias-evidence-dir "$CODEX_EV" > "$S/analysis.json"
python3 -m archivist_bench report --questions "$Q" --evidence-dir "$EV" \
  --analysis "$S/analysis.json" --bias-evidence-dir "$CODEX_EV" --out "$RES"
python3 -m archivist_bench analyze --questions "$CQ" --evidence-dir "$CEV" --primary-judge B \
  > "$S/contamination-analysis.json"
python3 -m archivist_bench report --questions "$CQ" --evidence-dir "$CEV" \
  --analysis "$S/contamination-analysis.json" --out "$RES/contamination"
# Codex on the same questions (all three repetitions, exploratory, judge only; the audited
# recomputation of the main subset follows in section 8)
IDS="LK04 LK05 LK06 LK09 LK15 MH01 MH02 MH03 MH04 MH14 MD04 MD09 MD12 MD14 MD15 MP01 MP02 MP05
  MP06 MP15 BR01 BR03 BR07 BR12 BR15 RC01 RC03 RC07 RC14 RC15 CT01 CT02"
CIDS="CN04 CN15 CN03 CN16 CN05 CN02 CN10 CN11"
python3 -m archivist_bench analyze --questions "$Q" --evidence-dir "$CODEX_EV" --primary-judge B \
  $(for i in $IDS; do printf -- '--question %s ' "$i"; done) > "$S/codex-subset-analysis.json"
python3 -m archivist_bench analyze --questions "$CQ" --evidence-dir "$CODEX_CEV" --primary-judge B \
  $(for i in $CIDS; do printf -- '--question %s ' "$i"; done) \
  > "$S/codex-contamination-subset-analysis.json"
```

## 8. Key corrections and the light audit (amendment 5)

Five answer key facts were corrected before any re-judge (`key-corrections-amendment-5.json`; the
question files keep their keys). The corrected facts are re-judged by pass B, four disputed audit
rows of the codex campaign are settled by human audit after checking the filings, and a blind
Codex pre check (pass P) checks every other sampled audit row. No agent run is made.

```bash
P=preregistration; Q=$P/questions.json; CQ=$P/contamination-questions.json
K=$P/key-corrections-amendment-5.json
A5=$EVIDENCE/amendment-5
EV6=$EVIDENCE/codex/campaign; S9=$EVIDENCE/grounding; CEV9=$S9/contamination-campaign
S10=$EVIDENCE/claude-code; EV10=$S10/campaign; CEV10=$S10/contamination-campaign
RES6=repro/codex; RES9=repro/grounding; RES10=repro/claude-code
B="python3 -m archivist_bench"
# 1. corrected keys validated, new and also stated quotes verified (Archivist passage reads)
$B validate-questions --questions "$Q" --key-corrections "$K"
$B validate-questions --contamination --questions "$CQ" --key-corrections "$K"
$B verify-quotes --questions "$Q" --key-corrections "$K" --out "$A5/verify"
$B verify-quotes --questions "$CQ" --key-corrections "$K" --out "$A5/verify"
# 2. pass B re-judge of the corrected facts per campaign, then the rescore CSVs
for C in "codex $Q $EV6 $RES6" "claude-code $Q $EV10 $RES10" \
  "grounding-contamination $CQ $CEV9 $RES9/contamination"; do
  set -- $C
  $B rejudge-keys --questions "$2" --key-corrections "$K" --evidence-dir "$3" --out "$A5/$1"
  $B rejudge-keys --questions "$2" --key-corrections "$K" --evidence-dir "$3" --out "$A5/$1" \
    --execute --max-calls <n>                          # once more for any grade_error
  $B rejudge-keys --questions "$2" --key-corrections "$K" --evidence-dir "$3" --out "$A5/$1" \
    --export "$4/key-rescore-amendment-5.csv"
done
# 3. human audit: in $RES6/audit-queue-001.csv set audit_verdict (and audit_note) on the
#    disputed rows after checking the filings; the published rulings for MH14.f4, BR15.f5,
#    RC14.f16 and RC14.f13 are in results/codex/audit-queue-001.csv
# 4. audit queues from the judge only analyses: claude-code main and contamination, and the
#    grounding contamination 5% sample (15 rows, also in the light route)
$B audit-export --questions "$Q" --evidence-dir "$EV10" --analysis "$S10/analysis.json" --out "$RES10"
$B audit-export --questions "$CQ" --evidence-dir "$CEV10" \
  --analysis "$S10/contamination-analysis.json" --out "$RES10/contamination"
$B audit-export --questions "$CQ" --evidence-dir "$CEV9" \
  --analysis "$S9/contamination-analysis.json" --out "$RES9/contamination"
# 5. pass P over the rows without a human audit verdict (dry run first), all four queues
$B audit-precheck --questions "$CQ" --key-corrections "$K" --evidence-dir "$CEV9" \
  --audit-queue "$RES9/contamination" \
  --key-rescore "$RES9/contamination/key-rescore-amendment-5.csv" \
  --out "$A5/precheck-grounding-contamination" --results "$RES9/contamination" \
  --execute --max-calls <n>
$B audit-precheck --questions "$Q" --key-corrections "$K" --evidence-dir "$EV6" \
  --audit-queue "$RES6" --key-rescore "$RES6/key-rescore-amendment-5.csv" \
  --out "$A5/precheck-codex" --results "$RES6" --execute --max-calls <n>
$B audit-precheck --questions "$Q" --key-corrections "$K" --evidence-dir "$EV10" \
  --audit-queue "$RES10" --key-rescore "$RES10/key-rescore-amendment-5.csv" \
  --out "$A5/precheck-claude-code" --results "$RES10" --execute --max-calls <n>
$B audit-precheck --questions "$CQ" --key-corrections "$K" --evidence-dir "$CEV10" \
  --audit-queue "$RES10/contamination" --out "$A5/precheck-claude-code-contamination" \
  --results "$RES10/contamination" --execute --max-calls <n>
# 5b. human review: pass P can finish and still write disputed rows to audit-owner-list.md in a
#     results folder (the human review list). A person checks each listed row against the
#     filing and fills audit_verdict and audit_note for it in that folder's audit-queue CSV;
#     only then analyze. In the published record every list was empty (pass P agreed on every
#     row), so no row needed this step.
ls "$RES6" "$RES10" "$RES10/contamination" "$RES9/contamination" | grep audit-owner-list.md
# 6. keep the judge only copies, then the audited analyses and reports. The published
#    claude-code contamination figures stay judge only: pass P agreed on both of its sampled
#    rows and no key correction touches that subset, so its analysis.json (harness 1.5.1, no
#    audit files) and the contamination part of its analysis-grounding.json were not recomputed.
#    If your human review fills a verdict there, also run the four claude-code contamination
#    commands at the end of this block.
for D in "$RES6" "$RES10" "$RES9/contamination"; do
  cp "$D/analysis.json" "$D/analysis-judge-only.json"
  cp "$D/comparisons.csv" "$D/comparisons-judge-only.csv"
done
A6="$RES6 $RES6/key-rescore-amendment-5.csv"
$B analyze --questions "$Q" --evidence-dir "$EV6" --primary-judge B --audit $A6 \
  > "$A5/analysis-codex.json"
$B report --questions "$Q" --evidence-dir "$EV6" --analysis "$A5/analysis-codex.json" \
  --audit $A6 --out "$RES6"
A9="$RES9/contamination $RES9/contamination/key-rescore-amendment-5.csv"
$B analyze --questions "$CQ" --evidence-dir "$CEV9" --primary-judge B --audit $A9 \
  > "$A5/analysis-grounding-contamination.json"
$B report --questions "$CQ" --evidence-dir "$CEV9" \
  --analysis "$A5/analysis-grounding-contamination.json" --audit $A9 --out "$RES9/contamination"
$B grounding-analyze --evidence-dir "$EV6" --out "$S9" --contamination-evidence-dir "$CEV9" \
  --contamination-questions "$CQ" --contamination-audit $A9
$B grounding-report --evidence-dir "$EV6" --out "$S9" --contamination-evidence-dir "$CEV9" \
  --contamination-questions "$CQ" --contamination-audit $A9 --results "$RES9"
A10="$RES10 $RES10/key-rescore-amendment-5.csv"
$B analyze --questions "$Q" --evidence-dir "$EV10" --primary-judge B --audit $A10 \
  --bias-evidence-dir "$EV6" > "$A5/analysis-claude-code.json"
$B report --questions "$Q" --evidence-dir "$EV10" --analysis "$A5/analysis-claude-code.json" \
  --audit $A10 --bias-evidence-dir "$EV6" --out "$RES10"
# only when a human audit verdict was filled in $RES10/contamination (not needed for the
# published record):
#   $B analyze --questions "$CQ" --evidence-dir "$CEV10" --primary-judge B \
#     --audit "$RES10/contamination" > "$A5/analysis-claude-code-contamination.json"
#   $B report --questions "$CQ" --evidence-dir "$CEV10" \
#     --analysis "$A5/analysis-claude-code-contamination.json" --audit "$RES10/contamination" \
#     --out "$RES10/contamination"
#   $B grounding-analyze --evidence-dir "$EV10" --out "$S10/grounding" --agent claude_code \
#     --contamination-evidence-dir "$CEV10" --contamination-questions "$CQ" \
#     --contamination-audit "$RES10/contamination"
#   $B grounding-report --evidence-dir "$EV10" --out "$S10/grounding" --agent claude_code \
#     --contamination-evidence-dir "$CEV10" --contamination-questions "$CQ" \
#     --contamination-audit "$RES10/contamination" --results "$RES10"
# 7. the Codex same question recomputation with the codex audit (exploratory JSON, not
#    reported); IDS as in section 7
$B analyze --questions "$Q" --evidence-dir "$EV6" --primary-judge B --audit $A6 \
  $(for i in $IDS; do printf -- '--question %s ' "$i"; done) > "$A5/codex-subset-analysis.json"
```

## 9. Expected outputs

| Step | Writes |
|---|---|
| `run --execute` | `$EV/runs/<run id>/` (`meta.json`, `command.json`, the run's Codex home or MCP config, `stdout.jsonl`, `stderr.log`, `answer.md`, `record.json`), `$EV/ledger.jsonl` (one row per attempt), `plan-probes.jsonl`, `closing-probe.json`, `archivist-usage.jsonl` |
| `judge` | `runs/<run id>/judgement.json`, `judge-ledger.jsonl`, `judge-logs/` |
| `analyze` | JSON on stdout: per stratum and arm accuracy, tokens, completion, paired bootstrap comparisons (10,000 resamples, seed 8105), billing warnings, `harness_version`, `questions_sha256`, `audit_files`, `evidence_fingerprint` |
| `audit-export` | `audit-queue-001.csv`, `audit-queue-001.md` (more shards if large), `audit-index.md` |
| `report` | `runs.csv`, `cells.csv`, `comparisons.csv`, `multi_period_tokens_by_filings.csv`, `analysis.json`, `answers/<run id>.md` (links redacted), `plan-probes.csv`, `logs/<run id>.jsonl.gz` and `logs/manifest.csv` (scrubbed, clipped logs); it refuses an analysis whose evidence fingerprint no longer matches |
| grounding commands | under `--out`: `sources/`, `hosts.json`, `host-table.json`, `claims/`, `crosscheck/`, `traps/`, `analysis-grounding.json`; `grounding-report` writes `host-categories.csv`, `exposure-runs.csv`, `exposure-cells.csv`, `claims-NNN.csv`, `crosscheck-sample.csv`, `crosscheck-extension-bins.csv`, `analysis-grounding.json` and `contamination/claims-NNN.csv` to `--results` |
| `rejudge-keys` | `$A5/<campaign>/key-rejudge/<run id>.json`; with `--export`, the rescore CSV |
| `audit-precheck` | `precheck/<run id>__<fact id>.json`; `audit-precheck.csv` and, only when a row disputes, `audit-owner-list.md` (the human review list) in `--results` |

Your `repro/<campaign>/` folders then hold the same files as the matching `results/` folders,
and their tables can be compared. `report` and `grounding-report` also run the harness's file
check (no upstream filing host, no credential pattern, the 1 MB file limit).

**The committed analyses.** `analyze` and `report` read a campaign's raw evidence (run records,
judgements, raw logs), which is not published: it holds account level session data and unclipped
third party page text. The committed `analysis.json` files therefore cannot be recomputed from
this repository alone. Rerunning `analyze` over the internal evidence on 2026-10-08 gave every
committed number and verdict again; the hash fields that differ because this edition changed a
few file bytes are listed in [`REDACTIONS.md`](REDACTIONS.md).

## 10. Publishing your own logs

`report` scrubs the logs it writes (credentials, account and session id keys, emails, links) and
`report.check_files` checks them, but that scrubber does not remove Codex thread and context
window ids (also in the `logs/manifest.csv` rollout file names), Claude Code session ids inside
saved tool result paths, the local socket path, local and drive letter paths, the upstream filing
paths that stay behind a masked host (`filing-source/...`) or Codex's encrypted reasoning blobs.
The logs in this repository went through an additional pass for those classes, listed class by
class in `REDACTIONS.md`. Before publishing reproduced logs, apply equivalent redaction and rerun
the checks: `gitleaks dir`, `redact.find_upstream_hosts` and `report.check_files` over every
file, gzip logs decompressed.

## 11. What cannot be reproduced exactly

- **Live web results.** The web and both arms used each agent's live web search and page fetches
  in October 2026. Search results, page contents and availability change, so token counts,
  sources and answers of a rerun will differ.
- **Model and service drift.** `gpt-6.1-sol` and `claude-opus-5-5` are hosted models that can
  change or be retired, and the agents' hosted search tools change without a client version
  bump. Pinning the CLI versions does not pin the services.
- **Plan limits.** Plan windows, usage caps and their reset times depend on your plan and on
  anything else using the same login; batches stop and resume at different points.
- **Judge nondeterminism.** Neither CLI exposes a sampling temperature (the fixed setting is the
  effort), so the same answer can get a different verdict on a rerun; the committed verdicts
  are the ones recorded.
- **Archivist index changes.** The answer keys were taken from the filings as stored on
  2026-10-06. The index adds filings and can re-chunk or re-rank passages over time, so Archivist
  results, token counts and `verify-quotes` passage reads can differ.
- **Host context.** Each agent session carries host supplied instructions (for Codex, its system
  skills list); the committed token counts include the context the agents had then, yours will
  include your own.

## 12. Run it on your own questions

You can measure tokens and sources on your own research questions. Answer keys are only needed
to score accuracy; without them you get cost and provenance, not a correctness score.

1. Write `my-questions.json`:
   `{"questions": [{"id": "MY01", "stratum": "my_set", "question": "..."}]}`. Ids must be
   unique; use any stratum label except `control`; `companies` is optional. No facts, filing ids,
   permalinks or quotes are needed.
2. Plan, naming every question (Claude Code otherwise runs only its fixed subset of the standard
   strata):
   `python3 -m archivist_bench plan --questions my-questions.json --allow-invalid-set --agent claude_code --repetitions 1 --question MY01 --question MY02`
   (`--allow-invalid-set` accepts a set without answer keys.)
3. Run the same plan with `run ... --execute --max-runs N --evidence-dir runs/my-set`, using the
   pinned tools from section 1 and the sign in from section 2 (`--claude-config-dir` for Claude
   Code). The archivist and both arms need archivist-cli signed in to a paid or trialing Mosaic
   account.
4. Tokens per question and the ratio against native web tools: `analyze`, then `report`.
   Accuracy is left empty and listed as dropped; `judge` needs answer keys.
5. Share of sources read that were filings: `grounding-sources`, then `grounding-analyze` and
   `grounding-report` (no model calls; filing hosts are built in).
6. Optional, answer claims citing a non filing source: `grounding-claims` (pass R), which calls
   Codex `gpt-6.1-sol` on a ChatGPT login even when the agent under test is Claude Code.

Archivist access: the Mosaic MCP and CLI plan is USD 8 per month with a 30 day free trial that
covers the usage required; fair use is 10,000 Archivist calls a month.
