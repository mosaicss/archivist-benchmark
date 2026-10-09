# Redactions: how this public edition differs from Mosaic's internal record

This repository copies the benchmark harness, its tests, the pre registration and the committed
results folders of Mosaic's private source repository as of 2026-10-08.

The public edition rewrites the documents to remove internal workflow references; no figure changed.

No number, verdict, count, run, row or results file was changed, added or removed. Gzip logs were
decompressed, edited and recompressed the way the harness writes them (gzip level 9, no
timestamp).

## Layout

| Internal path | Here |
|---|---|
| `infrastructure/benchmarks/archivist_bench/` | `archivist_bench/` |
| `infrastructure/benchmarks/README.md` | `docs/HARNESS.md` |
| `infrastructure/tests/test_archivist_bench.py`, `infrastructure/tests/fixtures/archivist_bench/` | `tests/` (same names) |
| `.../research/technical-archivist-benchmark-preregistration-2026-10-06/` | `preregistration/` (the cost estimate renamed `cost-estimate.md`) |
| `.../research/technical-archivist-benchmark-results-2026-10-07/` | `results/codex/` |
| `.../research/technical-archivist-grounding-results-2026-10-07/` | `results/grounding/` |
| `.../research/technical-archivist-claude-code-replication-2026-10-07/` | `results/claude-code/` |

New here: `README.md`, `RUNBOOK.md`, `LICENSE`, `LICENSE-DATA`, `NOTICE`, this file,
`pyproject.toml`, `.gitignore`, and a short "Public copy" note at the top of `docs/HARNESS.md`.

Not published: the raw evidence of every campaign (run directories, unredacted logs, judge
logs, ledgers, plan probes with account data), the 2026-10-05 exploratory pilot that the
`estimate` command reads, and Mosaic's internal records.

## String redactions in data and documents

| Class | What it was | Replaced with | Replacements | Files | Where |
|---|---|---|---:|---:|---|
| Local evidence paths | absolute and home relative paths of Mosaic's evidence store (run working directories, Codex homes, the store root in prose) | `<evidence>/...` in data and prose; `$EVIDENCE` in shell commands | 10,028 | 1,106 | Codex logs 8,291 in 829 files; contamination logs 1,530 in 153; Claude Code logs 184 in 120; `docs/HARNESS.md` 19; `preregistration/` 2; `results.md`, `replication.md` 1 each |
| Evidence placeholder names | the campaign directory names inside those placeholders (internal work item ids) | `<evidence>/codex/`, `<evidence>/grounding/`, `<evidence>/claude-code/` (also inside Claude Code's encoded project directory names) | 10,005 | 1,102 | Codex logs 8,291 in 829; grounding contamination logs 1,530 in 153; Claude Code logs 184 in 120 |
| Operator home paths | other paths under the operator's home directory (the operator's skill directory in each Codex session's skill roots) | `<home>/...` | 1,964 | 982 | Codex and contamination logs |
| Claude configuration directories | the operator's Claude configuration directories and paths under them (Claude Code saves large fetched pages there) | `<claude-config-dir>`; dropped in the pre registration | 69 | 16 | Claude Code logs 66 in 14 files; one Claude Code answer (MH04 web); `preregistration.md` 2 |
| Operator skill entry | one internal workflow skill installed for the operator that Codex listed in every session's skill list; no run invoked it | `<operator-skill>` with a redaction note | 1,964 | 982 | Codex and contamination logs (twice per session) |
| Internal database name | the internal name and connection role of Mosaic's filings database, in answer key source notes and the pre registration | "Mosaic filings database" | 70 | 6 | `questions.json` 29, `preregistration.md` 1, `audit-queue-001.csv` and `.md` of `results/codex` (34) and `results/claude-code` (6) |
| Cloud project id | the Google Cloud project id of the original (never used) Vertex judge | "Mosaic's Google Cloud project" | 2 | 1 | `preregistration.md` |
| Third party email | one contact email that the cross check extracted as a claim value from an aggregator page | `<email>` | 1 | 1 | `results/grounding/crosscheck-sample.csv` row X062 (bin `off_target`, unchanged) |
| Internal repository paths | the private repository's paths to the harness, its README and the research folders | this repository's paths | 40 | 5 | `docs/HARNESS.md` 29, `preregistration.md` 7, `grounding.md` 2, `results.md` 1, `replication.md` 1 |
| Session and thread ids | the Codex thread id of each session (`session_meta.payload.id`, every `thread_id`, the rollout file name in `logs/manifest.csv`), each session's context `window_id`, and Claude Code session ids inside saved tool result paths: 1,978 distinct ids | `<redacted>`, as the harness already masked `session_id` | 25,347 | 998 | Codex logs and manifest 23,483; contamination logs and manifest 1,800; Claude Code logs 64 |
| Local socket path | Claude Code's `messaging_socket_path` in each stream's init event | `<socket-path>` | 120 | 120 | Claude Code logs |
| Upstream filing paths | the path part of upstream filing addresses kept behind the masked host (`filing-source/...`, `filing-source.ca/...`, `filing-source.com/...`) in URLs, search queries and query text, plus host-less filing archive paths in snippets and fetched text | `filing-source/<path>` (host marker kept) or `<path>` | 2,334 | 193 | Codex logs 2,320; Claude Code logs 14 |
| Drive letter paths | Windows paths of filing agents' typesetting jobs shown as titles of upstream documents in search results | `<drive-path>` | 37 | 23 | Codex logs |
| Encrypted reasoning | Codex's opaque `encrypted_content` reasoning blobs (see below) | `<redacted: encrypted reasoning>` | 1,191 | 455 | Codex and contamination logs (count of placeholders in the published logs) |
| Revision and commit ids | the Archivist API's deployed revision name and private commit in `results.md`; a private working tree commit in `smoke-results.md`; a private commit in a test comment | "the production Archivist API revision, recorded in the internal run ledger"; "working tree of Mosaic's private repository"; dropped | 3 | 3 | `results/codex/results.md`, `preregistration/smoke-results.md`, `tests/test_archivist_bench.py` |
| Relative links | `../preregistration/preregistration.md` in the results documents' front matter, and the unpublished pilot's path | `../../preregistration/preregistration.md`; "not published" | 5 | 4 | `results.md`, `grounding.md`, `replication.md`, `preregistration.md`, `cost-estimate.md` |

The harness's own log scrubber (`report`) does not remove the classes from "Session and thread
ids" to "Drive letter paths" or the encrypted reasoning; [`RUNBOOK.md`](RUNBOOK.md), section 10,
says what to do when publishing reproduced logs.

Already masked by the harness before commit and left as they were: account, user and session
ids (`<redacted>`), credentials, emails in logs, upstream filing links (`<url:filing-source>`)
and other links (`<url:host>`).

Identifiers kept in the logs: per turn and per item ids that Codex and Claude Code generate for
each event (Codex `turn_id`, `root_turn_id`, `first_turn_id`, item, message and response ids;
Claude Code event `uuid` and message ids) and Mosaic filing, passage and chunk ids (`filing_id`,
`chunk_id`, `passage_id`, used in Mosaic permalinks). They identify single events or public
Mosaic documents, not a session, thread, account or user.

No analysis input in this repository reads the committed logs, answers or manifests (`analyze`,
`report` and the grounding commands read a campaign's raw evidence; the harness only writes and
checks these files), so none of the log edits changes a number.

## Data files: human readable notes edited

Only notes fields with an internal reference were reworded; no value that the harness computes
or scores changed.

| File | Field or column | Rows | Change |
|---|---|---:|---|
| `results/codex/audit-queue-001.csv` | `audit_note` | 4 | the human audit rulings on MH14.f4, BR15.f5, RC14.f16, RC14.f13 now read "Authors' ruling (2026-10-08), after checking the filing: ..." instead of quoting the internal decision; the explanation after it is unchanged; `audit_verdict` unchanged |
| `results/codex/audit-queue-001.md` | `audit_note` column of the Markdown table | 4 | same text as the CSV |
| `results/codex/analysis.json` | `audit_corrections[].audit_note` | 4 | same text as the CSV (scores, verdicts and every other field unchanged) |
| `results/codex/audit-queue-001.md`, `results/claude-code/audit-queue-001.md`, `results/claude-code/contamination/audit-queue-001.md`, `results/grounding/contamination/audit-queue-001.md` | the generated header sentence | 1 each | "Answer quality acceptance stays with the authors." (the harness now writes this wording) |
| `preregistration/key-corrections-amendment-5.json` | `corrections[].owner_ruling` | 5 | neutral statements of each ruling ("Ruled correct by the authors after checking the filing", ...); no corrected field changed |
| `preregistration/questions.json` | top level metadata key | 1 | the key holding an internal work item id became `"campaign": "codex"` (not read by the harness) |
| `preregistration/contamination-questions.json` | top level metadata key | 1 | the key holding an internal work item id became `"campaign": "grounding"` (not read by the harness) |
| `results/grounding/analysis-grounding.json` | `label`, `scope` | 1 each | "exploratory: every measure over the codex campaign runs" and "Codex campaign runs (completed, uncontaminated); ..." (the harness now writes this wording) |

## Documents rewritten

`preregistration/preregistration.md`, `smoke-results.md`, `cost-estimate.md`, the three results
documents, `docs/HARNESS.md`, `README.md` and `RUNBOOK.md` refer to campaigns by folder (codex,
grounding, claude-code, and the smoke runs) and to decisions as the authors' dated decisions.
Every number, date, hypothesis, rubric, verdict, scope statement and the substance of each dated
amendment and deviation is unchanged; verbatim quotations of internal conversations were replaced
by neutral statements of what was decided. Two internal cross references without public content
were dropped from the pre registration (a private change request number and a menu choice
label).

In `docs/HARNESS.md` the paths were rewritten for this layout, the amendment 3, 4 and 5 command blocks moved
to `RUNBOOK.md` (sections 6 to 8; three README tests now check those sequences there), and these corrections were made in the public edition only:

- Quickstart: the three run example gains `--repetitions 1` (the default plan is 3 repetitions
  x 3 arms = 9 runs, which `--max-runs 3` refuses).
- The two judge examples that run pass A gain `--claude-config-dir <Claude account dir>`; an
  executed judge with pass A requires it.
- "Campaign procedure": `--max-runs` must cover every planned run of the selection (it refuses a
  larger plan, it does not truncate).
- Amendment 5: the grounding contamination 5% sample (15 rows) is exported and pre checked like
  the other queues (pre registration 16.3, note of 2026-10-08, and
  `results/grounding/contamination/audit-precheck.csv`); `A9` passes the shard with the rescore
  CSV, as the committed analysis did.
- Reproduction output folders point at `repro/<campaign>/`, never at the published `results/`.

## Code and test changes

Wording only, unless stated:

| File | Change | Effect |
|---|---|---|
| `archivist_bench/*.py` | comments, docstrings, CLI help, printed messages and generated file comments name campaigns by folder and say "human audit", "auditor" and "launch" in prose; identifiers, JSON keys, CSV values and file names are unchanged | none on any result |
| `archivist_bench/__main__.py` | the `analysis-grounding.json` `label` and `scope` wording (see above) | output wording only |
| `archivist_bench/report.py` | the generated audit header sentence (see above) | output wording only |
| `archivist_bench/redact.py` | `REPOSITORY_MARKERS`: the private source forge hostname became `private-source-forge.invalid` (the public `github.com/mosaicss` and `mosaicss/` markers stay) | contamination detection no longer looks for the private host; no committed run was flagged by it |
| `archivist_bench/model.py` | the default evidence root (shown only by dry runs without `--evidence-dir`) is `~/archivist-bench-evidence/codex/campaign`; `MOSAIC_EVIDENCE_ROOT` still overrides the root | none on any result |
| `tests/test_archivist_bench.py` | paths for this layout; comments, docstrings and some test names reworded; four expected message fragments follow the new wording with the same strictness; the two pinned digests of `analysis-grounding.json` in `CODEX_GROUNDING_1_4_0` follow the label wording; the private forge host became the placeholder; two upstream hostnames are built at runtime; a `# gitleaks:allow` comment on a synthetic JWT | all 338 tests pass, as internally |

## Hashes that refer to the internal files

Five files changed bytes and are named by hash in the analyses:

| File | sha256 internal (referenced) | sha256 here |
|---|---|---|
| `preregistration/questions.json` | `53e7f9ef1906d5a34ffbab85896156039279bf835cd102bf40da675935a4d21a` | `64b1042767f00285aa55971dc04adeda4b129c372712897f648484ee888d9a10` |
| `preregistration/contamination-questions.json` | `806984fbfc8e087172ccdde7ab6decf0f96050efedbf5007d1e9749acebe2111` | `c90460ec56507f694047518c248fe97885bbc996101cc74b2ac99726f60f23cc` |
| `results/codex/audit-queue-001.csv` | `216b97ba6e3f46e085905079bc080c0a5b5a66ff72d52da516870e03d2ba433c` | `d591f0f3938074a5590fcdbb557a47e194b9d9647025f71c2865658b80390e02` |
| `results/claude-code/audit-queue-001.csv` | `30ae5c83430349100a4f399c2e8ce5c07e385a7eb658a309bd4c3b33ae6bb54f` | `12e78577b6d7e61aef293bfab2978523a2c53e30d777e1e6d27c15c8bcfd580f` |
| `preregistration/key-corrections-amendment-5.json` | `73cb8ace43cb1fdec1c1a3b317f27d89433f754b90957cb1216db67ba1e1c048` | `1d3948844723c13d841d7a7fa11aee766abc178a31f4ee02ef4f150f7d4b444b` (not referenced by any published file) |

`questions_sha256`, the `audit_files` hashes and the `evidence_fingerprint` and
`bias_evidence_fingerprint` values in the analyses (the fingerprints include the bytes of these
files; one is also quoted in `results.md`) therefore describe the internal files. Rerunning
`analyze` over Mosaic's internal evidence with this repository's harness and files on 2026-10-08
gave the same output as every committed analysis except these fields:

| Analysis | Fields that differ | With the files here |
|---|---|---|
| `results/codex/analysis.json` | `evidence_fingerprint`, `questions_sha256`, the `audit-queue-001.csv` hash | `da9989089c83623c87176e7ec898b87ce0987eeaaa64269237ffd76d7ead2766`, `64b10427...`, `d591f0f3...` |
| `results/claude-code/analysis.json` | `evidence_fingerprint`, `questions_sha256`, the `audit-queue-001.csv` hash, `bias_evidence_fingerprint` | `46400fe35f39d5c3bb6e5034d21dfa0c80639caa9b1ac4148485a3bd63e06a31`, `64b10427...`, `12e78577...`, `6ace5e1f32fd853b9c60ffbb7fa46f62c4c2ecea674ba381f312b08cfe09d8db` |
| `results/grounding/contamination/analysis.json` | `evidence_fingerprint`, `questions_sha256` | `399eb64a80bac7e22002f4f69478ccaabeb9af37a52794e942b2ffed1988b9b1`, `c90460ec...` |
| `results/claude-code/contamination/analysis.json` | `evidence_fingerprint`, `questions_sha256`, and `harness_version` (the committed file was written by 1.5.1) | `cec69685b9a15248c3591bb0de324852d9bbd2e4fee58bac680f05db2c71241f`, `c90460ec...`, `1.6.0` |

Hashes kept as internal references (they identify the internal originals): `logs/manifest.csv`
(`sha256` and `bytes` of each raw log before scrubbing), the `[clipped: original N chars, sha256
...]` markers inside logs (a clipped string's kept prefix may carry a redaction from the tables
above), and the source and prompt hashes inside the analyses.

## Kept on purpose

- Mosaic passage permalinks (`https://mosaic-finance.com/filings/...`) in the answer keys and
  audit files: the public way to read each key passage.
- Functional names that contain words used elsewhere for internal roles: the `owner` value of
  the `correction_source` audit column and `superseded_by`, the `owner_verdict` and
  `owner_ruling` keys, the `audit-owner-list.md` file name, `RunSpec` and `spec` variables, and
  `dispatched_at`, `not_dispatched`, `dispatch-refused.json` and the guard notes the harness
  writes into evidence. Renaming them would change the harness's file formats.
- `HERDR` in the harness's list of environment variable prefixes that agents never inherit (a
  terminal multiplexer's variables); removing it would change which variables are scrubbed.
- The archivist-cli commit of v0.2.32 in `smoke-results.md` (`aeb43de`): it belongs to the public
  `mosaicss/archivist` repository.
- Synthetic credentials, paths and emails in `tests/` (fake API keys, a fake home directory,
  addresses at `example.com`): fixtures for the scrubber and environment tests.
- The Apache License 2.0 text, which uses "copyright owner" as a legal term.

## Encrypted reasoning

Codex run logs carry the model's reasoning as opaque `encrypted_content` blobs that only the
model provider can decrypt. Every such value is replaced with `<redacted: encrypted reasoning>`
(2026-10-08). Token counts, tool events and answers are unaffected; the blobs carried no
readable content.
