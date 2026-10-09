# Human audit queue, shard 1 of 1

0 disputed facts (the two judges disagreed) and 15 sampled facts (5% of all other judged facts, seed 8105).
Scoring rule: pass B (Codex) scores every fact (pre registration amendment 2); a fact is disputed when its existing pass A (Claude Code) verdict differs.
Check each fact against its Mosaic permalink and the answer, then fill `audit_verdict`
(`correct`, `incorrect` or `missing`) and `audit_note` in the matching CSV shard; apply
with `analyze --audit <results folder>` (every `audit-queue-*.csv`). Blank rows keep the
judge score. Answer quality acceptance stays with the authors.

## 20261007T223034961821Z-codex.both.CN04.r1-a1

codex, arm both, contamination, CN04: What diluted earnings per share did CN (Canadian National Railway, TSX: CNR) report for the second quarter of 2022 on a reported basis, not adjusted, and by how much was it up year over year?

Answer (link redacted): [answers/20261007T223034961821Z-codex.both.CN04.r1-a1.md](answers/20261007T223034961821Z-codex.both.CN04.r1-a1.md)

| fact | selection | statement | expected | kind | source | quote | judge A | judge B | audit verdict | audit note |
|---|---|---|---|---|---|---|---|---|---|---|
| CN04.f1 | sample | CN reported diluted EPS for Q2 2022 was CAD 1.92 (reported basis) | 1.92 CAD per share (also: C$1.92; $1.92) | sourced | [permalink](https://mosaic-finance.com/filings/5ac4f76c-ee77-48e1-add3-81eea46bf395/p/e427680b-8c62-4e10-aca3-f4747ab5ef0a/k1.wTF6vS8RJhuYSrWAWqV1CqaQHIm78ANPs-HjdlwI_YY/) SEDAR+ filing: Mosaic stores no exchange document id for SEDAR+ filings | the Company reported diluted EPS up 32% to C$1.92 |  | correct |  |  |

## 20261007T223418767562Z-codex.both.CN12.r1-a1

codex, arm both, contamination, CN12: What was Coca Cola İçecek's (BIST: CCOLA) average number of employees in 2025?

Answer (link redacted): [answers/20261007T223418767562Z-codex.both.CN12.r1-a1.md](answers/20261007T223418767562Z-codex.both.CN12.r1-a1.md)

| fact | selection | statement | expected | kind | source | quote | judge A | judge B | audit verdict | audit note |
|---|---|---|---|---|---|---|---|---|---|---|
| CN12.f1 | sample | Coca Cola İçecek's average employee count for 2025 was 10,624 (5,748 blue collar, 4,876 white collar) | 10,624 employees (also: 10.624; 10624) | sourced | [permalink](https://mosaic-finance.com/filings/2c64505d-5bd5-4195-93e5-057a1b513974/p/80e4c6bf-2c97-44d7-9240-131f5a9520e5/k1.eOcVBW0KF4WjoaSMUP-6zizEtss7iG0Xwdi_NgCMAPU/) 1565282 | \|Ortalama Çalışan Sayısı\|10.624\|10.365\| |  | correct |  |  |

## 20261007T223700773942Z-codex.archivist.CN01.r2-a1

codex, arm archivist, contamination, CN01: What was GE Aerospace's total revenue on a reported (GAAP) basis for the third quarter of 2025, and by what percentage did it grow year over year?

Answer (link redacted): [answers/20261007T223700773942Z-codex.archivist.CN01.r2-a1.md](answers/20261007T223700773942Z-codex.archivist.CN01.r2-a1.md)

| fact | selection | statement | expected | kind | source | quote | judge A | judge B | audit verdict | audit note |
|---|---|---|---|---|---|---|---|---|---|---|
| CN01.f2 | sample | GE Aerospace total revenue (GAAP) for Q3 2025 grew 24% year over year | 24% percent | sourced | [permalink](https://mosaic-finance.com/filings/0ad0afd3-dd6a-4fc7-a19f-a758e3fe3174/p/00a434d2-b779-4729-90cb-f3e34d60a155/k1.OL30ERF3Y-oY_P45wDw7scfcyhncnbogj4Uv_xJPiDc/) 0000040545-25-000131 | Total revenue (GAAP) of $12.2B, +24% |  | correct |  |  |

## 20261007T223701191303Z-codex.both.CN01.r2-a1

codex, arm both, contamination, CN01: What was GE Aerospace's total revenue on a reported (GAAP) basis for the third quarter of 2025, and by what percentage did it grow year over year?

Answer (link redacted): [answers/20261007T223701191303Z-codex.both.CN01.r2-a1.md](answers/20261007T223701191303Z-codex.both.CN01.r2-a1.md)

| fact | selection | statement | expected | kind | source | quote | judge A | judge B | audit verdict | audit note |
|---|---|---|---|---|---|---|---|---|---|---|
| CN01.f2 | sample | GE Aerospace total revenue (GAAP) for Q3 2025 grew 24% year over year | 24% percent | sourced | [permalink](https://mosaic-finance.com/filings/0ad0afd3-dd6a-4fc7-a19f-a758e3fe3174/p/00a434d2-b779-4729-90cb-f3e34d60a155/k1.OL30ERF3Y-oY_P45wDw7scfcyhncnbogj4Uv_xJPiDc/) 0000040545-25-000131 | Total revenue (GAAP) of $12.2B, +24% |  | correct |  |  |

## 20261007T223722559036Z-codex.both.CN02.r2-a1

codex, arm both, contamination, CN02: How much cash and cash equivalents did Tesla hold at December 31, 2023?

Answer (link redacted): [answers/20261007T223722559036Z-codex.both.CN02.r2-a1.md](answers/20261007T223722559036Z-codex.both.CN02.r2-a1.md)

| fact | selection | statement | expected | kind | source | quote | judge A | judge B | audit verdict | audit note |
|---|---|---|---|---|---|---|---|---|---|---|
| CN02.f1 | sample | Tesla cash and cash equivalents at 2023-12-31 were USD 16,398 million | 16,398 USD millions (also: 16.4 billion; 16,398 million) | sourced | [permalink](https://mosaic-finance.com/filings/37287bf3-5667-44f2-bdcc-1b940830768b/p/8143410e-14c0-4a98-9975-12f70ab79e69/k1.oa03BojVL4XNMZ_S8kDnVEvj-nPatLUd1AAtM4q1HMk/) 0001628280-24-002390 | Cash and cash equivalents \| \| $ \| 16,398 |  | correct |  |  |

## 20261007T224032297379Z-codex.archivist.CN09.r2-a1

codex, arm archivist, contamination, CN09: What was Monster Beverage's operating income for the second quarter of 2024, and for the second quarter of 2025?

Answer (link redacted): [answers/20261007T224032297379Z-codex.archivist.CN09.r2-a1.md](answers/20261007T224032297379Z-codex.archivist.CN09.r2-a1.md)

| fact | selection | statement | expected | kind | source | quote | judge A | judge B | audit verdict | audit note |
|---|---|---|---|---|---|---|---|---|---|---|
| CN09.f1 | sample | Monster Beverage operating income for Q2 2024 was USD 527.2 million | 527.2 USD millions (also: $527.2 million) | sourced | [permalink](https://mosaic-finance.com/filings/bbd75155-76cb-4b93-9340-165c71298d34/p/3a3d44b9-b7a4-442e-9bd7-9a2d7438289b/k1.JSUdCudexAG9cRItcnhjGM-jasgiicWlOp-baIweBLQ/) 0001410578-25-001620 | Operating income was $631.6 million for the three-months ended June 30, 2025, an increase of approximately $104.5 million, or 19.8% higher than operating income of $527.2 million for the three-months ended June 30, 2024. |  | correct |  |  |

## 20261007T224055548380Z-codex.archivist.CN10.r2-a1

codex, arm archivist, contamination, CN10: How many stores did AutoZone operate in the U.S., in Mexico and in Brazil at the end of fiscal 2025 (August 30, 2025)?

Answer (link redacted): [answers/20261007T224055548380Z-codex.archivist.CN10.r2-a1.md](answers/20261007T224055548380Z-codex.archivist.CN10.r2-a1.md)

| fact | selection | statement | expected | kind | source | quote | judge A | judge B | audit verdict | audit note |
|---|---|---|---|---|---|---|---|---|---|---|
| CN10.f1 | sample | AutoZone operated 6,627 stores in the U.S. at 2025-08-30 | 6,627 stores | sourced | [permalink](https://mosaic-finance.com/filings/7c6eb938-639e-4e3a-aff3-be34c532c66f/p/9d859967-0826-4565-b71a-46050380342d/k1.SnOPOkwop0jSRZrtV4ksaCe4eyh2OCT-sH3nVegkV8g/) 0001104659-25-102611 | at August 30, 2025, operated 6,627 stores in the U.S., 883 stores in Mexico and 147 stores in Brazil |  | correct |  |  |

## 20261007T224135499535Z-codex.archivist.CN12.r2-a1

codex, arm archivist, contamination, CN12: What was Coca Cola İçecek's (BIST: CCOLA) average number of employees in 2025?

Answer (link redacted): [answers/20261007T224135499535Z-codex.archivist.CN12.r2-a1.md](answers/20261007T224135499535Z-codex.archivist.CN12.r2-a1.md)

| fact | selection | statement | expected | kind | source | quote | judge A | judge B | audit verdict | audit note |
|---|---|---|---|---|---|---|---|---|---|---|
| CN12.f1 | sample | Coca Cola İçecek's average employee count for 2025 was 10,624 (5,748 blue collar, 4,876 white collar) | 10,624 employees (also: 10.624; 10624) | sourced | [permalink](https://mosaic-finance.com/filings/2c64505d-5bd5-4195-93e5-057a1b513974/p/80e4c6bf-2c97-44d7-9240-131f5a9520e5/k1.eOcVBW0KF4WjoaSMUP-6zizEtss7iG0Xwdi_NgCMAPU/) 1565282 | \|Ortalama Çalışan Sayısı\|10.624\|10.365\| |  | correct |  |  |

## 20261007T224142768900Z-codex.both.CN12.r2-a1

codex, arm both, contamination, CN12: What was Coca Cola İçecek's (BIST: CCOLA) average number of employees in 2025?

Answer (link redacted): [answers/20261007T224142768900Z-codex.both.CN12.r2-a1.md](answers/20261007T224142768900Z-codex.both.CN12.r2-a1.md)

| fact | selection | statement | expected | kind | source | quote | judge A | judge B | audit verdict | audit note |
|---|---|---|---|---|---|---|---|---|---|---|
| CN12.f1 | sample | Coca Cola İçecek's average employee count for 2025 was 10,624 (5,748 blue collar, 4,876 white collar) | 10,624 employees (also: 10.624; 10624) | sourced | [permalink](https://mosaic-finance.com/filings/2c64505d-5bd5-4195-93e5-057a1b513974/p/80e4c6bf-2c97-44d7-9240-131f5a9520e5/k1.eOcVBW0KF4WjoaSMUP-6zizEtss7iG0Xwdi_NgCMAPU/) 1565282 | \|Ortalama Çalışan Sayısı\|10.624\|10.365\| |  | correct |  |  |

## 20261007T224244460873Z-codex.web.CN15.r2-a1

codex, arm web, contamination, CN15: What were Royal Bank of Canada's Tier 1 capital ratio and CET1 ratio at the end of its third quarter of fiscal 2025 (July 31, 2025)?

Answer (link redacted): [answers/20261007T224244460873Z-codex.web.CN15.r2-a1.md](answers/20261007T224244460873Z-codex.web.CN15.r2-a1.md)

| fact | selection | statement | expected | kind | source | quote | judge A | judge B | audit verdict | audit note |
|---|---|---|---|---|---|---|---|---|---|---|
| CN15.f1 | sample | RBC Tier 1 capital ratio at Q3 fiscal 2025 was 14.8% | 14.8% percent | sourced | [permalink](https://mosaic-finance.com/filings/b1a8f93d-1825-4056-968d-1a932389ab94/p/03cceab0-c862-48b1-8be3-226617a7adbf/k1.rYZR3riJhYBv05IAlZe2s5ISmFFqnNQj0xw0uQTnc8w/) SEDAR+ filing: Mosaic stores no exchange document id for SEDAR+ filings | Our Tier 1 capital ratio of 14.8% was up 10 bps |  | correct |  |  |

## 20261007T224425631277Z-codex.web.CN02.r3-a1

codex, arm web, contamination, CN02: How much cash and cash equivalents did Tesla hold at December 31, 2023?

Answer (link redacted): [answers/20261007T224425631277Z-codex.web.CN02.r3-a1.md](answers/20261007T224425631277Z-codex.web.CN02.r3-a1.md)

| fact | selection | statement | expected | kind | source | quote | judge A | judge B | audit verdict | audit note |
|---|---|---|---|---|---|---|---|---|---|---|
| CN02.f1 | sample | Tesla cash and cash equivalents at 2023-12-31 were USD 16,398 million | 16,398 USD millions (also: 16.4 billion; 16,398 million) | sourced | [permalink](https://mosaic-finance.com/filings/37287bf3-5667-44f2-bdcc-1b940830768b/p/8143410e-14c0-4a98-9975-12f70ab79e69/k1.oa03BojVL4XNMZ_S8kDnVEvj-nPatLUd1AAtM4q1HMk/) 0001628280-24-002390 | Cash and cash equivalents \| \| $ \| 16,398 |  | correct |  |  |

## 20261007T224600927191Z-codex.web.CN06.r3-a1

codex, arm web, contamination, CN06: How did Kohl's net sales and comparable sales change year over year in the second quarter of fiscal 2026, the quarter ended August 1, 2026?

Answer (link redacted): [answers/20261007T224600927191Z-codex.web.CN06.r3-a1.md](answers/20261007T224600927191Z-codex.web.CN06.r3-a1.md)

| fact | selection | statement | expected | kind | source | quote | judge A | judge B | audit verdict | audit note |
|---|---|---|---|---|---|---|---|---|---|---|
| CN06.f2 | sample | Kohl's net sales in Q2 fiscal 2026 were USD 3.3 billion | 3.3 USD billions (also: $3.3 billion; 3,318 million) | sourced | [permalink](https://mosaic-finance.com/filings/22b9b13c-33d1-4c88-8c37-f03b9339abfb/p/463fa97b-9eaf-46c7-ab3c-091faadb433c/k1.Z8xPz7nM9CJqkHR5YiNfYDTnQ5WYifdAILynUD5kSKg/) 0001193125-26-381893 | Net sales decreased 0.9%, to $3.3 billion, with comparable sales down 0.9%. |  | correct |  |  |

## 20261007T224739250786Z-codex.web.CN10.r3-a1

codex, arm web, contamination, CN10: How many stores did AutoZone operate in the U.S., in Mexico and in Brazil at the end of fiscal 2025 (August 30, 2025)?

Answer (link redacted): [answers/20261007T224739250786Z-codex.web.CN10.r3-a1.md](answers/20261007T224739250786Z-codex.web.CN10.r3-a1.md)

| fact | selection | statement | expected | kind | source | quote | judge A | judge B | audit verdict | audit note |
|---|---|---|---|---|---|---|---|---|---|---|
| CN10.f3 | sample | AutoZone operated 147 stores in Brazil at 2025-08-30 | 147 stores | sourced | [permalink](https://mosaic-finance.com/filings/7c6eb938-639e-4e3a-aff3-be34c532c66f/p/9d859967-0826-4565-b71a-46050380342d/k1.SnOPOkwop0jSRZrtV4ksaCe4eyh2OCT-sH3nVegkV8g/) 0001104659-25-102611 | at August 30, 2025, operated 6,627 stores in the U.S., 883 stores in Mexico and 147 stores in Brazil |  | correct |  |  |

## 20261007T224753339115Z-codex.both.CN10.r3-a1

codex, arm both, contamination, CN10: How many stores did AutoZone operate in the U.S., in Mexico and in Brazil at the end of fiscal 2025 (August 30, 2025)?

Answer (link redacted): [answers/20261007T224753339115Z-codex.both.CN10.r3-a1.md](answers/20261007T224753339115Z-codex.both.CN10.r3-a1.md)

| fact | selection | statement | expected | kind | source | quote | judge A | judge B | audit verdict | audit note |
|---|---|---|---|---|---|---|---|---|---|---|
| CN10.f3 | sample | AutoZone operated 147 stores in Brazil at 2025-08-30 | 147 stores | sourced | [permalink](https://mosaic-finance.com/filings/7c6eb938-639e-4e3a-aff3-be34c532c66f/p/9d859967-0826-4565-b71a-46050380342d/k1.SnOPOkwop0jSRZrtV4ksaCe4eyh2OCT-sH3nVegkV8g/) 0001104659-25-102611 | at August 30, 2025, operated 6,627 stores in the U.S., 883 stores in Mexico and 147 stores in Brazil |  | correct |  |  |

## 20261007T224802707494Z-codex.web.CN11.r3-a1

codex, arm web, contamination, CN11: As of December 31, 2025, how many company owned Chipotle restaurants were there in the United States, and how many internationally?

Answer (link redacted): [answers/20261007T224802707494Z-codex.web.CN11.r3-a1.md](answers/20261007T224802707494Z-codex.web.CN11.r3-a1.md)

| fact | selection | statement | expected | kind | source | quote | judge A | judge B | audit verdict | audit note |
|---|---|---|---|---|---|---|---|---|---|---|
| CN11.f1 | sample | Chipotle owned 3,938 restaurants in the U.S. at 2025-12-31 | 3,938 restaurants | sourced | [permalink](https://mosaic-finance.com/filings/5b1e9ba2-5a22-4871-9dca-b7c02dc983b2/p/eedeb557-524e-48c9-94da-29bb1198fdf6/k1.TiNu9g52Poo-Wg-2vHLqg17Oi2L7njVsEW3_FsBujpA/) 0001058090-26-000009 | As of December 31, 2025, we owned 3,938 Chipotle restaurants throughout the United States (“U.S.”) and 104 international Chipotle restaurants. |  | correct |  |  |
