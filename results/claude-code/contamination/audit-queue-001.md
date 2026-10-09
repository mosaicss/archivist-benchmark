# Human audit queue, shard 1 of 1

0 disputed facts (the two judges disagreed) and 2 sampled facts (5% of all other judged facts, seed 8105).
Scoring rule: pass B (Codex) scores every fact (pre registration amendment 2); a fact is disputed when its existing pass A (Claude Code) verdict differs.
Check each fact against its Mosaic permalink and the answer, then fill `audit_verdict`
(`correct`, `incorrect` or `missing`) and `audit_note` in the matching CSV shard; apply
with `analyze --audit <results folder>` (every `audit-queue-*.csv`). Blank rows keep the
judge score. Answer quality acceptance stays with the authors.

## 20261008T032601608723Z-claude_code.archivist.CN04.r1-a1

claude_code, arm archivist, contamination, CN04: What diluted earnings per share did CN (Canadian National Railway, TSX: CNR) report for the second quarter of 2022 on a reported basis, not adjusted, and by how much was it up year over year?

Answer (link redacted): [answers/20261008T032601608723Z-claude_code.archivist.CN04.r1-a1.md](answers/20261008T032601608723Z-claude_code.archivist.CN04.r1-a1.md)

| fact | selection | statement | expected | kind | source | quote | judge A | judge B | audit verdict | audit note |
|---|---|---|---|---|---|---|---|---|---|---|
| CN04.f2 | sample | CN reported diluted EPS for Q2 2022 was up 32% year over year | 32% percent | sourced | [permalink](https://mosaic-finance.com/filings/5ac4f76c-ee77-48e1-add3-81eea46bf395/p/e427680b-8c62-4e10-aca3-f4747ab5ef0a/k1.wTF6vS8RJhuYSrWAWqV1CqaQHIm78ANPs-HjdlwI_YY/) SEDAR+ filing: Mosaic stores no exchange document id for SEDAR+ filings | the Company reported diluted EPS up 32% to C$1.92 |  | correct |  |  |

## 20261008T032730940615Z-claude_code.both.CN05.r1-a1

claude_code, arm both, contamination, CN05: What total compensation did Home Depot report for its chief executive Ted Decker for fiscal 2024, and for fiscal 2025?

Answer (link redacted): [answers/20261008T032730940615Z-claude_code.both.CN05.r1-a1.md](answers/20261008T032730940615Z-claude_code.both.CN05.r1-a1.md)

| fact | selection | statement | expected | kind | source | quote | judge A | judge B | audit verdict | audit note |
|---|---|---|---|---|---|---|---|---|---|---|
| CN05.f1 | sample | Home Depot CEO Edward P. Decker total compensation for fiscal 2024 was USD 15,574,678 | 15,574,678 USD (also: 15.6 million; 15.57 million) | sourced | [permalink](https://mosaic-finance.com/filings/f16aff57-fd7e-4652-ab44-390d8fdd0d29/p/23139d32-9f23-40d3-bab0-32a182b2944f/k1.tidJrUe-uh1xROdT6aLJGcvzTNJ6X64kdc_hL628cIU/) 0000354950-26-000090 | 20241,426,923 — 9,043,035 2,199,952 2,743,532 — 161,237 15,574,678 |  | correct |  |  |
