---
title: 'Grounding, source provenance and the forum cross check (Codex)'
type: 'results'
campaign: grounding
date: '2026-10-07'
preregistration: '../../preregistration/preregistration.md (section 14, amendment 3; deviations G0 to G4, addendum G1; section 16, amendment 5, for the CN12.f1 key correction)'
harness: 'archivist_bench 1.4.0 (1.6.0 for the amendment 5 contamination rescoring)'
status: 'cross check sample audited 2026-10-08 by an agent on behalf of the authors; contamination key CN12.f1 corrected and rescored, and the contamination audit sample agent pre checked, under amendment 5 (2026-10-08); answer quality accepted by the authors 2026-10-08'
---

# Grounding, source provenance and the forum cross check

Scope of every number below: Codex CLI 0.160.0, `gpt-6.1-sol`, effort `medium`, ChatGPT plan;
archivist-cli 0.2.33; the codex campaign's question set (`results/codex/`; 90 scored questions,
3 repetitions, 270 runs per arm) unless the contamination stratum is named. Every measure over
the codex campaign runs is **exploratory** (deviation G0: raw Reddit counts were seen before the rubric was frozen). Only H6
and the contamination stratum were pre registered. The framing is provenance: a claim either
traces to an accountable filed source or it does not. Filings carry management's own framing;
nothing here says they are neutral.

Intervals are 95% percentile bootstrap intervals over questions (seed 8105, 10,000 resamples)
unless marked Wilson. Machine readable tables: `exposure-cells.csv` (exposure and reliance cells,
both attribution rules), `exposure-runs.csv`, `claims-NNN.csv`, `host-categories.csv`,
`crosscheck-sample.csv`, `crosscheck-extension-bins.csv`, `analysis-grounding.json`,
`contamination/`.

## 1. What the agents read (exposure)

Sources are counted from each run's Codex rollout: `shown` are search results the search tool
returned; `opened` are pages the agent opened, searched in or clicked; `archivist` are Archivist
results (filing passages). Hosts were categorized by the frozen seed table plus pass H (1,978 hosts,
all classified; `host-categories.csv`).

**Runs that touched forum or aggregator content (share of 270 scored runs per arm):**

| Arm | Forum shown by search | Forum opened by the agent | Aggregator shown only | Aggregator opened | Forum or aggregator touched |
|---|---|---|---|---|---|
| web | 0.907 (0.848 to 0.959) | 0.000 | 0.756 (0.681 to 0.826) | 0.178 (0.111 to 0.252) | 1.000 |
| both | 0.707 (0.633 to 0.778) | 0.000 | 0.626 (0.541 to 0.707) | 0.063 (0.026 to 0.107) | 0.781 (0.715 to 0.844) |
| archivist | 0 | 0 | 0 | 0 | 0 |

- Every forum exposure came from the search tool. In 828 runs no agent opened a forum page.
  Reddit results specifically reached 239 of 276 web arm runs and 186 of 276 both arm runs
  (controls included), the counts recorded in G0.
- Share of distinct search results by category (pooled): web arm forum 16.2% (14.6 to 18.1),
  aggregator 20.1% (18.4 to 21.9), filing systems 29.8% (27.4 to 32.2), issuer sites 23.3%; both
  arm forum 19.0% (16.6 to 21.5), aggregator 15.8% (13.6 to 18.2), filing 28.0%, issuer 25.0%.
- Share of what the agent chose to read (opened pages plus Archivist results, pooled): web arm
  filing 43.6% (36.1 to 52.5), issuer 22.2%, aggregator 5.5%, forum 0%, other 28.5%; both arm
  filing 87.5% (80.9 to 92.2), issuer 4.7%, aggregator 0.9%, forum 0%, other 6.8%; archivist arm
  filing 100%.
- "Other" among opened sources is almost entirely failed page loads (the web tool's
  "Internal Error" results with no URL: 827 web, 202 both); the frozen rule counts them there.

## 2. What reached the answers (reliance)

Pass R listed 7,906 fact claims in the 810 scored answers (web 2,624, both 2,577, archivist
2,705). A claim is `filing` when every usable value occurs in a filing source the same run read,
otherwise `secondary` when any usable value occurs in a non filing source or the claim cites one,
else `unattributed`. Two rules are reported (G2): the frozen primary rule requires every figure and
named entity; the numbers only rule matches a claim with figures on its figures alone (document
names such as "fiscal 2025 Form 10-K" otherwise had to occur verbatim).

| Arm | Filing (primary) | Filing (numbers only) | Secondary (primary) | Secondary (numbers only) | Unattributed (primary) | Unattributed (numbers only) |
|---|---|---|---|---|---|---|
| web | 46.2% (38.4 to 54.3) | 68.9% (62.6 to 75.0) | 50.1% (42.3 to 57.6) | 28.2% (22.2 to 34.3) | 3.7% | 2.9% |
| both | 52.3% (43.6 to 61.3) | 84.5% (79.8 to 88.4) | 25.1% (19.6 to 31.5) | 10.7% (7.3 to 15.0) | 22.5% | 4.8% |
| archivist | 53.9% (45.5 to 62.3) | 83.2% (77.6 to 87.9) | 0% | 0% | 46.1% | 16.8% |

- Secondary claims are mostly issuer sites (web 42.6% of claims primary, 21.4% numbers only;
  both 22.6% and 9.6%). Forum and aggregator values reach very few claims: web forum 2.4% and
  aggregator 3.9% (primary), both 1.4% and 0.8%.
- Claims that cite a non filing source: web 28.5% (22.1 to 35.0), both 8.6% (5.2 to 13.1). The
  same run also read a filing holding that value for 20.7% of them in the web arm (35.5% numbers
  only) and 28.1% in the both arm (35.7% numbers only).
- Both arm cross checking: of claims whose usable values appeared in non filing sources (495;
  789 numbers only), 15.4% (8.6 to 23.3) were later read in an Archivist result (30.7%, 20.5
  to 41.5, numbers only). In
  the codex campaign the both arm called Archivist in 122 of 270 scored runs.
- Evaluative statements are rare in these answers (2 web, 2 both and 8 archivist across 810
  answers; 4 of the archivist ones are attributed to the filer), too few to measure.
- Caveat on the matcher: these are matcher based shares that can hold false positives and false
  negatives. A value counts as read when any number in the read text rounds to it, and the web
  arm reads far more text per run than the archivist arm, so chance matches are more likely
  there; category precedence also lets a chance filing match take a claim out of `secondary`.
  Computed or restated values (sums, growth rates, names written differently) are possible
  causes of the archivist arm's `unattributed` share; no audit has categorized them.

## 3. Do the forum and aggregator claims hold up? (cross check)

Scope: content search ranked for these questions. 5,366 deduplicated claims were extracted from
a seeded sample of 1,500 of the 13,525 forum and aggregator texts the agents saw (G3); 400 were
sampled (seed 8105) and each was checked against the filings by a Codex run with Archivist only
(no web). Rates with Wilson 95% intervals:

| Source | Rows | Confirmed | Contradicted | Not checkable | Off target |
|---|---|---|---|---|---|
| All | 400 | 68.5% (63.8 to 72.9) | 3.5% (2.1 to 5.8) | 12.8% (9.8 to 16.4) | 15.3% (12.1 to 19.1) |
| Aggregator | 279 | 76.0% (70.6 to 80.6) | 3.2% (1.7 to 6.0) | 6.5% (4.1 to 10.0) | 14.3% (10.7 to 18.9) |
| Forum | 121 | 51.2% (42.4 to 60.0) | 4.1% (1.8 to 9.3) | 27.3% (20.1 to 35.8) | 17.4% (11.6 to 25.1) |

- Among checkable claims (confirmed or contradicted), 4.9% (2.9 to 8.0) were contradicted:
  wrong number 9, adjusted mixed with reported 2, stale period 2, wrong company 1.
- Forum claims were more often classified as not checkable (opinion, forecast, estimate or
  rumor): 27.3%, against 6.5% for aggregators. Among checkable forum claims, 62 of 67 were
  confirmed.
- Merit test for dated claims (286): a later filing confirmed 112 and refuted 11; 163 not
  settled by a later filing. Eight rows carry restatement flags: seven confirmed and one contradicted.
- Audit (2026-10-08, by an agent on behalf of the authors):
  every row of `crosscheck-audit.csv` checked against the filings (`audit_verdict` uses the
  judge vocabulary: `correct` means the checker's bin and subtype stand). All 10 seeded confirmed
  rows stand. Of the 14 contradicted rows, 10 stand, 2 are confirmed (X053: the aggregator
  truncates accession numbers in display; X144: Realty Income's 10-K lists Wynn Resorts at 1.9%
  of annualized base rent, as claimed) and 2 are off target (X130: a US dollar statement figure
  the corpus does not hold; X358: total procedure growth that no indexed filing states); X311
  stays contradicted with subtype wrong company. Audited, contradicted claims are 2.5% of the
  400 (10/400, Wilson 1.4 to 4.5) and 3.5% of checkable claims (10/286, 1.9 to 6.3). The two
  stale period rows (X319, X365: undated claims, correct for an older period) stay contradicted
  under the rubric. Only 10 of 274 confirmed rows were audited, so false confirmations elsewhere
  are unmeasured. The judge only figures above are unchanged; this audit qualifies them.
- The extension sample (G4, 300 reported figure claims, trap sourcing only, not in the rates
  above): 249 confirmed, 12 contradicted, 4 not checkable, 35 off target.

## 4. Contamination stratum (pre registered, H6)

17 questions phrased as users ask (12 traps built from contradicted rows, 5 controls from
confirmed rows; addendum G1), 3 arms x 3 repetitions, 153 runs, all completed. Accuracy by the
Codex judge (pass B), with CN12.f1 rescored by the same judge under the amendment 5 key
correction; trap adoption by pass T (unchanged).

| Arm | Trap adoption | Mean accuracy | Mean total tokens |
|---|---|---|---|
| web | 0.00 | 1.000 | 43,264 |
| both | 0.00 | 1.000 | 51,781 |
| archivist | 0.00 | 1.000 [judge only 0.980] | 83,271 |

- **H6 not supported.** No arm repeated any trap value in any of 117 trap judgements; the trap
  adoption difference (web minus arm) is 0.00 (0.00 to 0.00) for both archivist and both. A
  direct search of the 153 answers for the trap values agrees.
- Tokens: the web arm was cheaper here. Web over archivist 0.52 (0.45 to 0.59), so Archivist used
  about 1.9x the tokens; web over both 0.84 (0.75 to 0.93). Accuracy difference archivist minus
  web 0.000 (0.000 to 0.000) after the amendment 5 key correction of CN12.f1 [judge only -0.020
  (-0.059 to 0.000)]; both minus web 0.000. The one judge only miss was the archivist arm's CN12
  answer, which gave the blue and white collar split of the audited financial statement note
  while the key's statement followed the corporate governance table of the same filing, which
  reverses it; the scored value 10,624 was right in all nine answers (pre registration section
  16.1).
- Filing attribution, arm minus web (exploratory): archivist +7.4 points (-13.1 to +27.9),
  both -3.3 (-13.4 to +7.2); numbers only +10.9 (-8.5 to +30.1) and -1.0 (-12.7 to +10.5).
- No trap value was adopted. Some trap related values did appear in search results (for
  example Meta's March 2026 figure for a June 2026 question, and CN's adjusted EPS), often with
  their period or basis beside them; the reason for zero adoption was not isolated.

## 5. Where Mosaic did not win

1. The contamination stratum: no arm was misled, so Archivist showed no protective effect. The
   archivist arm cost about 1.9x the web arm's tokens on these single figure questions
   (consistent with the codex campaign's lookup result), at equal accuracy (1.000 in every arm after the
   amendment 5 key correction; judge only 0.980 against 1.000, difference -0.020, -0.059 to
   0.000); the both arm cost about 1.2x web. Neither showed a
   demonstrated filing attribution gain (both arm -3.3 points primary, -1.0 numbers only; every
   interval includes zero).
2. Forum exposure through search is common, but under the primary matcher only 2.4% of web arm
   fact claims were labelled secondary forum, and that label does not establish exclusive forum
   reliance (none of those 62 claims cites a forum). The provenance case rests on what is cited
   and read, not on agents repeating forum errors.
3. Across all 400 sampled claims 68.5% were confirmed and 3.5% contradicted; among checkable
   claims (confirmed or contradicted) 95.1% were confirmed (95.9% for aggregators) and 4.9%
   contradicted. After the 2026-10-08 audit of all 14 contradicted rows, 10 stand: 2.5% of
   the 400 and 3.5% of checkable claims.
4. Under the primary rule the archivist arm leaves 46% of fact claims unattributed (17% numbers
   only). Possible causes include computation and restatement; the causes have not been audited.

## 6. What does hold (provenance)

- With Archivist available, what the agent chose to read was mostly filings: 87.5% of opened
  pages plus Archivist results in the both arm and 100% in the archivist arm, against 43.6% in
  the web arm (opened web pages alone in the both arm: 48.6% filing).
- Claims citing a non filing source fell from 28.5% of claims in the web arm to 8.6% in the both
  arm and 0% in the archivist arm; most of those citations are issuer sites (651 of 749 web, 212
  of 221 both).
- Archivist results carry passage permalinks: 53.9% of archivist arm fact claims were labelled
  `filing` by the primary matcher, 83.2% under numbers only; web arm claims lean on
  issuer sites (42.6% primary) and, in small shares, aggregators and forums.

## 7. Deviations and limitations

- G0: raw Reddit counts seen before the freeze; all measures over the codex campaign runs exploratory.
- G2: numbers only attribution reported beside the primary rule.
- G3: pass X on a seeded sample of 1,500 texts; concurrency for grading passes (execution only;
  pass C cap raised from 2 to 4 after 142 checks, execution only).
- G4: a 300 claim extension sample used only to source traps.
- Single judge family: every grader (passes B, C, H, R, T, X) is Codex, the answering model's
  family. The Claude Code replication is the claude-code campaign (`results/claude-code/`).
- Host level categories: mixed sites (news with forum sections, contributor platforms) take one
  category.
- Sample sizes: 121 forum rows; per stratum cells are small and their intervals wide.
- Human review: answer quality for the grounding campaign was accepted by the authors on 2026-10-08.
  The codex campaign's audit queue took the light route
  (amendment 5, 2026-10-08; see `results/codex/results.md`), and the cross check audit was done
  by an agent on behalf of the authors (2026-10-08, section 3). The contamination stratum's 5%
  audit sample (15 facts, none disputed;
  `contamination/audit-queue-001.*`) took the same route (amendment 5, 16.3 note): a blind Codex
  pre check agreed with the judge on 15 of 15 (`contamination/audit-precheck.csv`), so no row went
  to human review. Those facts are agent pre checked, not human audited; every other contamination
  fact is judge only, apart from the nine CN12.f1 verdicts rescored under the key correction.
