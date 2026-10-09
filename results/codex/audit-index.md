# Human audit index

353 facts in 1 shards (whole runs per shard, run id order, each file under 900 KB). Fill `audit_verdict` in the CSV shards, then run `analyze --primary-judge B --audit <this folder>`.
Scoring rule: pass B (Codex) scores every fact (pre registration amendment 2); a fact is disputed when its existing pass A (Claude Code) verdict differs.

| shard | runs | disputed | sampled | facts |
|---|---:|---:|---:|---:|
| [audit-queue-001.csv](audit-queue-001.csv) ([md](audit-queue-001.md)) | 270 | 1 | 352 | 353 |
