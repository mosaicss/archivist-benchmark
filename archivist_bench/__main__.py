"""Command line: validate-questions, plan, run, extract, judge, analyze, audit-export, report,
estimate; harness 1.4.0: verify-quotes, grounding-sources, grounding-hosts,
grounding-claims, crosscheck-extract, crosscheck-sample, crosscheck-check, crosscheck-audit,
trap-judge, grounding-analyze, grounding-report; harness 1.5.0: ``--agent`` on the
grounding commands, Claude Code contamination runs, ``analyze --question`` and the cross
campaign judge bias check (``--bias-evidence-dir``); harness 1.5.1: the explicit Claude login
(``--claude-config-dir`` on run, plan and judge) and ``plan-probe``; harness 1.6.0 (amendment
5): ``--key-corrections``, ``rejudge-keys``, ``audit-precheck``, the ``correction_source`` audit
column and ``--contamination-audit``."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import threading
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import replace
from functools import partial
from pathlib import Path
from statistics import fmean
from typing import Any

from . import (
    __version__,
    claude_code,
    cost,
    crosscheck,
    grounding,
    judge,
    precheck,
    probe,
    questions,
    report,
    runner,
    stats,
)
from .model import (
    ARMS,
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    DEFAULT_ARCHIVIST_CEILING,
    DEFAULT_CONFIGS,
    DEFAULT_EVIDENCE_ROOT,
    DEFAULT_STOP_AT_WINDOW,
    EXCLUDED_STATUSES,
    MAX_CONCURRENCY,
    REPETITIONS,
    STATUS_COMPLETED,
    Agent,
    AgentConfig,
    Arm,
    RunRecord,
    RunSpec,
)


def _configs(args: argparse.Namespace) -> dict[Agent, AgentConfig]:
    cfgs = dict(DEFAULT_CONFIGS)
    if args.codex_auth:
        cfgs["codex"] = replace(cfgs["codex"], auth_mode=args.codex_auth)
    if args.claude_auth:
        cfgs["claude_code"] = replace(cfgs["claude_code"], auth_mode=args.claude_auth)
    if args.timeout_s:
        cfgs = {k: replace(v, timeout_s=args.timeout_s) for k, v in cfgs.items()}
    return cfgs


def _specs(args: argparse.Namespace) -> list[RunSpec]:
    data = questions.load(Path(args.questions))
    contamination = questions.is_contamination(data)
    if contamination and args.agent not in ("codex", "claude_code"):
        # Pre registration section 14.5 (Codex) and section 15 (the Claude Code replication):
        # a contamination set runs one agent at a time.
        print(
            "a contamination set runs with one agent: --agent codex or --agent claude_code",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if contamination and args.codex_auth not in (None, "chatgpt"):
        # ... on the ChatGPT plan allowance only (never a metered key).
        print("a contamination set runs on the ChatGPT login only", file=sys.stderr)
        raise SystemExit(2)
    if contamination and args.claude_auth not in (None, "subscription"):
        # ... and Claude Code on the subscription only (never ``--bare`` with a metered key).
        print("a contamination set runs on the Claude subscription only", file=sys.stderr)
        raise SystemExit(2)
    errors = questions.validate(data)
    if errors and not args.allow_invalid_set:
        raise SystemExit("question set invalid; run validate-questions")
    cfgs = _configs(args)
    agents: list[Agent] = ["codex", "claude_code"] if args.agent == "all" else [args.agent]
    arms: list[Arm] = list(ARMS) if not args.arms else [a for a in ARMS if a in args.arms]
    lookup = questions.by_id(data)
    specs: list[RunSpec] = []
    if args.question:
        repeated = sorted({i for i in args.question if args.question.count(i) > 1})
        if repeated:
            raise SystemExit(f"duplicate --question ids: {', '.join(repeated)}")
    for agent in agents:
        if args.question:
            ids = list(args.question)
        elif agent == "claude_code" and contamination:
            ids = questions.claude_contamination_subset(data)  # section 15
        elif agent == "claude_code":
            ids = questions.claude_subset(data)
        else:
            ids = [str(q["id"]) for q in data["questions"]]
        unknown = [i for i in ids if i not in lookup]
        if unknown:
            raise SystemExit(f"unknown question ids: {', '.join(unknown)}")
        specs += runner.build_plan(
            [lookup[i] for i in ids], agent, cfgs[agent], args.repetitions, arms
        )
    return specs


def _key_corrections(
    args: argparse.Namespace, data: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(questions, applied corrections): ``data`` itself and none without ``--key-corrections``;
    else a corrected copy (``questions.apply_key_corrections``, amendment 5) and the
    corrections that applied to its set. An invalid corrections file exits 2, nothing
    applied."""
    path = getattr(args, "key_corrections", None)
    if not path:
        return data, []
    try:
        doc = questions.load_key_corrections(Path(path))
        chosen = questions.key_corrections_for(data, doc)
        corrected = questions.apply_key_corrections(data, doc)
    except questions.CorrectionError as exc:
        print(f"key corrections not applied: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    return corrected, chosen


def _correction_line(data: dict[str, Any], chosen: Sequence[dict[str, Any]]) -> str:
    ids = ", ".join(str(c["fact_id"]) for c in chosen) or "none"
    return (
        f"key corrections: {len(chosen)} applied to the {questions.question_set(data)} set ({ids})"
    )


def cmd_validate(args: argparse.Namespace) -> int:
    data = questions.load(Path(args.questions))
    data, chosen = _key_corrections(args, data)
    if getattr(args, "key_corrections", None):
        print(_correction_line(data, chosen))
    contamination = bool(getattr(args, "contamination", False))
    errors = questions.validate(data, contamination=contamination)
    if getattr(args, "crosscheck", None):
        if not contamination:
            raise SystemExit("--crosscheck needs --contamination")
        state = Path(args.crosscheck)
        rows = {
            str(r.get("row_id")): r
            for name in (crosscheck.SAMPLE_FILE, crosscheck.EXTENSION_FILE)
            for r in (grounding.read_json(_crosscheck_dir(state) / name) or {}).get("rows") or []
        }
        if not rows:
            errors.append(f"no cross check sample in {state}")
        errors += questions.validate_crosscheck_rows(data, _check_results(state), rows)
    for error in errors:
        print(f"VIOLATION {error}")
    qs = data["questions"]
    facts = sum(len(q.get("facts") or []) for q in qs)
    if contamination:
        roles = Counter(str(q.get("role")) for q in qs)
        traps = sum(len(grounding.trap_facts(q)) for q in qs)
        print(
            f"{len(qs)} contamination questions ({roles.get('trap', 0)} trap, "
            f"{roles.get('control', 0)} control), {facts} facts ({traps} trap facts), "
            f"{len(errors)} violations"
        )
        return 1 if errors else 0
    non_us = sum(1 for q in qs if "non_us" in (q.get("tags") or []))
    print(
        f"{len(qs)} questions, {facts} facts, {non_us} tagged non_us, "
        f"{len(errors)} violations; Claude Code subset: {', '.join(questions.claude_subset(data))}"
    )
    return 1 if errors else 0


def _shown_root(args: argparse.Namespace) -> Path:
    """Evidence root for dry run display only; executed runs, judge and analyze need an
    explicit ``--evidence-dir`` (one directory per campaign, never a silent default)."""
    return Path(args.evidence_dir or DEFAULT_EVIDENCE_ROOT)


def _claude_account(args: argparse.Namespace, needed: bool) -> Path | None:
    """The chosen Claude account (``--claude-config-dir``, harness 1.5.1) as an absolute resolved
    directory, or None when the command makes no Claude call (the flag is then ignored). Never
    read from the environment. Exit 2, before anything is prepared, probed or called, when a
    Claude call needs it and it is missing or does not name an existing directory."""
    if not needed:
        return None
    value = getattr(args, "claude_config_dir", None)
    if value is None:
        print(
            "this command makes Claude calls: pass --claude-config-dir DIR (the Claude account "
            "the runs, judge calls and the plan gate probe use; never read from the environment)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        return claude_code.resolve_config_dir(value)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc


def cmd_plan(args: argparse.Namespace) -> int:
    specs = _specs(args)
    print(runner.describe_plan(specs, _shown_root(args) / "runs"))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    specs = _specs(args)
    if not args.execute:
        print(runner.describe_plan(specs, _shown_root(args) / "runs"))
        if args.resume and args.evidence_dir:
            plan = runner.resume_plan(
                specs, runner.campaign_records(Path(args.evidence_dir)), args.rerun_invalid
            )
            _print_resume(plan)
        print("dry run: pass --execute and --max-runs to spawn agents")
        return 0
    if args.rerun_invalid and not args.resume:
        raise SystemExit("--rerun-invalid needs --resume")
    if args.max_runs is None:
        raise SystemExit("--execute needs --max-runs")
    if not args.evidence_dir:
        raise SystemExit("--execute needs an explicit --evidence-dir (one per campaign)")
    if args.concurrency > MAX_CONCURRENCY:
        raise SystemExit(f"--concurrency is capped at {MAX_CONCURRENCY}")
    account = _claude_account(args, any(s.agent == "claude_code" for s in specs))
    keys = [s.run_key for s in specs]
    repeated = sorted({k for k in keys if keys.count(k) > 1})
    if repeated:
        raise SystemExit(f"duplicate planned run keys: {', '.join(repeated)}")
    evidence = Path(args.evidence_dir)
    if args.resume:
        for run_id in runner.stamp_classifications(evidence):
            print(f"kept the recorded invalid classification of {run_id}", file=sys.stderr)
        for record in runner.reconcile_interrupted(evidence):
            print(
                f"reconciled interrupted attempt {record.run_id} as infra_error (interrupted)",
                file=sys.stderr,
            )
    plan = runner.resume_plan(specs, runner.campaign_records(evidence), args.rerun_invalid)
    started = len(plan.done) + len(plan.start_attempts) + len(plan.excluded)
    if started and not args.resume:
        raise SystemExit(
            f"{evidence} already holds records for {started} planned cells; pass --resume to "
            "continue the campaign (a second record for a cell would make analyze refuse it)"
        )
    if args.resume:
        _print_resume(plan)
    pending = plan.pending
    if not pending:
        print("nothing to run: every planned cell has a final record or is excluded")
        return 0
    try:
        budget = runner.Budget(args.max_runs, args.stop_at_window, args.max_window_step)
        gate = runner.ArchivistGate(
            args.archivist_ceiling, env=runner.child_env(dict(os.environ), {})
        )
        records = runner.execute_plan(
            pending,
            evidence,
            args.max_runs,
            args.concurrency,
            budget=budget,
            start_attempts=plan.start_attempts,
            rerun_attempts=plan.rerun_attempts,
            archivist_gate=gate,
            claude_config_dir=account,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    for reading in gate.readings:
        _append_jsonl(evidence / "archivist-usage.jsonl", reading)
    print(runner.ledger_markdown(records))
    if budget.closing is not None:
        reasons = budget.closing["reasons"]
        print(
            "closing probe: " + ("; ".join(reasons) if reasons else "no drop, no hot window"),
            file=sys.stderr,
        )
    recorded = {r.run_key for r in records}
    skipped = [s.run_key for s in pending if s.run_key not in recorded]
    if skipped:
        print(
            f"{len(skipped)} planned runs have no record (attempt ceiling or batch stop): "
            + ", ".join(skipped),
            file=sys.stderr,
        )
    after = runner.resume_plan(pending, runner.campaign_records(evidence))
    if after.excluded:
        print(
            f"{len(after.excluded)} cells excluded after three infra_error attempts: "
            + ", ".join(after.excluded),
            file=sys.stderr,
        )
    return 1 if skipped or budget.stopped_by is not None else 0


def _print_resume(plan: runner.ResumePlan) -> None:
    print(
        f"resume: {len(plan.done)} cells done, {len(plan.pending)} to run "
        f"({len(plan.start_attempts)} continuing after infra_error attempts), "
        f"{len(plan.excluded)} excluded after three infra_error attempts",
        file=sys.stderr,
    )
    for key in plan.reruns:
        print(
            f"rerun once after an invalid record: {key} "
            f"({plan.rerun_attempts.get(key)} attempts left in its sequence)",
            file=sys.stderr,
        )
    for key in plan.excluded:
        why = (
            "invalid, its one rerun sequence used"
            if key in plan.excluded_invalid
            else "three infra_error attempts"
        )
        print(f"excluded ({why}): {key}", file=sys.stderr)


def cmd_plan_probe(args: argparse.Namespace) -> int:
    """Exactly one Claude plan probe call on the chosen account (harness 1.5.1), appended to
    ``<evidence>/plan-probes.jsonl``; prints the reading and the gate's refusal reasons at
    ``--stop-at-window`` (from that reading, no second probe). Exit 0 when the reading was taken
    and admits, 1 when the probe failed or the gate refuses, 2 on invalid arguments. Never an
    agent, judge or Archivist call."""
    account = _claude_account(args, True)
    assert account is not None
    try:
        gate = probe.PlanGate(args.stop_at_window)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    evidence = Path(args.evidence_dir).expanduser()
    evidence.mkdir(parents=True, exist_ok=True)
    gate.configure(
        probe.claude_probe_env(dict(os.environ), account),
        evidence / "plan-probe-scratch",
        evidence / runner.PROBE_LEDGER,
    )
    report = gate.probe_claude()
    reading = report.get("reading") if report.get("ok") else None
    reasons = (
        gate.claude_reasons(reading)
        if isinstance(reading, dict)
        else [f"Claude plan probe failed: {report.get('error')}"]
    )
    row = probe._probe_row("claude_probe", str((reading or {}).get("observed_at") or ""), report)
    out = {
        "ok": row["ok"],
        "error": row["error"],
        "observed_at": (reading or {}).get("observed_at"),
        "status": row["status"],
        "windows": row["windows"],
        "overage_status": row["overage_status"],
        "overage_disabled_reason": row["overage_disabled_reason"],
        "is_using_overage": row["is_using_overage"],
        "claude_config_dir": str(account),
        "stop_at_window": gate.threshold,
        "admitted": bool(report.get("ok")) and not reasons,
        "reasons": reasons,
    }
    print(json.dumps(out, indent=2))
    for reason in reasons:
        print(f"refused: {reason}", file=sys.stderr)
    return 0 if out["admitted"] else 1


def cmd_extract(args: argparse.Namespace) -> int:
    for run_dir in args.run_dir:
        try:
            record = runner.build_record(Path(run_dir))
        except ValueError as exc:
            print(f"not extracted: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(record.as_dict(), indent=2))
    return 0


def _records(evidence: Path) -> list[dict[str, Any]]:
    """Latest record per run directory (re-extracted records win over the ledger copy)."""
    out = []
    for path in sorted((evidence / "runs").glob("*/record.json")):
        out.append(json.loads(path.read_text(encoding="utf-8")))
    return out


MAX_JUDGE_BATCHES = 2  # a judge_error is judged once more in a later batch, then excluded


def _judgement(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / "judgement.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _judge_closing(executors: dict[str, Any], evidence: Path) -> bool:
    """The closing probe of every judge executor that launched a Codex call; True when one
    found a credit drop, a hot window or a window step."""
    failed = False
    for pass_name, ex in executors.items():
        closer = getattr(ex, "closing_probe", None)
        if closer is not None and int(getattr(ex, "dispatched", 0) or 0) > 0:
            stamp = runner._now().replace(":", "")
            log_dir = evidence / "judge-logs" / f"closing-{stamp}-{pass_name}"
            closing = closer(log_dir)
            _append_jsonl(
                evidence / runner.PROBE_LEDGER,
                {"time": closing["probed_at"], "kind": "codex_judge_closing", **closing},
            )
            if closing["reasons"]:
                failed = True
                print(
                    "closing probe after the judge batch: " + "; ".join(closing["reasons"]),
                    file=sys.stderr,
                )
    return failed


def _requested_passes(args: argparse.Namespace) -> tuple[str, ...]:
    chosen = set(getattr(args, "passes", None) or judge.PASSES)
    return tuple(name for name in judge.PASSES if name in chosen)


def cmd_judge(args: argparse.Namespace) -> int:
    data = questions.load(Path(args.questions))
    lookup = questions.by_id(data)
    evidence = Path(args.evidence_dir)
    only = set(args.run_id or [])
    passes = _requested_passes(args)
    if questions.is_contamination(data) and passes != ("B",):
        # Pre registration section 14.5: contamination answers are judged by pass B only.
        print("a contamination set is judged with --passes B only", file=sys.stderr)
        return 2
    if args.force and passes != judge.PASSES:
        # Amendment 2: a single pass run must never discard a stored verdict of that pass.
        print("--force is refused with --passes: valid stored verdicts are kept", file=sys.stderr)
        return 2
    # Fewer than both passes (amendment 2: ``--passes B``) merges the requested passes into the
    # existing judgement; both passes keep the amendment 1 behaviour (a judge_error is judged
    # again by both, a valid judgement is left alone).
    merge = passes != judge.PASSES
    todo = []
    for rec in _records(evidence):
        if only and rec["run_id"] not in only:
            continue
        if rec["status"] != STATUS_COMPLETED or rec.get("contaminated"):
            continue
        q = lookup.get(rec["question_id"])
        if q is None or q.get("stratum") == questions.CONTROL_STRATUM:
            continue
        run_dir = evidence / "runs" / rec["run_id"]
        prior = _judgement(run_dir)
        batches = int(prior.get("batches", 1)) if prior else 0
        if prior is not None:
            if merge:
                missing = any(judge.pass_verdicts(prior, name) is None for name in passes)
                retry = missing or args.force
            else:
                retry = prior.get("status") == "judge_error" or args.force
            if not retry or batches >= MAX_JUDGE_BATCHES:
                continue
        todo.append((rec, q, run_dir, batches, prior))
    calls = len(passes) * len(todo)
    names = ", ".join(
        f"{p} {c.cli} {c.model} {c.effort}" for p, c in judge.JUDGES.items() if p in passes
    )
    print(f"{len(todo)} answers to judge, at least {calls} judge calls ({names})")
    if not args.execute:
        print("dry run: pass --execute and --max-calls to call the judges")
        return 0
    account = _claude_account(args, "A" in passes)
    if args.max_calls is None or calls > args.max_calls:
        raise SystemExit(f"judge needs --max-calls of at least {calls} (retries may add up to 2x)")
    ledger = evidence / "judge-ledger.jsonl"
    try:
        # Pass A runs on the chosen account; a Codex only batch passes none (unchanged call).
        logs = evidence / "judge-logs"
        if merge and account is None:
            executors = judge.make_executors(logs, passes=passes)
        elif merge:
            executors = judge.make_executors(logs, passes=passes, claude_config_dir=account)
        else:
            executors = judge.make_executors(logs, claude_config_dir=account)
    except judge.JudgeLoginError as exc:
        raise SystemExit(f"judge login check failed, nothing called: {exc}") from exc
    executors = {name: ex for name, ex in executors.items() if name in passes}
    # One pre launch plan gate for the batch: the Codex probe baseline and the latest Claude
    # plan window of the campaign (agent records and earlier judge calls). Without pass A no
    # Claude reading is looked up and the gate refuses any Claude call (amendment 2).
    claude = "A" in passes
    gate = probe.PlanGate(args.stop_at_window, args.max_window_step, allow_claude=claude)
    if claude:
        # The probe reads the account pass A calls use (one chosen value for both).
        gate.configure(
            probe.claude_probe_env(dict(os.environ), account),
            evidence / "plan-probe-scratch",
            evidence / runner.PROBE_LEDGER,
        )
        gate.observe_claude(probe.latest_claude_window(evidence, account))
    for ex in executors.values():
        if hasattr(ex, "gate"):
            ex.gate = gate
    used = 0
    judged = 0
    current: dict[str, Any] = {}

    class CeilingReached(Exception):
        pass

    def counted(pass_name: str) -> judge.Executor:
        execute = executors[pass_name]
        cfg = judge.JUDGES[pass_name]

        def call(prompt: str) -> str | judge.Reply:
            nonlocal used
            if used >= args.max_calls:
                raise CeilingReached
            used += 1
            row: dict[str, Any] = {
                "run_id": current["run_id"],
                "batch": current["batch"],
                "pass": pass_name,
                **{k: v for k, v in cfg.as_dict().items() if k != "pass_name"},
                "started_at": runner._now(),
            }
            # A started row first, so a crash or interrupt mid call still leaves the call.
            _append_jsonl(ledger, row | {"status": "started"})
            try:
                reply = execute(prompt)
            except judge.JudgeCallError as exc:
                infra = isinstance(exc, judge.JudgeInfraError)
                _append_jsonl(
                    ledger, row | {"status": "infra_error" if infra else "error", "error": str(exc)}
                )
                raise
            if isinstance(reply, judge.Reply):
                row |= {"usage": reply.usage, "cost_usd": reply.cost_usd, "meta": reply.meta}
            _append_jsonl(ledger, row | {"status": "ok"})
            return reply

        return call

    wrapped = {name: counted(name) for name in passes}
    closing_failed = False
    try:
        for rec, q, run_dir, batches, prior in todo:
            if used + len(passes) > args.max_calls:  # one call per judge per answer at least
                break
            answer = (run_dir / "answer.md").read_text(encoding="utf-8")
            current.update(run_id=rec["run_id"], batch=batches + 1)
            for ex in executors.values():
                begin = getattr(ex, "begin", None)
                if begin is not None:
                    begin(evidence / "judge-logs" / rec["run_id"] / f"batch-{batches + 1}")
            # Persist the batch first, so a ceiling hit or crash mid answer still counts it.
            # In merge mode the passes not requested (pass A for ``--passes B``) are kept
            # untouched, in the pending record too.
            keep = (
                {
                    name: stored
                    for name, stored in ((prior or {}).get("passes") or {}).items()
                    if name not in passes and isinstance(stored, dict)
                }
                if merge
                else {}
            )
            pending: dict[str, Any] = {
                "run_id": rec["run_id"],
                "status": "judge_error",
                "error": "interrupted before both passes finished"
                if not merge
                else f"interrupted before pass {', '.join(passes)} finished",
                "batches": batches + 1,
            }
            if merge:
                pending |= {"passes_requested": list(passes), "passes_kept": sorted(keep)}
                if keep:
                    pending["passes"] = keep
            path = run_dir / "judgement.json"
            path.write_text(json.dumps(pending, indent=2), "utf-8")
            try:
                result = judge.judge(
                    q, answer, rec["run_id"], wrapped, passes=passes, keep=keep or None
                )
            except (CeilingReached, judge.JudgeInfraError) as exc:
                # A call ceiling or a plan limit says nothing about the answer: restore its state
                # (the batch does not count) and stop. A crash keeps the pending judge_error.
                if prior is None:
                    path.unlink()
                else:
                    path.write_text(json.dumps(prior, indent=2), "utf-8")
                reason = (
                    "call ceiling" if isinstance(exc, CeilingReached) else f"infra error: {exc}"
                )
                print(f"{rec['run_id']}: batch stopped ({reason})", file=sys.stderr)
                break
            out = dict(result.as_dict(), batches=batches + 1)
            path.write_text(json.dumps(out, indent=2), "utf-8")
            judged += 1
            print(f"{rec['run_id']}: {result.status} accuracy={result.accuracy}")
    finally:
        # Guaranteed cleanup, whatever ended the batch (exceptions and interrupts included):
        # the closing probe after the last Codex call; the original exception is re-raised.
        print(f"judge calls used: {used}")
        try:
            closing_failed = _judge_closing(executors, evidence)
        except Exception as probe_exc:  # never mask the batch's own outcome
            closing_failed = True
            print(f"closing probe failed: {probe_exc!r}", file=sys.stderr)
    if closing_failed:
        return 1
    if judged < len(todo):
        print(f"judge call ceiling or batch stop: {len(todo) - judged} answers left unjudged")
        return 1
    return 0


PARTIAL_STATUSES = frozenset({STATUS_COMPLETED, "timeout", "turn_limit"})


def analysis_rows(
    evidence: Path,
    data: dict[str, Any],
    corrections: dict[str, dict[str, str]] | None = None,
    primary: str = judge.DEFAULT_PRIMARY_JUDGE,
) -> list[dict[str, Any]]:
    """One row per non excluded run. ``completion`` is ``None`` (excluded and listed) for a
    completed run without a valid judgement; ``tokens_with_partial`` keeps the partial usage of
    timed out and turn limited runs for the sensitivity table. ``corrections`` (run id to fact
    id to audit verdict) replace judge verdicts in the run accuracy; the single judge
    recomputations stay each judge's own. ``primary`` is the scoring rule (``judge.scoring``):
    ``combined`` (amendment 1) or ``B`` (amendment 2, the pass B verdict scores every fact)."""
    lookup = questions.by_id(data)
    corrections = corrections or {}
    rows = []
    for rec in _records(evidence):
        if rec["status"] in EXCLUDED_STATUSES or rec.get("contaminated"):
            continue
        q = lookup.get(rec["question_id"], {})
        verdict = _judgement(evidence / "runs" / rec["run_id"]) or {}
        scored = judge.scoring(verdict, primary)
        valid = scored is not None
        completed = rec["status"] == STATUS_COMPLETED
        accuracy = scored.accuracy if completed and scored is not None else None
        audited = corrections.get(str(rec["run_id"]))
        if accuracy is not None and audited:
            accuracy = report.audited_accuracy(verdict, audited, primary)
        single: dict[str, float | None] = {}
        # Each pass's single judge values stand on their own (``judge.single_judge_*`` decide
        # validity per pass): a completed scored run, whatever the primary rule's verdict.
        judged_ok = completed and q.get("stratum") != questions.CONTROL_STRATUM
        for p in judge.PASSES:
            single[f"accuracy_judge_{p}"] = (
                judge.single_judge_accuracy(verdict, p) if judged_ok else None
            )
            single[f"completion_judge_{p}"] = (
                judge.single_judge_complete(verdict, p) if judged_ok else None
            )
        completion: float | None = 0.0
        if completed:
            completion = scored.complete if scored is not None else None
        if valid:
            judge_status: str | None = "ok"
        elif verdict.get("status") == "ok":
            # Valid under another rule only (a pass B only judgement under ``combined``).
            judge_status = f"no_valid_{primary}_judgement"
        else:
            judge_status = verdict.get("status")
        if q.get("stratum") == questions.CONTROL_STRATUM:
            completion = 1.0 if completed else 0.0
            answer = report.answer_text(evidence / "runs" / str(rec["run_id"]))
            if completed and answer is not None:
                accuracy = judge.control_score(q, answer)
        total = (rec.get("usage") or {}).get("total")
        rows.append(
            {
                "run_id": rec["run_id"],
                "agent": rec["agent"],
                "stratum": rec["stratum"],
                "arm": rec["arm"],
                "question_id": rec["question_id"],
                "status": rec["status"],
                "total_tokens": total if completed else None,
                "tokens_with_partial": total if rec["status"] in PARTIAL_STATUSES else None,
                "accuracy": accuracy,
                **single,
                "completion": completion,
                "judge_status": judge_status,
                "judge_batches": verdict.get("batches"),
                "timeout": rec.get("timeout"),
                "turn_limit": rec.get("turn_limit"),
                "compactions": rec.get("compactions"),
                "truncations": rec.get("truncations"),
            }
        )
    return rows


def control_overhead(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[float]] = {}
    for r in rows:
        if r["stratum"] == questions.CONTROL_STRATUM and r["total_tokens"] is not None:
            groups.setdefault((r["agent"], r["arm"]), []).append(float(r["total_tokens"]))
    return [
        {"agent": a, "arm": arm, "runs": len(v), "mean_total_tokens": round(sum(v) / len(v))}
        for (a, arm), v in sorted(groups.items())
    ]


def duplicate_cells(records: list[dict[str, Any]]) -> dict[str, list[str]]:
    """run_keys with more than one final record (a mixed campaign). ``infra_error`` and
    ``invalid`` records are excluded statuses (listed under ``excluded_runs``), so a cell may
    hold an invalid record plus its one rerun."""
    by_key: dict[str, list[str]] = {}
    for r in records:
        if r["status"] not in EXCLUDED_STATUSES:
            by_key.setdefault(str(r["run_key"]), []).append(str(r["run_id"]))
    return {k: v for k, v in sorted(by_key.items()) if len(v) > 1}


def _single_judge_sensitivity(
    rows: list[dict[str, Any]], resamples: int, seed: int
) -> list[dict[str, Any]]:
    """Every accuracy and completion comparison recomputed from pass A alone and pass B alone
    (amendment 1; deferred under amendment 2)."""
    kept = ("accuracy_diff_arm_minus_baseline", "completion_diff_arm_minus_baseline")
    return [
        {k: v for k, v in entry.items() if k in ("agent", "stratum", "arm", "baseline", *kept)}
        | {"judge_pass": p}
        for p in judge.PASSES
        for entry in stats.analyze(
            [
                dict(
                    r,
                    accuracy=r[f"accuracy_judge_{p}"],
                    # Completed runs only: timeouts and turn limits keep their pooled 0.
                    completion=r[f"completion_judge_{p}"]
                    if r["status"] == STATUS_COMPLETED
                    else r["completion"],
                )
                for r in rows
            ],
            resamples,
            seed,
        )
        if any(k in entry for k in kept)
    ]


def _bias_dir(args: argparse.Namespace, evidence: Path) -> Path | str | None:
    """The ``--bias-evidence-dir`` (read only), None without it, or the reason it is refused:
    it needs ``--primary-judge B``, must differ from ``--evidence-dir`` and hold run records."""
    if not args.bias_evidence_dir:
        return None
    bias = Path(args.bias_evidence_dir)
    if args.primary_judge != "B":
        return "--bias-evidence-dir needs --primary-judge B"
    if bias.resolve() == evidence.resolve():
        return "--bias-evidence-dir must be another campaign than --evidence-dir"
    if not any((bias / "runs").glob("*/record.json")):
        return f"--bias-evidence-dir holds no run records: {bias}"
    return bias


def bias_evidence(
    bias: Path, shared: set[str], own_runs: set[str]
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """The other campaign of the judge bias check (amendment 4, read only): its scored,
    uncontaminated runs on the questions of this evidence (``shared``) as a run map, their
    judgements, and the recorded summary (fingerprint and counts, never a path)."""
    runs: dict[str, dict[str, Any]] = {}
    for rec in _records(bias):
        if rec["status"] in EXCLUDED_STATUSES or rec.get("contaminated"):
            continue
        if str(rec["question_id"]) in shared:
            runs[str(rec["run_id"])] = {"agent": rec["agent"], "question_id": rec["question_id"]}
    overlap = sorted(set(runs) & own_runs)
    if overlap:
        raise ValueError(f"run ids in both campaigns: {', '.join(overlap)}")
    found = [(run_id, _judgement(bias / "runs" / run_id)) for run_id in sorted(runs)]
    judgements = [j for _run_id, j in found if j is not None]
    both = [
        j for j in judgements if all(judge.pass_verdicts(j, p) is not None for p in judge.PASSES)
    ]
    summary = {
        "label": "exploratory",
        "runs": len(runs),
        "judged_runs": len(judgements),
        "runs_graded_by_both_judges": len(both),
        "questions": len({str(r["question_id"]) for r in runs.values()}),
        "agents": sorted({str(r["agent"]) for r in runs.values()}),
    }
    return runs, judgements, summary


def cmd_analyze(args: argparse.Namespace) -> int:
    evidence = Path(args.evidence_dir)
    if args.format == "ledger":
        if args.question or args.bias_evidence_dir:
            print("--question and --bias-evidence-dir need --format json", file=sys.stderr)
            return 2
        records = [RunRecord.from_dict(r) for r in _records(evidence)]
        print(runner.ledger_markdown(records))
        return 0
    bias_dir = _bias_dir(args, evidence)
    if isinstance(bias_dir, str):
        print(bias_dir, file=sys.stderr)
        return 2
    duplicates = duplicate_cells(_records(evidence))
    if duplicates:
        for key, ids in duplicates.items():
            print(f"duplicate cell {key}: {', '.join(ids)}", file=sys.stderr)
        print("refusing to analyze: one evidence directory must hold one campaign", file=sys.stderr)
        return 1
    data = questions.load(Path(args.questions))
    only: set[str] | None = None
    if args.question:
        unknown = sorted(set(args.question) - set(questions.by_id(data)))
        if unknown:
            print(f"unknown question ids: {', '.join(unknown)}", file=sys.stderr)
            return 2
        only = set(args.question)
    question_of = {str(r["run_id"]): str(r["question_id"]) for r in _records(evidence)}
    primary = args.primary_judge
    judgements = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((evidence / "runs").glob("*/judgement.json"))
    ]
    corrections: list[dict[str, Any]] = []
    superseded: list[dict[str, Any]] = []
    audit_files: list[Path] = []
    if args.audit:
        try:
            audit_files = report.audit_files([Path(a) for a in args.audit])
            corrections, superseded = report.load_audit_sources(audit_files, judgements, primary)
        except report.AuditError as exc:
            print(f"audit not applied: {exc}", file=sys.stderr)
            return 1
    rows = analysis_rows(evidence, data, report.corrections_by_run(corrections), primary)
    if only is not None:
        # ``--question``: rows, judgements and comparisons of those questions only (exploratory
        # recomputation, recorded as ``question_filter``); audits validate against every run.
        rows = [r for r in rows if str(r["question_id"]) in only]
        judgements = [j for j in judgements if question_of.get(str(j.get("run_id"))) in only]
        corrections = [c for c in corrections if question_of.get(str(c["run_id"])) in only]
        superseded = [c for c in superseded if question_of.get(str(c["run_id"])) in only]
    accuracy_of = {str(r["run_id"]): r["accuracy"] for r in rows}
    judged_accuracy = {
        str(j.get("run_id")): graded.accuracy
        for j in judgements
        if (graded := judge.scoring(j, primary)) is not None
    }
    corrections = [
        c
        | {
            "run_accuracy_judged": judged_accuracy.get(c["run_id"]),
            "run_accuracy_audited": accuracy_of.get(c["run_id"]),
        }
        for c in corrections
    ]
    excluded = [
        {
            "run_id": r["run_id"],
            "status": r["status"],
            "reason": r.get("status_reason"),
            "contaminated": r.get("contaminated"),
        }
        for r in _records(evidence)
        if (r["status"] in EXCLUDED_STATUSES or r.get("contaminated"))
        and (only is None or str(r["question_id"]) in only)
    ]
    no_judgement = [
        {
            "run_id": r["run_id"],
            "judge_status": r["judge_status"] or "not judged",
            "batches": r["judge_batches"],
            "excluded": r["judge_status"] == "judge_error"
            and int(r["judge_batches"] or 1) >= MAX_JUDGE_BATCHES,
        }
        for r in rows
        if r["status"] == STATUS_COMPLETED
        and r["stratum"] != questions.CONTROL_STRATUM
        and r["judge_status"] != "ok"
    ]
    scored = [r for r in rows if r["stratum"] != questions.CONTROL_STRATUM]
    pooled = [dict(r, stratum="all_scored") for r in scored]
    sensitivity = [
        {
            k: v
            for k, v in entry.items()
            if k in ("agent", "stratum", "arm", "baseline", "token_ratio_baseline_over_arm")
        }
        | {"dropped_questions": entry["dropped_questions"].get("token_ratio_baseline_over_arm", [])}
        for entry in stats.analyze(
            scored + pooled, args.resamples, args.seed, token_key="tokens_with_partial"
        )
    ]
    runs_of = {
        str(r["run_id"]): {"agent": r["agent"], "question_id": r["question_id"]} for r in rows
    }
    single_judge: list[dict[str, Any]] | dict[str, str] = {
        "deferred": judge.DEFERRED_WITH_CLAUDE_ARM
    }
    bias: dict[str, Any] = {"deferred": judge.DEFERRED_WITH_CLAUDE_ARM}
    if primary == "combined":
        single_judge = _single_judge_sensitivity(scored + pooled, args.resamples, args.seed)
        bias = judge.bias_report(judgements, runs_of, args.resamples, args.seed)
    bias_extra: dict[str, Any] = {}
    if isinstance(bias_dir, Path):
        # Amendment 4: the family interaction over this evidence's judgements plus the other
        # campaign's (read only) on this evidence's questions; single judge sensitivity for
        # this evidence. No path is recorded, only the other campaign's fingerprint and counts.
        shared = {str(r["question_id"]) for r in rows}
        try:
            other_runs, other_judgements, summary = bias_evidence(bias_dir, shared, set(runs_of))
        except ValueError as exc:
            print(f"refusing the bias check: {exc}", file=sys.stderr)
            return 2
        single_judge = _single_judge_sensitivity(scored + pooled, args.resamples, args.seed)
        bias = judge.bias_report(
            [*judgements, *other_judgements], runs_of | other_runs, args.resamples, args.seed
        )
        bias_extra = {
            "bias_evidence_fingerprint": report.evidence_fingerprint(
                bias_dir, Path(args.questions), []
            ),
            "bias_evidence": summary,
        }
    audit_digests = report.audit_digests(audit_files)
    result = {
        "harness_version": __version__,
        "primary_judge": primary,
        "evidence_fingerprint": report.evidence_fingerprint(
            evidence, Path(args.questions), audit_files
        ),
        "questions_sha256": report.sha256_file(Path(args.questions)),
        "audit_files": audit_digests,
        "comparisons": stats.analyze(scored + pooled, args.resamples, args.seed),
        "single_judge_sensitivity": single_judge,
        "judge_bias": bias,
        **bias_extra,
        **({"question_filter": sorted(only)} if only is not None else {}),
        **(
            {"pass_agreement": judge.pass_agreement(judgements, set(runs_of))}
            if primary == "B"
            else {}
        ),
        "token_sensitivity_with_partial_usage": sensitivity,
        "control_overhead": control_overhead(rows),
        "audit_queue": judge.audit_queue(judgements, args.seed, primary),
        "audit_corrections": corrections,
        # Amendment 5: key correction rows a human audit verdict overrode (only with --audit).
        **({"superseded_corrections": superseded} if args.audit else {}),
        "completion": stats.completion_rates(rows),
        "excluded_runs": excluded,
        "runs_without_valid_judgement": no_judgement,
        "billing_warnings": report.billing_warnings(evidence, _records(evidence)),
        "answer_file_mismatches": [
            {"run_id": r["run_id"], "answer_file": pointer}
            for r in _records(evidence)
            if (pointer := report.answer_mismatch(r, evidence / "runs" / str(r["run_id"])))
        ],
    }
    print(json.dumps(result, indent=2))
    return 0


def _load_analysis(path: Path) -> tuple[str, dict[str, Any]] | str:
    """(text, analysis) of an ``analyze`` output, or the reason it is not one."""
    if not path.is_file():
        return f"analysis file not found: {path}"
    text = path.read_text(encoding="utf-8")
    try:
        analysis = json.loads(text)
    except json.JSONDecodeError as exc:
        return f"analysis file is not JSON: {exc}"
    if not isinstance(analysis, dict) or not analysis.get("comparisons"):
        return "analysis file is not an analyze output (no comparisons)"
    return text, analysis


def analysis_rule(analysis: dict[str, Any]) -> str:
    """The scoring rule an analysis used (an analysis before harness 1.3.0 is ``combined``)."""
    return str(analysis.get("primary_judge") or judge.DEFAULT_PRIMARY_JUDGE)


def cmd_audit_export(args: argparse.Namespace) -> int:
    evidence = Path(args.evidence_dir)
    data = questions.load(Path(args.questions))
    records = {str(r["run_id"]): r for r in _records(evidence)}
    primary = args.primary_judge or judge.DEFAULT_PRIMARY_JUDGE
    expected: list[tuple[str, str]] | None = None
    if args.analysis:
        loaded = _load_analysis(Path(args.analysis))
        if isinstance(loaded, str):
            print(loaded, file=sys.stderr)
            return 1
        rule = analysis_rule(loaded[1])
        if args.primary_judge and args.primary_judge != rule:
            print(
                f"refusing to export: --primary-judge {args.primary_judge} but the analysis "
                f"used {rule}",
                file=sys.stderr,
            )
            return 1
        primary = rule
        expected = [
            (str(i.get("run_id")), str(i.get("fact_id")))
            for i in loaded[1].get("audit_queue") or []
        ]
    try:
        rows = report.export_audit(
            evidence, data, records, Path(args.out), args.seed, primary=primary, expected=expected
        )
    except report.AuditError as exc:
        print(f"audit export failed: {exc}", file=sys.stderr)
        return 1
    out = Path(args.out)
    disputed = sum(1 for r in rows if r["selection"] == "disputed")
    shards = sorted({str(r["shard"]) for r in rows})
    print(
        f"audit queue ({primary} rule): {disputed} disputed and {len(rows) - disputed} sampled "
        f"facts in {len(shards)} shards ({out / 'audit-index.md'})"
    )
    problems = report.check_files([p for p in out.rglob("*") if p.is_file()])
    for problem in problems:
        print(f"CHECK FAILED {problem}", file=sys.stderr)
    return 1 if problems else 0


def cmd_report(args: argparse.Namespace) -> int:
    loaded = _load_analysis(Path(args.analysis))
    if isinstance(loaded, str):
        print(loaded, file=sys.stderr)
        return 1
    text, analysis = loaded
    evidence = Path(args.evidence_dir)
    if not analysis.get("evidence_fingerprint"):
        print("analysis file has no evidence_fingerprint (rerun analyze)", file=sys.stderr)
        return 1
    primary = analysis_rule(analysis)
    if primary not in judge.PRIMARY_JUDGES:
        print(f"refusing to report: unknown primary_judge {primary!r}", file=sys.stderr)
        return 1
    if args.primary_judge and args.primary_judge != primary:
        print(
            f"refusing to report: --primary-judge {args.primary_judge} but the analysis used "
            f"{primary}",
            file=sys.stderr,
        )
        return 1
    if analysis.get("question_filter"):
        print(
            "refusing to report: a --question analysis is an exploratory recomputation, not a "
            "results report (keep its JSON instead)",
            file=sys.stderr,
        )
        return 1
    bias_problem = _check_bias_evidence(args, analysis)
    if bias_problem:
        print(f"refusing to report: {bias_problem}", file=sys.stderr)
        return 1
    try:
        report.check_audit_rule(sorted(Path(args.out).glob("audit-queue-*.csv")), primary)
    except report.AuditError as exc:
        print(f"refusing to report: {exc}", file=sys.stderr)
        return 1
    duplicates = duplicate_cells(_records(evidence))
    if duplicates:
        print(
            "refusing to report: duplicate cells " + ", ".join(sorted(duplicates)),
            file=sys.stderr,
        )
        return 1
    try:
        audit_paths = report.audit_files([Path(a) for a in args.audit or []])
    except report.AuditError as exc:
        print(f"refusing to report: {exc}", file=sys.stderr)
        return 1
    if report.audit_digests(audit_paths) != (analysis.get("audit_files") or []):
        print(
            "refusing to report: the --audit files are not the ones this analysis applied "
            "(names or current bytes differ); pass the same files, or rerun analyze",
            file=sys.stderr,
        )
        return 1
    fingerprint = report.evidence_fingerprint(evidence, Path(args.questions), audit_paths)
    if fingerprint != analysis["evidence_fingerprint"]:
        print(
            "refusing to report: the evidence changed since this analysis (fingerprint "
            f"{fingerprint[:12]} is not {str(analysis['evidence_fingerprint'])[:12]}); rerun "
            "analyze",
            file=sys.stderr,
        )
        return 1
    data = questions.load(Path(args.questions))
    corrections = report.corrections_by_run(analysis.get("audit_corrections") or [])
    rows = analysis_rows(evidence, data, corrections, primary)
    problems = report.write_report(
        Path(args.out), text, analysis, rows, _records(evidence), data, evidence
    )
    for problem in problems:
        print(f"CHECK FAILED {problem}", file=sys.stderr)
    print(f"report written to {args.out}" + (" with failed checks" if problems else ""))
    return 1 if problems else 0


def _check_bias_evidence(args: argparse.Namespace, analysis: dict[str, Any]) -> str | None:
    """Why an analysis' judge bias check cannot be reported: an analysis with a
    ``bias_evidence_fingerprint`` needs ``--bias-evidence-dir`` whose recomputed fingerprint
    matches; ``--bias-evidence-dir`` without such an analysis is refused too."""
    recorded = analysis.get("bias_evidence_fingerprint")
    given = getattr(args, "bias_evidence_dir", None)
    if not recorded and not given:
        return None
    if not given:
        return "the analysis has a judge bias check: pass its --bias-evidence-dir"
    if not recorded:
        return "--bias-evidence-dir given but the analysis has no judge bias check"
    current = report.evidence_fingerprint(Path(given), Path(args.questions), [])
    if current != recorded:
        return (
            f"the bias evidence changed since this analysis (fingerprint {current[:12]} is not "
            f"{str(recorded)[:12]}); rerun analyze"
        )
    return None


# --- Grounding, provenance and the forum cross check (pre registration section 14) ---


def _is_campaign(path: Path) -> bool:
    """A campaign evidence directory (its run ledger beside ``runs/``): read only input."""
    return (path / "ledger.jsonl").is_file() and (path / "runs").is_dir()


def _state_dirs(args: argparse.Namespace) -> tuple[Path | None, Path]:
    """(evidence dir, out dir) after the protected input guard: no write destination
    (``--out``, ``--results``, ``--export``) may be or lie inside a read only input
    (``--evidence-dir``, ``--contamination-evidence-dir``) or any campaign evidence directory.
    Checked before anything is created, deleted or written."""
    evidence = Path(args.evidence_dir) if getattr(args, "evidence_dir", None) else None
    out = Path(args.out)
    protected = [
        Path(p).resolve()
        for p in (evidence, getattr(args, "contamination_evidence_dir", None))
        if p
    ]
    for flag in ("out", "results", "export"):
        value = getattr(args, flag, None)
        if not value:
            continue
        dest = Path(value).resolve()
        for guard in protected:
            if dest == guard or guard in dest.parents:
                raise SystemExit(f"--{flag} {value} lies inside the read only input {guard}")
        for place in (dest, *dest.parents):
            if _is_campaign(place):
                raise SystemExit(f"--{flag} {value} lies inside the campaign evidence {place}")
    return evidence, out


def _completed(evidence: Path, agent: str, scored_only: bool = False) -> list[dict[str, Any]]:
    """The completed, uncontaminated runs of one agent in an evidence dir (scored: no
    controls)."""
    return [
        r
        for r in _records(evidence)
        if r.get("agent") == agent
        and r.get("status") == STATUS_COMPLETED
        and not r.get("contaminated")
        and not (scored_only and r.get("stratum") == questions.CONTROL_STRATUM)
    ]


def _completed_codex(evidence: Path, scored_only: bool = False) -> list[dict[str, Any]]:
    """The completed, uncontaminated Codex runs of an evidence dir (scored: no controls)."""
    return _completed(evidence, "codex", scored_only)


def _run_meta(rec: dict[str, Any]) -> dict[str, Any]:
    return {k: rec.get(k) for k in grounding.RUN_META}


def cmd_grounding_sources(args: argparse.Namespace) -> int:
    evidence, out = _state_dirs(args)
    assert evidence is not None
    lookup = questions.by_id(questions.load(Path(args.questions)))
    agent = args.agent
    log_name = "stream" if agent == "claude_code" else "rollout"
    written = kept = 0
    failed: list[str] = []
    for rec in _completed(evidence, agent):
        run_id = str(rec["run_id"])
        target = out / grounding.SOURCES_DIR / f"{run_id}.json"
        prior = grounding.read_json(target) if target.is_file() else None
        if isinstance(prior, dict) and prior.get("complete") is True and not args.force:
            kept += 1
            continue
        run_dir = evidence / "runs" / run_id
        rollout = report.log_source(run_dir, agent)
        try:
            if rollout is None:
                raise OSError(f"no {log_name}")
            raw = rollout.read_bytes()
            lines = raw.decode("utf-8", errors="replace").splitlines()
            problem = grounding.log_problem(agent, lines)
            if problem:
                raise OSError(problem)
        except OSError as exc:
            # No output file: a later run picks the run up once its log is back.
            failed.append(run_id)
            print(
                f"{run_id}: {log_name} missing, unreadable or incomplete ({exc})", file=sys.stderr
            )
            continue
        sources = grounding.parse_agent_sources(agent, lines)
        _, links = grounding.tokenize_links(report.answer_text(run_dir) or "")
        grounding.write_json(
            target,
            {
                **_run_meta(rec),
                "agent": rec.get("agent"),
                "harness_version": __version__,
                "companies": grounding.question_companies(lookup.get(str(rec["question_id"]), {})),
                f"{log_name}_sha256": hashlib.sha256(raw).hexdigest(),
                "sources": [s.as_dict() for s in sources],
                "answer_links": links,
                "complete": True,
            },
        )
        written += 1
    inventory = grounding.host_inventory(out)
    grounding.write_json(out / grounding.HOST_INVENTORY, inventory)
    pending = [e for e in inventory if grounding.seed_category(e["host"]) is None]
    print(
        f"sources: {written} runs parsed, {kept} kept; {len(inventory)} hosts in the inventory, "
        f"{len(pending)} not in the seed table (pass H)"
    )
    if failed:
        print(
            f"{len(failed)} runs without a complete {log_name}: {', '.join(failed)}",
            file=sys.stderr,
        )
        return 1
    return 0


def _answer(evidence: Path, run_id: str, missing: list[str]) -> str | None:
    """A run's answer, or None (the run id appended to ``missing``) when it is missing or
    empty: never graded, never cached, not a call; graded once it is restored."""
    text = report.answer_text(evidence / "runs" / run_id)
    if text is None or not text.strip():
        missing.append(run_id)
        return None
    return text


def _missing_answers(missing: Sequence[str]) -> int:
    if missing:
        print(
            f"{len(missing)} completed runs without an answer (not graded): {', '.join(missing)}",
            file=sys.stderr,
        )
        return 1
    return 0


def _grading_preamble(args: argparse.Namespace, todo: int, calls: int, what: str) -> bool:
    """Print the plan; False for a dry run. Executed passes need --max-calls >= calls."""
    print(
        f"{todo} {what}, at least {calls} calls (Codex {judge.JUDGES['B'].model} "
        f"{judge.JUDGES['B'].effort}, ChatGPT plan allowance)"
    )
    if not args.execute:
        print("dry run: pass --execute and --max-calls to call the grader")
        return False
    if args.max_calls is None or calls > args.max_calls:
        raise SystemExit(f"needs --max-calls of at least {calls} (retries may add up to 2x)")
    return True


def _executors(
    args: argparse.Namespace,
    out: Path,
    pass_name: str,
    make: Callable[[], Any] | None = None,
) -> list[Any]:
    """One executor per worker (``--concurrency``), every one behind one shared pre launch
    plan gate (thread safe), so the threshold, the window step and the credit baseline hold
    for the batch as a whole."""
    gate = probe.PlanGate(args.stop_at_window, args.max_window_step, allow_claude=False)
    try:
        executors = [
            make()
            if make is not None
            else grounding.make_executor(
                out / "logs" / pass_name, args.stop_at_window, args.max_window_step
            )
            for _ in range(int(getattr(args, "concurrency", 1) or 1))
        ]
    except judge.JudgeLoginError as exc:
        raise SystemExit(f"Codex login check failed, nothing called: {exc}") from exc
    for executor in executors:
        if hasattr(executor, "gate"):
            executor.gate = gate
    return executors


def _finish(executor: Any, out: Path, pass_name: str, stopped: str | None, left: int) -> int:
    try:
        reasons = grounding.closing(executor, out, pass_name)
    except Exception as exc:  # never mask the batch's own outcome
        reasons = [f"closing probe failed: {exc!r}"]
    for reason in reasons:
        print(f"closing probe after pass {pass_name}: {reason}", file=sys.stderr)
    if stopped:
        print(f"pass {pass_name} batch stopped ({stopped}); resume later", file=sys.stderr)
    if left:
        print(f"pass {pass_name}: {left} items left", file=sys.stderr)
    return 1 if reasons or stopped or left else 0


def _batch_id(prefix: str, keys: Sequence[str]) -> str:
    return f"{prefix}-" + hashlib.sha256("\n".join(keys).encode()).hexdigest()[:12]


def cmd_grounding_hosts(args: argparse.Namespace) -> int:
    _, out = _state_dirs(args)
    inventory = grounding.read_json(out / grounding.HOST_INVENTORY)
    if inventory is None:
        raise SystemExit("no hosts.json: run grounding-sources first")
    table: dict[str, Any] = grounding.read_json(out / grounding.HOST_TABLE) or {}
    todo = [
        e
        for e in inventory
        if grounding.seed_category(e["host"]) is None
        and grounding.needs_grading(table.get(e["host"]))
    ]
    size = args.batch_size
    batches = [todo[i : i + size] for i in range(0, len(todo), size)]
    if not _grading_preamble(args, len(todo), len(batches), f"hosts in {len(batches)} batches"):
        return 0
    executors = _executors(args, out, "H")
    grader = grounding.Grader("H", out, executors[0], args.max_calls, grounding.PASS_H_SCHEMA)
    lock = threading.Lock()

    def work(batch: list[dict[str, Any]], worker: int) -> None:
        prompt, labels = grounding.pass_h_prompt(batch)
        item = _batch_id("hosts", list(labels.values()))
        parsed, error, calls = grader.grade(
            item, prompt, partial(grounding.parse_pass_h, labels=labels), executors[worker]
        )
        grounding.write_json(
            out / "pass-h" / f"{item}.json",
            {"hosts": list(labels.values()), "error": error, "calls": calls},
        )
        with lock:
            for label, host in labels.items():
                prior = table.get(host) or {}
                batches_n = int(prior.get("batches") or 0) + 1
                if parsed is not None:
                    table[host] = {
                        "status": "ok",
                        "basis": "pass_h",
                        "batch": item,
                        "batches": batches_n,
                        **parsed[label],
                    }
                else:
                    table[host] = {"status": "grade_error", "error": error, "batches": batches_n}
            grounding.write_json(out / grounding.HOST_TABLE, dict(sorted(table.items())))

    done, stopped = 0, None
    try:
        done, stopped = grounding.run_items(batches, work, args.concurrency, grader)
    finally:
        rc = _finish(executors, out, "H", stopped, len(batches) - done)
    return rc


def cmd_grounding_claims(args: argparse.Namespace) -> int:
    evidence, out = _state_dirs(args)
    assert evidence is not None
    lookup = questions.by_id(questions.load(Path(args.questions)))
    only = set(args.run_id or [])
    todo = []
    missing: list[str] = []
    for rec in _completed(evidence, args.agent, scored_only=True):
        run_id = str(rec["run_id"])
        if only and run_id not in only:
            continue
        prior = grounding.read_json(out / grounding.CLAIMS_DIR / f"{run_id}.json")
        if grounding.needs_grading(prior) and _answer(evidence, run_id, missing) is not None:
            todo.append((rec, prior))
    if not _grading_preamble(args, len(todo), len(todo), "answers for pass R"):
        return _missing_answers(missing)
    executors = _executors(args, out, "R")
    grader = grounding.Grader("R", out, executors[0], args.max_calls, grounding.PASS_R_SCHEMA)

    def work(entry: tuple[dict[str, Any], Any], worker: int) -> None:
        rec, prior = entry
        run_id = str(rec["run_id"])
        answer = report.answer_text(evidence / "runs" / run_id) or ""  # checked above
        q = lookup.get(str(rec["question_id"]), {})
        prompt, tokens = grounding.pass_r_prompt(str(q.get("question", "")), answer)
        parsed, error, calls = grader.grade(
            run_id, prompt, partial(grounding.parse_pass_r, tokens=tokens), executors[worker]
        )
        batches_n = int((prior or {}).get("batches") or 0) + 1
        extra = {**_run_meta(rec), "token_map": tokens}
        if parsed is not None:
            record = grounding.pass_record("ok", batches_n, calls, claims=parsed, **extra)
        else:
            record = grounding.pass_record("grade_error", batches_n, calls, error=error, **extra)
        grounding.write_json(out / grounding.CLAIMS_DIR / f"{run_id}.json", record)
        print(f"{run_id}: {record['status']} ({len(record.get('claims') or [])} claims)")

    done, stopped = 0, None
    try:
        done, stopped = grounding.run_items(todo, work, args.concurrency, grader)
    finally:
        rc = _finish(executors, out, "R", stopped, len(todo) - done)
    return max(rc, _missing_answers(missing))


def _crosscheck_dir(out: Path) -> Path:
    return out / crosscheck.CROSSCHECK_DIR


def _check_results(out: Path) -> dict[str, dict[str, Any]]:
    """Pass C results by sample row id (``crosscheck/checks/<row_id>.json``)."""
    return {
        p.stem: data
        for p in sorted((_crosscheck_dir(out) / crosscheck.CHECKS_DIR).glob("*.json"))
        if isinstance(data := grounding.read_json(p), dict)
    }


def cmd_crosscheck_extract(args: argparse.Namespace) -> int:
    evidence, out = _state_dirs(args)
    assert evidence is not None
    table = grounding.HostTable.load(out)
    run_ids = [str(r["run_id"]) for r in _completed_codex(evidence)]
    missing = grounding.missing_sources(out, run_ids)
    if missing:
        raise SystemExit(
            f"{len(missing)} runs have no complete sources file (run grounding-sources first): "
            + ", ".join(missing)
        )
    pool = crosscheck.build_pool(out, run_ids, table)
    cdir = _crosscheck_dir(out)
    extracted: dict[str, Any] = grounding.read_json(cdir / crosscheck.EXTRACT_FILE) or {}
    # Deviation G3: pass X extracts a seeded stratified sample of the pool texts only.
    chosen = crosscheck.text_sample(pool)
    prior_sample = grounding.read_json(cdir / crosscheck.TEXT_SAMPLE_FILE)
    if (
        isinstance(prior_sample, dict)
        and prior_sample.get("text_ids") != chosen["text_ids"]
        and any(t in extracted for t in prior_sample.get("text_ids") or [])
    ):
        raise SystemExit(
            "refusing to replace a text sample whose texts already have extractions (the pool "
            "changed since; restore the inputs it was drawn from)"
        )
    grounding.write_json(cdir / crosscheck.POOL_FILE, pool)
    grounding.write_json(cdir / crosscheck.TEXT_SAMPLE_FILE, chosen)
    sampled = set(chosen["text_ids"])
    todo = [
        e
        for e in pool
        if e["text_id"] in sampled and grounding.needs_grading(extracted.get(e["text_id"]))
    ]
    size = args.batch_size
    batches = [todo[i : i + size] for i in range(0, len(todo), size)]
    if table.unclassified:
        print(f"{len(table.unclassified)} hosts unclassified (counted as other)", file=sys.stderr)
    rc = 0
    if _grading_preamble(args, len(todo), len(batches), f"pool texts in {len(batches)} batches"):
        executors = _executors(args, out, "X")
        grader = grounding.Grader("X", out, executors[0], args.max_calls, grounding.PASS_X_SCHEMA)
        lock = threading.Lock()

        def work(batch: list[dict[str, Any]], worker: int) -> None:
            prompt, labels = grounding.pass_x_prompt(batch)
            item = _batch_id("texts", list(labels.values()))
            parsed, error, calls = grader.grade(
                item, prompt, partial(grounding.parse_pass_x, labels=labels), executors[worker]
            )
            grounding.write_json(
                cdir / "pass-x" / f"{item}.json",
                {"texts": list(labels.values()), "error": error, "calls": calls},
            )
            with lock:
                for label, text_id in labels.items():
                    prior = extracted.get(text_id) or {}
                    batches_n = int(prior.get("batches") or 0) + 1
                    if parsed is not None:
                        claims = [
                            {k: v for k, v in c.items() if k != "text"}
                            for c in parsed
                            if c["text"] == label
                        ]
                        extracted[text_id] = {
                            "status": "ok",
                            "batch": item,
                            "batches": batches_n,
                            "claims": claims,
                        }
                    else:
                        extracted[text_id] = {
                            "status": "grade_error",
                            "error": error,
                            "batches": batches_n,
                        }
                grounding.write_json(
                    cdir / crosscheck.EXTRACT_FILE, dict(sorted(extracted.items()))
                )

        done, stopped = 0, None
        try:
            done, stopped = grounding.run_items(batches, work, args.concurrency, grader)
        finally:
            rc = _finish(executors, out, "X", stopped, len(batches) - done)
    claims = crosscheck.dedupe_claims(pool, extracted, sampled)
    grounding.write_json(cdir / crosscheck.CLAIMS_FILE, claims)
    pending = sum(1 for t in sampled if grounding.needs_grading(extracted.get(t)))
    print(
        f"pool {len(pool)} texts, {len(sampled)} sampled for pass X ({pending} pending); "
        f"{len(claims)} deduplicated claims"
    )
    return rc


def cmd_crosscheck_sample(args: argparse.Namespace) -> int:
    _, out = _state_dirs(args)
    cdir = _crosscheck_dir(out)
    pool = grounding.read_json(cdir / crosscheck.POOL_FILE)
    extracted = grounding.read_json(cdir / crosscheck.EXTRACT_FILE) or {}
    chosen = grounding.read_json(cdir / crosscheck.TEXT_SAMPLE_FILE)
    if pool is None or not isinstance(chosen, dict):
        raise SystemExit("no crosscheck pool or text sample: run crosscheck-extract first")
    sampled = set(chosen.get("text_ids") or [])
    pending = [t for t in sorted(sampled) if grounding.needs_grading(extracted.get(t))]
    if pending:
        raise SystemExit(
            f"{len(pending)} sampled texts still need pass X; finish crosscheck-extract"
        )
    claims = crosscheck.dedupe_claims(pool, extracted, sampled)
    if args.extension:
        return _extension_sample(args, cdir, claims)
    size = args.size or crosscheck.SAMPLE_SIZE
    seed = BOOTSTRAP_SEED if args.seed is None else args.seed
    rows, allocation = crosscheck.stratified_sample(claims, size, seed)
    target = cdir / crosscheck.SAMPLE_FILE
    sample = {
        "seed": seed,
        "size": size,
        "pool_claims": len(claims),
        "allocation": allocation,
        "text_sample": {k: v for k, v in chosen.items() if k != "text_ids"},
        "excluded_texts": sorted(
            t for t in sampled if (extracted.get(t) or {}).get("status") != "ok"
        ),
        "rows": rows,
    }
    prior = grounding.read_json(target)
    checked = any((cdir / crosscheck.CHECKS_DIR).glob("[XE]*.json"))
    extended = (cdir / crosscheck.EXTENSION_FILE).exists()
    if prior is not None and prior.get("rows") != rows and (checked or extended):
        raise SystemExit(
            "refusing to replace a sample that already has checks or an extension sample"
        )
    grounding.write_json(target, sample)
    print(f"sample: {len(rows)} of {len(claims)} claims; allocation {allocation}")
    return 0


def _extension_sample(args: argparse.Namespace, cdir: Path, claims: list[dict[str, Any]]) -> int:
    """Deviation G4: the extension sample for trap sourcing (300 ``reported_figure`` claims
    outside the 400 claim sample, seed 8106, rows E001..)."""
    main = grounding.read_json(cdir / crosscheck.SAMPLE_FILE)
    if not isinstance(main, dict):
        raise SystemExit("no 400 claim sample: run crosscheck-sample first")
    size, seed = crosscheck.EXTENSION_SIZE, crosscheck.EXTENSION_SEED
    if args.size not in (None, size) or args.seed not in (None, seed):
        raise SystemExit(f"the G4 extension is {size} claims with seed {seed}; nothing written")
    in_sample = {str(r["claim_id"]) for r in main.get("rows") or []}
    eligible = sum(
        1
        for c in claims
        if c.get("kind") == crosscheck.EXTENSION_KIND and c["claim_id"] not in in_sample
    )
    if eligible < size:
        raise SystemExit(
            f"only {eligible} eligible {crosscheck.EXTENSION_KIND} claims outside the 400 "
            f"sample, fewer than {size}; nothing written"
        )
    rows, allocation = crosscheck.extension_sample(claims, in_sample, size, seed)
    target = cdir / crosscheck.EXTENSION_FILE
    prior = grounding.read_json(target)
    checked = any((cdir / crosscheck.CHECKS_DIR).glob("E*.json"))
    if prior is not None and prior.get("rows") != rows and checked:
        raise SystemExit("refusing to replace an extension sample that already has checks")
    grounding.write_json(
        target,
        {
            "label": crosscheck.EXTENSION_LABEL,
            "seed": seed,
            "size": size,
            "kind": crosscheck.EXTENSION_KIND,
            "allocation": allocation,
            "rows": rows,
        },
    )
    print(f"extension sample: {len(rows)} claims; allocation {allocation}")
    return 0


def _sample_rows(out: Path, extension: bool = False) -> list[dict[str, Any]]:
    """The 400 claim sample's rows (X), or the G4 extension's rows (E)."""
    name = crosscheck.EXTENSION_FILE if extension else crosscheck.SAMPLE_FILE
    sample = grounding.read_json(_crosscheck_dir(out) / name)
    if sample is None:
        what = "crosscheck-sample --extension" if extension else "crosscheck-sample"
        raise SystemExit(f"no {name}: run {what} first")
    return list(sample["rows"])


def _checked_rows(out: Path, extension: bool = False) -> list[dict[str, Any]]:
    """The sample rows (X only, unless ``extension``: the E rows) merged with their pass C
    results (status None: unchecked)."""
    rows = []
    for row in _sample_rows(out, extension):
        result = grounding.read_json(
            _crosscheck_dir(out) / crosscheck.CHECKS_DIR / f"{row['row_id']}.json"
        )
        merged = dict(row, status=None)
        if result:
            merged.update(
                {k: v for k, v in result.items() if k not in ("calls", "row")},
                status=result.get("status"),
            )
        rows.append(merged)
    return rows


def cmd_crosscheck_check(args: argparse.Namespace) -> int:
    _, out = _state_dirs(args)
    cdir = _crosscheck_dir(out)
    only = set(args.row or [])
    todo = [
        r
        for r in _sample_rows(out, args.extension)
        if (not only or r["row_id"] in only)
        and crosscheck.needs_check(
            grounding.read_json(cdir / crosscheck.CHECKS_DIR / f"{r['row_id']}.json")
        )
    ]
    if not _grading_preamble(args, len(todo), len(todo), "sampled claims for pass C (Archivist)"):
        return 0
    gate = runner.ArchivistGate(args.archivist_ceiling, env=runner.child_env(dict(os.environ), {}))
    executors = _executors(
        args,
        out,
        "C",
        lambda: crosscheck.make_checker(
            out / "logs" / "C", args.stop_at_window, args.max_window_step, gate
        ),
    )
    grader = grounding.Grader("C", out, executors[0], args.max_calls, crosscheck.PASS_C_SCHEMA)

    def work(row: dict[str, Any], worker: int) -> None:
        row_id = str(row["row_id"])
        prior = grounding.read_json(cdir / crosscheck.CHECKS_DIR / f"{row_id}.json")
        parsed, error, calls = grader.grade(
            row_id,
            crosscheck.pass_c_prompt(row),
            partial(crosscheck.parse_pass_c, claim=row),
            executors[worker],
        )
        batches_n = int((prior or {}).get("batches") or 0) + 1
        meta = (calls[-1].get("meta") if calls else None) or {}
        extra = {
            "row": row,
            "archivist_calls": meta.get("archivist_calls"),
            "checker": crosscheck.checker_config(),
        }
        if parsed is not None:
            record = grounding.pass_record("ok", batches_n, calls, **parsed, **extra)
        elif error and error.startswith("call: arm_isolation"):
            record = grounding.pass_record(
                "invalid", batches_n, calls, error=f"invalid: {error[6:]}", **extra
            )
        else:
            record = grounding.pass_record("grade_error", batches_n, calls, error=error, **extra)
        grounding.write_json(cdir / crosscheck.CHECKS_DIR / f"{row_id}.json", record)
        print(f"{row_id}: {record['status']} {record.get('bin') or ''}".rstrip())

    done, stopped = 0, None
    try:
        done, stopped = grounding.run_items(todo, work, args.concurrency, grader)
    finally:
        for reading in gate.readings:
            grounding._append(out / "archivist-usage.jsonl", reading)
        rc = _finish(executors, out, "C", stopped, len(todo) - done)
    return rc


def cmd_crosscheck_audit(args: argparse.Namespace) -> int:
    _, out = _state_dirs(args)
    rows = _checked_rows(out)
    unchecked = [r["row_id"] for r in rows if r["status"] is None or crosscheck.needs_check(r)]
    if unchecked:
        raise SystemExit(
            f"{len(unchecked)} sampled rows still need pass C; finish crosscheck-check"
        )
    results = Path(args.results)
    target = results / "crosscheck-audit.csv"
    if crosscheck.filled_audit(target):
        print(f"refusing to overwrite human audit verdicts or notes in {target}", file=sys.stderr)
        return 1
    audit = crosscheck.audit_rows(rows, args.seed)
    results.mkdir(parents=True, exist_ok=True)
    target.write_text(report.csv_text(crosscheck.AUDIT_COLUMNS, audit), encoding="utf-8")
    picked = Counter(r["selection"] for r in audit)
    print(f"crosscheck audit: {dict(picked)} rows in {target} (auditor columns blank)")
    problems = report.check_files([target])
    for problem in problems:
        print(f"CHECK FAILED {problem}", file=sys.stderr)
    return 1 if problems else 0


def cmd_trap_judge(args: argparse.Namespace) -> int:
    evidence, out = _state_dirs(args)
    assert evidence is not None
    lookup = questions.by_id(questions.load(Path(args.questions)))
    only = set(args.run_id or [])
    todo = []
    missing: list[str] = []
    for rec in _completed(evidence, args.agent):
        q = lookup.get(str(rec["question_id"]))
        if q is None or not grounding.trap_facts(q):
            continue
        if only and str(rec["run_id"]) not in only:
            continue
        prior = grounding.read_json(out / grounding.TRAPS_DIR / f"{rec['run_id']}.json")
        answered = grounding.needs_grading(prior) and (
            _answer(evidence, str(rec["run_id"]), missing) is not None
        )
        if answered:
            todo.append((rec, q, prior))
    if not _grading_preamble(args, len(todo), len(todo), "answers for pass T"):
        return _missing_answers(missing)
    executors = _executors(args, out, "T")
    grader = grounding.Grader("T", out, executors[0], args.max_calls, grounding.PASS_T_SCHEMA)

    def work(entry: tuple[dict[str, Any], dict[str, Any], Any], worker: int) -> None:
        rec, q, prior = entry
        run_id = str(rec["run_id"])
        answer = report.answer_text(evidence / "runs" / run_id) or ""
        prompt, labels = grounding.pass_t_prompt(q, answer)
        parsed, error, calls = grader.grade(
            run_id, prompt, partial(grounding.parse_pass_t, labels=labels), executors[worker]
        )
        batches_n = int((prior or {}).get("batches") or 0) + 1
        extra = _run_meta(rec)
        if parsed is not None:
            verdicts = {labels[k]: v for k, v in parsed.items()}
            adoption = fmean(1.0 if v == "adopted" else 0.0 for v in verdicts.values())
            record = grounding.pass_record(
                "ok", batches_n, calls, verdicts=verdicts, adoption=adoption, **extra
            )
        else:
            record = grounding.pass_record("grade_error", batches_n, calls, error=error, **extra)
        grounding.write_json(out / grounding.TRAPS_DIR / f"{run_id}.json", record)
        print(f"{run_id}: {record['status']} adoption={record.get('adoption')}")

    done, stopped = 0, None
    try:
        done, stopped = grounding.run_items(todo, work, args.concurrency, grader)
    finally:
        rc = _finish(executors, out, "T", stopped, len(todo) - done)
    return max(rc, _missing_answers(missing))


def _reliance(
    recs: Sequence[dict[str, Any]], out: Path, table: grounding.HostTable
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Claim rows of the scored runs with a valid pass R output; missing and grade error ids."""
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    errors: list[str] = []
    for rec in recs:
        run_id = str(rec["run_id"])
        data = grounding.read_json(out / grounding.CLAIMS_DIR / f"{run_id}.json")
        if data is None:
            missing.append(run_id)
            continue
        if data.get("status") != "ok":
            errors.append(run_id)
            continue
        tokens = data.get("token_map") or {}
        try:
            sources = grounding.run_sources(out, run_id)
        except ValueError as exc:  # attribution needs the run's sources
            raise SystemExit(str(exc)) from exc
        attributed = grounding.attribute_run(data.get("claims") or [], sources, tokens, table)
        rows += [grounding.claim_row(_run_meta(rec), c, tokens) for c in attributed]
    return rows, missing, errors


def _grounding_inputs(
    evidence: Path, out: Path, recs: Sequence[dict[str, Any]], extra: Sequence[Path] = ()
) -> list[tuple[str, Path]]:
    items: list[tuple[str, Path]] = []
    for rec in recs:
        run_id = str(rec["run_id"])
        items += [
            (f"record/{run_id}", evidence / "runs" / run_id / "record.json"),
            (f"sources/{run_id}", out / grounding.SOURCES_DIR / f"{run_id}.json"),
            (f"claims/{run_id}", out / grounding.CLAIMS_DIR / f"{run_id}.json"),
        ]
    cdir = _crosscheck_dir(out)
    items += [
        ("host-table", out / grounding.HOST_TABLE),
        ("hosts", out / grounding.HOST_INVENTORY),
        ("crosscheck/sample", cdir / crosscheck.SAMPLE_FILE),
        ("crosscheck/sample-extension", cdir / crosscheck.EXTENSION_FILE),
    ]
    items += [(f"check/{p.name}", p) for p in sorted((cdir / crosscheck.CHECKS_DIR).glob("*.json"))]
    items += [(f"extra/{p.as_posix()}", p) for p in extra]
    return items


ContaminationAudit = tuple[list[Path], list[dict[str, Any]], list[dict[str, Any]]]


def _contamination(
    args: argparse.Namespace,
    out: Path,
    table: grounding.HostTable,
    audit: ContaminationAudit | None = None,
) -> tuple[dict[str, Any], list[tuple[str, Path]], list[dict[str, Any]]]:
    """The contamination stratum: trap adoption (pass T), claim attribution (pass R) and pass B
    accuracy and tokens per arm, with paired bootstrap intervals; H6 is applied by hand.
    ``audit`` (``--contamination-audit``, amendment 5): the audit files with their applied and
    superseded corrections."""
    cev = Path(args.contamination_evidence_dir)
    cq = Path(args.contamination_questions)
    data = questions.load(cq)
    lookup = questions.by_id(data)
    agent = args.agent
    recs = _completed(cev, agent)
    adoption_rows = []
    trap_missing = []
    for rec in recs:
        q = lookup.get(str(rec["question_id"]), {})
        if not grounding.trap_facts(q):
            continue
        result = grounding.read_json(out / grounding.TRAPS_DIR / f"{rec['run_id']}.json")
        if not result or result.get("status") != "ok":
            trap_missing.append(str(rec["run_id"]))
            continue
        adoption_rows.append(
            {
                "agent": agent,
                "stratum": "contamination",
                "arm": rec["arm"],
                "question_id": rec["question_id"],
                "trap_adoption": float(result["adoption"]),
            }
        )
    claim_rows, claims_missing, claims_errors = _reliance(recs, out, table)
    trap_means = stats.question_means(adoption_rows, "trap_adoption")
    h6: dict[str, Any] = {}
    for arm in ("archivist", "both"):
        pairs = stats.paired(trap_means, agent, "contamination", arm)
        if pairs:
            h6[arm] = stats.paired_bootstrap(
                pairs, lambda s: fmean(a - b for a, b in s), args.resamples, args.seed
            ).as_dict()
    attribution: dict[str, dict[str, Any]] = {rule: {} for rule in grounding.RULES}
    for rule in grounding.RULES:
        ruled = claim_rows
        if rule == "numbers_only":
            ruled = [grounding.numbers_only_row(r) for r in claim_rows]
        filing_share: dict[tuple[str, str], list[float]] = defaultdict(list)
        for rec in recs:
            facts = [r for r in ruled if r["run_id"] == rec["run_id"] and r.get("type") == "fact"]
            if facts:
                share = sum(1 for r in facts if r.get("label") == "filing") / len(facts)
                filing_share[(str(rec["arm"]), str(rec["question_id"]))].append(share)
        attribution_rows = [
            {
                "agent": agent,
                "stratum": "contamination",
                "arm": arm,
                "question_id": qid,
                "filing_share": fmean(v),
            }
            for (arm, qid), v in sorted(filing_share.items())
        ]
        attr_means = stats.question_means(attribution_rows, "filing_share")
        for arm in ("archivist", "both"):
            pairs = stats.paired(attr_means, agent, "contamination", arm)
            if pairs:
                attribution[rule][arm] = stats.paired_bootstrap(
                    pairs, stats.mean_difference, args.resamples, args.seed
                ).as_dict()
    # Amendment 5: the contamination accuracy with its audit corrections (pass B rule).
    audit_paths, applied, superseded = audit or ([], [], [])
    corrections = report.corrections_by_run(applied) if audit_paths else None
    rows = [r for r in analysis_rows(cev, data, corrections, "B") if r["agent"] == agent]
    per_arm = {}
    for arm in ARMS:
        trap = list(trap_means.get((agent, "contamination", arm), {}).values())
        acc = [r["accuracy"] for r in rows if r["arm"] == arm and r["accuracy"] is not None]
        tok = [r["total_tokens"] for r in rows if r["arm"] == arm and r["total_tokens"] is not None]
        per_arm[arm] = {
            "trap_adoption_question_mean": round(fmean(trap), 4) if trap else None,
            "trap_questions": len(trap),
            "mean_run_accuracy": round(fmean(acc), 4) if acc else None,
            "mean_run_total_tokens": round(fmean(tok)) if tok else None,
            "completed_runs": sum(1 for r in recs if r["arm"] == arm),
        }
    label = (
        "pre registered (section 14, H6: trap adoption); accuracy and tokens per section 6; "
        "filing attribution exploratory (labelled on each result)"
    )
    if agent == "claude_code":
        label = (
            "replication, reduced power (section 15: H6 applied by hand to Claude Code); "
            "accuracy and tokens per section 6; filing attribution exploratory (labelled on "
            "each result)"
        )
    section = {
        "label": label,
        **({"agent": agent} if agent != "codex" else {}),
        "questions_sha256": report.sha256_file(cq),
        "per_arm": per_arm,
        "h6_trap_adoption_web_minus_arm": h6,
        "h6_rule": (
            "for archivist and both: trap adoption difference (web minus arm) CI lower bound "
            "above 0 is supported; point estimate above 0 is partly supported; else not "
            "supported (applied by hand)"
        ),
        # Attribution is exploratory under both rules (section 14.3 and deviation G2).
        "filing_attribution_arm_minus_web": {
            "label": "exploratory",
            "rule": "primary",
            "by_arm": attribution["primary"],
        },
        "filing_attribution_arm_minus_web_numbers_only": {
            "label": "exploratory",
            "rule": "numbers_only",
            "by_arm": attribution["numbers_only"],
        },
        "comparisons": stats.analyze(rows, args.resamples, args.seed),
        "runs_without_trap_judgement": sorted(trap_missing),
        "runs_without_claims": sorted(claims_missing),
        "runs_with_claim_grade_error": sorted(claims_errors),
    }
    if audit_paths:
        section |= {
            "audit_files": report.audit_digests(audit_paths),
            "audit_corrections": [
                {
                    k: c.get(k)
                    for k in ("run_id", "fact_id", "source", "judge_verdict", "audit_verdict")
                }
                for c in applied
            ],
            "superseded_corrections": superseded,
        }
    return section, _contamination_inputs(args, out), claim_rows


def _load_contamination_audit(args: argparse.Namespace) -> ContaminationAudit | None:
    """The ``--contamination-audit`` files validated against the contamination judgements
    (pass B rule) with their (applied, superseded) corrections; None (printed why) when one is
    invalid."""
    paths = _contamination_audit_files(args)
    if not paths:
        return [], [], []
    judgements = report._judgements(Path(args.contamination_evidence_dir))
    try:
        applied, superseded = report.load_audit_sources(paths, judgements, "B")
    except report.AuditError as exc:
        print(f"contamination audit not applied: {exc}", file=sys.stderr)
        return None
    return paths, applied, superseded


def _contamination_audit_files(args: argparse.Namespace) -> list[Path]:
    """The ``--contamination-audit`` files (amendment 5), or none; a missing file exits 1."""
    given = getattr(args, "contamination_audit", None)
    if not given:
        return []
    try:
        return report.audit_files([Path(a) for a in given])
    except report.AuditError as exc:
        print(f"contamination audit not applied: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _contamination_inputs(args: argparse.Namespace, out: Path) -> list[tuple[str, Path]]:
    cev = Path(args.contamination_evidence_dir)
    recs = _completed(cev, args.agent)
    inputs = _grounding_inputs(cev, out, recs, [Path(args.contamination_questions)])
    for rec in recs:
        run_id = str(rec["run_id"])
        inputs += [
            (f"trap/{run_id}", out / grounding.TRAPS_DIR / f"{run_id}.json"),
            (f"judgement/{run_id}", cev / "runs" / run_id / "judgement.json"),
        ]
    inputs += [(f"audit/{p.name}", p) for p in _contamination_audit_files(args)]
    return [(f"contamination/{label}", path) for label, path in inputs]


ROWS_FILE = "grounding-rows.json"


def cmd_grounding_analyze(args: argparse.Namespace) -> int:
    evidence, out = _state_dirs(args)
    assert evidence is not None
    if bool(args.contamination_evidence_dir) != bool(args.contamination_questions):
        raise SystemExit("--contamination-evidence-dir and --contamination-questions go together")
    if args.contamination_audit and not args.contamination_evidence_dir:
        raise SystemExit("--contamination-audit needs --contamination-evidence-dir")
    audit = _load_contamination_audit(args) if args.contamination_evidence_dir else None
    if args.contamination_evidence_dir and audit is None:
        return 1
    table = grounding.HostTable.load(out)
    claude = args.agent == "claude_code"
    recs = _completed(evidence, args.agent)
    missing_sources = grounding.missing_sources(out, (str(r["run_id"]) for r in recs))
    if missing_sources:
        raise SystemExit(
            f"{len(missing_sources)} runs have no complete sources file (run grounding-sources): "
            + ", ".join(missing_sources)
        )
    exposure_rows = [
        grounding.exposure_row(
            _run_meta(r),
            grounding.run_exposure(grounding.run_sources(out, str(r["run_id"])), table),
            summaries=claude,
        )
        for r in recs
    ]
    scored = [r for r in recs if r.get("stratum") != questions.CONTROL_STRATUM]
    claim_rows, claims_missing, claims_errors = _reliance(scored, out, table)
    checked = (
        _checked_rows(out) if (_crosscheck_dir(out) / crosscheck.SAMPLE_FILE).is_file() else []
    )
    extension = (
        _checked_rows(out, True)
        if (_crosscheck_dir(out) / crosscheck.EXTENSION_FILE).is_file()
        else []
    )
    inputs = _grounding_inputs(evidence, out, recs)
    header: dict[str, Any] = {
        "amendment": "pre registration section 14 (amendment 3)",
        "label": "exploratory: every measure over the codex campaign runs",
        "scope": "Codex campaign runs (completed, uncontaminated); cross check: "
        + crosscheck.SCOPE,
    }
    if claude:
        header = {
            "agent": args.agent,
            "amendment": (
                "pre registration section 15 (amendment 4): the section 14 measures applied to "
                "Claude Code streams"
            ),
            "label": (
                "replication, reduced power (section 15); exposure and reliance exploratory as "
                "in section 14"
            ),
            "scope": (
                "Claude Code runs of this campaign (completed, uncontaminated); WebSearch "
                "summaries outside exposure, counted as search_summaries; cross check not "
                "repeated (section 15)"
            ),
        }
    result: dict[str, Any] = {
        "harness_version": __version__,
        **header,
        "runs": len(recs),
        "exposure": grounding.exposure_cells(exposure_rows, args.resamples, args.seed),
        "reliance": [
            cell
            for rule in grounding.RULES
            for cell in grounding.reliance_cells(
                claim_rows,
                [r for r in exposure_rows if r["stratum"] != questions.CONTROL_STRATUM],
                args.resamples,
                args.seed,
                rule,
            )
        ],
        "runs_without_claims": sorted(claims_missing),
        "runs_with_claim_grade_error": sorted(claims_errors),
        "crosscheck": {
            "label": "exploratory",
            "scope": crosscheck.SCOPE,
            "rows": len(checked),
            "rates": crosscheck.rate_rows(checked),
        },
        # Deviation G4: the extension sources traps only; bins counted separately, no rates.
        "crosscheck_extension": {
            "label": crosscheck.EXTENSION_LABEL,
            "rows": len(extension),
            "bins": crosscheck.extension_bins(extension),
        },
    }
    contamination_claims: list[dict[str, Any]] = []
    if args.contamination_evidence_dir:
        section, more, contamination_claims = _contamination(args, out, table, audit)
        result["contamination"] = section
        inputs += more
    result["unclassified_hosts"] = sorted({grounding.display_host(h) for h in table.unclassified})
    result["inputs_fingerprint"] = grounding.fingerprint(inputs)
    grounding.write_json(out / grounding.ANALYSIS_FILE, result)
    grounding.write_json(
        out / ROWS_FILE,
        {
            "inputs_fingerprint": result["inputs_fingerprint"],
            "exposure_runs": exposure_rows,
            "claims": claim_rows,
            "contamination_claims": contamination_claims,
            "crosscheck": checked,
            "crosscheck_extension": extension,
        },
    )
    print(f"grounding analysis written to {out / grounding.ANALYSIS_FILE}")
    return 0


HOST_COLUMNS = ("host", "category", "basis", "reason", "runs", "kinds")
EXPOSURE_CELL_COLUMNS = (
    "label",
    "rule",
    "arm",
    "stratum",
    "measure",
    "kind",
    "name",
    "estimate",
    "ci_low",
    "ci_high",
    "n_questions",
    "runs",
    "runs_with",
    "sources",
    "total",
    "claims",
    "of",
)
EXTENSION_BIN_COLUMNS = (
    "label",
    "category",
    "rows",
    *crosscheck.BINS,
    "invalid",
    "grade_error",
    "unchecked",
)
SAMPLE_COLUMNS = (
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
    "occurrences",
    "status",
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
    "error",
)


def host_rows(out: Path, table: grounding.HostTable) -> list[dict[str, Any]]:
    """``host-categories.csv``: every host with its category and basis; filing hosts collapse
    into one ``filing-source`` row."""
    merged: dict[str, dict[str, Any]] = {}
    for entry in grounding.read_json(out / grounding.HOST_INVENTORY) or []:
        category, basis, reason = table.lookup(entry["host"])
        host = grounding.display_host(entry["host"])
        row = merged.setdefault(
            host,
            {
                "host": host,
                "category": category,
                "basis": basis,
                "reason": reason,
                "runs": set(),
                "kinds": set(),
            },
        )
        row["runs"].update(entry.get("runs") or [])
        row["kinds"].update(entry.get("kinds") or [])
    return [
        dict(r, runs=len(r["runs"]), kinds="; ".join(sorted(r["kinds"])))
        for _, r in sorted(merged.items())
    ]


def _flat_cell(cell: dict[str, Any]) -> dict[str, Any]:
    interval = cell.get("interval") or {}
    ci = interval.get("ci95") or [None, None]
    return {
        **{k: v for k, v in cell.items() if k != "interval"},
        "estimate": interval.get("estimate"),
        "ci_low": ci[0],
        "ci_high": ci[1],
        "n_questions": interval.get("n_questions"),
    }


def cmd_grounding_report(args: argparse.Namespace) -> int:
    evidence, out = _state_dirs(args)
    assert evidence is not None
    analysis = grounding.read_json(out / grounding.ANALYSIS_FILE)
    rows = grounding.read_json(out / ROWS_FILE)
    if analysis is None or rows is None:
        raise SystemExit("no grounding analysis: run grounding-analyze first")
    if str(analysis.get("agent") or "codex") != args.agent:
        print(
            f"refusing to report: the grounding analysis is of agent "
            f"{analysis.get('agent') or 'codex'}, not --agent {args.agent}",
            file=sys.stderr,
        )
        return 1
    table = grounding.HostTable.load(out)
    recs = _completed(evidence, args.agent)
    inputs = _grounding_inputs(evidence, out, recs)
    if args.contamination_audit and not args.contamination_evidence_dir:
        raise SystemExit("--contamination-audit needs --contamination-evidence-dir")
    if args.contamination_evidence_dir:
        inputs += _contamination_inputs(args, out)
    if grounding.fingerprint(inputs) != analysis.get("inputs_fingerprint") or rows.get(
        "inputs_fingerprint"
    ) != analysis.get("inputs_fingerprint"):
        print(
            "refusing to report: the grounding inputs changed since grounding-analyze (or other "
            "--contamination options); rerun grounding-analyze",
            file=sys.stderr,
        )
        return 1
    results = Path(args.results)
    results.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {
        "host-categories.csv": report.csv_text(HOST_COLUMNS, host_rows(out, table)),
        "exposure-runs.csv": report.csv_text(
            grounding.exposure_run_columns(args.agent), rows["exposure_runs"]
        ),
        "exposure-cells.csv": report.csv_text(
            EXPOSURE_CELL_COLUMNS,
            [_flat_cell(c) for c in [*analysis["exposure"], *analysis["reliance"]]],
        ),
        "crosscheck-sample.csv": report.csv_text(SAMPLE_COLUMNS, rows["crosscheck"]),
        "crosscheck-extension-bins.csv": report.csv_text(
            EXTENSION_BIN_COLUMNS,
            (analysis.get("crosscheck_extension") or {}).get("bins") or [],
        ),
        "analysis-grounding.json": report.tidy(report.compact_json(analysis)),
    }
    for stale in sorted(results.glob("claims-*.csv")):
        stale.unlink()
    claims = sorted(rows["claims"], key=lambda r: (str(r["run_id"]), int(r["claim_index"])))
    for n, text in enumerate(report.shard_csv(grounding.CLAIM_COLUMNS, claims), start=1):
        files[f"claims-{n:03d}.csv"] = text
    if rows.get("contamination_claims"):
        contamination = sorted(
            rows["contamination_claims"], key=lambda r: (str(r["run_id"]), int(r["claim_index"]))
        )
        (results / "contamination").mkdir(exist_ok=True)
        for stale in sorted((results / "contamination").glob("claims-*.csv")):
            stale.unlink()
        for n, text in enumerate(report.shard_csv(grounding.CLAIM_COLUMNS, contamination), 1):
            files[f"contamination/claims-{n:03d}.csv"] = text
    for name, text in files.items():
        (results / name).write_text(text, encoding="utf-8")
    problems = report.check_files([p for p in results.rglob("*") if p.is_file()])
    for problem in problems:
        print(f"CHECK FAILED {problem}", file=sys.stderr)
    print(f"grounding report written to {results}" + (" with failed checks" if problems else ""))
    return 1 if problems else 0


def cmd_verify_quotes(args: argparse.Namespace) -> int:
    """Every sourced fact's quote against its stored passage (``archivist read passage <chunk>
    --window 0 --format json``, cached under ``<out>/passages``); with ``--key-corrections``
    (amendment 5) the corrected key's facts plus every ``also_stated`` quote of an applied
    correction against its own chunk."""
    data = questions.load(Path(args.questions))
    data, chosen = _key_corrections(args, data)
    _, out = _state_dirs(args)
    cache = out / "passages"
    facts = [
        f
        for q in data["questions"]
        for f in q.get("facts") or []
        if f.get("kind") == "sourced" and (f.get("source") or {}).get("chunk_id")
    ]
    also = [(str(c["fact_id"]), entry) for c in chosen for entry in c.get("also_stated") or []]
    chunks = {str(f["source"]["chunk_id"]) for f in facts}
    chunks |= {str(entry["chunk_id"]) for _fid, entry in also}
    needed = sorted(c for c in chunks if not (cache / f"{c}.json").is_file())
    if needed and args.offline:
        print(f"{len(needed)} passages not cached and --offline given", file=sys.stderr)
        return 1
    if needed and _read_passages(needed, out, args.archivist_ceiling):
        return 1
    problems: list[str] = []
    for fact in facts:
        chunk = str(fact["source"]["chunk_id"])
        passage = (grounding.read_json(cache / f"{chunk}.json") or {}).get("passage") or {}
        problems += questions.verify_quote(fact, passage)
    for fid, entry in also:
        chunk = str(entry["chunk_id"])
        passage = (grounding.read_json(cache / f"{chunk}.json") or {}).get("passage") or {}
        problems += questions.verify_also_stated(fid, entry, passage)
    for problem in problems:
        print(f"VIOLATION {problem}")
    if getattr(args, "key_corrections", None):
        print(_correction_line(data, chosen))
        print(
            f"{len(facts)} sourced facts checked, {len(also)} also stated quotes checked, "
            f"{len(problems)} violations"
        )
    else:
        print(f"{len(facts)} sourced facts checked, {len(problems)} violations")
    return 1 if problems else 0


def _read_passages(needed: Sequence[str], out: Path, ceiling: int) -> int:
    """Read and cache ``needed`` passages under ``<out>/passages`` (one Archivist read each,
    the quota gate checked before every read, readings appended to
    ``<out>/archivist-usage.jsonl``). 0 when all were read, 1 (printed why) on a gate stop or a
    failed read; the passages read before stay cached."""
    cache = out / "passages"
    env = runner.child_env(dict(os.environ), {})
    gate = runner.ArchivistGate(ceiling, env=env)
    out.mkdir(parents=True, exist_ok=True)
    try:
        for chunk in needed:
            blocked = gate.check()  # before every Archivist read
            if blocked:
                print("archivist quota gate: " + "; ".join(blocked), file=sys.stderr)
                return 1
            fetched = fetch_passage(chunk, env)
            if fetched is None:
                print(f"could not read passage {chunk}", file=sys.stderr)
                return 1
            grounding.write_json(cache / f"{chunk}.json", fetched)
    finally:
        for reading in gate.readings:
            grounding._append(out / "archivist-usage.jsonl", reading)
    return 0


def fetch_passage(chunk: str, env: dict[str, str]) -> dict[str, Any] | None:
    """``archivist read passage <chunk> --window 0 --format json`` (one Archivist call). Tests
    replace this function."""
    import subprocess

    try:
        done = subprocess.run(
            ["archivist", "read", "passage", chunk, "--window", "0", "--format", "json"],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        data = json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("passage"), dict) else None


# --- Amendment 5 (pre registration section 16, harness 1.6.0): key corrections, light audit ---

REJUDGE_DIR = "key-rejudge"
PRECHECK_DIR = "precheck"
PRECHECK_FILE = "audit-precheck.csv"
OWNER_LIST_FILE = "audit-owner-list.md"
PRECHECK_COLUMNS = (
    "selection",
    "run_id",
    "agent",
    "arm",
    "stratum",
    "question_id",
    "fact_id",
    "judge_verdict",
    "judge_verdict_source",
    "precheck_verdict",
    "key_check",
    "reason",
    "agree",
    "status",
)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rejudge_scope(
    args: argparse.Namespace, evidence: Path, out: Path
) -> (
    tuple[
        dict[str, Any],
        list[dict[str, Any]],
        dict[str, list[str]],
        list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], Any]],
    ]
    | None
):
    """(corrected questions, applied corrections, corrected fact ids by question, eligible
    runs): every completed, uncontaminated run of a question with an applied correction whose
    judgement has a valid pass B, as (record, corrected question, stored judgement, prior
    re-judge record or None). None (printed why) when a prior record was made under other
    corrections."""
    if not getattr(args, "key_corrections", None):
        raise SystemExit("rejudge-keys needs --key-corrections")
    data, chosen = _key_corrections(args, questions.load(Path(args.questions)))
    digest = questions.corrections_digest(chosen)
    corrected: dict[str, list[str]] = defaultdict(list)
    for item in chosen:
        corrected[str(item["question_id"])].append(str(item["fact_id"]))
    lookup = questions.by_id(data)
    eligible = []
    stale = []
    for rec in _records(evidence):
        qid = str(rec["question_id"])
        if rec["status"] != STATUS_COMPLETED or rec.get("contaminated") or qid not in corrected:
            continue
        run_id = str(rec["run_id"])
        stored = _judgement(evidence / "runs" / run_id)
        if stored is None or judge.pass_verdicts(stored, "B") is None:
            continue
        prior = grounding.read_json(out / REJUDGE_DIR / f"{run_id}.json")
        if isinstance(prior, dict) and prior.get("corrections_sha256") != digest:
            stale.append(run_id)
        eligible.append((rec, lookup[qid], stored, prior))
    if stale:
        print(
            f"refusing: {len(stale)} re-judge records under {out / REJUDGE_DIR} were made with "
            "other key corrections (use a new --out): " + ", ".join(sorted(stale)),
            file=sys.stderr,
        )
        return None
    return data, chosen, dict(corrected), eligible


def _finally_failed(prior: Any) -> bool:
    return (
        isinstance(prior, dict)
        and prior.get("status") != "ok"
        and not grounding.needs_grading(prior)
    )


def cmd_rejudge_keys(args: argparse.Namespace) -> int:
    """Pass B re-judge of the corrected facts (amendment 5, section 16.2): judge calls only."""
    evidence, out = _state_dirs(args)
    assert evidence is not None
    scope = _rejudge_scope(args, evidence, out)
    if scope is None:
        return 1
    data, chosen, corrected, eligible = scope
    print(_correction_line(data, chosen))
    if args.export:
        return _export_rescore(args, out, chosen, corrected, eligible)
    digest = questions.corrections_digest(chosen)
    file_digest = report.sha256_file(Path(args.key_corrections))
    missing: list[str] = []
    todo = [
        entry
        for entry in eligible
        if grounding.needs_grading(entry[3])
        and _answer(evidence, str(entry[0]["run_id"]), missing) is not None
    ]
    failed = sorted(str(e[0]["run_id"]) for e in eligible if _finally_failed(e[3]))
    done_ok = sum(1 for e in eligible if isinstance(e[3], dict) and e[3].get("status") == "ok")
    print(
        f"{len(eligible)} eligible answers: {done_ok} re-judged, {len(todo)} to re-judge, "
        f"{len(failed)} failed after two batches (stored verdicts stay)"
    )
    for rec, _q, _stored, _prior in todo:
        qid = str(rec["question_id"])
        print(f"  {rec['run_id']} ({qid}: {', '.join(corrected[qid])})")
    for run_id in failed:
        print(f"failed after two batches, stored verdict stays: {run_id}", file=sys.stderr)
    if not _grading_preamble(
        args, len(todo), len(todo), "answers to re-judge on the corrected key (pass B)"
    ):
        return _missing_answers(missing)
    executors = _executors(args, out, "B")
    grader = grounding.Grader("B", out, executors[0], args.max_calls, judge.VERDICT_SCHEMA)

    def work(
        entry: tuple[dict[str, Any], dict[str, Any], dict[str, Any], Any], worker: int
    ) -> None:
        rec, q, stored, prior = entry
        run_id, qid = str(rec["run_id"]), str(rec["question_id"])
        answer = report.answer_text(evidence / "runs" / run_id) or ""  # checked above
        # The section 12 prompt exactly as for the stored judgement (same run id seed), with the
        # corrected fields in place.
        prompt = judge.build_prompt(q, answer, run_id)
        label_of = judge.fact_labels(q)
        parsed, error, calls = grader.grade(
            run_id,
            prompt,
            partial(judge.parse_verdict, labels=set(label_of.values())),
            executors[worker],
        )
        stored_b = judge.pass_verdicts(stored, "B") or {}
        fixed = corrected[qid]
        extra: dict[str, Any] = {
            **_run_meta(rec),
            "agent": rec.get("agent"),
            "corrections_sha256": digest,
            "corrections_file_sha256": file_digest,
            "prompt_sha256": _sha256_text(prompt),
            "corrected_facts": fixed,
            "stored": stored_b,
            "stored_complete": ((stored.get("passes") or {}).get("B") or {}).get("complete"),
        }
        batches_n = int((prior or {}).get("batches") or 0) + 1
        if parsed is not None:
            by_label = {label: fact for fact, label in label_of.items()}
            verdicts = {by_label[label]: v for label, v in parsed[0].items()}
            others = sorted(f for f in verdicts if f not in fixed and f in stored_b)
            agreed = sum(1 for f in others if verdicts[f] == stored_b[f])
            record = grounding.pass_record(
                "ok",
                batches_n,
                calls,
                verdicts=dict(sorted(verdicts.items())),
                complete_rejudged=parsed[1],
                taken={f: verdicts[f] for f in fixed},
                uncorrected_agreement={
                    "label": "descriptive stability check",
                    "facts": len(others),
                    "agreed": agreed,
                    "share": round(agreed / len(others), 4) if others else None,
                },
                **extra,
            )
        else:
            record = grounding.pass_record("grade_error", batches_n, calls, error=error, **extra)
        grounding.write_json(out / REJUDGE_DIR / f"{run_id}.json", record)
        print(f"{run_id}: {record['status']} taken={record.get('taken')}")

    done, stopped = 0, None
    try:
        done, stopped = grounding.run_items(todo, work, args.concurrency, grader)
    finally:
        rc = _finish(executors, out, "B", stopped, len(todo) - done)
    for entry in todo:
        prior = grounding.read_json(out / REJUDGE_DIR / f"{entry[0]['run_id']}.json")
        if _finally_failed(prior):
            print(
                f"failed after two batches, stored verdict stays: {entry[0]['run_id']}",
                file=sys.stderr,
            )
    return max(rc, _missing_answers(missing))


def _export_rescore(
    args: argparse.Namespace,
    out: Path,
    chosen: Sequence[dict[str, Any]],
    corrected: dict[str, list[str]],
    eligible: Sequence[tuple[dict[str, Any], dict[str, Any], dict[str, Any], Any]],
) -> int:
    """``rejudge-keys --export CSV`` (no call): one row per run and corrected fact in the audit
    columns plus ``correction_source`` ``key_correction``, ``primary_judge`` B, the stored
    verdict as ``judge_verdict`` and the taken verdict as ``audit_verdict``. Refused (exit 1,
    nothing written) while an eligible run is neither re-judged ok nor failed after its two
    batches; finally failed facts are not exported and are listed."""
    target = Path(args.export)
    by_fact = {str(c["fact_id"]): c for c in chosen}
    pending: list[str] = []
    failed: list[str] = []
    rows: list[dict[str, Any]] = []
    changed = facts_n = agreed_n = 0
    for rec, q, stored, prior in sorted(eligible, key=lambda e: str(e[0]["run_id"])):
        run_id, qid = str(rec["run_id"]), str(rec["question_id"])
        if _finally_failed(prior):
            failed += [f"{run_id} {fid}" for fid in corrected[qid]]
            continue
        if not isinstance(prior, dict) or prior.get("status") != "ok":
            pending.append(run_id)
            continue
        facts = {str(f["id"]): f for f in q.get("facts") or []}
        passes = stored.get("passes") or {}
        stored_b = judge.pass_verdicts(stored, "B") or {}
        agreement = prior.get("uncorrected_agreement") or {}
        facts_n += int(agreement.get("facts") or 0)
        agreed_n += int(agreement.get("agreed") or 0)
        for fid in corrected[qid]:
            taken = str((prior.get("taken") or {}).get(fid))
            was = stored_b.get(fid)
            changed += taken != was
            fields = ", ".join(sorted(by_fact[fid].get("fields") or {}))
            rows.append(
                {
                    "selection": report.KEY_CORRECTION_SOURCE,
                    "run_id": run_id,
                    "agent": rec.get("agent"),
                    "arm": rec.get("arm"),
                    "stratum": rec.get("stratum"),
                    "question_id": qid,
                    "question": q.get("question"),
                    "fact_id": fid,
                    **report._fact_row(facts[fid]),
                    "judge_verdict": was,
                    "primary_judge": "B",
                    **{
                        f"judge_{p}_verdict": (
                            (passes.get(p) or {}).get("fact_verdicts") or {}
                        ).get(fid)
                        for p in judge.PASSES
                    },
                    "answer_file": None,
                    "audit_verdict": taken,
                    "audit_note": (
                        f"amendment 5 key correction (pre registration section 16.2): {fid} "
                        f"{fields} replaced; pass B re-judged on the corrected key, stored "
                        f"verdict {was}"
                    ),
                    "correction_source": report.KEY_CORRECTION_SOURCE,
                }
            )
    if pending:
        print(
            f"refusing to export: {len(pending)} eligible runs not yet re-judged (run "
            "rejudge-keys --execute): " + ", ".join(pending),
            file=sys.stderr,
        )
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report.csv_text(report.RESCORE_COLUMNS, rows), encoding="utf-8")
    share = f"{agreed_n / facts_n:.4f}" if facts_n else "n/a"
    print(
        f"rescore: {len(rows)} corrected facts exported to {target} ({changed} changed, "
        f"{len(rows) - changed} unchanged from the stored pass B verdict); stability on the "
        f"uncorrected facts of the same answers: {agreed_n} of {facts_n} agree ({share})",
        file=sys.stderr,
    )
    for item in failed:
        print(
            f"not exported (failed after two batches, stored verdict stays): {item}",
            file=sys.stderr,
        )
    problems = report.check_files([target])
    for problem in problems:
        print(f"CHECK FAILED {problem}", file=sys.stderr)
    return 1 if problems else 0


def _stored_b(evidence: Path, run_id: str) -> dict[str, str] | None:
    """The run's valid stored pass B verdicts (fact id to verdict), else None."""
    return judge.pass_verdicts(_judgement(evidence / "runs" / run_id), "B")


def _rescore_verdicts(
    paths: Sequence[str] | None,
    records: dict[str, dict[str, Any]],
    chosen: Sequence[dict[str, Any]],
    evidence: Path,
) -> dict[tuple[str, str], str]:
    """(run id, fact id) to the taken verdict of rescore CSVs (``rejudge-keys --export``).
    Every row must be a ``key_correction`` row under ``primary_judge`` B on a corrected fact
    of the applied corrections (``chosen``), of a run with a valid stored pass B; raises
    ValueError naming every bad row."""
    corrected = {(str(c["question_id"]), str(c["fact_id"])) for c in chosen}
    out: dict[tuple[str, str], str] = {}
    errors: list[str] = []
    for given in paths or []:
        path = Path(given)
        if not path.is_file():
            raise ValueError(f"key rescore file not found: {path}")
        with path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            need = {"run_id", "fact_id", "audit_verdict", "correction_source", "primary_judge"}
            if need - set(reader.fieldnames or []):
                raise ValueError(f"{path.name} is not a key rescore CSV (lacks {sorted(need)})")
            for n, row in enumerate(reader, start=2):
                line = f"{path.name}:{n}"
                run_id = (row.get("run_id") or "").strip()
                fact_id = (row.get("fact_id") or "").strip()
                source = (row.get("correction_source") or "").strip()
                if source != report.KEY_CORRECTION_SOURCE:
                    errors.append(f"{line}: correction_source {source!r} is not key_correction")
                    continue
                if (row.get("primary_judge") or "").strip() != "B":
                    errors.append(f"{line}: primary_judge is not B")
                    continue
                rec = records.get(run_id)
                if rec is None or (str(rec["question_id"]), fact_id) not in corrected:
                    errors.append(f"{line}: {run_id} {fact_id} is not a corrected fact")
                    continue
                if _stored_b(evidence, run_id) is None:
                    errors.append(f"{line}: {run_id} has no valid stored pass B")
                    continue
                verdict = (row.get("audit_verdict") or "").strip().lower()
                if verdict not in judge.VERDICTS:
                    errors.append(f"{line}: invalid audit_verdict {verdict!r}")
                    continue
                key = (run_id, fact_id)
                if out.get(key, verdict) != verdict:
                    errors.append(f"{line}: conflicting rescore verdicts for {run_id} {fact_id}")
                    continue
                out[key] = verdict
    if errors:
        raise ValueError("; ".join(errors))
    return out


def _precheck_rows(
    paths: Sequence[Path],
    records: dict[str, dict[str, Any]],
    lookup: dict[str, dict[str, Any]],
    rescore: dict[tuple[str, str], str],
    evidence: Path,
) -> list[dict[str, Any]]:
    """The audit queue rows without a human ``audit_verdict``, each with its run record, its
    fact under the (corrected) key and the effective judge verdict (the rescore verdict of a
    corrected fact, else the row's ``judge_verdict``). Raises ValueError naming every problem:
    a queue file exported under another rule than B, an unknown run or fact, a row given
    twice, or a ``judge_verdict`` that is not the run's valid stored pass B verdict."""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    errors: list[str] = []
    for path in paths:
        rules = report._file_rules(path)
        if rules != {"B"}:
            errors.append(f"{path.name}: exported under {', '.join(sorted(rules))}, not B")
            continue
        with path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            need = {"run_id", "fact_id", "judge_verdict", "audit_verdict"}
            if need - set(reader.fieldnames or []):
                raise ValueError(f"{path.name} lacks columns: {', '.join(sorted(need))}")
            for n, row in enumerate(reader, start=2):
                if (row.get("audit_verdict") or "").strip():
                    continue  # a human audit verdict: a person settled the row
                line = f"{path.name}:{n}"
                run_id = (row.get("run_id") or "").strip()
                fact_id = (row.get("fact_id") or "").strip()
                rec = records.get(run_id)
                if rec is None:
                    errors.append(f"{line}: unknown run {run_id!r}")
                    continue
                question = lookup.get(str(rec["question_id"]))
                facts = {str(f.get("id")): f for f in (question or {}).get("facts") or []}
                if question is None or fact_id not in facts:
                    errors.append(f"{line}: unknown fact {fact_id!r} of {run_id}")
                    continue
                if (run_id, fact_id) in seen:
                    errors.append(f"{line}: row {run_id} {fact_id} given twice")
                    continue
                seen.add((run_id, fact_id))
                listed = (row.get("judge_verdict") or "").strip().lower()
                stored = (_stored_b(evidence, run_id) or {}).get(fact_id)
                if stored is None or listed != stored:
                    errors.append(
                        f"{line}: judge_verdict {listed!r} of {run_id} {fact_id} is not the "
                        f"stored pass B verdict {stored!r}"
                    )
                    continue
                if (run_id, fact_id) in rescore:
                    effective, source = rescore[(run_id, fact_id)], "key_rejudge"
                else:
                    effective, source = listed, "stored_judge"
                rows.append(
                    {
                        "selection": (row.get("selection") or "").strip(),
                        "run_id": run_id,
                        "record": rec,
                        "question": question,
                        "fact": facts[fact_id],
                        "fact_id": fact_id,
                        "judge_verdict": effective,
                        "judge_verdict_source": source,
                        "key": precheck.row_key(run_id, fact_id),
                    }
                )
    if errors:
        raise ValueError("; ".join(errors))
    return rows


def _precheck_prompt(row: dict[str, Any], cache: Path, evidence: Path) -> str | None:
    """The pass P prompt a row gets now (from the cached passages and the current answer), or
    None when a passage is not cached or the answer is missing."""
    facts = {str(f.get("id")): f for f in row["question"].get("facts") or []}
    passages: dict[str, dict[str, Any]] = {}
    for chunk in precheck.needed_chunks(row["fact"], facts):
        cached = grounding.read_json(cache / f"{chunk}.json")
        if not isinstance(cached, dict):
            return None
        passages[chunk] = cached.get("passage") or {}
    answer = report.answer_text(evidence / "runs" / row["run_id"])
    if answer is None or not answer.strip():
        return None
    return precheck.pass_p_prompt(row["question"], row["fact"], passages, answer)[0]


def _current_precheck(row: dict[str, Any], out: Path, evidence: Path) -> Any:
    """The row's stored pass P record when it was made on the prompt the row gets now (same
    ``prompt_sha256``: same key, passages and answer), else None: a record of another prompt
    is not reused and its batches do not count."""
    prior = grounding.read_json(out / PRECHECK_DIR / f"{row['key']}.json")
    if not isinstance(prior, dict):
        return None
    prompt = _precheck_prompt(row, out / "passages", evidence)
    if prompt is None or prior.get("prompt_sha256") != _sha256_text(prompt):
        return None
    return prior


def cmd_audit_precheck(args: argparse.Namespace) -> int:
    """Pass P (amendment 5, section 16.3): one Codex call per audit queue row without a human
    audit verdict, blind to the judge verdict; ``--results`` gets ``audit-precheck.csv`` and,
    when a row disputes, ``audit-owner-list.md``."""
    evidence, out = _state_dirs(args)
    assert evidence is not None
    data, chosen = _key_corrections(args, questions.load(Path(args.questions)))
    if args.key_corrections:
        print(_correction_line(data, chosen))
    if args.key_rescore and not args.key_corrections:
        print(
            "--key-rescore needs --key-corrections (the corrections it rescored)", file=sys.stderr
        )
        return 2
    records = {str(r["run_id"]): r for r in _records(evidence)}
    try:
        rescore = _rescore_verdicts(args.key_rescore, records, chosen, evidence)
        paths = report.audit_files([Path(a) for a in args.audit_queue])
        rows = _precheck_rows(paths, records, questions.by_id(data), rescore, evidence)
    except (ValueError, OSError) as exc:  # AuditError is a ValueError
        print(f"refusing the pre check: {exc}", file=sys.stderr)
        return 1
    cache = out / "passages"
    missing: list[str] = []
    todo = []
    for row in rows:
        prior = _current_precheck(row, out, evidence)
        if grounding.needs_grading(prior) and _answer(evidence, row["run_id"], missing):
            todo.append((row, prior))
    facts_of = {
        r["key"]: {str(f.get("id")): f for f in r["question"].get("facts") or []} for r in rows
    }
    needed = sorted(
        {
            chunk
            for row, _prior in todo
            for chunk in precheck.needed_chunks(row["fact"], facts_of[row["key"]])
            if not (cache / f"{chunk}.json").is_file()
        }
    )
    print(
        f"{len(rows)} audit rows without a human audit verdict: {len(rows) - len(todo)} pre "
        f"checked or failed, {len(todo)} to pre check; {len(needed)} passages to read (Archivist)"
    )
    if not todo:
        if not args.execute:
            print("dry run: nothing to pre check; pass --execute to write the results")
            return _missing_answers(sorted(set(missing)))
        return max(
            _precheck_results(args, out, rows, evidence), _missing_answers(sorted(set(missing)))
        )
    if not _grading_preamble(args, len(todo), len(todo), "audit rows for pass P"):
        return _missing_answers(sorted(set(missing)))
    if needed and _read_passages(needed, out, args.archivist_ceiling):
        return 1  # no model call: every passage is read before the batch
    passages = {
        chunk: (grounding.read_json(cache / f"{chunk}.json") or {}).get("passage") or {}
        for row, _prior in todo
        for chunk in precheck.needed_chunks(row["fact"], facts_of[row["key"]])
    }
    executors = _executors(args, out, "P")
    grader = grounding.Grader("P", out, executors[0], args.max_calls, precheck.PASS_P_SCHEMA)

    def work(entry: tuple[dict[str, Any], Any], worker: int) -> None:
        row, prior = entry
        rec = row["record"]
        answer = report.answer_text(evidence / "runs" / row["run_id"]) or ""  # checked above
        prompt, given = precheck.pass_p_prompt(row["question"], row["fact"], passages, answer)
        parsed, error, calls = grader.grade(
            row["key"],
            prompt,
            partial(precheck.parse_pass_p, passage_given=given),
            executors[worker],
        )
        batches_n = int((prior or {}).get("batches") or 0) + 1
        extra: dict[str, Any] = {
            **_run_meta(rec),
            "agent": rec.get("agent"),
            "fact_id": row["fact_id"],
            "selection": row["selection"],
            "passage_given": given,
            "passages": precheck.needed_chunks(row["fact"], facts_of[row["key"]]),
            "prompt_sha256": _sha256_text(prompt),
        }
        if parsed is not None:
            record = grounding.pass_record("ok", batches_n, calls, **parsed, **extra)
        else:
            record = grounding.pass_record("grade_error", batches_n, calls, error=error, **extra)
        grounding.write_json(out / PRECHECK_DIR / f"{row['key']}.json", record)
        print(f"{row['key']}: {record['status']} {record.get('verdict') or ''}".rstrip())

    done, stopped = 0, None
    try:
        done, stopped = grounding.run_items(todo, work, args.concurrency, grader)
    finally:
        rc = _finish(executors, out, "P", stopped, len(todo) - done)
    rc = max(rc, _missing_answers(sorted(set(missing))))
    if rc:
        return rc
    return _precheck_results(args, out, rows, evidence)


def _precheck_results(
    args: argparse.Namespace, out: Path, rows: Sequence[dict[str, Any]], evidence: Path
) -> int:
    """``audit-precheck.csv`` (one row per checked row) and, when any row disputes,
    ``audit-owner-list.md`` in ``--results``; written only once every row is final (pre checked
    ok, or failed after its retry and one later batch). ``report.check_files`` on both."""
    table: list[dict[str, Any]] = []
    disputed: list[dict[str, Any]] = []
    pending: list[str] = []
    for row in rows:
        result = _current_precheck(row, out, evidence)  # a record of another prompt is pending
        rec = row["record"]
        base = {
            "selection": row["selection"],
            "run_id": row["run_id"],
            "agent": rec.get("agent"),
            "arm": rec.get("arm"),
            "stratum": rec.get("stratum"),
            "question_id": rec.get("question_id"),
            "fact_id": row["fact_id"],
            "judge_verdict": row["judge_verdict"],
            "judge_verdict_source": row["judge_verdict_source"],
        }
        if isinstance(result, dict) and result.get("status") == "ok":
            agree = precheck.agrees(result, row["judge_verdict"])
            line = base | {
                "precheck_verdict": result.get("verdict"),
                "key_check": result.get("key_check"),
                "reason": result.get("reason"),
                "agree": agree,
                "status": "ok",
            }
        elif _finally_failed(result):
            agree = False
            line = base | {"reason": precheck.FAILED_REASON, "agree": False, "status": "failed"}
        else:
            pending.append(row["key"])
            continue
        table.append(line)
        if not agree:
            disputed.append(line | {"fact": row["fact"], "question": row["question"]})
    if pending:
        print(
            f"{len(pending)} rows still need pass P (resume later); results not written: "
            + ", ".join(pending),
            file=sys.stderr,
        )
        return 1
    results = Path(args.results)
    results.mkdir(parents=True, exist_ok=True)
    written = [results / PRECHECK_FILE]
    written[0].write_text(report.csv_text(PRECHECK_COLUMNS, table), encoding="utf-8")
    owner_list = results / OWNER_LIST_FILE
    if disputed:
        owner_list.write_text(_owner_list(disputed, len(table)), encoding="utf-8")
        written.append(owner_list)
    elif owner_list.is_file():
        owner_list.unlink()  # a list from an earlier pre check no longer holds
    agreed = len(table) - len(disputed)
    print(
        f"pre check: {agreed} of {len(table)} rows agree (agent pre checked), {len(disputed)} "
        f"to the human review list ({', '.join(p.name for p in written)})"
    )
    problems = report.check_files(written)
    for problem in problems:
        print(f"CHECK FAILED {problem}", file=sys.stderr)
    return 1 if problems else 0


def _owner_list(disputed: Sequence[dict[str, Any]], checked: int) -> str:
    md = report._md_cell
    lines = [
        "# Audit human review list (amendment 5, light audit route)",
        "",
        f"{len(disputed)} of {checked} pre checked rows dispute the judge (pre registration "
        "section 16.3): the pre check verdict differs from the effective judge verdict, the "
        "key check is contradicted or not in the passage, or the pre check failed after its "
        "retry and one later batch. Each row keeps the judge score until a person rules: fill "
        "`audit_verdict` (`correct`, `incorrect` or `missing`) and `audit_note` for it in its "
        "audit queue CSV. Agreed rows are agent pre checked, never human audited.",
        "",
        "| row | judge verdict | pre check verdict | key check | reason | filing evidence |",
        "|---|---|---|---|---|---|",
    ]
    for line in disputed:
        facts = {str(f.get("id")): f for f in line["question"].get("facts") or []}
        evidence = []
        for link, text in precheck.evidence_cells(line["fact"], facts):
            part = md(text)
            if link and questions.PERMALINK_RE.match(str(link)):
                part = f"[permalink]({link}) {part}"
            evidence.append(part)
        lines.append(
            f"| {md(line['run_id'])} {md(line['fact_id'])} ({md(line['selection'])}) | "
            f"{md(line['judge_verdict'])} ({md(line['judge_verdict_source'])}) | "
            f"{md(line.get('precheck_verdict') or 'none')} | {md(line.get('key_check') or '')} | "
            f"{md(line.get('reason'))} | {'<br>'.join(evidence)} |"
        )
    return report.tidy("\n".join(lines))


def cmd_estimate(args: argparse.Namespace) -> int:
    data = questions.load(Path(args.questions))
    counts: dict[str, dict[str, int]] = {"codex": {}, "claude_code": {}}
    subset = set(questions.claude_subset(data))
    for q in data["questions"]:
        stratum = str(q["stratum"])
        counts["codex"][stratum] = counts["codex"].get(stratum, 0) + 1
        if q["id"] in subset:
            counts["claude_code"][stratum] = counts["claude_code"].get(stratum, 0) + 1
    if args.ledger:
        raw = [json.loads(x) for x in Path(args.ledger).read_text().splitlines() if x.strip()]
    elif args.evidence_dir:
        raw = _records(Path(args.evidence_dir))  # re-extracted records win over ledger rows
    else:
        raw = []
    smoke = cost.ledger_rows(raw)
    pilot = cost.pilot_rows(json.loads(Path(args.pilot).read_text())) if args.pilot else []
    models: dict[str, str] = {agent: cfg.model for agent, cfg in DEFAULT_CONFIGS.items()}
    result = cost.build_estimate(counts, smoke, pilot, models, args.repetitions)
    if args.format == "json":
        print(json.dumps(result, indent=2))
        return 0
    for agent, block in result["agents"].items():
        print(
            f"### {agent} ({block['model']}): {block['runs']} runs, "
            f"{block['usd_api_equivalent']} USD API list price equivalent, "
            f"{block['archivist_calls']} Archivist calls"
        )
        print("| stratum | arm | runs | tokens per run | USD | Archivist calls | profile basis |")
        print("|---|---|---:|---:|---:|---:|---|")
        for line in block["lines"]:
            print(
                f"| {line['stratum']} | {line['arm']} | {line['runs']} | "
                f"{line['tokens_per_run']:,} | {line['usd']:.2f} | {line['archivist_calls']} | "
                f"{line['basis']} |"
            )
        print()
    print(f"Measured Claude to Codex token ratio (stratum/arm): {result['claude_ratio']}")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="archivist_bench", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser, needs_questions: bool = True) -> None:
        if needs_questions:
            sp.add_argument("--questions", required=True, help="questions.json")
        sp.add_argument(
            "--evidence-dir", required=True, help="this campaign's own evidence directory"
        )

    def claude_account(sp: argparse.ArgumentParser, needed: str) -> None:
        sp.add_argument(
            "--claude-config-dir",
            metavar="DIR",
            help="the Claude account (an existing CLAUDE_CONFIG_DIR) of every Claude call and of "
            f"the plan gate's Claude probe; required for {needed}, ignored otherwise; never read "
            "from the environment",
        )

    v = sub.add_parser("validate-questions", help="check the question set")
    v.add_argument("--questions", required=True)
    v.add_argument(
        "--contamination",
        action="store_true",
        help="validate the contamination stratum rules (pre registration section 14)",
    )
    v.add_argument(
        "--crosscheck",
        metavar="OUT",
        help="with --contamination: the grounding state; every crosscheck_rows id must be a "
        "pass C result with status ok, contradicted for trap questions, confirmed for controls",
    )
    key_corrections(v)
    v.set_defaults(func=cmd_validate)

    for name, func in (("plan", cmd_plan), ("run", cmd_run)):
        sp = sub.add_parser(name, help=f"{name} the run matrix")
        sp.add_argument("--questions", required=True, help="questions.json")
        sp.add_argument(
            "--evidence-dir",
            help="this campaign's own evidence directory (required with --execute)",
        )
        sp.add_argument("--agent", choices=["codex", "claude_code", "all"], default="all")
        sp.add_argument("--arms", nargs="*", choices=list(ARMS))
        sp.add_argument("--question", action="append", help="limit to these question ids")
        sp.add_argument("--repetitions", type=int, default=REPETITIONS)
        sp.add_argument("--codex-auth", choices=["chatgpt", "api_key"])
        sp.add_argument("--claude-auth", choices=["subscription", "api_key"])
        sp.add_argument("--timeout-s", type=int)
        sp.add_argument("--allow-invalid-set", action="store_true", help=argparse.SUPPRESS)
        claude_account(sp, "run --execute with a Claude Code run in the plan")
        if name == "run":
            sp.add_argument("--execute", action="store_true", help="actually spawn agents")
            sp.add_argument("--max-runs", type=int, help="refuse plans (and attempts) beyond it")
            sp.add_argument("--concurrency", type=int, default=1)
            sp.add_argument(
                "--stop-at-window",
                type=_percent,
                default=DEFAULT_STOP_AT_WINDOW,
                metavar="PCT",
                help="start no new attempt once a run reports a plan window at or above PCT "
                "percent (1 to 100, default 90)",
            )
            sp.add_argument(
                "--max-window-step",
                type=_step,
                default=probe.DEFAULT_MAX_WINDOW_STEP,
                metavar="POINTS",
                help="stop the batch when a Codex window rises more than POINTS percentage "
                "points between consecutive probes (default 5)",
            )
            sp.add_argument(
                "--rerun-invalid",
                action="store_true",
                help="with --resume: give each cell whose only final records are invalid one "
                "rerun sequence (pre registration section 10): attempts numbered after its last, "
                "up to three when earlier ones end infra_error; never a second sequence",
            )
            sp.add_argument(
                "--resume",
                action="store_true",
                help="continue a campaign: skip cells with a final record, continue infra only "
                "cells at their next attempt (at most three), run missing cells",
            )
            sp.add_argument(
                "--archivist-ceiling",
                type=_ceiling,
                default=DEFAULT_ARCHIVIST_CEILING,
                metavar="CALLS",
                help="before each archivist or both arm launch, read `archivist usage`; stop "
                "the batch at cli_this_month >= CALLS or on a failed read (default 9800)",
            )
        sp.set_defaults(func=func)

    e = sub.add_parser("extract", help="re-extract run directories into records")
    e.add_argument("run_dir", nargs="+")
    e.set_defaults(func=cmd_extract)

    j = sub.add_parser(
        "judge", help="blinded cross family judge, Claude Code and Codex (dry run by default)"
    )
    common(j)
    j.add_argument("--execute", action="store_true")
    j.add_argument("--max-calls", type=int)
    j.add_argument("--force", action="store_true")
    j.add_argument("--run-id", action="append", help="limit to these run ids")
    j.add_argument(
        "--passes",
        nargs="+",
        choices=list(judge.PASSES),
        default=list(judge.PASSES),
        help="judge passes to run (default A B). Fewer than both (amendment 2: --passes B) "
        "skips answers whose judgement has a valid verdict for them, runs them otherwise and "
        "merges them into the judgement, keeping the other pass untouched; without pass A no "
        "Claude executor, plan gate reading or probe is created or called",
    )
    j.add_argument(
        "--stop-at-window",
        type=_percent,
        default=DEFAULT_STOP_AT_WINDOW,
        metavar="PCT",
        help="refuse a judge call when a plan window is at or above PCT percent (default 90)",
    )
    j.add_argument(
        "--max-window-step",
        type=_step,
        default=probe.DEFAULT_MAX_WINDOW_STEP,
        metavar="POINTS",
        help="stop when a Codex window rises more than POINTS between probes (default 5)",
    )
    claude_account(j, "judge --execute with pass A")
    j.set_defaults(func=cmd_judge)

    pp = sub.add_parser(
        "plan-probe",
        help="one Claude plan probe call on the chosen account, logged; prints the reading and "
        "the gate's refusal reasons (exit 0 admit, 1 refuse or probe failure)",
    )
    pp.add_argument("--evidence-dir", required=True, help="the campaign's evidence directory")
    claude_account(pp, "plan-probe")
    pp.add_argument(
        "--stop-at-window",
        type=_percent,
        default=DEFAULT_STOP_AT_WINDOW,
        metavar="PCT",
        help="the gate threshold the reading is checked against (1 to 100, default 90)",
    )
    pp.set_defaults(func=cmd_plan_probe)

    a = sub.add_parser("analyze", help="paired bootstrap, completion and the run ledger")
    common(a, needs_questions=False)
    a.add_argument("--questions")
    a.add_argument("--format", choices=["json", "ledger"], default="json")
    a.add_argument("--resamples", type=int, default=BOOTSTRAP_RESAMPLES)
    a.add_argument("--seed", type=int, default=BOOTSTRAP_SEED)
    a.add_argument(
        "--audit",
        nargs="+",
        help="audit CSV files or directories (every audit-queue-*.csv inside) with human "
        "audit_verdict values to apply; validated all or nothing",
    )
    a.add_argument(
        "--primary-judge",
        choices=list(judge.PRIMARY_JUDGES),
        default=judge.DEFAULT_PRIMARY_JUDGE,
        help="scoring rule: combined (amendment 1, default) or B (amendment 2: the pass B "
        "verdict scores every fact)",
    )
    a.add_argument(
        "--question",
        action="append",
        help="limit rows, judgements and comparisons to these question ids (exploratory "
        "recomputation, recorded as question_filter; unknown ids exit 2)",
    )
    a.add_argument(
        "--bias-evidence-dir",
        help="with --primary-judge B: another campaign (read only) whose judgements join this "
        "evidence's in the judge bias check, on this evidence's questions (amendment 4); adds "
        "single judge sensitivity for this evidence and records the other campaign's "
        "fingerprint and counts",
    )
    a.set_defaults(func=cmd_analyze)

    x = sub.add_parser("audit-export", help="human audit queue: disputed facts plus a 5%% sample")
    common(x)
    x.add_argument("--out", required=True, help="directory for audit-queue.csv and .md")
    x.add_argument("--seed", type=int, default=BOOTSTRAP_SEED)
    x.add_argument(
        "--primary-judge",
        choices=list(judge.PRIMARY_JUDGES),
        help="scoring rule of the queue (default: the --analysis rule, else combined); "
        "refused when it differs from the --analysis rule",
    )
    x.add_argument(
        "--analysis",
        help="the analyze JSON output: its rule is used and its audit_queue must match",
    )
    x.set_defaults(func=cmd_audit_export)

    r = sub.add_parser("report", help="committed results files from an analyze output")
    common(r)
    r.add_argument("--analysis", required=True, help="the analyze JSON output to report")
    r.add_argument("--out", required=True, help="results folder")
    r.add_argument(
        "--audit",
        nargs="+",
        help="the audit files or directories analyze applied (verified against its digests)",
    )
    r.add_argument(
        "--primary-judge",
        choices=list(judge.PRIMARY_JUDGES),
        help="expected scoring rule; the report uses the analysis' rule and refuses a mismatch",
    )
    r.add_argument(
        "--bias-evidence-dir",
        help="the --bias-evidence-dir analyze used (its fingerprint must still match)",
    )
    r.set_defaults(func=cmd_report)

    _grounding_parsers(sub)

    s = sub.add_parser("estimate", help="codex campaign cost and Archivist quota estimate")
    s.add_argument("--questions", required=True)
    s.add_argument("--ledger", help="ledger.jsonl rows instead of the evidence dir records")
    s.add_argument("--evidence-dir", help="evidence directory with the measured runs")
    s.add_argument("--pilot", help="pilot results.json")
    s.add_argument("--repetitions", type=int, default=REPETITIONS)
    s.add_argument("--format", choices=["markdown", "json"], default="markdown")
    s.set_defaults(func=cmd_estimate)
    return p


def key_corrections(sp: argparse.ArgumentParser, required: bool = False) -> None:
    """``--key-corrections FILE`` (amendment 5, harness 1.6.0)."""
    sp.add_argument(
        "--key-corrections",
        metavar="FILE",
        required=required,
        help="amendment 5 key corrections file: the named fields (statement, value, unit, "
        "accept, source) of the corrections whose set is this question file's set are replaced "
        "in memory; an unknown question or fact, another field or a duplicate fact exits 2",
    )


def _grounding_parsers(sub: Any) -> None:
    """Grounding subcommands (pre registration section 14). ``--evidence-dir`` is read only
    input, ``--out`` the grounding state (refused inside the evidence dir); grading passes are dry
    runs unless ``--execute`` with ``--max-calls`` and resume by skipping valid outputs."""

    def state(sp: argparse.ArgumentParser, evidence: bool = True) -> None:
        if evidence:
            sp.add_argument(
                "--evidence-dir", required=True, help="campaign evidence (read only input)"
            )
        sp.add_argument("--out", required=True, help="the grounding state directory")

    def agent(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--agent",
            choices=list(grounding.AGENTS),
            default="codex",
            help="the runs to read (default codex; claude_code: the section 15 Claude Code "
            "stream rules, outputs tagged with the agent)",
        )

    def grading(sp: argparse.ArgumentParser, most: int = 4) -> None:
        sp.add_argument("--execute", action="store_true", help="actually call the grader")
        sp.add_argument(
            "--concurrency",
            type=_bounded(1, most),
            default=1,
            help=f"parallel workers, 1 to {most} (default 1; execution only, same outputs)",
        )
        sp.add_argument("--max-calls", type=int, help="refuse more calls than this")
        sp.add_argument(
            "--stop-at-window",
            type=_percent,
            default=DEFAULT_STOP_AT_WINDOW,
            metavar="PCT",
            help="refuse a call when a Codex plan window is at or above PCT percent (90)",
        )
        sp.add_argument(
            "--max-window-step",
            type=_step,
            default=probe.DEFAULT_MAX_WINDOW_STEP,
            metavar="POINTS",
            help="stop when a Codex window rises more than POINTS between probes (5)",
        )

    q = sub.add_parser("verify-quotes", help="check sourced fact quotes against stored passages")
    q.add_argument("--questions", required=True)
    q.add_argument("--out", required=True, help="the grounding state (passage cache)")
    q.add_argument("--offline", action="store_true", help="use cached passages only")
    q.add_argument("--archivist-ceiling", type=_ceiling, default=DEFAULT_ARCHIVIST_CEILING)
    key_corrections(q)
    q.set_defaults(func=cmd_verify_quotes)

    g = sub.add_parser(
        "grounding-sources", help="parse every completed run's sources (Codex by default)"
    )
    state(g)
    g.add_argument("--questions", required=True, help="the campaign's questions file")
    g.add_argument("--force", action="store_true", help="reparse runs already parsed")
    agent(g)
    g.set_defaults(func=cmd_grounding_sources)

    h = sub.add_parser("grounding-hosts", help="pass H: categories of hosts outside the seeds")
    state(h, evidence=False)
    grading(h)
    h.add_argument(
        "--batch-size", type=_batch(grounding.PASS_H_BATCH), default=grounding.PASS_H_BATCH
    )
    h.set_defaults(func=cmd_grounding_hosts)

    c = sub.add_parser("grounding-claims", help="pass R: the claims of every scored answer")
    state(c)
    grading(c)
    c.add_argument("--questions", required=True)
    c.add_argument("--run-id", action="append", help="limit to these run ids")
    agent(c)
    c.set_defaults(func=cmd_grounding_claims)

    x = sub.add_parser("crosscheck-extract", help="pass X: forum and aggregator claims")
    state(x)
    grading(x)
    x.add_argument(
        "--batch-size", type=_batch(grounding.PASS_X_BATCH), default=grounding.PASS_X_BATCH
    )
    x.set_defaults(func=cmd_crosscheck_extract)

    m = sub.add_parser("crosscheck-sample", help="the seeded stratified cross check sample")
    state(m, evidence=False)
    m.add_argument("--size", type=int, help="sample size (default 400; extension 300)")
    m.add_argument("--seed", type=int, help="seed (default 8105; extension 8106)")
    m.add_argument(
        "--extension",
        action="store_true",
        help="deviation G4: the 300 reported_figure claim extension for trap sourcing (E rows)",
    )
    m.set_defaults(func=cmd_crosscheck_sample)

    k = sub.add_parser("crosscheck-check", help="pass C: Codex with Archivist only per claim")
    state(k, evidence=False)
    grading(k)
    k.add_argument("--row", action="append", help="limit to these sample row ids")
    k.add_argument(
        "--extension", action="store_true", help="check the G4 extension rows (E) instead"
    )
    k.add_argument("--archivist-ceiling", type=_ceiling, default=DEFAULT_ARCHIVIST_CEILING)
    k.set_defaults(func=cmd_crosscheck_check)

    u = sub.add_parser("crosscheck-audit", help="human audit sample of the cross check")
    state(u, evidence=False)
    u.add_argument("--results", required=True, help="results folder for crosscheck-audit.csv")
    u.add_argument("--seed", type=int, default=BOOTSTRAP_SEED)
    u.set_defaults(func=cmd_crosscheck_audit)

    t = sub.add_parser("trap-judge", help="pass T: trap adoption in contamination answers")
    state(t)
    grading(t)
    t.add_argument("--questions", required=True, help="contamination-questions.json")
    t.add_argument("--run-id", action="append", help="limit to these run ids")
    agent(t)
    t.set_defaults(func=cmd_trap_judge)

    for name, func in (
        ("grounding-analyze", cmd_grounding_analyze),
        ("grounding-report", cmd_grounding_report),
    ):
        a = sub.add_parser(name, help=f"{name.split('-')[1]} the grounding measures")
        state(a)
        a.add_argument("--contamination-evidence-dir", help="contamination campaign evidence")
        a.add_argument("--contamination-questions", help="contamination-questions.json")
        a.add_argument(
            "--contamination-audit",
            nargs="+",
            metavar="CSV",
            help="amendment 5: audit CSVs (human audit shards, a key correction rescore CSV) "
            "whose verdicts the contamination per arm accuracy applies (pass B rule); "
            "fingerprinted and recorded in the analysis; an invalid file exits 1",
        )
        agent(a)
        if name == "grounding-analyze":
            a.add_argument("--resamples", type=int, default=BOOTSTRAP_RESAMPLES)
            a.add_argument("--seed", type=int, default=BOOTSTRAP_SEED)
        else:
            a.add_argument("--results", required=True, help="the committed results folder")
        a.set_defaults(func=func)

    rk = sub.add_parser(
        "rejudge-keys",
        help="amendment 5: pass B re-judge of the facts a key correction changed (judge calls "
        "only; dry run by default)",
    )
    state(rk)
    rk.add_argument("--questions", required=True, help="the campaign's questions file")
    key_corrections(rk, required=True)
    grading(rk)
    rk.add_argument(
        "--export",
        metavar="CSV",
        help="write the rescore CSV (audit columns plus correction_source key_correction, no "
        "call); refused while an eligible run is not re-judged",
    )
    rk.set_defaults(func=cmd_rejudge_keys)

    pc = sub.add_parser(
        "audit-precheck",
        help="amendment 5: pass P pre check of the audit rows without a human audit verdict (dry "
        "run by default)",
    )
    state(pc)
    pc.add_argument("--questions", required=True, help="the campaign's questions file")
    key_corrections(pc)
    pc.add_argument(
        "--audit-queue",
        nargs="+",
        required=True,
        metavar="CSV",
        help="audit queue CSVs (or folders: every audit-queue-*.csv); rows with a human "
        "audit_verdict are left to the human audit",
    )
    pc.add_argument(
        "--key-rescore",
        nargs="+",
        metavar="CSV",
        help="rescore CSVs (rejudge-keys --export): their verdict is the effective judge "
        "verdict of a corrected fact",
    )
    pc.add_argument(
        "--results",
        required=True,
        help="results folder for audit-precheck.csv and audit-owner-list.md",
    )
    grading(pc)
    pc.add_argument("--archivist-ceiling", type=_ceiling, default=DEFAULT_ARCHIVIST_CEILING)
    pc.set_defaults(func=cmd_audit_precheck)


def _bounded(low: int, high: int) -> Any:
    def parse(text: str) -> int:
        value = int(text)
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be {low} to {high}")
        return value

    return parse


def _batch(maximum: int) -> Any:
    """A batch size of 1 to the frozen maximum (pass H 40, pass X 15)."""

    def parse(text: str) -> int:
        value = int(text)
        if not 1 <= value <= maximum:
            raise argparse.ArgumentTypeError(f"must be 1 to {maximum}")
        return value

    return parse


def _ceiling(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _step(text: str) -> float:
    value = float(text)
    if not 0 < value <= 100:
        raise argparse.ArgumentTypeError("must be above 0 and at most 100")
    return value


def _percent(text: str) -> float:
    value = float(text)
    if not 1 <= value <= 100:
        raise argparse.ArgumentTypeError("must be 1 to 100")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "analyze" and args.format == "json" and not args.questions:
        raise SystemExit("analyze --format json needs --questions")
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
