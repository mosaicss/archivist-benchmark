---
title: 'Archivist versus native web tools: Codex results'
type: 'results'
campaign: codex
run: '2026-10-07'
preregistration: '../../preregistration/preregistration.md (amendments 1, 2 and 5, deviations D1 to D5)'
harness: 'archivist_bench 1.2.1 for the runs, 1.3.0 for judging and analysis, 1.6.0 for the amendment 5 rescoring, pre check and audited analysis'
status: 'complete for Codex; light audit applied (amendment 5, 2026-10-08); answer quality review by the authors pending; Claude Code arm in the claude-code campaign (results/claude-code/)'
---

# Archivist versus native web tools: Codex results

Scope of every number below: Codex CLI 0.160.0, `gpt-6.1-sol`, effort `medium`, default tier,
ChatGPT plan login; 92 pre registered questions (90 scored plus 2 controls), three arms, three
repetitions, 828 runs; accuracy graded by the Codex judge (pass B) against answer keys taken
from the filings, audited by the light route of amendment 5 (2026-10-08: four key facts corrected
and rescored by the same judge, four human audit verdicts by the authors, every other sampled fact agent pre checked;
see "Judging, audit and limits"). Where an accuracy figure moved, the judge only figure (before
amendment 5) follows it in brackets as "judge only"; every token, ratio and completion figure is
unchanged by the audit. "Ratio" is web arm total tokens over the other arm's
(sum over sum, completed runs; above 1 means the other arm used fewer). Intervals are paired
bootstrap 95% percentile intervals over questions (10,000 resamples, seed 8105). The Claude Code
arm is deferred by amendment 2 and no number here describes Claude Code.

## Headline

On the 90 scored questions, Codex with Archivist alone used **1.93x fewer tokens** than Codex
with its own web search (95% CI 1.60 to 2.32), and Codex with both (the delivered product) used
**1.62x fewer** (1.35 to 1.93), at the same accuracy: accuracy differences are within one point
(audited: archivist minus web -0.000, CI -0.004 to +0.004; both minus web +0.001, CI -0.003 to
+0.004; judge only: -0.001, CI -0.006 to +0.003, and -0.001, CI -0.006 to +0.002) and every arm
completed every task. The gain is concentrated where Archivist was designed to
help: breadth across companies (2.88x, CI 1.84 to 4.13) and reach across 20 to 26 filings of
one company (2.31x, 1.93 to 2.72), and non US filings (SEDAR+ and KAP, 2.69x pooled,
exploratory). It is absent or reversed on single lookups and on US multi period questions.

Accuracy sits at the ceiling in every arm (audited question mean 0.991 to 1.000; judge only 0.987
to 1.000), so this benchmark
measures cost at equal accuracy; it cannot show an accuracy advantage for any arm.

## Tested system

| Item | Value |
|---|---|
| Agent | Codex CLI 0.160.0 (pinned install), `gpt-6.1-sol`, effort `medium`, default tier, ChatGPT login (plan allowance; no API key, no credits used: balance 62,500 before and after) |
| Archivist | archivist-cli v0.2.33 (8 MCP tools; release checksum verified), the production Archivist API revision, recorded in the internal run ledger (read before every batch) |
| Judge | Codex CLI 0.160.0, `gpt-6.1-sol`, effort `medium`, blinded prompt (pre registration section 8 rubric); 551 of 810 answers were also graded by Claude Code `claude-opus-5-5` before amendment 2 |
| Runs | 2026-10-07 03:28Z to 14:55Z; 829 attempts: 828 completed, 1 invalid (D5, rerun once) |
| Archivist calls | 9,291 on the account's monthly CLI counter (0 after the 03:28Z reset; read before and after every batch, internal run ledger, not published); `runs.csv` records 9,348 Archivist MCP tool calls, a different measure (tool calls as the agent logged them) |

## Results per stratum and arm

Question means (repetitions averaged first). Tokens are total tokens per run (input including
cached, plus output); completion is the judge's `complete` (all 1.0).

| Stratum (n questions) | Arm | Accuracy | Tokens per run | Ratio web over arm (95% CI) | Accuracy arm minus web (95% CI) |
|---|---|---:|---:|---|---|
| lookup (15) | web | 1.000 | 63,928 | | |
| | archivist | 1.000 | 82,067 | 0.78 (0.62 to 0.99) | +0.000 (+0.000 to +0.000) |
| | both | 1.000 | 73,822 | 0.87 (0.74 to 0.97) | +0.000 (+0.000 to +0.000) |
| multi hop document (15) | web | 0.994 | 157,191 | | |
| | archivist | 1.000 | 88,387 | 1.78 (0.87 to 3.30) | +0.006 (+0.000 to +0.017) |
| | both | 1.000 [judge only 0.994] | 104,432 | 1.51 (0.86 to 2.31) | +0.006 (+0.000 to +0.017) [judge only +0.000] |
| multi document (15) | web | 1.000 | 109,105 | | |
| | archivist | 0.991 | 88,918 | 1.23 (0.89 to 1.63) | -0.009 (-0.027 to +0.000) |
| | both | 1.000 | 119,330 | 0.91 (0.78 to 1.04) | +0.000 (+0.000 to +0.000) |
| multi period (15) | web | 1.000 | 225,155 | | |
| | archivist | 1.000 | 184,633 | 1.22 (0.95 to 1.51) | +0.000 (+0.000 to +0.000) |
| | both | 1.000 | 218,214 | 1.03 (0.82 to 1.29) | +0.000 (+0.000 to +0.000) |
| breadth (15) | web | 0.994 [judge only 0.987] | 598,310 | | |
| | archivist | 1.000 [judge only 0.993] | 207,516 | 2.88 (1.84 to 4.13) | +0.006 (+0.000 to +0.017) |
| | both | 0.998 [judge only 0.990] | 264,723 | 2.26 (1.45 to 3.21) | +0.003 (-0.007 to +0.017) |
| reach (15) | web | 1.000 | 662,303 | | |
| | archivist | 0.996 [judge only 0.993] | 287,218 | 2.31 (1.93 to 2.72) | -0.004 (-0.011 to +0.000) [judge only -0.007 (-0.022 to +0.000)] |
| | both | 0.994 [judge only 0.989] | 340,464 | 1.95 (1.63 to 2.31) | -0.006 (-0.017 to +0.000) [judge only -0.011 (-0.033 to +0.000)] |
| **all scored (90)** | archivist | | | **1.93 (1.60 to 2.32)** | -0.000 (-0.004 to +0.004) [judge only -0.001 (-0.006 to +0.003)] |
| | both | | | **1.62 (1.35 to 1.93)** | +0.001 (-0.003 to +0.004) [judge only -0.001 (-0.006 to +0.002)] |

Reach completion (H5): every arm completed all 45 reach runs (completion rate 1.0 for web,
archivist and both; difference 0.0, CI 0.0 to 0.0); no timeout, turn limit or compaction in any
run. Controls (fixed tool overhead, not compared): web 26,282, archivist 39,982, both 37,452
tokens per run.

### Both arm against each single arm

| Stratum | Web over both: ratio (CI); accuracy both minus web (CI) | Both against archivist: ratio archivist over both (CI); accuracy both minus archivist (CI) |
|---|---|---|
| lookup | 0.87 (0.74 to 0.97); +0.000 | 1.11 (0.91 to 1.38); +0.000 |
| multi hop document | 1.51 (0.86 to 2.31); +0.006 (+0.000 to +0.017) [judge only +0.000] | 0.85 (0.68 to 1.06); +0.000 [judge only -0.006 (-0.017 to +0.000)] |
| multi document | 0.91 (0.78 to 1.04); +0.000 | 0.75 (0.60 to 0.95); +0.009 (+0.000 to +0.027) |
| multi period | 1.03 (0.82 to 1.29); +0.000 | 0.85 (0.73 to 1.00); +0.000 |
| breadth | 2.26 (1.45 to 3.21); +0.003 (-0.007 to +0.017) | 0.78 (0.66 to 0.95); -0.003 (-0.007 to +0.000) |
| reach | 1.95 (1.63 to 2.31); -0.006 (-0.017 to +0.000) [judge only -0.011 (-0.033 to +0.000)] | 0.84 (0.76 to 0.94); -0.002 (-0.006 to +0.000) [judge only -0.004 (-0.011 to +0.000)] |
| all scored | 1.62 (1.35 to 1.93); +0.001 (-0.003 to +0.004) [judge only -0.001 (-0.006 to +0.002)] | 0.84 (0.78 to 0.90); +0.001 (-0.002 to +0.004) [judge only -0.001 (-0.004 to +0.004)] |

In the "both against archivist" column a ratio below 1 means the both arm used more tokens than
Archivist alone. Exploratory: in the both arm Codex called Archivist in 122 of 270 scored runs
(66 with web search too, 56 Archivist only) and answered from web search alone in 148.

### Tokens versus number of filings (multi period)

Mean total tokens per run by the number of distinct filings in the answer key
(`multi_period_tokens_by_filings.csv`, exploratory series):

| Question | Filings | Source | Web | Archivist | Both | Web over archivist |
|---|---:|---|---:|---:|---:|---:|
| MP04 | 5 | SEDAR+ | 174,804 | 147,068 | 165,911 | 1.19 |
| MP05 | 5 | KAP | 583,132 | 286,923 | 277,475 | 2.03 |
| MP01 | 6 | SEDAR+ | 260,959 | 133,324 | 260,243 | 1.96 |
| MP15 | 6 | SEC | 104,213 | 110,905 | 140,200 | 0.94 |
| MP03 | 7 | SEDAR+ | 290,921 | 231,171 | 373,940 | 1.26 |
| MP07 | 7 | SEC | 234,009 | 178,175 | 331,896 | 1.31 |
| MP14 | 7 | SEC | 124,082 | 189,559 | 148,361 | 0.65 |
| MP02 | 8 | SEDAR+ | 410,570 | 162,446 | 239,410 | 2.53 |
| MP08 | 8 | SEC | 154,085 | 159,075 | 178,877 | 0.97 |
| MP09 | 8 | SEC | 166,610 | 161,338 | 145,363 | 1.03 |
| MP10 | 8 | SEC | 124,494 | 180,333 | 99,578 | 0.69 |
| MP11 | 8 | SEC | 326,305 | 258,095 | 303,488 | 1.26 |
| MP12 | 8 | SEC | 141,114 | 169,943 | 176,475 | 0.83 |
| MP13 | 8 | SEC | 139,409 | 197,182 | 165,326 | 0.71 |
| MP06 | 10 | SEDAR+ | 142,616 | 203,965 | 266,661 | 0.70 |

Tokens do not grow with the number of filings in any arm over this 5 to 10 filing range; the
source matters more than the count (Archivist used fewer tokens on five of the six non US
questions, web search on six of the nine SEC questions). Reach questions (20 to 26 filings) show the larger,
consistent Archivist gain above.

## Pre registered hypotheses (Codex)

Decision rules from pre registration section 4, applied by hand; amendment 2 (Codex only,
pass B scoring), audited under amendment 5. The audit moved no verdict: applied to the judge only
figures (in brackets) every verdict is the same. The single judge recomputation and the judge bias check are deferred with the
Claude Code arm (amendment 2), so no hypothesis carries a judge sensitivity label.

| Id | Test | Estimate (95% CI) | Verdict |
|---|---|---|---|
| H1 | multi period, archivist vs web: ratio at least 2x and no accuracy loss | ratio 1.22 (0.95 to 1.51); accuracy +0.000 (+0.000 to +0.000) | **not supported** (CI lower bound below 2.0 and below 1.0; point below 2.0) |
| H2 | both arm as accurate as the best single arm (margin 0.05) and cheaper than web | pooled: ratio 1.62 (1.35 to 1.93); accuracy vs web +0.001 (-0.003 to +0.004), vs archivist +0.001 (-0.002 to +0.004) [judge only -0.001 (-0.006 to +0.002) and -0.001 (-0.004 to +0.004)] | **supported pooled**; per stratum: breadth and reach **supported**, multi hop document and multi period **partly** (accuracy met, ratio point above 1, CI includes 1), lookup and multi document **not supported** (ratio point below 1) |
| H3 | lookup, archivist no worse than web | accuracy +0.000 (CI +0.000 to +0.000); ratio 0.78 (0.62 to 0.99) | **not supported** (ratio CI lower bound below 0.9 and point below 1.0) |
| H4 | breadth, archivist at least equal | accuracy +0.006 (+0.000 to +0.017); ratio 2.88 (1.84 to 4.13) | **supported** |
| H5 | reach, archivist and both complete tasks web fails | completion difference 0.0 (0.0 to 0.0) for both arms; accuracy archivist -0.004 (-0.011 to +0.000), both -0.006 (-0.017 to +0.000) [judge only -0.007 (-0.022 to +0.000) and -0.011 (-0.033 to +0.000)] | **not supported** (web completed every reach task; Archivist's reach advantage is cost, 2.31x and 1.95x fewer tokens) |

## Where Mosaic did not win

1. **Lookups (H3).** One to three facts from one filing: the Archivist arm used 1.28x the web
   arm's tokens (ratio 0.78), 5.4 model calls against 3.3, and 22 s against 14 s per run; US
   lookups were worst (ratio 0.65, exploratory). The both arm also cost more than web (0.87).
2. **Multi period, the stratum pre registered for Archivist (H1).** 1.22x (CI 0.95 to 1.51), far from the
   2x hypothesis and below the 2026-10-05 pilot's figure. On SEC filings web search was cheaper
   or equal (0.94 pooled US, exploratory); MP06 (10 SEDAR+ filings) favoured web as well.
3. **The both arm cost more than web on multi document questions** (119,330 against 109,105
   tokens per run, ratio 0.91, CI 0.78 to 1.04) as well as on lookups (0.87).
4. **The both arm costs more than Archivist alone in every stratum but lookup** (pooled 0.84,
   CI 0.78 to 0.90): adding web search to Archivist made Codex spend about 19% more tokens than
   Archivist alone, and in 148 of 270 scored both arm runs Codex never called Archivist.
5. **US multi hop questions** (exploratory): Archivist arm ratio 0.82, and US multi document
   about even (1.08); the large multi hop gain (1.78 overall) comes from the non US questions
   (3.81).
6. **Accuracy.** No arm demonstrated accuracy superiority (the small positive differences, such
   as archivist +0.006 on multi hop and breadth, have intervals touching 0); the Mosaic arms were
   marginally lower on reach
   (audited archivist 0.996, both 0.994 against web 1.000; judge only 0.993 and 0.989) and multi
   document (archivist 0.991 against 1.000), within the 0.05 margin and with intervals touching 0.
7. **Completion (H5).** Codex with web search alone completed every reach question within the
   wall limit; Archivist did not rescue any failing task, because none failed.
8. **Fixed overhead and speed.** The Archivist tool list adds about 14,000 tokens per run on the
   controls (39,982 against 26,282); pooled wall time per run was 50 s against 46 s for web.
9. **Tool output size.** Codex cut Archivist tool results 172 times across the archivist arm
   (`Warning: truncated output`), against 28 cuts of web results; large search pages still
   exceed Codex's tool output budget.
10. **Archivist call volume.** The campaign made 9,291 Archivist calls against 3,585 estimated
   for the Codex share; breadth and reach batches used about 1,100 and 1,950 calls per 45 runs.

## Judging, audit and limits

- **Judge.** Every scored answer was graded by the Codex judge (pass B). The 551 answers graded
  by both judges before amendment 2 agree on 2,714 of 2,715 facts (99.96%) and on every
  `complete` flag (exploratory `pass_agreement`). The one disagreement is the only disputed
  fact.
- **Human audit (light route, amendment 5, 2026-10-08).** The queue (`audit-queue-001.csv` and
  `.md`, index `audit-index.md`): the 1 disputed fact plus a seeded 5% sample of the other judged
  facts (352), each with the question, key statement, expected value, Mosaic filing permalink,
  exchange document id or absence reason, key quote, judge verdicts and the link redacted answer.
  - Human audited: the four rows the judge marked incorrect (MH14.f4, BR15.f5, RC14.f16,
    RC14.f13). Each was a key error, not an answer error, and the authors ruled each answer correct after checking the filing
    (`audit_verdict` `correct`, the ruling quoted in `audit_note`): the filing states 395,772 as
    well as 396,000 tonnes (MH14); 290,236 ounces is the Rainy River mine and the company total is
    353,772 (BR15); the Shopify MD&As state $236 billion and $50 billion as well as $235.9 billion
    and $49.6 billion (RC14).
  - Key corrections: those four key facts are corrected (pre registration section 16.1,
    `key-corrections-amendment-5.json`) and rescored in all 27 Codex answers that carry them by
    the same pass B judge on the corrected key (judge calls only; `key-rescore-amendment-5.csv`):
    20 of 36 fact verdicts changed, all from incorrect to correct (16 by the re-judge, 4 by the
    human audit verdicts, on which the re-judge agreed); on the other facts of the same answers the
    re-judge agreed with the stored verdicts on 297 of 297.
  - Agent pre checked, not human audited: the other 349 sampled facts got one blind Codex pre
    check each against the filing passage (pass P, section 16.3; `audit-precheck.csv`). It agreed
    with the judge on 349 of 349 (332 key passages supported the key, 17 absence facts had no
    passage to check), so the human review list is empty and those facts keep the judge score. All 349
    were judged correct, so the sample tests the judge's correct verdicts only.
  - Scope of "audited" below and above: amendment 5 covers 385 of the 7,047 judged fact
    instances: the 353 sampled ones (4 human audited, 349 agent pre checked) and 32 more rescored
    under the corrected keys (the 36 rescored instances less the 4 human audited rows); every other fact
    is judge only.
- **Same family judge.** The judge is from the answering model's family. Every arm is graded by
  the same judge with the same blinded prompt (links, hosts and tool names hidden), which removes
  the obvious ways a judge could tell the arms apart, but arm dependent judging bias is untested:
  the judge bias check is deferred with the Claude Code arm. The absolute accuracy level may
  also be generous. The human audit is the check, and the cross family judge returns with the
  Claude Code arm.
- **Ceiling.** With accuracy at or near 1.0 everywhere, the accuracy intervals show
  non inferiority, not superiority, and token cost is the discriminating measure.
- **Deviations.** D1 to D4 (before the campaign) and D5 (C1 isolation false positive, one cell
  rerun once); amendment 2 (Codex only) made before any analysis. Pre registration sections 11
  to 13.
- **Measurement notes.** Tool call counts can undercount calls inside Codex code mode; USD in
  `runs.csv` is an API list price equivalent (pooled per run: web 0.169, archivist 0.099, both
  0.123), not a bill: every run drew on the ChatGPT plan.

## Files

| File | Content |
|---|---|
| `runs.csv` | every attempt (829): identity, status, tokens, calls, tools, events, wall time, cost basis, plan window, accuracy, completion, judge status, answer and log paths |
| `cells.csv`, `comparisons.csv` | per stratum and arm summaries; every comparison with estimate, 95% CI, n, seed and dropped questions (none dropped) |
| `multi_period_tokens_by_filings.csv` | the multi period series |
| `analysis.json` | the audited `analyze --primary-judge B --audit` output these tables come from (amendment 5; evidence fingerprint `1e88c94cd2959596d5ebaf1a8a002c8c76c005aa485603e1a7a32f47c2aae568`) |
| `analysis-judge-only.json`, `comparisons-judge-only.csv` | the judge only analysis and comparisons before amendment 5 (fingerprint `0a9bb5d6587cf5f276b6c531df2d792630f266dae8e6648b929f42d152149920`) |
| `answers/`, `logs/` | each run's answer (links redacted) and its Codex rollout (links redacted, strings over 4,000 characters clipped with length and sha256, secrets and account ids masked, gzip); `logs/manifest.csv` maps each to the raw file kept outside git |
| `audit-index.md`, `audit-queue-001.*` | the human audit queue, with the four human audit verdicts by the authors |
| `key-rescore-amendment-5.csv` | the amendment 5 rescoring of the corrected key facts (pass B re-judge) |
| `audit-precheck.csv` | the pass P pre check of the other 349 sampled facts |
| `plan-probes.csv` | the billing evidence: every plan probe and closing probe of the campaign |

Raw evidence (unredacted rollouts, judge logs, ledgers) stays outside git in
`<evidence>/codex/`.
