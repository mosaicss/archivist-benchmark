"""Human audit queue, audit corrections and the committed results folder (codex campaign).

``audit-export`` writes every disputed fact plus the seeded 5% sample of agreed facts with what
a person needs to check it against the filing (key, Mosaic permalink, quote, both judge
verdicts, the link redacted answer) and blank auditor columns. Under the amendment 2 rule
(``--primary-judge B``) a fact is disputed when an existing pass A verdict differs from pass B,
the sample is 5% of every other judged fact and the scoring verdict is pass B's; each row names
its rule (``primary_judge``) and a file of another rule is refused. ``analyze --audit`` applies
the auditors' filled verdicts. ``report`` turns an ``analyze`` output plus the campaign evidence
into the committed results files. Every committed text passes ``redact.redact_links``; nothing here
decides a hypothesis.
"""

from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import io
import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from statistics import fmean
from typing import Any

from . import codex, judge, questions, stats
from . import probe as probe_module
from .model import STATUS_COMPLETED
from .redact import find_upstream_hosts, redact_links
from .runner import window_percents

AUDIT_VERDICTS = judge.VERDICTS  # correct, incorrect, missing
AUDIT_SCORES = {"correct": 1.0, "incorrect": 0.0, "missing": 0.0}
CLIP_CHARS = 4000
MAX_COMMITTED_BYTES = 1000 * 1024  # pre-commit check-added-large-files --maxkb=1000
# Account identifiers a raw log can carry (Codex ``session_meta``); masked in committed logs.
ACCOUNT_KEYS = frozenset(
    {
        "creator_user_id",
        "creator_account_id",
        "account_id",
        "chatgpt_account_id",
        "user_id",
        "organization_id",
        "org_id",
        "email",
    }
)
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

AUDIT_COLUMNS = (
    "selection",
    "run_id",
    "agent",
    "arm",
    "stratum",
    "question_id",
    "question",
    "fact_id",
    "fact_kind",
    "statement",
    "expected_value",
    "unit",
    "accepted",
    "permalink",
    "exchange_document_id",
    "document_id_absent_reason",
    "absence",
    "formula",
    "quote",
    "judge_verdict",
    "primary_judge",
    "judge_A_verdict",
    "judge_B_verdict",
    "answer_file",
    "audit_verdict",
    "audit_note",
)
# Amendment 5 (pre registration section 16.2, harness 1.6.0): the source of an audit row. Blank
# or ``owner``: a human audit verdict; ``key_correction``: a key correction rescore row
# (``rejudge-keys --export``), applied like an audit row but never human audited. A human row on
# the same run and fact wins; the key correction row is then listed as superseded.
OWNER_SOURCE = "owner"
KEY_CORRECTION_SOURCE = "key_correction"
CORRECTION_SOURCES = (OWNER_SOURCE, KEY_CORRECTION_SOURCE)
RESCORE_COLUMNS = (*AUDIT_COLUMNS, "correction_source")


class AuditError(ValueError):
    """An audit export or audit file that cannot be used; nothing is written or applied."""


# --- shared helpers ------------------------------------------------------------------------------


def tidy(text: str) -> str:
    """Strip trailing whitespace per line and end with one newline, so the repository's
    whitespace and end of file fixers never rewrite a committed file."""
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


# Columns holding a validated Mosaic passage permalink (questions.validate): kept as links.
LINK_COLUMNS = frozenset({"permalink"})


def _csv(columns: Sequence[str], rows: Iterable[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(columns), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _cell(row.get(k), k in LINK_COLUMNS) for k in columns})
    return tidy(buf.getvalue())


def _cell(value: Any, keep_links: bool = False) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.6g}" if abs(value) < 1e6 else f"{value:.0f}"
    if isinstance(value, dict | list):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return scrub_free_text(str(value) if keep_links else redact_links(str(value)))


def answer_path(run_dir: Path) -> Path:
    """The one canonical answer of a run: ``<run_dir>/answer.md``. Scoring, fingerprinting and
    export all read it; a record's ``answer_file`` is never followed."""
    return run_dir / "answer.md"


def answer_text(run_dir: Path) -> str | None:
    path = answer_path(run_dir)
    return path.read_text(encoding="utf-8") if path.is_file() else None


def answer_mismatch(rec: dict[str, Any], run_dir: Path) -> str | None:
    """The record's ``answer_file`` when it points anywhere but the canonical answer."""
    pointer = rec.get("answer_file")
    if not pointer:
        return None
    try:
        same = Path(str(pointer)).resolve() == answer_path(run_dir).resolve()
    except OSError:
        same = False
    return None if same else str(pointer)


def write_answer(out: Path, run_id: str, text: str) -> str:
    """``answers/<run_id>.md``, link redacted; returns the path relative to ``out``."""
    rel = f"answers/{run_id}.md"
    path = out / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tidy(scrub_free_text(redact_links(text))), encoding="utf-8")
    return rel


def _judgements(evidence: Path) -> list[dict[str, Any]]:
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((evidence / "runs").glob("*/judgement.json"))
    ]


# --- audit export --------------------------------------------------------------------------------


def _fact_row(fact: dict[str, Any]) -> dict[str, Any]:
    src = fact.get("source") or {}
    absence = ""
    if fact.get("kind") == "absence":
        terms = "; ".join(map(str, fact.get("absent_terms") or []))
        absence = f"filing {src.get('filing_uuid', '')} has none of: {terms}. " + str(
            fact.get("absence_reason") or ""
        )
    return {
        "fact_kind": fact.get("kind"),
        "statement": fact.get("statement"),
        "expected_value": fact.get("value"),
        "unit": fact.get("unit"),
        "accepted": "; ".join(map(str, fact.get("accept") or [])) or None,
        "permalink": src.get("permalink"),
        "exchange_document_id": src.get("exchange_document_id"),
        "document_id_absent_reason": src.get("document_id_absent_reason"),
        "absence": absence.strip() or None,
        "formula": fact.get("formula"),
        "quote": src.get("quote"),
    }


def audit_rows(
    evidence: Path,
    data: dict[str, Any],
    records: dict[str, dict[str, Any]],
    seed: int,
    primary: str = judge.DEFAULT_PRIMARY_JUDGE,
) -> list[dict[str, Any]]:
    """One row per queued fact (``judge.audit_queue`` under the scoring rule: disputed facts,
    then the sample). ``judge_verdict`` is the scoring verdict of that rule (combined: the
    agreed verdict or ``disputed``; B: the pass B verdict)."""
    lookup = questions.by_id(data)
    judgements = _judgements(evidence)
    by_run = {str(j.get("run_id")): j for j in judgements}
    rows: list[dict[str, Any]] = []
    for item in judge.audit_queue(judgements, seed, primary):
        run_id, fact_id = item["run_id"], item["fact_id"]
        rec = records.get(run_id)
        if rec is None:
            raise AuditError(f"judgement for unknown run {run_id}")
        q = lookup.get(str(rec["question_id"]))
        facts = {str(f["id"]): f for f in (q or {}).get("facts") or []}
        if fact_id not in facts:
            raise AuditError(f"unknown fact id {fact_id} (run {run_id})")
        passes = by_run[run_id].get("passes") or {}
        selection = item.get("selection") or (
            "disputed" if item["verdict"] == "disputed" else "sample"
        )
        rows.append(
            {
                "selection": selection,
                "run_id": run_id,
                "agent": rec["agent"],
                "arm": rec["arm"],
                "stratum": rec["stratum"],
                "question_id": rec["question_id"],
                "question": (q or {}).get("question"),
                "fact_id": fact_id,
                **_fact_row(facts[fact_id]),
                "judge_verdict": item["verdict"],
                "primary_judge": primary,
                **{
                    f"judge_{p}_verdict": ((passes.get(p) or {}).get("fact_verdicts") or {}).get(
                        fact_id
                    )
                    for p in judge.PASSES
                },
                "answer_file": None,
                "audit_verdict": "",
                "audit_note": "",
            }
        )
    return rows


def _md_cell(value: Any) -> str:
    text = _cell(value)
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")


AUDIT_SHARD_BYTES = 900 * 1024  # each shard's CSV and Markdown stay under this


def _rule_text(primary: str) -> str:
    if primary == "B":
        return (
            "Scoring rule: pass B (Codex) scores every fact (pre registration amendment 2); a "
            "fact is disputed when its existing pass A (Claude Code) verdict differs."
        )
    return "Scoring rule: combined (amendment 1); a fact is disputed when the judges disagree."


def _audit_intro(
    rows: Sequence[dict[str, Any]],
    seed: int,
    title: str,
    primary: str = judge.DEFAULT_PRIMARY_JUDGE,
) -> list[str]:
    disputed = sum(1 for r in rows if r["selection"] == "disputed")
    sampled = (
        f"sampled facts ({judge.AUDIT_SAMPLE_SHARE:.0%} of all other judged facts, seed {seed})."
        if primary == "B"
        else f"sampled agreed facts ({judge.AUDIT_SAMPLE_SHARE:.0%} of agreed facts, seed {seed})."
    )
    return [
        f"# {title}",
        "",
        f"{disputed} disputed facts (the two judges disagreed) and {len(rows) - disputed} "
        + sampled,
        _rule_text(primary),
        "Check each fact against its Mosaic permalink and the answer, then fill `audit_verdict`",
        "(`correct`, `incorrect` or `missing`) and `audit_note` in the matching CSV shard; apply",
        "with `analyze --audit <results folder>` (every `audit-queue-*.csv`). Blank rows keep the",
        "judge score. Answer quality acceptance stays with the authors.",
    ]


def _run_block(items: Sequence[dict[str, Any]]) -> list[str]:
    first = items[0]
    out = [
        "",
        f"## {first['run_id']}",
        "",
        f"{first['agent']}, arm {first['arm']}, {first['stratum']}, {first['question_id']}: "
        f"{_md_cell(first['question'])}",
        "",
        f"Answer (link redacted): [{first['answer_file']}]({first['answer_file']})"
        if first["answer_file"]
        else "Answer: none stored",
        "",
        "| fact | selection | statement | expected | kind | source | quote | judge A | "
        "judge B | audit verdict | audit note |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in items:
        expected = _md_cell(r["expected_value"]) + (f" {_md_cell(r['unit'])}" if r["unit"] else "")
        if r["accepted"]:
            expected += f" (also: {_md_cell(r['accepted'])})"
        source = " ".join(
            part
            for part in (
                f"[permalink]({r['permalink']})" if r["permalink"] else "",
                _md_cell(r["exchange_document_id"]),
                _md_cell(r["document_id_absent_reason"]),
                _md_cell(r["absence"]),
                f"formula {_md_cell(r['formula'])}" if r["formula"] else "",
            )
            if part
        )
        out.append(
            f"| {r['fact_id']} | {r['selection']} | {_md_cell(r['statement'])} | {expected} "
            f"| {_md_cell(r['fact_kind'])} | {source} | {_md_cell(r['quote'])} | "
            f"{_md_cell(r['judge_A_verdict'])} | {_md_cell(r['judge_B_verdict'])} |  |  |"
        )
    return out


def _by_run(rows: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row["run_id"]].append(row)
    return [groups[k] for k in sorted(groups)]


def audit_markdown(
    rows: Sequence[dict[str, Any]],
    seed: int,
    title: str = "Human audit queue",
    primary: str = judge.DEFAULT_PRIMARY_JUDGE,
) -> str:
    out = _audit_intro(rows, seed, title, primary)
    for items in _by_run(rows):
        out += _run_block(items)
    return tidy("\n".join(out))


def shard_audit(
    rows: Sequence[dict[str, Any]], limit: int = AUDIT_SHARD_BYTES
) -> list[list[dict[str, Any]]]:
    """Split the queue into shards of whole runs (run id order), each shard's CSV and Markdown
    under ``limit`` bytes; deterministic for the same rows. A single run larger than the limit
    gets a shard of its own."""
    header_csv = len(_csv(AUDIT_COLUMNS, []).encode())
    header_md = 2000  # the intro of a shard, generously
    shards: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    csv_size, md_size = header_csv, header_md
    for items in _by_run(rows):
        add_csv = len(_csv(AUDIT_COLUMNS, items).encode()) - header_csv
        add_md = len(("\n".join(_run_block(items)) + "\n").encode())
        if current and (csv_size + add_csv > limit or md_size + add_md > limit):
            shards.append(current)
            current, csv_size, md_size = [], header_csv, header_md
        current.extend(items)
        csv_size += add_csv
        md_size += add_md
    if current:
        shards.append(current)
    return shards


def _filled_shards(out: Path) -> list[Path]:
    filled = []
    for path in sorted(out.glob("audit-queue*.csv")):
        with path.open(encoding="utf-8", newline="") as fh:
            if any((row.get("audit_verdict") or "").strip() for row in csv.DictReader(fh)):
                filled.append(path)
    return filled


def export_audit(
    evidence: Path,
    data: dict[str, Any],
    records: dict[str, dict[str, Any]],
    out: Path,
    seed: int,
    shard_bytes: int = AUDIT_SHARD_BYTES,
    primary: str = judge.DEFAULT_PRIMARY_JUDGE,
    expected: Sequence[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Write the audit shards ``audit-queue-NNN.csv`` and ``.md``, ``audit-index.md`` and the
    queued runs' answers under the scoring rule ``primary``. Refuses to overwrite a shard that
    already holds human audit verdicts, and, given the ``expected`` (run id, fact id) queue of an
    analysis, refuses a queue that differs from it."""
    rows = audit_rows(evidence, data, records, seed, primary)
    if expected is not None and [(r["run_id"], r["fact_id"]) for r in rows] != list(expected):
        raise AuditError(
            "the audit queue differs from the analysis' audit_queue (the evidence changed "
            "since the analysis, or another rule or seed); rerun analyze"
        )
    out.mkdir(parents=True, exist_ok=True)
    filled = _filled_shards(out)
    if filled:
        raise AuditError(
            "refusing to overwrite audit files with human audit verdicts: "
            + ", ".join(p.name for p in filled)
        )
    for stale in sorted(out.glob("audit-queue*.csv")) + sorted(out.glob("audit-queue*.md")):
        stale.unlink()
    for row in rows:
        text = answer_text(evidence / "runs" / row["run_id"])
        row["answer_file"] = write_answer(out, row["run_id"], text) if text else None
    shards = shard_audit(rows, shard_bytes)
    index = [
        "# Human audit index",
        "",
        f"{len(rows)} facts in {len(shards)} shards (whole runs per shard, run id order, each "
        f"file under {shard_bytes // 1024} KB). Fill `audit_verdict` in the CSV shards, then run "
        f"`analyze --primary-judge {primary} --audit <this folder>`.",
        _rule_text(primary),
        "",
        "| shard | runs | disputed | sampled | facts |",
        "|---|---:|---:|---:|---:|",
    ]
    for n, shard in enumerate(shards, start=1):
        name = f"audit-queue-{n:03d}"
        for row in shard:
            row["shard"] = name
        title = f"Human audit queue, shard {n} of {len(shards)}"
        (out / f"{name}.csv").write_text(_csv(AUDIT_COLUMNS, shard), encoding="utf-8")
        (out / f"{name}.md").write_text(
            audit_markdown(shard, seed, title, primary), encoding="utf-8"
        )
        disputed = sum(1 for r in shard if r["selection"] == "disputed")
        index.append(
            f"| [{name}.csv]({name}.csv) ([md]({name}.md)) | {len({r['run_id'] for r in shard})} "
            f"| {disputed} | {len(shard) - disputed} | {len(shard)} |"
        )
    (out / "audit-index.md").write_text(tidy("\n".join(index)), encoding="utf-8")
    return rows


# --- audit application ---------------------------------------------------------------------------


def audit_files(paths: Sequence[Path]) -> list[Path]:
    """The audit CSVs named by ``analyze --audit``: files as given, directories as every
    ``audit-queue-*.csv`` inside (sorted); each once."""
    out: list[Path] = []
    for path in paths:
        if path.is_dir():
            found = sorted(path.glob("audit-queue-*.csv"))
            if not found:
                raise AuditError(f"no audit-queue-*.csv in {path}")
            out += found
        elif path.is_file():
            out.append(path)
        else:
            raise AuditError(f"audit file not found: {path}")
    unique: list[Path] = []
    for path in out:
        if path.resolve() not in {u.resolve() for u in unique}:
            unique.append(path)
    return unique


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _file_rules(path: Path) -> set[str]:
    """The scoring rules an audit CSV was exported under: its non blank ``primary_judge``
    values (rows an auditor added by hand may leave it blank); no such value or no column (a
    harness 1.2 export) means ``combined``."""
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if "primary_judge" not in (reader.fieldnames or []):
            return {judge.DEFAULT_PRIMARY_JUDGE}
        rules = {(row.get("primary_judge") or "").strip() for row in reader} - {""}
        return rules or {judge.DEFAULT_PRIMARY_JUDGE}


def check_audit_rule(paths: Sequence[Path], primary: str) -> None:
    """Refuse audit files exported under another scoring rule than ``primary``."""
    bad = []
    for path in paths:
        if not path.is_file():
            continue
        rules = _file_rules(path)
        if rules - {primary}:
            bad.append(f"{path.name} ({', '.join(sorted(rules))})")
    if bad:
        raise AuditError(
            f"audit files exported under another scoring rule than {primary}: " + ", ".join(bad)
        )


def load_audit(
    paths: Path | Sequence[Path],
    judgements: Sequence[dict[str, Any]],
    primary: str = judge.DEFAULT_PRIMARY_JUDGE,
) -> list[dict[str, Any]]:
    """The filled rows of the audit CSVs, validated against the judgements across every file
    under the scoring rule ``primary``. Raises AuditError naming every bad row (invalid
    verdict, unknown run or fact, conflicting duplicates, also across shards) or a file
    exported under another rule, so a bad set applies nothing. Each entry carries its
    ``source`` (``load_audit_sources``); key correction rows a human row overrides are left
    out."""
    return load_audit_sources(paths, judgements, primary)[0]


def load_audit_sources(
    paths: Path | Sequence[Path],
    judgements: Sequence[dict[str, Any]],
    primary: str = judge.DEFAULT_PRIMARY_JUDGE,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(applied, superseded) audit corrections (amendment 5). Every filled row is validated as
    in ``load_audit``; its ``correction_source`` (blank or ``owner``, or ``key_correction``)
    must be known, and conflicting verdicts of the same source on one run and fact are refused
    as before. On a run and fact with both, the human row is applied and the key correction row
    is listed in ``superseded`` (with the human audit verdict that replaced it)."""
    files = [paths] if isinstance(paths, Path) else list(paths)
    by_run = {
        str(j.get("run_id")): scored
        for j in judgements
        if (scored := judge.scoring(j, primary)) is not None
    }
    raw: list[tuple[str, int, dict[str, str]]] = []
    for path in files:
        if not path.exists():
            raise AuditError(f"audit file not found: {path}")
        with path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            missing = {"run_id", "fact_id", "audit_verdict"} - set(reader.fieldnames or [])
            if missing:
                raise AuditError(f"{path.name} lacks columns: {', '.join(sorted(missing))}")
            raw += [(path.name, n, row) for n, row in enumerate(reader, start=2)]
    check_audit_rule(files, primary)
    errors: list[str] = []
    seen: dict[tuple[str, str, str], str] = {}
    filled: list[dict[str, Any]] = []
    for name, number, row in raw:
        line = f"{name}:{number}"
        verdict = (row.get("audit_verdict") or "").strip().lower()
        if not verdict:
            continue
        run_id, fact_id = (row.get("run_id") or "").strip(), (row.get("fact_id") or "").strip()
        if verdict not in AUDIT_VERDICTS:
            errors.append(f"{line}: invalid audit_verdict {verdict!r}")
            continue
        source = (row.get("correction_source") or "").strip().lower() or OWNER_SOURCE
        if source not in CORRECTION_SOURCES:
            errors.append(f"{line}: invalid correction_source {source!r}")
            continue
        judgement = by_run.get(run_id)
        if judgement is None:
            errors.append(f"{line}: unknown run {run_id!r} (no valid judgement)")
            continue
        if fact_id not in judgement.scores:
            errors.append(f"{line}: unknown fact {fact_id!r} for run {run_id}")
            continue
        key = (run_id, fact_id, source)
        if key in seen and seen[key] != verdict:
            errors.append(f"{line}: conflicting audit verdicts for {run_id} {fact_id}")
            continue
        seen[key] = verdict
        filled.append(
            {
                "run_id": run_id,
                "fact_id": fact_id,
                "audit_verdict": verdict,
                "audit_note": (row.get("audit_note") or "").strip(),
                "judge_verdict": judgement.verdicts.get(fact_id),
                "judge_score": judgement.scores.get(fact_id),
                "audit_score": AUDIT_SCORES[verdict],
                "source": source,
            }
        )
    if errors:
        raise AuditError("; ".join(errors))
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in filled:
        unique.setdefault((item["run_id"], item["fact_id"], item["source"]), item)
    applied: list[dict[str, Any]] = []
    superseded: list[dict[str, Any]] = []
    for run_id, fact_id in sorted({(r, f) for r, f, _s in unique}):
        owner = unique.get((run_id, fact_id, OWNER_SOURCE))
        key_row = unique.get((run_id, fact_id, KEY_CORRECTION_SOURCE))
        chosen = owner if owner is not None else key_row
        assert chosen is not None
        applied.append(chosen)
        if owner is not None and key_row is not None:
            superseded.append(
                {
                    k: key_row[k]
                    for k in ("run_id", "fact_id", "source", "audit_verdict", "audit_note")
                }
                | {"superseded_by": OWNER_SOURCE, "owner_verdict": owner["audit_verdict"]}
            )
    return applied, superseded


def evidence_fingerprint(evidence: Path, questions_path: Path, audit_paths: Sequence[Path]) -> str:
    """sha256 over everything ``analyze`` and ``report`` consume, in sorted order: each run's
    ``record.json``, ``judgement.json``, ``answer.md`` and raw log source (rollout or stream),
    the billing evidence (``billing_files``), the questions file, and the current bytes of
    each audit file. ``report`` recomputes it from
    the current files and refuses a mismatch."""
    h = hashlib.sha256()

    def part(label: str, path: Path) -> None:
        if path.is_file():
            h.update(f"{label}\0".encode() + hashlib.sha256(path.read_bytes()).digest())

    for record in sorted((evidence / "runs").glob("*/record.json")):
        run_dir = record.parent
        h.update(f"run\0{run_dir.name}\0".encode())
        part("record", record)
        part("judgement", run_dir / "judgement.json")
        part("answer", answer_path(run_dir))
        try:
            agent = str(json.loads(record.read_text(encoding="utf-8")).get("agent"))
        except json.JSONDecodeError:
            agent = ""
        source = log_source(run_dir, agent)
        if source is not None:
            part("log", source)
    for path in billing_files(evidence):
        h.update(f"billing\0{path.relative_to(evidence).as_posix()}\0".encode())
        part("billing_bytes", path)
    part("questions", questions_path)
    for path in audit_paths:
        h.update(f"audit\0{path.name}\0".encode())
        part("audit_bytes", path)
    return h.hexdigest()


# --- billing evidence (review round 4) -----------------------------------------------------------

PROBE_COLUMNS = (
    "time",
    "kind",
    "source",
    "ok",
    "error",
    "status",
    "windows",
    "credits_balance",
    "has_credits",
    "overage_status",
    "overage_disabled_reason",
    "is_using_overage",
    "warning",
)
WARNING_PREFIXES = ("metered_fallback_guard:", "plan_window_guard:", "plan_rejected_guard:")
JUDGE_REFUSALS = ("metered_fallback_guard", "plan probe refused", "plan gate refused")


def billing_files(evidence: Path) -> list[Path]:
    """The campaign's billing evidence: the probe ledger, the agent and judge closing probes,
    refused launches and the judge ledger."""
    files = [evidence / "plan-probes.jsonl", evidence / "closing-probe.json"]
    files += sorted((evidence / "judge-logs").glob("closing-*/closing-probe.json"))
    files += sorted((evidence / "runs").glob("*/dispatch-refused.json"))
    files.append(evidence / "judge-ledger.jsonl")
    return [f for f in files if f.is_file()]


def _jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _json_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _closing_rows(evidence: Path) -> list[dict[str, Any]]:
    """Closing probes from the ledger plus closing-probe.json files the ledger lacks."""
    rows = [
        r | {"source": "plan-probes.jsonl"}
        for r in _jsonl(evidence / "plan-probes.jsonl")
        if str(r.get("kind", "")).endswith("closing")
    ]
    seen = {(r.get("probed_at") or r.get("time"), json.dumps(r.get("reasons"))) for r in rows}
    files = [evidence / "closing-probe.json"]
    files += sorted((evidence / "judge-logs").glob("closing-*/closing-probe.json"))
    for path in files:
        data = _json_file(path) if path.is_file() else {}
        key = (data.get("probed_at"), json.dumps(data.get("reasons")))
        if data and key not in seen:
            kind = "codex_closing" if path.parent == evidence else "codex_judge_closing"
            rows.append(
                data
                | {
                    "time": data.get("probed_at"),
                    "kind": kind,
                    "source": path.relative_to(evidence).as_posix(),
                }
            )
            seen.add(key)
    return rows


def billing_warnings(evidence: Path, records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every billing warning of a campaign: closing probe findings (credit drops, hot windows,
    window steps), refused launches, failed Claude probes, record guard notes and judge
    calls refused by the plan gate or the metered fallback guard."""
    out: list[dict[str, Any]] = []
    for row in _closing_rows(evidence):
        for reason in row.get("reasons") or []:
            out.append(
                {
                    "time": row.get("time"),
                    "kind": row.get("kind"),
                    "source": row.get("source"),
                    "detail": str(reason),
                }
            )
    for path in sorted((evidence / "runs").glob("*/dispatch-refused.json")):
        data = _json_file(path)
        for reason in data.get("reasons") or []:
            out.append(
                {
                    "time": data.get("refused_at"),
                    "kind": "dispatch_refused",
                    "source": path.parent.name,
                    "detail": str(reason),
                }
            )
    for row in _jsonl(evidence / "plan-probes.jsonl"):
        if row.get("kind") == "claude_probe" and not row.get("ok"):
            out.append(
                {
                    "time": row.get("time"),
                    "kind": "claude_probe_failed",
                    "source": "plan-probes.jsonl",
                    "detail": str(row.get("error")),
                }
            )
    for rec in records:
        for note in rec.get("notes") or []:
            if str(note).startswith(WARNING_PREFIXES):
                out.append(
                    {
                        "time": rec.get("started_at"),
                        "kind": "record_guard",
                        "source": rec.get("run_id"),
                        "detail": str(note),
                    }
                )
    for row in _jsonl(evidence / "judge-ledger.jsonl"):
        error = str(row.get("error") or "")
        if row.get("status") == "infra_error" and any(k in error for k in JUDGE_REFUSALS):
            out.append(
                {
                    "time": row.get("started_at"),
                    "kind": "judge_refusal",
                    "source": f"{row.get('run_id')} pass {row.get('pass')}",
                    "detail": error,
                }
            )
    return sorted(out, key=lambda w: (str(w["time"]), str(w["kind"]), str(w["detail"])))


def _codex_windows(limits: dict[str, Any]) -> str:
    parts = []
    for key in ("primary", "secondary"):
        window = limits.get(key)
        if isinstance(window, dict):
            parts.append(
                f"{key} {window.get('usedPercent')}% ({window.get('windowDurationMins')} min)"
            )
    return "; ".join(parts)


def plan_probe_rows(evidence: Path) -> list[dict[str, Any]]:
    """``plan-probes.csv``: every Claude probe and closing probe, redacted (no account ids)."""
    rows = []
    for row in _jsonl(evidence / "plan-probes.jsonl"):
        if row.get("kind") == "claude_probe":
            windows = row.get("windows") or {}
            rows.append(
                {
                    "time": row.get("time"),
                    "kind": "claude_probe",
                    "source": "plan-probes.jsonl",
                    "ok": row.get("ok"),
                    "error": row.get("error"),
                    "status": row.get("status"),
                    "windows": "; ".join(
                        f"{name} {(w or {}).get('utilization')} (resets {(w or {}).get('resetsAt')})"
                        for name, w in sorted(windows.items())
                    ),
                    "overage_status": row.get("overage_status"),
                    "overage_disabled_reason": row.get("overage_disabled_reason"),
                    "is_using_overage": row.get("is_using_overage"),
                }
            )
    for row in _closing_rows(evidence):
        report = probe_module.sanitize(row.get("probe") or {})
        result = report.get("result") if isinstance(report, dict) else None
        limits = (result or {}).get("rateLimits") or {}
        credits = limits.get("credits") or {}
        rows.append(
            {
                "time": row.get("time"),
                "kind": row.get("kind"),
                "source": row.get("source"),
                "ok": report.get("ok") if isinstance(report, dict) else None,
                "error": report.get("error") if isinstance(report, dict) else None,
                "windows": _codex_windows(limits),
                "credits_balance": credits.get("balance"),
                "has_credits": credits.get("hasCredits"),
                "warning": "; ".join(map(str, row.get("reasons") or [])),
            }
        )
    return sorted(rows, key=lambda r: (str(r["time"]), str(r["kind"]), str(r["source"])))


def audit_digests(paths: Sequence[Path]) -> list[dict[str, str]]:
    return [{"name": p.name, "sha256": sha256_file(p)} for p in paths]


def corrections_by_run(corrections: Iterable[dict[str, Any]]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = defaultdict(dict)
    for c in corrections:
        out[str(c["run_id"])][str(c["fact_id"])] = str(c["audit_verdict"])
    return dict(out)


def audited_accuracy(
    judgement: dict[str, Any],
    corrections: dict[str, str],
    primary: str = judge.DEFAULT_PRIMARY_JUDGE,
) -> float | None:
    """Run accuracy with the audited facts scored 1 (correct) or 0, the rest as judged under
    the scoring rule ``primary``."""
    scored = judge.scoring(judgement, primary)
    if scored is None:
        return None
    scores = dict(scored.scores)
    if not scores:
        return scored.accuracy
    for fact_id, verdict in corrections.items():
        if fact_id in scores:
            scores[fact_id] = AUDIT_SCORES[verdict]
    return round(sum(scores.values()) / len(scores), 6)


# --- results report ------------------------------------------------------------------------------

RUN_COLUMNS = (
    "run_id",
    "run_key",
    "agent",
    "arm",
    "question_id",
    "stratum",
    "repetition",
    "attempt",
    "status",
    "status_reason",
    "contaminated",
    "model",
    "effort",
    "service_tier",
    "auth_mode",
    "billing_mode",
    "cli_version",
    "total_tokens",
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "uncached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "model_calls",
    "num_turns",
    "tool_calls",
    "web_actions",
    "compactions",
    "truncations",
    "paged_results",
    "turn_limit",
    "timeout",
    "wall_s",
    "cost_usd",
    "cost_basis",
    "mcp_loaded",
    "plan_window_max_percent",
    "plan_windows",
    "plan_type",
    "credit_balance",
    "metered_signs",
    "guard_notes",
    "accuracy",
    "completion",
    "judge_status",
    "judge_batches",
    "started_at",
    "answer_file",
    "log_file",
)
CELL_COLUMNS = (
    "agent",
    "stratum",
    "arm",
    "questions",
    "runs",
    "completed",
    "excluded_attempts",
    "question_mean_accuracy",
    "accuracy_questions",
    "question_mean_total_tokens",
    "token_questions",
    "question_mean_completion",
    "completion_questions",
)
COMPARISON_COLUMNS = (
    "source",
    "judge_pass",
    "agent",
    "stratum",
    "arm",
    "baseline",
    "metric",
    "estimate",
    "ci_low",
    "ci_high",
    "n_questions",
    "resamples",
    "seed",
    "dropped_questions",
)
MULTI_PERIOD_COLUMNS = (
    "question_id",
    "distinct_key_filings",
    "agent",
    "arm",
    "runs",
    "completed_reps",
    "mean_total_tokens",
    "mean_accuracy",
)
MANIFEST_COLUMNS = ("run_id", "source", "bytes", "sha256", "log_file", "clipped_strings")
METRICS = (
    "token_ratio_baseline_over_arm",
    "accuracy_diff_arm_minus_baseline",
    "completion_diff_arm_minus_baseline",
)


def plan_window_cells(plan_window: dict[str, Any] | None) -> dict[str, Any]:
    """The plan window in a few short columns (the full event stays in the committed log)."""
    if not plan_window:
        return {}
    percents = window_percents(plan_window)
    credits = plan_window.get("credits") or {}
    reached = plan_window.get("rate_limit_reached_seen") or plan_window.get(
        "rate_limit_reached_type"
    )
    first, lowest = credits.get("balance_first"), credits.get("balance_min")
    dropped = ""
    try:
        if first is not None and lowest is not None and float(lowest) < float(first):
            dropped = f"credit balance dropped {first} to {lowest}"
    except (TypeError, ValueError):
        dropped = ""
    signs = [
        f"rate_limit_reached_type={reached}" if reached else "",
        dropped,
        "is_using_overage" if plan_window.get("is_using_overage") is True else "",
        "rate_limit_rejected" if plan_window.get("rejected_seen") else "",
    ]
    return {
        "plan_window_max_percent": max(p for _, p in percents) if percents else None,
        "plan_windows": "; ".join(f"{name.split(' ', 1)[1]} {p:g}" for name, p in percents),
        "plan_type": plan_window.get("plan_type"),
        "credit_balance": credits.get("balance"),
        "metered_signs": "; ".join(s for s in signs if s),
    }


GUARD_PREFIXES = (
    "metered_fallback_guard:",
    "plan_window_guard:",
    "plan_rejected_guard:",
    "interrupted:",
)


def guard_notes(rec: dict[str, Any]) -> list[str]:
    """Every guard note of a record (credit drops included)."""
    return [str(n) for n in rec.get("notes") or [] if str(n).startswith(GUARD_PREFIXES)]


def metered_signs(rec: dict[str, Any]) -> str:
    """Billing signs from the plan window plus every metered fallback guard detail."""
    signs = [
        s
        for s in str(plan_window_cells(rec.get("plan_window")).get("metered_signs") or "").split(
            "; "
        )
        if s
    ]
    for note in guard_notes(rec):
        if note.startswith("metered_fallback_guard:"):
            signs += [
                part.strip()
                for part in note.split(":", 1)[1].split(";")
                if part.strip() and part.strip() not in signs
            ]
    return "; ".join(signs)


def run_rows(
    records: Sequence[dict[str, Any]],
    analysis: dict[str, dict[str, Any]],
    answers: dict[str, str],
    logs: dict[str, str],
) -> list[dict[str, Any]]:
    out = []
    for rec in sorted(records, key=lambda r: str(r["run_id"])):
        usage = rec.get("usage") or {}
        row = analysis.get(str(rec["run_id"]), {})
        out.append(
            {
                **{k: rec.get(k) for k in RUN_COLUMNS if k in rec},
                "total_tokens": usage.get("total"),
                "input_tokens": usage.get("input"),
                "cached_input_tokens": usage.get("cached_input"),
                "cache_write_input_tokens": usage.get("cache_write_input"),
                "uncached_input_tokens": usage.get("uncached_input"),
                "output_tokens": usage.get("output"),
                "reasoning_tokens": usage.get("reasoning"),
                **plan_window_cells(rec.get("plan_window")),
                "metered_signs": metered_signs(rec),
                "guard_notes": "; ".join(guard_notes(rec)),
                "accuracy": row.get("accuracy"),
                "completion": row.get("completion"),
                "judge_status": row.get("judge_status"),
                "judge_batches": row.get("judge_batches"),
                "answer_file": answers.get(str(rec["run_id"])),
                "log_file": logs.get(str(rec["run_id"])),
            }
        )
    return out


def _qmean(means: dict[tuple[str, str, str], dict[str, float]], key: tuple[str, str, str]) -> Any:
    values = list(means.get(key, {}).values())
    return (round(fmean(values), 6), len(values)) if values else (None, 0)


def cell_rows(
    rows: Sequence[dict[str, Any]], records: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Per agent, stratum and arm: question means (repetitions averaged first, the analysis
    unit) of accuracy, completed run tokens and completion, with run counts."""
    acc = stats.question_means(rows, "accuracy")
    tok = stats.question_means(rows, "total_tokens")
    comp = stats.question_means(rows, "completion")
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[(r["agent"], r["stratum"], r["arm"])].append(r)
    excluded: dict[tuple[str, str, str], int] = defaultdict(int)
    kept = {str(r["run_id"]) for r in rows}
    for rec in records:
        if str(rec["run_id"]) not in kept:
            excluded[(rec["agent"], rec["stratum"], rec["arm"])] += 1
    out = []
    for key in sorted(set(groups) | set(excluded)):
        items = groups.get(key, [])
        a, an = _qmean(acc, key)
        t, tn = _qmean(tok, key)
        c, cn = _qmean(comp, key)
        out.append(
            {
                "agent": key[0],
                "stratum": key[1],
                "arm": key[2],
                "questions": len({r["question_id"] for r in items}),
                "runs": len(items),
                "completed": sum(1 for r in items if r["status"] == STATUS_COMPLETED),
                "excluded_attempts": excluded.get(key, 0),
                "question_mean_accuracy": a,
                "accuracy_questions": an,
                "question_mean_total_tokens": round(t) if t is not None else None,
                "token_questions": tn,
                "question_mean_completion": c,
                "completion_questions": cn,
            }
        )
    return out


def comparison_rows(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Every comparison of the analysis: the main one, the partial usage token sensitivity and
    each single judge recomputation."""
    sources = (
        ("main", analysis.get("comparisons") or []),
        ("tokens_with_partial_usage", analysis.get("token_sensitivity_with_partial_usage") or []),
        ("single_judge", analysis.get("single_judge_sensitivity") or []),
    )
    out = []
    for source, entries in sources:
        if not isinstance(entries, list):
            continue  # ``{"deferred": ...}`` (amendment 2): nothing to tabulate
        for entry in entries:
            for metric in METRICS:
                value = entry.get(metric)
                if not isinstance(value, dict):
                    continue
                gone = entry.get("dropped_questions")
                if isinstance(gone, dict):
                    gone = gone.get(metric, [])
                ci = value.get("ci95") or [None, None]
                out.append(
                    {
                        "source": source,
                        "judge_pass": entry.get("judge_pass"),
                        "agent": entry.get("agent"),
                        "stratum": entry.get("stratum"),
                        "arm": entry.get("arm"),
                        "baseline": entry.get("baseline"),
                        "metric": metric,
                        "estimate": value.get("estimate"),
                        "ci_low": ci[0],
                        "ci_high": ci[1],
                        "n_questions": value.get("n_questions"),
                        "resamples": value.get("resamples"),
                        "seed": value.get("seed"),
                        "dropped_questions": "; ".join(gone or []),
                    }
                )
    return out


def key_filings(question: dict[str, Any]) -> int:
    """Distinct filings the answer key cites (sourced and absence facts)."""
    return len(
        {
            str((f.get("source") or {}).get("filing_uuid"))
            for f in question.get("facts") or []
            if (f.get("source") or {}).get("filing_uuid")
        }
    )


def multi_period_rows(rows: Sequence[dict[str, Any]], data: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    lookup = questions.by_id(data)
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r["stratum"] == "multi_period":
            groups[(r["question_id"], r["agent"], r["arm"])].append(r)
    for (qid, agent, arm), items in groups.items():
        tokens = [float(r["total_tokens"]) for r in items if r["total_tokens"] is not None]
        accuracy = [float(r["accuracy"]) for r in items if r["accuracy"] is not None]
        out.append(
            {
                "question_id": qid,
                "distinct_key_filings": key_filings(lookup.get(qid, {})),
                "agent": agent,
                "arm": arm,
                "runs": len(items),
                "completed_reps": sum(1 for r in items if r["status"] == STATUS_COMPLETED),
                "mean_total_tokens": round(fmean(tokens)) if tokens else None,
                "mean_accuracy": round(fmean(accuracy), 6) if accuracy else None,
            }
        )
    return sorted(
        out, key=lambda r: (r["distinct_key_filings"], r["question_id"], r["agent"], r["arm"])
    )


# Keys whose values are masked in committed logs, whatever the value's type (case
# insensitive; ``token`` only as a key ending, so usage counts like ``input_tokens`` stay).
SENSITIVE_KEY = re.compile(
    r"token$|secret|passw(?:or)?d|api_?key|authorization|cookie|session|account_?id|"
    r"user_?id|e_?mail",
    re.IGNORECASE,
)
# In text secrets (also checked by ``check_files`` on every committed file).
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "bearer",
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
        "Bearer <redacted>",
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]+"),
        "<redacted-jwt>",
    ),
    ("sk key", re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), "<redacted-key>"),
    ("ak key", re.compile(r"\bak_[A-Za-z0-9_-]{8,}"), "<redacted-key>"),
    ("mst key", re.compile(r"\bmst_[A-Za-z0-9_-]{8,}"), "<redacted-key>"),
    (
        "token query",
        re.compile(r"\b(access_token|refresh_token|id_token)=[^&\s\"'<>]+", re.IGNORECASE),
        r"\1=<redacted>",
    ),
)


SENSITIVE_PARTS = (
    "secret",
    "password",
    "passwd",
    "apikey",
    "authorization",
    "cookie",
    "session",
    "accountid",
    "userid",
    "organizationid",
    "email",
)


def normalize_key(key: str) -> str:
    """Lowercase without ``_`` and ``-``: organizationId, organization_id, org-id and orgid
    all compare alike."""
    return key.lower().replace("_", "").replace("-", "")


def _sensitive(key: str | None) -> bool:
    if key is None:
        return False
    norm = normalize_key(key)
    return (
        norm in {normalize_key(k) for k in ACCOUNT_KEYS}
        or norm.endswith("token")
        or norm in ("orgid", "org")
        or norm.endswith("orgid")
        or any(part in norm for part in SENSITIVE_PARTS)
        or bool(SENSITIVE_KEY.search(key))
    )


MASKED = frozenset({"<redacted>", "<email>"})
# ``key=value``, ``key: value`` and ``"key": value`` in prose: the key and separator, then a
# value that is quoted (to its matching delimiter, escapes honoured) or bare (to the next
# separator). Keys are searched at every position, so a key inside another value counts.
KEY_SEP = re.compile(
    r"(?P<kq>[\"']?)(?P<key>[A-Za-z_][A-Za-z0-9_.-]{0,63})(?P=kq)(?P<sep>\s*[:=]\s*)"
)
VALUE = re.compile(
    r"\"(?P<dq>(?:[^\"\\\n]|\\.)*)\"|'(?P<sq>(?:[^'\\\n]|\\.)*)'"
    r"|(?P<val>[^\s\"',;&}\])\x00]+)"
)


def assignments(text: str) -> list[tuple[str, int, int, str]]:
    """(key, value start, value end, value) for every assignment in a text; the span covers
    the value inside its quotes."""
    out: list[tuple[str, int, int, str]] = []
    pos = 0
    while True:
        m = KEY_SEP.search(text, pos)
        if m is None:
            return out
        v = VALUE.match(text, m.end())
        if v is not None:
            for group in ("dq", "sq", "val"):
                if v.group(group) is not None:
                    out.append((m.group("key"), v.start(group), v.end(group), v.group(group)))
                    break
        pos = m.end()


# A whole authorization header value: the scheme and the credentials.
# An authorization header's whole value, whatever the scheme (Basic, Bearer, Digest with its
# parameters): a quoted value to its matching quote (escapes honoured), else to the end of
# the line. ``\x00`` marks a Markdown cell or line break boundary during validation.
AUTH_HEADER = re.compile(
    r"(?P<key>\b(?:proxy-)?authorization)(?P<sep>[\"']?\s*[:=]\s*)"
    r"(?:\"(?P<dq>(?:[^\"\\\n]|\\.)*)\"?|'(?P<sq>(?:[^'\\\n]|\\.)*)'?|(?P<val>[^\r\n\x00]*))",
    re.IGNORECASE,
)
# Credentials in free text (answers, CSV cells, Markdown): only these exact singular keys
# (normalized), so filing prose such as "Trade secrets:" or "tokens:" is never a finding.
# JSON documents and logs keep the full sensitive key set.
CREDENTIAL_KEYS = frozenset(
    {
        "token",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "apikey",
        "password",
        "passwd",
        "secret",
        "clientsecret",
        "authorization",
        "cookie",
        "setcookie",
    }
)
KeyPolicy = Callable[[str | None], bool]


# A prefixed credential key: one token without spaces, a prefix joined by ``-`` or ``_``, then
# an exact singular credential key (``X-API-Key``, ``auth_token``, ``x-auth-token``,
# ``client-secret``, ``db_password``). Plurals and multi word prose never match.
PREFIXED_CREDENTIAL = re.compile(
    r"^[a-z0-9][a-z0-9._-]*[-_](?:token|access[-_]?token|refresh[-_]?token|id[-_]?token|"
    r"api[-_]?key|password|passwd|secret|client[-_]?secret|authorization|cookie|"
    r"set[-_]?cookie)$"
)


def _credential(key: str | None) -> bool:
    """The free text credential key rule: an exact singular credential key (normalized) or a
    prefixed one (``PREFIXED_CREDENTIAL``)."""
    if key is None or any(c.isspace() for c in key):
        return False
    return normalize_key(key) in CREDENTIAL_KEYS or bool(PREFIXED_CREDENTIAL.match(key.lower()))


def _header_value(match: re.Match[str]) -> tuple[str, int, int]:
    for group in ("dq", "sq", "val"):
        if match.group(group) is not None:
            return match.group(group), match.start(group), match.end(group)
    return "", match.end(), match.end()


def _header_masked(value: str) -> bool:
    """A header value already masked (anything after the mask is the JSON or prose around it;
    no credential starts with the mask)."""
    stripped = value.strip()
    return not stripped or stripped.startswith("<redacted>")


def mask_assignments(text: str, policy: KeyPolicy | None = None) -> str:
    """Whole authorization header values, then the values of sensitive keys assigned in
    prose, replaced by the mask."""
    sensitive = policy or _sensitive

    headers = [
        (start, end)
        for m in AUTH_HEADER.finditer(text)
        for value, start, end in [_header_value(m)]
        if not _header_masked(value)
    ]
    for start, end in sorted(headers, reverse=True):
        text = text[:start] + "<redacted>" + text[end:]
    spans = [
        (start, end)
        for key, start, end, value in assignments(text)
        if sensitive(key) and value and value not in MASKED
    ]
    for start, end in sorted(set(spans), reverse=True):
        text = text[:start] + "<redacted>" + text[end:]
    return text


def embedded_json(text: str) -> list[tuple[int, int, Any]]:
    """JSON objects embedded in prose: (start, end, parsed) for every ``{...}`` that parses."""
    decoder = json.JSONDecoder()
    found: list[tuple[int, int, Any]] = []
    index = 0
    while True:
        start = text.find("{", index)
        if start < 0:
            return found
        try:
            obj, end = decoder.raw_decode(text, start)
        except ValueError:
            index = start + 1
            continue
        if isinstance(obj, dict):
            found.append((start, end, obj))
            index = end
        else:
            index = start + 1


def findings_at(text: str, policy: KeyPolicy | None = None) -> list[tuple[int, str]]:
    """(offset, what) for every unmasked authorization header, sensitive assignment and
    embedded JSON object with an unmasked sensitive key in a text."""
    sensitive = policy or _sensitive
    found = [
        (m.start(), f"{m.group('key')} header")
        for m in AUTH_HEADER.finditer(text)
        if not _header_masked(_header_value(m)[0])
    ]
    found += [
        (start, f"{key}=")
        for key, start, _end, value in assignments(text)
        if sensitive(key) and value and value not in MASKED
    ]
    for start, _end, obj in embedded_json(text):
        found += [(start, p) for p in unmasked_sensitive(obj, None, "<json>", sensitive)]
    return sorted(found)


def text_findings(text: str, path: str = "$", policy: KeyPolicy | None = None) -> list[str]:
    """Sensitive assignments, authorization headers and embedded JSON with unmasked
    sensitive keys in one string."""
    return [f"{path}<{what}>" for _offset, what in findings_at(text, policy)]


def unmasked_sensitive(
    value: Any, key: str | None = None, path: str = "$", policy: KeyPolicy | None = None
) -> list[str]:
    """Paths of sensitive keys that still hold a value other than the mask (any non null,
    non mask value: numbers, lists and objects included), walking JSON, JSON held inside
    strings and sensitive assignments in prose (the independent check of committed JSON)."""
    sensitive = policy or _sensitive
    found: list[str] = []
    if sensitive(key) and value is not None:
        if isinstance(value, str) and value in MASKED:
            return []
        return [path]
    if isinstance(value, dict):
        for k, v in value.items():
            found += unmasked_sensitive(v, str(k), f"{path}.{k}", sensitive)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            found += unmasked_sensitive(v, key, f"{path}[{i}]", sensitive)
    elif isinstance(value, str):
        inner: Any = None
        if value.strip()[:1] in ("{", "["):
            try:
                inner = json.loads(value.strip())
            except json.JSONDecodeError:
                inner = None
        if isinstance(inner, dict | list):
            found += unmasked_sensitive(inner, None, f"{path}<json>", sensitive)
        else:
            found += text_findings(value, path, sensitive)
    return found


def bearer_like(token: str) -> bool:
    """One rule for masking and checking free text: a ``Bearer`` value with no spaces that has
    8 or more characters with a digit or symbol, or 12 or more characters of mixed case with at
    least two uppercase letters after the first character and at least two lowercase letters
    (a title case word such as "Certificates", "Bonds" or "instruments" is prose)."""
    if len(token) >= 8 and re.search(r"[0-9._~+/=-]", token):
        return True
    inner_upper = sum(1 for c in token[1:] if c.isupper())
    lower = sum(1 for c in token if c.islower())
    return len(token) >= 12 and inner_upper >= 2 and lower >= 2


def credential_tokens(text: str) -> list[tuple[int, int, str]]:
    """(start, end, kind) of credentials in any text: a ``Bearer`` value that ``bearer_like``
    accepts and a ``Basic`` value that is base64 of ``user:password``."""
    found: list[tuple[int, int, str]] = []
    for m in re.finditer(r"\bBearer\s+([A-Za-z0-9._~+/=-]+)", text, re.IGNORECASE):
        token = m.group(1).rstrip(".")
        if bearer_like(token):
            found.append((m.start(1), m.start(1) + len(token), "bearer"))
    for m in re.finditer(r"\bBasic\s+([A-Za-z0-9+/]{8,}={0,2})", text, re.IGNORECASE):
        try:
            decoded = base64.b64decode(m.group(1), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        if ":" in decoded and decoded.isprintable():
            found.append((m.start(1), m.end(1), "basic"))
    return found


def _mask_spans(text: str, spans: Sequence[tuple[int, int, str]]) -> str:
    for start, end, _kind in sorted(spans, reverse=True):
        text = text[:start] + "<redacted>" + text[end:]
    return text


CHECK_PATTERNS = tuple(p for p in ("jwt", "sk key", "ak key", "mst key", "token query"))


def scrub_free_text(text: str) -> str:
    """Credentials masked in generated free text (answers, CSV cells, Markdown): token and key
    patterns, bearer and basic credentials, authorization headers, credential and token key
    assignments and such keys in embedded JSON. Emails and identifiers stay (filings quote
    investor relations contacts); links are handled by ``redact_links``."""
    text = _mask_spans(text, credential_tokens(text))
    for name, pattern, replacement in SECRET_PATTERNS:
        if name in CHECK_PATTERNS:
            text = pattern.sub(replacement, text)
    pieces: list[str] = []
    index = 0
    for start, end, obj in embedded_json(text):
        if unmasked_sensitive(obj, None, "$", _credential):
            pieces.append(text[index:start])
            pieces.append(json.dumps(_mask_keys(obj, _credential), ensure_ascii=False))
            index = end
    pieces.append(text[index:])
    return mask_assignments("".join(pieces), _credential)


def _mask_keys(value: Any, policy: KeyPolicy, key: str | None = None) -> Any:
    if policy(key) and value is not None:
        return "<redacted>"
    if isinstance(value, dict):
        return {k: _mask_keys(v, policy, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_keys(v, policy, key) for v in value]
    if isinstance(value, str):
        return scrub_free_text(value)
    return value


def _json_documents(path: Path, text: str) -> list[Any]:
    """The JSON a committed file holds: one document per line for JSONL (gzip logs), the
    whole file for ``.json``; nothing for other files."""
    name = path.name
    if name.endswith((".jsonl", ".jsonl.gz")):
        docs = []
        for line in text.splitlines():
            try:
                docs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return docs
    if name.endswith(".json"):
        try:
            return [json.loads(text)]
        except json.JSONDecodeError:
            return []
    return []


def mask_text(text: str) -> str:
    """Secret patterns, bearer and basic credentials, authorization headers, sensitive
    ``key=value`` / ``key: value`` / ``"key": value`` assignments, links (``redact_links``)
    and emails masked in one string."""
    text = _mask_spans(text, credential_tokens(text))
    for _name, pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    text = mask_assignments(text)
    return EMAIL.sub("<email>", redact_links(text))


def _scrub_embedded(text: str, clipped: list[int]) -> str:
    """JSON objects inside prose (``HTTP response body: {...}``) scrubbed in place."""
    pieces: list[str] = []
    index = 0
    for start, end, obj in embedded_json(text):
        pieces.append(text[index:start])
        pieces.append(json.dumps(_scrub(obj, None, clipped), ensure_ascii=False))
        index = end
    pieces.append(text[index:])
    return "".join(pieces)


def _scrub(value: Any, key: str | None, clipped: list[int]) -> Any:
    if _sensitive(key) and value is not None:
        return "<redacted>"
    if isinstance(value, dict):
        return {k: _scrub(v, str(k), clipped) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, key, clipped) for v in value]
    if not isinstance(value, str):
        return value
    text = value
    stripped = value.strip()
    if stripped[:1] in ("{", "["):
        # A string that is itself JSON (a tool result, an encoded payload): scrub inside it.
        try:
            inner = json.loads(stripped)
        except json.JSONDecodeError:
            inner = None
        if isinstance(inner, dict | list):
            text = json.dumps(_scrub(inner, None, clipped), ensure_ascii=False)
        else:
            text = _scrub_embedded(value, clipped)
    else:
        text = _scrub_embedded(value, clipped)
    text = mask_text(text)
    if len(value) > CLIP_CHARS:
        clipped.append(1)
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        text = f"{text[:CLIP_CHARS]}[clipped: original {len(value)} chars, sha256 {digest}]"
    return text


def scrub_log(raw: bytes) -> tuple[bytes, int]:
    """A raw JSONL log made committable: secrets and the values of sensitive keys (tokens,
    keys, cookies, sessions, account and user ids, emails; numbers too) masked, also inside
    strings that are JSON themselves; links redacted; every string over 4,000 characters
    clipped with its original length and sha256; gzip without a timestamp, so the same input
    gives the same bytes."""
    clipped: list[int] = []
    lines = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            obj: Any = json.loads(line)
        except json.JSONDecodeError:
            obj = {"unparsed_line": line}
        lines.append(json.dumps(_scrub(obj, None, clipped), ensure_ascii=False))
    data = ("\n".join(lines) + "\n").encode("utf-8")
    return gzip.compress(data, compresslevel=9, mtime=0), len(clipped)


def log_source(run_dir: Path, agent: str) -> Path | None:
    """The extraction source of a run: the Codex rollout or the Claude Code stream."""
    if agent == "codex":
        rollout = codex.find_rollout(run_dir / "codex-home")
        if rollout is None and (run_dir / "rollout.jsonl").exists():
            rollout = run_dir / "rollout.jsonl"
        return rollout
    stream = run_dir / "stdout.jsonl"
    return stream if stream.exists() else None


def write_report(
    out: Path,
    analysis_text: str,
    analysis: dict[str, Any],
    rows: Sequence[dict[str, Any]],
    records: Sequence[dict[str, Any]],
    data: dict[str, Any],
    evidence: Path,
) -> list[str]:
    """Write the results folder; return the problems found by the final checks (an upstream
    filing host in any written file, or a file over the commit size limit)."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "logs").mkdir(exist_ok=True)
    written: list[Path] = []
    answers: dict[str, str] = {}
    logs: dict[str, str] = {}
    manifest: list[dict[str, Any]] = []
    for rec in sorted(records, key=lambda r: str(r["run_id"])):
        run_id = str(rec["run_id"])
        run_dir = evidence / "runs" / run_id
        text = answer_text(run_dir)
        if text:
            answers[run_id] = write_answer(out, run_id, text)
            written.append(out / answers[run_id])
        source = log_source(run_dir, str(rec["agent"]))
        if source is None:
            continue
        raw = source.read_bytes()
        packed, clipped = scrub_log(raw)
        rel = f"logs/{run_id}.jsonl.gz"
        (out / rel).write_bytes(packed)
        written.append(out / rel)
        logs[run_id] = rel
        manifest.append(
            {
                "run_id": run_id,
                "source": source.relative_to(run_dir).as_posix(),
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "log_file": rel,
                "clipped_strings": clipped,
            }
        )
    by_run = {str(r["run_id"]): r for r in rows}
    files = {
        "runs.csv": _csv(RUN_COLUMNS, run_rows(records, by_run, answers, logs)),
        "cells.csv": _csv(CELL_COLUMNS, cell_rows(rows, records)),
        "comparisons.csv": _csv(COMPARISON_COLUMNS, comparison_rows(analysis)),
        "multi_period_tokens_by_filings.csv": _csv(
            MULTI_PERIOD_COLUMNS, multi_period_rows(rows, data)
        ),
        "analysis.json": tidy(analysis_text),
        "logs/manifest.csv": _csv(MANIFEST_COLUMNS, manifest),
        "plan-probes.csv": _csv(PROBE_COLUMNS, plan_probe_rows(evidence)),
    }
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8")
        written.append(out / name)
    # Every file of the final folder, not only the ones written now: audit shards edited by the
    # auditors and files kept from earlier runs are committed too.
    return check_files([p for p in out.rglob("*") if p.is_file()])


def compact_json(data: Any, indent: int = 0) -> str:
    """Committed JSON that stays small and diffable: objects one key per line, a list of
    objects one compact object per line, everything else compact."""
    pad, inner = " " * indent, " " * (indent + 2)
    if isinstance(data, dict) and data:
        items = [
            f"{inner}{json.dumps(str(k))}: {compact_json(v, indent + 2).lstrip()}"
            for k, v in data.items()
        ]
        return pad + "{\n" + ",\n".join(items) + "\n" + pad + "}"
    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
        items = [inner + json.dumps(x, ensure_ascii=False, separators=(",", ":")) for x in data]
        return pad + "[\n" + ",\n".join(items) + "\n" + pad + "]"
    return pad + json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def csv_text(columns: Sequence[str], rows: Iterable[dict[str, Any]]) -> str:
    """A committed CSV: every cell redacted (links, filing hosts) and credential scrubbed,
    whitespace tidy (``LINK_COLUMNS`` keep their validated Mosaic permalinks)."""
    return _csv(columns, rows)


def shard_csv(
    columns: Sequence[str], rows: Sequence[dict[str, Any]], limit: int = AUDIT_SHARD_BYTES
) -> list[str]:
    """Committed CSV shards of ``rows`` in their given order, each under ``limit`` bytes (like
    the audit queue); deterministic for the same rows. A single row larger than the limit gets
    a shard of its own; no rows give one header only shard."""
    header = len(_csv(columns, []).encode())
    shards: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = header
    for row in rows:
        add = len(_csv(columns, [row]).encode()) - header
        if current and size + add > limit:
            shards.append(current)
            current, size = [], header
        current.append(row)
        size += add
    if current or not shards:
        shards.append(current)
    return [_csv(columns, shard) for shard in shards]


def check_files(paths: Iterable[Path]) -> list[str]:
    """Every committed file, decompressed for gzip logs: upstream filing hosts, credential
    patterns (and emails in logs), the commit size limit, and unmasked sensitive values: JSON
    documents (``.json``, ``.jsonl``, gzip logs) walked with every sensitive key; CSV cell by
    cell and Markdown or other text by line with the credential keys, authorization headers
    and embedded JSON objects. Auditor edited files are reported with file and row, never
    rewritten."""
    problems = []
    for path in sorted(set(paths)):
        data = path.read_bytes()
        if len(data) > MAX_COMMITTED_BYTES:
            problems.append(f"{path}: {len(data)} bytes, over the {MAX_COMMITTED_BYTES} limit")
        is_log = path.suffix == ".gz"
        if is_log:
            data = gzip.decompress(data)
        text = data.decode("utf-8", errors="replace")
        hosts = find_upstream_hosts(text)
        if hosts:
            problems.append(f"{path}: upstream filing host text {', '.join(hosts)}")
        found = [
            name
            for name, pattern, _ in SECRET_PATTERNS
            if name in CHECK_PATTERNS and pattern.search(text)
        ]
        found += sorted({kind for _s, _e, kind in credential_tokens(text)})
        if is_log and EMAIL.search(text):
            found.append("email")
        if found:
            problems.append(f"{path}: secret like text ({', '.join(found)})")
        name = path.name
        if name.endswith((".json", ".jsonl", ".jsonl.gz")):
            keys = [p for doc in _json_documents(path, text) for p in unmasked_sensitive(doc)]
            if keys:
                problems.append(f"{path}: unmasked sensitive keys {', '.join(keys[:5])}")
        elif name.endswith(".csv"):
            cells = []
            for row_number, row in enumerate(csv.reader(io.StringIO(text)), start=1):
                for column, cell in enumerate(row, start=1):
                    for _offset, what in findings_at(cell, _credential):
                        cells.append(f"row {row_number} column {column} ({what})")
            if cells:
                problems.append(f"{path}: unmasked sensitive values at {', '.join(cells[:5])}")
        else:
            if name.endswith(".md"):
                # Markdown cell and line breaks end a value: ``<br>`` and ``|`` become
                # same length boundaries, so offsets and line numbers stay right.
                text = text.replace("<br>", "\x00" * 4).replace("|", "\x00")
            lines = [
                f"line {text.count(chr(10), 0, offset) + 1} ({what})"
                for offset, what in findings_at(text, _credential)
            ]
            if lines:
                problems.append(f"{path}: unmasked sensitive values at {', '.join(lines[:5])}")
    return problems
