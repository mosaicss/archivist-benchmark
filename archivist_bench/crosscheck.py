"""The forum and aggregator cross check (pre registration section 14).

Pool every forum and aggregator source text the agents saw (shown snippets and opened page
blocks), deduplicated by normalized text hash; pass X extracts claims about named companies;
claims are deduplicated on normalized company, metric, value and period; a seeded sample
stratified by source category and question stratum goes to pass C, a Codex agent with the
archivist arm configuration (Archivist only, no web) and an output schema, which bins each
claim against the filings. ``audit_rows`` draws the human audit sample (blank auditor columns).

Rates are reported in both directions (confirmed beside contradicted) with Wilson intervals and
the scope "content search ranked for these questions". Nothing here decides a hypothesis.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import random
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import codex, grounding, judge, probe
from .model import BOOTSTRAP_SEED, DEFAULT_CONFIGS, AgentConfig, stops_batch
from .questions import PERMALINK_RE
from .runner import (
    ArchivistGate,
    ProcessExecutor,
    ProcessRequest,
    child_env,
    subprocess_executor,
)

CROSSCHECK_DIR = "crosscheck"
POOL_FILE = "pool.json"
EXTRACT_FILE = "extract.json"
CLAIMS_FILE = "claims.json"
SAMPLE_FILE = "sample.json"
CHECKS_DIR = "checks"
POOL_TEXT_CHARS = 8000
SAMPLE_SIZE = 400
MIN_PER_CELL = 10
AUDIT_CONTRADICTED = 30
AUDIT_CONFIRMED = 10
SCOPE = "content search ranked for these questions"


def _digest(text: str, n: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


# --- pool ----------------------------------------------------------------------------------------


def source_texts(source: grounding.Source) -> list[str]:
    """The texts of one source: its exec output blocks, else its search snippet."""
    texts = [str(text) for _order, text in source.segments if str(text).strip()]
    if not texts and source.snippet.strip():
        texts = [source.snippet]
    return list(dict.fromkeys(texts))


def build_pool(
    out: Path, run_ids: Sequence[str], table: grounding.HostTable
) -> list[dict[str, Any]]:
    """Every forum and aggregator source text of the given runs, split into windows of at
    most 8,000 characters overlapping by 1,000 (nothing clipped); each window is a pool entry with its
    ``window`` index and ``parent_text_id``, deduplicated by the hash of its folded text, and
    keeps the first run's stratum (run id order), every question id and company it came with,
    its category and host (state only, never in a prompt)."""
    pool: dict[str, dict[str, Any]] = {}
    for run_id in sorted(run_ids):
        data = grounding.complete_sources(out, run_id)
        if data is None:
            raise ValueError(f"{run_id}: no complete sources file (run grounding-sources)")
        for raw in data.get("sources") or []:
            source = grounding.Source.from_dict(raw)
            category = table.category(source.host)
            if category not in grounding.POOL_CATEGORIES:
                continue
            for whole in source_texts(source):
                parent = grounding.fold(whole)
                if not parent:
                    continue
                for index, text in enumerate(text_windows(whole)):
                    folded = grounding.fold(text)
                    if not folded:
                        continue
                    text_id = "T" + _digest(folded)
                    entry = pool.setdefault(
                        text_id,
                        {
                            "text_id": text_id,
                            "parent_text_id": "T" + _digest(parent),
                            "window": index,
                            "category": category,
                            "host": source.host,
                            "kind": source.kind,
                            "stratum": data.get("stratum"),
                            "arm": data.get("arm"),
                            "run_ids": [],
                            "question_ids": [],
                            "companies": [],
                            "text": text,
                        },
                    )
                    _note(entry, run_id, data)
    return [pool[k] for k in sorted(pool)]


POOL_WINDOW_OVERLAP = 1000


def text_windows(
    text: str, size: int = POOL_TEXT_CHARS, overlap: int = POOL_WINDOW_OVERLAP
) -> list[str]:
    """Windows of at most ``size`` characters covering the whole text, each starting
    ``overlap`` characters before the previous one ends, so a claim across a boundary appears
    whole in one window (claims seen twice collapse in ``dedupe_claims``)."""
    if len(text) <= size:
        return [text]
    step = size - overlap
    windows = []
    for start in range(0, len(text), step):
        windows.append(text[start : start + size])
        if start + size >= len(text):
            break
    return windows


def _note(entry: dict[str, Any], run_id: str, data: dict[str, Any]) -> None:
    if run_id not in entry["run_ids"]:
        entry["run_ids"].append(run_id)
    if data.get("question_id") not in entry["question_ids"]:
        entry["question_ids"].append(data.get("question_id"))
    for company in data.get("companies") or []:
        if company not in entry["companies"]:
            entry["companies"].append(company)


# --- claims --------------------------------------------------------------------------------------


def claim_key(claim: dict[str, Any]) -> str:
    """Normalized company, metric, value (folded, spaces removed) and period."""
    value = re.sub(r"\s+", "", grounding.fold(str(claim.get("value") or "")))
    parts = [
        grounding.fold(str(claim.get("company") or "")),
        grounding.fold(str(claim.get("metric") or "")),
        value,
        grounding.fold(str(claim.get("period") or "")),
    ]
    return "\x1f".join(parts)


TEXT_SAMPLE_FILE = "text-sample.json"
TEXT_SAMPLE_SIZE = 1500
TEXT_SAMPLE_FLOOR = 60


def text_sample(
    pool: Sequence[dict[str, Any]],
    size: int = TEXT_SAMPLE_SIZE,
    seed: int = BOOTSTRAP_SEED,
    floor: int = TEXT_SAMPLE_FLOOR,
) -> dict[str, Any]:
    """Deviation G3: the seeded stratified sample of pool texts pass X extracts. Cells (source
    category, question stratum) are allocated by ``allocate`` with at least ``floor`` per
    nonempty cell (all of a smaller cell), drawn with one ``random.Random(seed)`` over the
    cells in sorted order, each cell's texts in text id order. Deterministic."""
    cells: dict[tuple[str, str], list[str]] = defaultdict(list)
    for entry in sorted(pool, key=lambda e: str(e["text_id"])):
        cells[(str(entry["category"]), str(entry["stratum"]))].append(str(entry["text_id"]))
    alloc = allocate({k: len(v) for k, v in cells.items()}, size, floor)
    rng = random.Random(seed)
    chosen: list[str] = []
    for cell in sorted(cells):
        k = alloc.get(cell, 0)
        chosen += rng.sample(cells[cell], k) if k < len(cells[cell]) else list(cells[cell])
    return {
        "seed": seed,
        "size": size,
        "floor": floor,
        "pool_texts": len(pool),
        "allocation": {f"{c[0]}|{c[1]}": n for c, n in sorted(alloc.items())},
        "text_ids": sorted(chosen),
    }


def dedupe_claims(
    pool: Sequence[dict[str, Any]],
    extracted: dict[str, dict[str, Any]],
    sampled: Collection[str] | None = None,
) -> list[dict[str, Any]]:
    """Every pass X claim of the pool (of the sampled texts only when ``sampled`` is given,
    deviation G3: an extraction stored for a text outside the sample is ignored),
    deduplicated on ``claim_key``; the first occurrence (pool text id order, then claim order)
    is kept with its text's category and stratum and the number of occurrences."""
    by_key: dict[str, dict[str, Any]] = {}
    for entry in pool:
        if sampled is not None and entry["text_id"] not in sampled:
            continue
        result = extracted.get(entry["text_id"]) or {}
        if result.get("status") != "ok":
            continue
        for claim in result.get("claims") or []:
            key = claim_key(claim)
            if key in by_key:
                by_key[key]["occurrences"] += 1
                continue
            by_key[key] = {
                "claim_id": "C" + _digest(key),
                "category": entry["category"],
                "stratum": entry["stratum"],
                "text_id": entry["text_id"],
                "companies": entry["companies"],
                "occurrences": 1,
                **{
                    k: claim[k]
                    for k in ("company", "metric", "value", "period", "stated_date", "kind")
                },
            }
    return sorted(by_key.values(), key=lambda c: c["claim_id"])


def allocate(
    sizes: dict[tuple[str, str], int], total: int, floor: int = MIN_PER_CELL
) -> dict[tuple[str, str], int]:
    """Proportional allocation of ``total`` over cells with at least ``floor`` per nonempty
    cell (all of a smaller cell): cells whose proportional share falls below their floor are
    fixed at it, the rest is shared proportionally again until stable, then rounded by largest
    remainder within each cell's size. When the floors alone exceed ``total`` (never at 400
    over the pre registered cells) the allocation is proportional only. Deterministic."""
    cells = {k: n for k, n in sizes.items() if n > 0}
    if sum(cells.values()) <= total:
        return dict(cells)
    if sum(min(floor, n) for n in cells.values()) > total:
        floor = 0  # the floors alone exceed the sample: proportional only
    fixed: dict[tuple[str, str], int] = {}
    while True:
        free = {k: n for k, n in cells.items() if k not in fixed}
        left = total - sum(fixed.values())
        pool = sum(free.values())
        newly = {k: min(floor, n) for k, n in free.items() if left * n / pool < min(floor, n)}
        if not newly:
            break
        fixed.update(newly)
    free = {k: n for k, n in cells.items() if k not in fixed}
    left = total - sum(fixed.values())
    pool = sum(free.values())
    shares = {k: left * n / pool for k, n in free.items()} if pool else {}
    alloc = {k: min(int(v), free[k]) for k, v in shares.items()}
    order = sorted(shares, key=lambda k: (-(shares[k] - int(shares[k])), k))
    remaining = left - sum(alloc.values())
    while remaining > 0 and order:
        progressed = False
        for k in order:
            if remaining and alloc[k] < free[k]:
                alloc[k] += 1
                remaining -= 1
                progressed = True
        if not progressed:
            break
    return {**fixed, **alloc}


def stratified_sample(
    claims: Sequence[dict[str, Any]],
    size: int = SAMPLE_SIZE,
    seed: int = BOOTSTRAP_SEED,
    prefix: str = "X",
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """The seeded sample: cells (category, stratum) allocated by ``allocate``, drawn with one
    ``random.Random(seed)`` over the cells in sorted order (each cell's claims in claim id
    order); rows numbered X001.. (``prefix``) in cell then claim id order. Returns rows and
    allocation."""
    cells: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for claim in sorted(claims, key=lambda c: c["claim_id"]):
        cells[(str(claim["category"]), str(claim["stratum"]))].append(claim)
    alloc = allocate({k: len(v) for k, v in cells.items()}, size)
    rng = random.Random(seed)
    rows: list[dict[str, Any]] = []
    for cell in sorted(cells):
        k = alloc.get(cell, 0)
        chosen = rng.sample(cells[cell], k) if k < len(cells[cell]) else list(cells[cell])
        rows += sorted(chosen, key=lambda c: c["claim_id"])
    numbered = [dict(row, row_id=f"{prefix}{n:03d}") for n, row in enumerate(rows, start=1)]
    return numbered, {f"{c[0]}|{c[1]}": n for c, n in sorted(alloc.items())}


EXTENSION_FILE = "sample-extension.json"
EXTENSION_SIZE = 300
EXTENSION_SEED = 8106
EXTENSION_KIND = "reported_figure"
EXTENSION_LABEL = "extension, trap sourcing only"


def extension_sample(
    claims: Sequence[dict[str, Any]],
    sampled: Collection[str],
    size: int = EXTENSION_SIZE,
    seed: int = EXTENSION_SEED,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Deviation G4: a seeded extension sample for trap sourcing only, drawn from the
    deduplicated claims of kind ``reported_figure`` that are not in the 400 claim sample
    (``sampled`` claim ids), stratified like 14.4 (at least 10 per nonempty cell); rows
    E001.. Its rows never enter the 14.4 rates, subtypes, merit counts or audit sample."""
    pool = [c for c in claims if c.get("kind") == EXTENSION_KIND and c["claim_id"] not in sampled]
    return stratified_sample(pool, size, seed, prefix="E")


def extension_bins(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The extension's separate bin count table (no rates): per source category and over
    both, the count of each bin plus invalid, grade error and unchecked rows."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row["category"])].append(row)
        groups["all"].append(row)
    out = []
    for category, items in sorted(groups.items()):
        valid = [r for r in items if r.get("status") == "ok"]
        out.append(
            {
                "label": EXTENSION_LABEL,
                "category": category,
                "rows": len(items),
                **{name: sum(1 for r in valid if r.get("bin") == name) for name in BINS},
                "invalid": sum(1 for r in items if r.get("status") == "invalid"),
                "grade_error": sum(1 for r in items if r.get("status") == "grade_error"),
                "unchecked": sum(1 for r in items if not r.get("status")),
            }
        )
    return out


# --- pass C --------------------------------------------------------------------------------------

BINS = ("confirmed", "contradicted", "not_checkable", "off_target")
SUBTYPES: dict[str, tuple[str, ...]] = {
    "confirmed": ("none",),
    "contradicted": (
        "wrong_number",
        "stale_period",
        "adjusted_mixed_with_reported",
        "wrong_company",
    ),
    "not_checkable": ("opinion", "forecast", "consensus_estimate", "rumor"),
    "off_target": ("none",),
}
MERITS = ("later_confirmed", "later_refuted", "not_settled", "not_applicable")
ALL_SUBTYPES = tuple(dict.fromkeys(s for v in SUBTYPES.values() for s in v))

PASS_C_RUBRIC = """Check one claim taken from a {category} web page against the company's filings, using your research tools (the filings). Do not run shell commands.
Claim: company {company}; metric {metric}; value {value}; period {period}; stated date {stated_date}; kind {kind}.
Give "bin":
- "confirmed": the filings state the same value for the same company, metric and period (equivalent formatting and rounding count as the same).
- "contradicted" with "subtype" "wrong_number" (the filings give a different value), "stale_period" (the value is the filings' figure for another period), "adjusted_mixed_with_reported" (an adjusted or non GAAP figure presented as the reported one, or the reverse) or "wrong_company" (the figure belongs to another company or entity).
- "not_checkable" with "subtype" "opinion", "forecast", "consensus_estimate" or "rumor": the claim is not a statement of a filed fact.
- "off_target": the claim is not about a covered issuer, or the filings in the corpus cannot settle it.
Use "subtype" "none" for confirmed and off_target. For confirmed and contradicted give the Mosaic "permalink" of the passage you relied on, its "exchange_document_id" (or "document_id_absent_reason" when none is stored) and an exact "quote" from that passage; leave them "" otherwise. Set "restatement" true with a "restatement_note" when a later filing restated the figure. For a claim with a stated date, when a filing after that date settles it, give "merit" "later_confirmed" or "later_refuted", or "not_settled" when no later filing settles it; give "not_applicable" for a claim without a stated date.
Return only the JSON object of the output schema."""

PASS_C_SCHEMA: dict[str, Any] = grounding._strict_object(
    {
        "bin": {"type": "string", "enum": list(BINS)},
        "subtype": {"type": "string", "enum": list(ALL_SUBTYPES)},
        "permalink": grounding.STRING,
        "exchange_document_id": grounding.STRING,
        "document_id_absent_reason": grounding.STRING,
        "quote": grounding.STRING,
        "restatement": {"type": "boolean"},
        "restatement_note": grounding.STRING,
        "merit": {"type": "string", "enum": list(MERITS)},
    }
)


def pass_c_prompt(row: dict[str, Any]) -> str:
    """The pass C prompt: the claim, its source category and stated date (never the host or
    URL of its source)."""
    return grounding.safe_prompt(
        PASS_C_RUBRIC.format(
            category=row["category"],
            company=row["company"],
            metric=row["metric"],
            value=row["value"],
            period=row["period"] or "none",
            stated_date=row["stated_date"] or "none",
            kind=row["kind"],
        )
    )


def parse_pass_c(text: str, claim: dict[str, Any] | None = None) -> dict[str, Any]:
    """A pass C verdict: known bin, a subtype of that bin, merit, boolean restatement (true
    needs a note); for confirmed and contradicted a Mosaic passage permalink, a quote and an
    exchange document id or its absence reason. Given the sampled ``claim``: a stated date
    needs merit ``later_confirmed``, ``later_refuted`` or ``not_settled``, no stated date
    needs ``not_applicable``."""
    data = grounding._loads(text)
    if not isinstance(data, dict):
        raise ValueError("verdict is not an object")
    row = {
        name: grounding._str(data.get(name), name).strip()
        for name in (
            "bin",
            "subtype",
            "permalink",
            "exchange_document_id",
            "document_id_absent_reason",
            "quote",
            "restatement_note",
            "merit",
        )
    }
    if row["bin"] not in BINS or row["subtype"] not in SUBTYPES[row["bin"]]:
        raise ValueError(f"bad bin or subtype: {row['bin']}/{row['subtype']}")
    if row["merit"] not in MERITS:
        raise ValueError(f"bad merit {row['merit']!r}")
    if not isinstance(data.get("restatement"), bool):
        raise ValueError("restatement must be a boolean")
    if data["restatement"] and not row["restatement_note"]:
        raise ValueError("a restatement needs a restatement_note")
    if claim is not None:
        dated = bool(str(claim.get("stated_date") or "").strip())
        if dated and row["merit"] == "not_applicable":
            raise ValueError(
                "a dated claim needs merit later_confirmed, later_refuted or not_settled"
            )
        if not dated and row["merit"] != "not_applicable":
            raise ValueError("a claim without a stated date takes merit not_applicable")
    if row["bin"] in ("confirmed", "contradicted"):
        if not PERMALINK_RE.match(row["permalink"]):
            raise ValueError("a confirmed or contradicted claim needs a Mosaic passage permalink")
        if not row["quote"]:
            raise ValueError("a confirmed or contradicted claim needs a quote")
        if not (row["exchange_document_id"] or row["document_id_absent_reason"]):
            raise ValueError("exchange_document_id or document_id_absent_reason required")
    return {**row, "restatement": bool(data["restatement"])}


class IsolationError(judge.JudgeCallError):
    """A checker run that used the web or a tool other than Archivist (``invalid:
    arm_isolation``); rerun once."""

    def __init__(self, violations: list[str], meta: dict[str, Any]) -> None:
        super().__init__("arm_isolation: " + "; ".join(violations))
        self.violations = violations
        self.meta = meta


def checker_violations(ex: Any) -> list[str]:
    """The archivist arm isolation rule for the checker: any web search, shell, file or
    extension tool, or MCP tool outside Archivist (the Codex MCP resource built ins allowed)
    breaks it; an Archivist server reported failed breaks it. A checker that never called
    Archivist is noted (``archivist_calls`` 0), not invalid."""
    return [
        v for v in codex.isolation_violations("archivist", ex) if "not connected (unseen)" not in v
    ]


class CheckerExecutor:
    """Pass C: one ``codex exec`` per call with the archivist arm configuration (Archivist MCP
    only, no web search, read only sandbox, no shell) plus ``--output-schema``, in a fresh
    Codex home under the call directory (kept: its rollout is the evidence). Before launch:
    the ChatGPT login check, the plan gate probe and the Archivist quota gate; after: the
    rollout's isolation and Archivist call count. Plan and quota stops raise
    ``judge.JudgeInfraError``."""

    def __init__(
        self,
        log_dir: Path,
        gate: probe.PlanGate,
        archivist_gate: ArchivistGate | None,
        process: ProcessExecutor = subprocess_executor,
        parent_env: dict[str, str] | None = None,
        codex_auth: Path | None = None,
        plan_probe: Callable[[dict[str, str], Path], dict[str, Any]] | None = None,
        cfg: AgentConfig | None = None,
    ) -> None:
        self.log_dir = log_dir
        self.gate = gate
        self.archivist_gate = archivist_gate
        self.process = process
        self.parent_env = dict(os.environ) if parent_env is None else parent_env
        self.codex_auth = codex_auth or Path.home() / ".codex" / "auth.json"
        self.plan_probe = plan_probe
        self.cfg = cfg or DEFAULT_CONFIGS["codex"]
        if self.cfg.service_tier == "fast" or self.cfg.auth_mode != "chatgpt":
            raise ValueError("the checker runs on the ChatGPT plan, default tier only")
        self.dispatched = 0
        self.count = 0

    def begin(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self.count = 0

    def closing_probe(self, log_dir: Path) -> dict[str, Any]:
        return judge.CliExecutor(
            judge.JUDGES["B"],
            log_dir,
            parent_env=self.parent_env,
            codex_auth=self.codex_auth,
            plan_probe=self.plan_probe,
            gate=self.gate,
        ).closing_probe(log_dir)

    def __call__(self, prompt: str, schema: dict[str, Any] | None = None) -> judge.Reply:
        self.count += 1
        while (self.log_dir / f"C-{self.count}").exists():
            self.count += 1
        call_dir = self.log_dir / f"C-{self.count}"
        call_dir.mkdir(parents=True)
        cwd, home = call_dir / "cwd", call_dir / "codex-home"
        cwd.mkdir()
        judge.check_chatgpt_login(self.codex_auth)
        codex.prepare_home(home, "archivist", self.cfg, cwd, self.codex_auth)
        schema_path = call_dir / "schema.json"
        schema_path.write_text(json.dumps(schema or PASS_C_SCHEMA), encoding="utf-8")
        last = call_dir / "last-message.json"
        argv = codex.command(self.cfg, prompt, cwd, last)
        argv[-1:-1] = ["--output-schema", str(schema_path)]
        env = child_env(self.parent_env, codex.env_extras(home, self.cfg, self.parent_env))
        plan = (self.plan_probe or probe.app_server_probe)(env, cwd)
        (call_dir / "plan-probe.json").write_text(json.dumps(plan, indent=2), "utf-8")
        refused = self.gate.check_codex(plan)
        if refused:
            raise judge.JudgeInfraError("plan probe refused the call: " + "; ".join(refused))
        if self.archivist_gate is not None:
            blocked = self.archivist_gate.check()
            if blocked:
                raise judge.JudgeInfraError("archivist quota gate: " + "; ".join(blocked))
        shown = [a if a != prompt else "<prompt>" for a in argv]
        (call_dir / "command.json").write_text(
            json.dumps({"argv": shown, "env_keys": sorted(env)}, indent=2), encoding="utf-8"
        )
        (call_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        self.dispatched += 1
        stdout, stderr = call_dir / "stdout.jsonl", call_dir / "stderr.log"
        done = self.process(
            ProcessRequest(argv, env, cwd, None, stdout, stderr, float(self.cfg.timeout_s))
        )
        if done.timed_out:
            raise judge.JudgeCallError(f"checker timeout after {self.cfg.timeout_s} s")
        rollout = codex.find_rollout(home)
        try:
            if rollout is None:
                raise OSError("no rollout in the checker's Codex home")
            lines = rollout.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            # Without the rollout neither isolation nor Archivist use can be shown.
            raise judge.JudgeCallError(f"checker evidence missing: {exc}") from exc
        ex = codex.parse_rollout(lines)
        out_lines = stdout.read_text(encoding="utf-8").splitlines() if stdout.exists() else []
        codex.parse_exec_stdout(out_lines, ex)
        codex.parse_stderr(stderr.read_text(errors="replace") if stderr.exists() else "", ex)
        if ex.infra_reason:
            if stops_batch(ex.infra_reason):
                raise judge.JudgeInfraError(f"checker stop: {ex.infra_reason}")
            raise judge.JudgeCallError(f"checker infra: {ex.infra_reason}")
        finished = any(
            r.get("type") == "event_msg" and (r.get("payload") or {}).get("type") == "task_complete"
            for r in codex._json_lines(lines)
        )
        if not lines or ex.incomplete_reason or not finished:
            why = (
                "empty rollout"
                if not lines
                else ex.incomplete_reason or "no task_complete in the rollout"
            )
            raise judge.JudgeCallError(f"checker evidence incomplete: {why}")
        calls = sum(v for k, v in ex.tool_calls.items() if k.startswith("mcp:archivist."))
        meta = {
            "log": str(call_dir),
            "rollout": str(rollout) if rollout else None,
            "archivist_calls": calls,
            "tool_calls": ex.tool_calls,
            "web_actions": ex.web_actions,
            "model": ex.model,
            "effort": ex.effort,
            "cli_version": ex.cli_version,
            "wall_s": done.wall_s,
            "returncode": done.returncode,
        }
        violations = checker_violations(ex)
        if violations:
            raise IsolationError(violations, meta)
        text = last.read_text(encoding="utf-8") if last.exists() else ex.answer
        if done.returncode not in (0, None) and not text.strip():
            raise judge.JudgeCallError(f"checker exit {done.returncode}")
        return judge.Reply(text=text, usage=ex.usage.as_dict() if ex.usage else {}, meta=meta)


def make_checker(
    log_dir: Path,
    stop_at_window: float,
    max_window_step: float,
    archivist_gate: ArchivistGate,
) -> CheckerExecutor:
    """The live pass C executor (tests replace it)."""
    gate = probe.PlanGate(stop_at_window, max_window_step, allow_claude=False)
    checker = CheckerExecutor(log_dir, gate, archivist_gate)
    judge.check_chatgpt_login(checker.codex_auth)
    return checker


def check_status(record: dict[str, Any] | None) -> str | None:
    return None if not record else str(record.get("status"))


def needs_check(record: dict[str, Any] | None) -> bool:
    """A sampled row runs when it has no result, or a ``grade_error`` from fewer than two
    batches; ``ok`` and ``invalid`` (arm isolation after its one rerun) are final."""
    if not record:
        return True
    return record.get("status") == "grade_error" and int(record.get("batches") or 1) < 2


def checker_config() -> dict[str, Any]:
    cfg = DEFAULT_CONFIGS["codex"]
    return {
        "agent": "codex",
        "arm_config": "archivist",
        "model": cfg.model,
        "effort": cfg.effort,
        "service_tier": cfg.service_tier,
        "auth_mode": cfg.auth_mode,
        "billing_mode": cfg.billing_mode,
        "timeout_s": cfg.timeout_s,
    }


def config_with_timeout(timeout_s: int | None) -> AgentConfig:
    cfg = DEFAULT_CONFIGS["codex"]
    return replace(cfg, timeout_s=timeout_s) if timeout_s else cfg


# --- rates and audit -----------------------------------------------------------------------------


def rate_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bin rates per source category and stratum, per category over every stratum and over
    everything: counts, each bin's share of the valid checked rows with its Wilson 95%
    interval (confirmed and contradicted side by side), the contradicted share of checkable
    rows (confirmed plus contradicted), subtypes, merit of dated claims, restatements, and the
    rows left out (invalid, grade error, unchecked)."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        category, stratum = str(row["category"]), str(row["stratum"])
        for key in ((category, stratum), (category, "all"), ("all", "all")):
            groups[key].append(row)
    out = []
    for (category, stratum), items in sorted(groups.items()):
        valid = [r for r in items if r.get("status") == "ok"]
        n = len(valid)
        bins = Counter(str(r["bin"]) for r in valid)
        entry: dict[str, Any] = {
            "category": category,
            "stratum": stratum,
            "scope": SCOPE,
            "rows": len(items),
            "checked": n,
            "invalid": sum(1 for r in items if r.get("status") == "invalid"),
            "grade_error": sum(1 for r in items if r.get("status") == "grade_error"),
            "unchecked": sum(1 for r in items if not r.get("status")),
        }
        for name in BINS:
            entry[name] = bins[name]
            entry[f"{name}_rate"] = round(bins[name] / n, 4) if n else None
            interval = grounding.wilson(bins[name], n)
            entry[f"{name}_ci95"] = list(interval) if interval else None
        checkable = bins["confirmed"] + bins["contradicted"]
        entry["contradicted_share_of_checkable"] = (
            round(bins["contradicted"] / checkable, 4) if checkable else None
        )
        interval = grounding.wilson(bins["contradicted"], checkable)
        entry["contradicted_share_of_checkable_ci95"] = list(interval) if interval else None
        entry["subtypes"] = dict(
            sorted(Counter(f"{r['bin']}:{r['subtype']}" for r in valid).items())
        )
        dated = [r for r in valid if str(r.get("stated_date") or "").strip()]
        entry["dated"] = len(dated)
        entry["merit"] = dict(sorted(Counter(str(r.get("merit")) for r in dated).items()))
        entry["restatements"] = sum(1 for r in valid if r.get("restatement"))
        out.append(entry)
    return out


AUDIT_COLUMNS = (
    "selection",
    "row_id",
    "claim_id",
    "category",
    "stratum",
    "company",
    "metric",
    "value",
    "period",
    "stated_date",
    "kind",
    "bin",
    "subtype",
    "permalink",
    "exchange_document_id",
    "document_id_absent_reason",
    "quote",
    "merit",
    "restatement",
    "restatement_note",
    "archivist_calls",
    "audit_verdict",
    "audit_note",
)


def audit_rows(rows: Sequence[dict[str, Any]], seed: int = BOOTSTRAP_SEED) -> list[dict[str, Any]]:
    """The human audit sample: a seeded sample of 30 contradicted rows (all when fewer) plus 10
    confirmed rows (all when fewer), row id order, blank auditor columns."""
    rng = random.Random(seed)
    out: list[dict[str, Any]] = []
    for name, k in (("contradicted", AUDIT_CONTRADICTED), ("confirmed", AUDIT_CONFIRMED)):
        pool = sorted(
            (r for r in rows if r.get("status") == "ok" and r.get("bin") == name),
            key=lambda r: str(r["row_id"]),
        )
        chosen = rng.sample(pool, k) if len(pool) > k else pool
        out += [
            dict(r, selection=name, audit_verdict="", audit_note="")
            for r in sorted(chosen, key=lambda r: str(r["row_id"]))
        ]
    return out


def filled_audit(path: Path) -> bool:
    """True when an existing audit CSV holds any auditor input, a verdict or a note (never
    overwritten)."""
    if not path.is_file():
        return False
    with path.open(encoding="utf-8", newline="") as fh:
        return any(
            (r.get("audit_verdict") or "").strip() or (r.get("audit_note") or "").strip()
            for r in csv.DictReader(fh)
        )


def to_csv(
    columns: Sequence[str], rows: Sequence[dict[str, Any]], cell: Callable[[Any], str]
) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(columns), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: cell(row.get(k)) for k in columns})
    return buf.getvalue()
