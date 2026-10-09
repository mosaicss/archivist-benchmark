---
title: 'Archivist versus native web tools: a light Claude Code replication'
type: 'results'
campaign: claude-code
run: '2026-10-08'
preregistration: '../../preregistration/preregistration.md (section 15, amendment 4, with its 2026-10-08 notes; section 16, amendment 5, for the key corrections and the light audit)'
harness: 'archivist_bench 1.5.1 (Claude login selection only; the measurement is the frozen 1.5.0 design); 1.6.0 for the amendment 5 rescoring, pre check and audited analysis'
status: 'replication, reduced power; audited by the light route of amendment 5 (2026-10-08); answer quality review by the authors pending'
---

# Archivist versus native web tools: a light Claude Code replication

Scope of every Claude Code number below: Claude Code 2.1.289 (pinned install), `claude-opus-5-5`,
effort `medium`, default tier, `claude -p --restricted` on the Claude subscription (plan
allowance, `apiKeySource: none` in every run; a separate Claude subscription login chosen explicitly with
`--claude-config-dir`, pre registration 15.4 note);
archivist-cli 0.2.33. Main set: the section 7 Claude Code subset (30 scored questions, 5 per
stratum, plus 2 controls), **one repetition**, three arms, 96 runs. Contamination subset: 6 trap
and 2 control questions (section 15.1), one repetition, 24 runs. All 120 runs completed on the
first attempt (2026-10-08 02:12Z to 03:31Z); none timed out, hit the turn limit or compacted.

Every Claude Code figure and verdict is **replication, reduced power** (section 15.6): n = 5
questions per stratum and one repetition, so intervals are wide and a single question moves a
stratum. The design was fixed after the Codex results were known (section 15, status when
made), so nothing here is new confirmatory evidence. Codex figures appear beside them twice: as
published in the codex and grounding campaigns (`results/codex/`, `results/grounding/`; all 90 questions, three repetitions), and recomputed on the same 30
questions (`analyze --question`, all three repetitions, exploratory).

"Ratio" is web arm total tokens over the other arm's (sum over sum, completed runs; above 1 means
the other arm used fewer). Intervals are paired bootstrap 95% percentile intervals over questions
(10,000 resamples, seed 8105). Accuracy is graded by the Codex judge (pass B, the scoring rule of
the Codex numbers); for Claude Code answers pass B is the cross family judge. Accuracy is audited
by the light route of amendment 5 (2026-10-08): four key facts corrected (MH14.f4, BR15.f5,
RC14.f16, RC14.f13) and rescored by the same judge in every answer, Claude Code and Codex alike,
the four human audit verdicts by the authors from the codex campaign applied, every other sampled fact agent pre checked. Where an accuracy
figure or verdict moved, the judge only figure (before amendment 5) follows in brackets as
"judge only"; every token, ratio, completion and grounding figure is unchanged by the audit.

## Headline

- **Tokens.** On the 30 scored questions Claude Code with Archivist alone used **3.26x fewer
  tokens** than Claude Code with its own web tools (95% CI 2.13 to 4.80), and with both
  **3.57x fewer** (2.44 to 5.03). Codex on the same 30 questions: 1.75x (1.44 to 2.10) and 1.36x
  (1.16 to 1.58); Codex on all 90 (codex campaign): 1.93x and 1.62x. The direction replicates; the size is
  larger for Claude Code.
- **Accuracy.** No demonstrated accuracy difference pooled: archivist minus web +0.026 (-0.006 to
  +0.076), both minus web +0.016 (-0.011 to +0.060) [judge only +0.033 (+0.001 to +0.082) and
  +0.024 (+0.000 to +0.067)]. Accuracy is near the ceiling (pooled question means 0.970 to 0.996;
  judge only 0.967 to 1.000), as for Codex, so this measures cost at equal accuracy. On breadth
  the Archivist arms gave New Gold's Rainy River mine figure instead of the company total (one
  fact), which costs H4 and H2 on that stratum (below).
- **The both arm behaved differently.** Given web tools and Archivist together, Claude Code
  called Archivist in every scored run and web search once in 32 runs (a control). Codex in the codex campaign
  called Archivist in 122 of 270 scored both arm runs and searched the web in the rest. For
  Claude Code the both arm is close to an Archivist only arm, so its numbers mostly repeat the
  archivist arm.
- **Provenance.** What Claude Code chose to read in the both arm was 100% filings (478 of 478
  opened sources and Archivist results; Codex 87.5%), and no both or archivist arm fact claim
  traced to a non filing source (Codex both arm 25.1% under the primary rule). In the web arm
  filings were 58.1% of what it read (Codex 43.6%).
- **Judges.** The two judge families agreed on every one of the 831 Claude Code facts. The
  pre registered judge bias check (family interaction) is -0.0013 (-0.0042 to 0.0000), not
  flagged.

## Results per stratum and arm

Question means (one run per question and arm for Claude Code; repetitions averaged first for
Codex). Tokens are total tokens per run (input including cached, plus output); every Claude Code
run was judged complete except MH04 in the web arm.

| Stratum (5 questions) | Arm | Claude Code accuracy | Claude Code tokens per run | Codex accuracy (same questions) | Codex tokens per run (same questions) |
|---|---|---:|---:|---:|---:|
| lookup | web | 1.000 | 74,245 | 1.000 | 78,102 |
| | archivist | 1.000 | 29,290 | 1.000 | 76,386 |
| | both | 1.000 | 24,175 | 1.000 | 80,212 |
| multi hop document | web | 0.830 | 439,856 | 0.983 | 161,251 |
| | archivist | 1.000 | 49,193 | 1.000 | 91,704 |
| | both | 0.950 | 61,356 | 1.000 [judge only 0.983] | 115,860 |
| multi document | web | 1.000 | 157,965 | 1.000 | 133,685 |
| | archivist | 1.000 | 39,330 | 1.000 | 85,111 |
| | both | 1.000 | 40,609 | 1.000 | 137,330 |
| multi period | web | 1.000 | 333,619 | 1.000 | 300,298 |
| | archivist | 1.000 | 125,283 | 1.000 | 179,513 |
| | both | 1.000 | 102,617 | 1.000 | 236,798 |
| breadth | web | 1.000 [judge only 0.978] | 510,694 | 1.000 [judge only 0.978] | 444,152 |
| | archivist | 0.978 [judge only 1.000] | 97,058 | 1.000 [judge only 0.978] | 230,469 |
| | both | 0.978 [judge only 1.000] | 103,917 | 0.993 [judge only 0.970] | 304,229 |
| reach | web | 0.992 | 387,797 | 1.000 | 536,724 |
| | archivist | 1.000 | 244,272 | 0.989 [judge only 0.978] | 279,489 |
| | both | 0.992 | 200,156 | 0.983 [judge only 0.967] | 337,602 |
| **all scored (30)** | web | 0.970 [judge only 0.967] | 317,363 | 0.997 [judge only 0.994] | 275,702 |
| | archivist | 0.996 [judge only 1.000] | 97,404 | 0.998 [judge only 0.993] | 157,112 |
| | both | 0.987 [judge only 0.990] | 88,805 | 0.996 [judge only 0.987] | 202,005 |

Paired comparisons (Claude Code, replication, reduced power; `comparisons.csv`):

| Stratum | Archivist: ratio web over arm | Archivist: accuracy arm minus web | Both: ratio web over arm | Both: accuracy arm minus web | Both minus archivist: accuracy |
|---|---|---|---|---|---|
| lookup | 2.53 (0.99 to 4.74) | +0.000 (+0.000 to +0.000) | 3.07 (1.21 to 6.39) | +0.000 (+0.000 to +0.000) | +0.000 (+0.000 to +0.000) |
| multi hop document | 8.94 (5.92 to 11.9) | +0.170 (+0.000 to +0.410) | 7.17 (3.74 to 10.4) | +0.120 (+0.000 to +0.360) | -0.050 (-0.150 to +0.000) |
| multi document | 4.02 (3.36 to 4.68) | +0.000 (+0.000 to +0.000) | 3.89 (3.33 to 4.41) | +0.000 (+0.000 to +0.000) | +0.000 (+0.000 to +0.000) |
| multi period | 2.66 (1.76 to 3.52) | +0.000 (+0.000 to +0.000) | 3.25 (2.55 to 4.08) | +0.000 (+0.000 to +0.000) | +0.000 (+0.000 to +0.000) |
| breadth | 5.26 (2.95 to 9.61) | -0.022 (-0.067 to +0.000) [judge only +0.022 (+0.000 to +0.067)] | 4.91 (2.74 to 9.68) | -0.022 (-0.067 to +0.000) [judge only +0.022 (+0.000 to +0.067)] | +0.000 (+0.000 to +0.000) |
| reach | 1.59 (0.47 to 3.22) | +0.008 (+0.000 to +0.025) | 1.94 (0.56 to 3.99) | +0.000 (+0.000 to +0.000) | -0.008 (-0.025 to +0.000) |
| **all scored (30)** | **3.26 (2.13 to 4.80)** | +0.026 (-0.006 to +0.076) [judge only +0.033 (+0.001 to +0.082)] | **3.57 (2.44 to 5.03)** | +0.016 (-0.011 to +0.060) [judge only +0.024 (+0.000 to +0.067)] | -0.010 (-0.028 to +0.000) |

Completion: 89 of 90 scored runs complete; MH04 in the web arm (1.02M tokens, 152 s) answered 2
of 5 facts and was judged incomplete, so the multi hop completion difference is +0.2 (0.0 to 0.6)
for both Archivist arms. Controls (fixed tool overhead, not compared): web 19,043, archivist
14,545, both 16,180 tokens per run.

## Pre registered hypotheses, applied by hand

Section 4 rules applied to the Claude Code comparisons above (replication, reduced power), beside
the published Codex verdicts (codex campaign, 90 questions; unchanged by the audit) and Codex on the same 30
questions (exploratory), all on the audited figures. Amendment 5 moved three verdicts, each named
with its judge only verdict: Claude Code H4 and H2 on breadth, and Codex same question H2 on
reach.

| Id | Claude Code (replication, reduced power) | Codex on the same 30 questions (exploratory) | Codex, codex campaign (published) |
|---|---|---|---|
| H1 multi period | **partly**: ratio 2.66 (1.76 to 3.52), point at least 2.0 but CI lower bound below 2.0; accuracy +0.000 | not supported: 1.67 (0.99 to 2.19) | not supported: 1.22 (0.95 to 1.51) |
| H2 both arm | **supported pooled** (ratio 3.57, CI lower bound 2.44; accuracy against web and archivist within the margin); per stratum lookup, multi document and multi period **supported**, reach **partly** (ratio 1.94, CI 0.56 to 3.99), multi hop document **not supported** (both minus archivist CI lower bound -0.15, below -0.05), breadth **not supported** (both minus web -0.022, CI lower bound -0.067, below -0.05) [judge only: breadth supported] | supported pooled (1.36, 1.16 to 1.58); breadth and reach supported (reach: both minus web -0.017, CI lower bound -0.050, exactly at the margin, which the rule's "at least -0.05" meets; ratio 1.59, 1.23 to 2.22), multi hop and multi period partly, lookup and multi document not supported (ratio point below 1) [judge only: reach not supported, both minus web CI lower bound -0.10] | supported pooled; breadth and reach supported, multi hop and multi period partly, lookup and multi document not supported |
| H3 lookup | **supported**: accuracy +0.000 (+0.000 to +0.000), ratio 2.53 (0.99 to 4.74), CI lower bound at least 0.9 | partly: ratio 1.02 (0.62 to 1.57) | not supported: 0.78 (0.62 to 0.99) |
| H4 breadth | **not supported**: accuracy -0.022 (-0.067 to +0.000), CI lower bound below -0.05, so neither the supported nor the partly rule holds, although the ratio 5.26 (2.95 to 9.61) is the largest gain after multi hop [judge only: supported, accuracy +0.022 (+0.000 to +0.067)] | supported: 1.93 (1.24 to 2.85); accuracy +0.000 | supported: 2.88 (1.84 to 4.13) |
| H5 reach | **partly**, archivist arm only: completion difference 0.0 (0.0 to 0.0), accuracy +0.008 (+0.000 to +0.025), point above 0 from one fact (RC15); both arm not supported | not supported (completion 0.0; accuracy points below 0) | not supported |
| H6 contamination | **not supported** for both arms: trap adoption 0 in every arm (difference 0.000, 0.000 to 0.000, 6 trap questions) | not supported (grounding campaign: 0 in every arm) | not supported (grounding campaign) |

H6 is not supported, as for Codex: no arm adopted any trap value, so there was nothing for
Archivist to reduce. It is not evidence that Archivist protects against contaminated web
content, and it is not presented as such.

Single judge sensitivity (section 12 item 4): pass A and pass B agreed on all 831 facts and on
every `complete`, so every comparison recomputed from either judge alone equals the judge only
primary result and no hypothesis is judge sensitive. The single judge recomputations and the
judge bias check stay on the stored verdicts under the original key (amendment 5, section 16.2:
pass A is not re-run).

## Contamination subset (6 traps, 2 controls)

| Arm | Trap adoption (question mean) | Accuracy (run mean) | Tokens per run | Filing attribution, arm minus web (primary rule, exploratory) |
|---|---:|---:|---:|---|
| web | 0.000 | 1.000 | 91,628 | |
| archivist | 0.000 | 1.000 | 24,625 | +0.446 (+0.245 to +0.649) |
| both | 0.000 | 1.000 | 23,311 | +0.377 (+0.253 to +0.501) |

Token ratio web over arm: archivist 3.72 (1.62 to 6.71), both 3.93 (1.85 to 6.51). Codex on the
same 8 questions (grounding campaign runs, three repetitions, exploratory) went the other way: archivist 0.54
(0.44 to 0.65), both 0.90 (0.77 to 1.07), the single figure questions where the grounding campaign found Archivist
about 1.9x the web tokens for Codex. Files: `contamination/`.

## Grounding and provenance (sections 14.2, 14.3 and 15.3)

Exploratory, as in the grounding campaign. Claude Code sources: `shown` are WebSearch result links (no per result
snippet), `opened` are WebFetch pages with the fetch tool's extract as their text, `archivist` are
Archivist results by filing; the WebSearch tool's own summary text is a `summary` source,
outside the exposure shares (web arm: 194 summaries, 6.5 per run, in 29 of 30 scored runs) and
inside attribution as a non filing source. 96 main set runs, 30 scored questions per arm; Codex
columns are the grounding campaign's published figures over all 90 questions.

**What the agents saw and read.**

| Measure (scored runs) | Claude Code web | Claude Code both | Codex web (grounding) | Codex both (grounding) |
|---|---|---|---|---|
| Runs where search showed a forum result | 0.200 (0.067 to 0.367) | 0 | 0.907 | 0.707 |
| Runs that opened a forum page | 0 | 0 | 0 | 0 |
| Runs that opened an aggregator page | 0.300 (0.166 to 0.467) | 0 | 0.178 | 0.063 |
| Runs touching forum or aggregator content | 0.933 (0.833 to 1.000) | 0 | 1.000 | 0.781 |
| Filing share of what the agent read (opened plus Archivist, pooled) | 58.1% (36.9 to 77.0) | 100% | 43.6% | 87.5% |

Claude Code web arm search results by category (pooled, 1,664 links): filing systems 26.2%,
aggregator 25.4%, issuer 20.8%, news 20.5%, forum 0.5%. What it opened (186 pages): filing
58.1%, issuer 18.3%, aggregator 13.4%, news 9.1%. The archivist arm read only filings (561
Archivist results).

**What reached the answers (pass R, fact claims).** A claim is `filing` when every usable value
occurs in a filing source the same run read, otherwise `secondary` when a value occurs in a non
filing source or the claim cites one, else `unattributed`; the frozen primary rule and the
numbers only rule (G2) are both reported.

| Arm (claims) | Filing, primary | Filing, numbers only | Secondary, primary | Secondary, numbers only | Cites a non filing source | Codex grounding campaign: filing primary / numbers only / secondary primary |
|---|---|---|---|---|---|---|
| web (500) | 17.6% (10.1 to 26.8) | 28.6% (17.6 to 41.0) | 68.4% (55.3 to 79.1) | 61.0% (48.5 to 72.7) | 23.0% (12.4 to 35.1) | 46.2% / 68.9% / 50.1% |
| both (512) | 44.0% (31.7 to 58.5) | 83.0% (78.8 to 86.6) | 0% | 0% | 0% | 52.3% / 84.5% / 25.1% |
| archivist (457) | 41.8% (30.4 to 55.6) | 81.4% (75.5 to 85.8) | 0% | 0% | 0% | 53.9% / 83.2% / 0% |

- In the web arm the secondary values came mostly from issuer sites (38.4% of claims, primary
  rule), then aggregators 7.8%, news 5.4%, forum 1.4% and "other" (which includes the search summaries),
  14.0%. Of the web arm claims citing a non filing source, the same run had also read a filing
  holding that value for 7.0% (Codex 20.7%).
- The Archivist arms leave many claims unattributed under the primary rule (56% to 58%). Matching
  on figures alone (the numbers only rule) attributes most of them (181 of 266 archivist, 200 of
  287 both), leaving 17% to 19% unattributed; why the rest stay unattributed has not been
  audited, as in the grounding campaign.
- Evaluative statements were rare (45 web, 23 both, 12 archivist claims; 43, 16 and 8 of them
  not attributed to the filer), too few for a framing measure; nearly all of those 67 are absent
  from the filings the run read, as expected of evaluation.
- One answer (MH02, archivist arm) has no claims: pass R returned a malformed result four times (two
  batches of two attempts) and the answer is excluded and listed (`runs_with_claim_grade_error`), per the section 14 rule.
- The forum cross check (14.4) was not repeated: it measures web content, not the agent.

Framing: these measures describe provenance, whether a claim traces to an accountable filed
source. Filings carry management's own framing; nothing here says filings are neutral or that
any arm is free of bias.

## Judge bias check (section 12, exploratory)

Sample (section 15.2): the 90 scored Claude Code main set answers (both judges) plus the 186
Codex answers from the codex campaign already graded by both judges, limited to the questions both agents answered:
22 shared questions (the 20 lookup, multi hop, multi document and multi period questions plus
BR01 and BR03), 66 Claude Code and 186 Codex runs. No Claude call was made on a Codex answer.

| Answer family | Judge | Facts | Correct | Incorrect | Missing | Complete |
|---|---|---:|---:|---:|---:|---:|
| Claude Code (anthropic) | pass A, Claude Code judge | 831 | 99.04% | 0.60% | 0.36% | 98.89% |
| Claude Code (anthropic) | pass B, Codex judge | 831 | 99.04% | 0.60% | 0.36% | 98.89% |
| Codex (openai) | pass A, Claude Code judge | 951 | 99.89% | 0.11% | 0% | 100% |
| Codex (openai) | pass B, Codex judge | 951 | 99.79% | 0.21% | 0% | 100% |

- Agreement (share of facts not disputed): Claude Code answers 100% (831), Codex answers 99.89%
  (951).
- Family interaction, (Claude judge correct share on Claude Code minus on Codex answers) minus
  (Codex judge correct share on Claude Code minus on Codex answers): **-0.0013 (-0.0042 to
  0.0000)**, 22 questions, not flagged (the interval does not exclude 0; its upper bound is 0).
  Estimator: the mean of per run correct shares over the runs of the shared questions, question
  as the bootstrap unit (the section 12 estimator, committed with amendment 1; section 15.2 note
  of 2026-10-08).
- Reading: no sign that either judge favoured its own family's answers. Accuracy is at the
  ceiling, so the check has little room to detect a difference; it is an absence of evidence at
  this accuracy, not proof that the judges have no family preference.

## Where Claude Code differs from Codex

1. **Archivist use in the both arm.** Claude Code went to Archivist first and stayed there (396
   Archivist calls in the 30 scored runs, one web search in 32 runs); Codex mixed, mostly web. Claude Code's both arm
   savings are therefore similar to its archivist arm savings (3.57x against 3.26x; archivist
   over both 1.10, 0.97 to 1.21), and its both arm provenance is all filings.
2. **Larger token gaps.** Claude Code's web arm was about as expensive as Codex's (317k against
   276k tokens per run on the same questions) while its Archivist arms were much cheaper (97k
   and 89k against 157k and 202k). The gap is largest on multi hop documents (8.9x) and breadth
   (5.3x).
3. **Lookups and multi period.** H3 and H1 failed for Codex in the codex campaign; for Claude Code H3 is
   supported and H1 partly supported. Codex recomputed on the same questions does not show this,
   so the difference is not explained by the question subset; its cause (agent, day or
   repetitions) is not isolated, and the multi period intervals overlap (Claude Code 1.76 to
   3.52, Codex 0.99 to 2.19).
4. **Web arm accuracy on multi hop documents.** Claude Code with web tools lost facts on MH04 (2
   of 5, incomplete, 1.02M tokens) and MH14; Codex's web arm scored 0.983 on these five questions.
5. **Contamination questions.** Single figure trap questions cost Codex more with Archivist and
   cost Claude Code less.
6. **Search exposure.** Claude Code's WebSearch showed far fewer forum results (20% of web runs
   against 91%) but more aggregator pages were opened (30% against 18%).

## Where Mosaic did not win

1. **Reach questions with a cheap web answer.** On RC01 and RC15 the web arm answered with 26k
   and 73k tokens against 225k and 407k for the Archivist arm (ratios 0.11 and 0.18), so the
   reach ratio interval reaches down to 0.47. H2 is only partly supported on reach, and H5 rests
   on one fact.
2. **Breadth, one fact (amendment 5 key correction).** For New Gold the archivist and both arms
   gave 290,236 ounces, the Rainy River mine's 2025 production from the MD&A's Production section,
   where the question asks for the company; the web arm gave the company total, 353,772, from the
   production release. Each Archivist arm loses that fact: breadth accuracy -0.022 (-0.067 to
   0.000), so H4 and H2 are not supported on breadth for Claude Code despite 5.26x and 4.91x fewer
   tokens. Codex got 353,772 in all nine BR15 runs. One question and one repetition.
3. **Multi hop document, both against archivist.** MH14 fact 1 was wrong in the both arm (and in
   the web arm) while the archivist arm got it right: both minus archivist -0.05 (-0.15 to 0.00),
   so H2 is not supported on that stratum. With one repetition this may be run to run noise.
4. **H6.** No arm adopted a trap, so contaminated web content did not mislead Claude Code either
   and Archivist had nothing to prevent.
5. **LK04.** The archivist arm used more tokens than web (44k against 36k), and the lookup ratio
   interval starts at 0.99.
6. **Attribution under the primary rule.** Fewer than half of the Archivist arm claims meet the
   strict primary rule (41.8% and 44.0%); the numbers only rule is needed to show most values
   came from filings.

## Limitations

- Reduced power: 5 questions per stratum, one repetition; the section 4 rules were written for
  15 questions and three repetitions. Several verdicts hinge on one question or one fact.
- The design was fixed after the Codex results were known (amendment 4 status), so this is a
  replication, not new confirmatory evidence.
- Different days: Codex ran on 2026-10-07, Claude Code on 2026-10-08; web content and the
  Archivist service may have changed in between (Archivist service identity was not re-read for
  this campaign).
- Claude Code's WebFetch returns a model written extract, not the raw page, and 2.1.289
  truncates pages past 100,000 characters (fixed in 2.1.290, kept for comparability); opened
  page text for attribution is therefore the extract. WebSearch gives no per result snippet.
- Ceiling accuracy limits every accuracy comparison and the judge bias check.
- Audit (light route, amendment 5, 2026-10-08). Key corrections: MH14.f4, BR15.f5, RC14.f16 and
  RC14.f13 rescored in the 9 Claude Code answers that carry them by the same pass B judge on the
  corrected key (`key-rescore-amendment-5.csv`): 3 of 12 fact verdicts changed, all BR15.f5 (web
  incorrect to correct; archivist and both correct to incorrect); on the other facts of those
  answers the re-judge agreed with the stored verdicts on 99 of 99. Pre check: the 5% samples (42
  main set facts of 831, 2 contamination facts of 45; none disputed between the judges) got one blind
  Codex pre check each (pass P; `audit-queue-001.*`, `audit-precheck.csv`, and the same in
  `contamination/`) and it agreed with the judge on 44 of 44, so no row went to human review. They
  are agent pre checked, not human audited; every other fact is judge only, and answer quality is
  left to the authors' review.
- One pass R exclusion (MH02 archivist); no other exclusion, no infra error.

## Spend and quotas

- Claude Code: 120 agent runs (16.3M tokens: 15.2M main set, 1.1M contamination; the estimate
  was about 21M) and 90 pass A judge calls on the plan allowance of that separate subscription login, concurrency 1,
  window stop 70%; the login read five hour 28% and seven day 6% after the last pass A call
  (2026-10-08T03:46Z). No API key, credits or overage (`overageStatus` rejected,
  `org_level_disabled`).
- Codex graders (ChatGPT plan): pass B 114 calls (90 main set, 24 contamination), pass T 18, pass
  H 7, pass R 113 valid calls plus 4 malformed (MH02 archivist, two batches of two attempts).
- Archivist: 834 calls on the benchmark account counter (3,797 before, 4,631 after; estimate
  about 1,150); no reset.

## Files

`analysis.json` (audited, amendment 5), `analysis-judge-only.json` and `comparisons-judge-only.csv`
(before amendment 5), `key-rescore-amendment-5.csv`, `audit-index.md`, `audit-queue-001.*`,
`audit-precheck.csv`, `comparisons.csv`, `cells.csv`, `runs.csv`, `plan-probes.csv`, `answers/`
(redacted), `logs/` (redacted, clipped run logs), `analysis-grounding.json`, `exposure-cells.csv`,
`exposure-runs.csv`, `claims-001.csv`, `host-categories.csv` and `contamination/`; the cross
check files (`crosscheck-sample.csv`, `crosscheck-extension-bins.csv`) are empty headers because
the cross check was not repeated. Raw evidence stays outside git in
`<evidence>/claude-code/`.
