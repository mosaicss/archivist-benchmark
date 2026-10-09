"""Plan, execute and extract runs; the run ledger.

``run`` is a dry run unless ``--execute``; an executed plan needs ``--max-runs`` and is refused
when it is larger; every attempt (reruns included) counts against that ceiling. Concurrency
defaults to 1 and is capped at 2. Raw evidence stays under the evidence root, outside git.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__, claude_code, codex, probe
from .cost import run_cost
from .model import (
    ARMS,
    DEFAULT_ARCHIVIST_CEILING,
    DEFAULT_STOP_AT_WINDOW,
    MAX_CONCURRENCY,
    MAX_INFRA_RERUNS,
    PROMPT_TEMPLATE,
    STATUS_COMPLETED,
    STATUS_INCOMPLETE,
    STATUS_INFRA_ERROR,
    STATUS_INVALID,
    STATUS_TIMEOUT,
    STATUS_TURN_LIMIT,
    Agent,
    AgentConfig,
    Arm,
    Extraction,
    RunRecord,
    RunSpec,
    stops_batch,
)
from .redact import contamination_hits

# Only these variables reach a child; everything else (CLAUDE_CODE_*, CLAUDE_EFFORT, CODEX_*,
# session messaging sockets and tokens, cloud credentials) is dropped.
ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "TERM",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)
FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "CLAUDE",
    "CODEX_",
    "ANTHROPIC_",
    "OPENAI_",
    "HERDR",
    "ARCHIVIST_",
    "GOOGLE_",
    "GCLOUD",
)
# Extras a harness module may set on purpose (the agent's own switches and the chosen key).
PERMITTED_EXTRAS = frozenset(
    {
        "CODEX_HOME",
        "CODEX_API_KEY",
        "ANTHROPIC_API_KEY",
        "ENABLE_TOOL_SEARCH",
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS",
        "DISABLE_AUTOUPDATER",
        # The chosen Claude account (harness 1.5.1): only ever set explicitly from
        # ``--claude-config-dir`` (``claude_code.account_extras``); the parent's value is never
        # copied (it is not in ENV_ALLOWLIST).
        "CLAUDE_CONFIG_DIR",
    }
)
SECRET_ENV = frozenset({"CODEX_API_KEY", "ANTHROPIC_API_KEY"})


def child_env(parent: dict[str, str], extras: dict[str, str]) -> dict[str, str]:
    env = {key: parent[key] for key in ENV_ALLOWLIST if key in parent}
    for key in extras:
        if key not in PERMITTED_EXTRAS:
            raise ValueError(f"extra environment variable {key} is not permitted")
    env.update(extras)
    for key in env:
        if key.startswith(FORBIDDEN_PREFIXES) and key not in PERMITTED_EXTRAS:
            raise AssertionError(f"forbidden variable {key} reached a child environment")
    return env


def prompt_for(question_text: str) -> str:
    return PROMPT_TEMPLATE.format(question=question_text.strip())


def build_plan(
    questions: Sequence[dict[str, Any]],
    agent: Agent,
    cfg: AgentConfig,
    repetitions: int,
    arms: Sequence[Arm] = ARMS,
) -> list[RunSpec]:
    """Question-major order: every arm of a question and repetition runs back to back."""
    specs = []
    for rep in range(1, repetitions + 1):
        for q in questions:
            for arm in arms:
                specs.append(
                    RunSpec(
                        agent=agent,
                        arm=arm,
                        question_id=str(q["id"]),
                        stratum=str(q["stratum"]),
                        repetition=rep,
                        prompt=prompt_for(str(q["question"])),
                        config=cfg,
                    )
                )
    return specs


def arm_commands(spec: RunSpec, run_dir: Path) -> tuple[list[str], str | None]:
    """argv and stdin for one run (paths inside ``run_dir``)."""
    cwd = run_dir / "cwd"
    if spec.agent == "codex":
        return codex.command(spec.config, spec.prompt, cwd, run_dir / "last-message.md"), None
    argv = claude_code.command(spec.config, spec.arm, str(run_dir / "mcp-config.json"))
    return argv, spec.prompt


def describe_plan(specs: Sequence[RunSpec], placeholder: Path) -> str:
    """Human readable dry run: arm configs, commands, the run list and counts."""
    out: list[str] = []
    agents = sorted({s.agent for s in specs})
    for agent in agents:
        mine = [s for s in specs if s.agent == agent]
        cfg = mine[0].config
        out.append(
            f"== {agent}: model={cfg.model} effort={cfg.effort} tier={cfg.service_tier} "
            f"auth={cfg.auth_mode} billing={cfg.billing_mode} max_turns={cfg.max_turns} "
            f"timeout_s={cfg.timeout_s}"
        )
        for arm in ARMS:
            sample = next((s for s in mine if s.arm == arm), None)
            if sample is None:
                continue
            run_dir = placeholder / f"<run-id:{agent}.{arm}>"
            argv, stdin = arm_commands(sample, run_dir)
            shown = [a if a != sample.prompt else "<prompt>" for a in argv]
            out.append(f"-- arm {arm}: isolated home {run_dir}")
            out.append("   command: " + " ".join(json.dumps(a) for a in shown))
            if stdin is not None:
                out.append("   stdin: <prompt>")
            if agent == "codex":
                rendered = codex.render_config(arm, cfg, run_dir / "cwd")
                out.append("   config.toml:")
                out += [f"     {line}" for line in rendered.splitlines()]
            else:
                out.append(f"   mcp-config.json: {json.dumps(claude_code.mcp_config(arm))}")
        out.append(f"-- runs ({len(mine)}):")
        prompts: dict[str, str] = {}
        for s in mine:
            prompts.setdefault(s.question_id, s.prompt)
            out.append(f"   {s.run_key} [{s.stratum}]")
        out.append("-- prompts (identical in every arm):")
        out += [f"   {qid}: {text}" for qid, text in prompts.items()]
        controls = sorted({s.question_id for s in mine if s.stratum == "control"})
        out.append(f"-- control questions: {', '.join(controls) or 'none'}")
    out.append(f"== total runs: {len(specs)} (dry run: nothing executed)")
    return "\n".join(out)


@dataclass(frozen=True)
class ProcessRequest:
    argv: list[str]
    env: dict[str, str]
    cwd: Path
    stdin_text: str | None
    stdout_path: Path
    stderr_path: Path
    timeout_s: float


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    timed_out: bool
    wall_s: float


ProcessExecutor = Callable[[ProcessRequest], ProcessResult]
# (Codex home, child environment, working directory) -> preflight report
Preflight = Callable[[Path, dict[str, str], Path], dict[str, Any]]


def codex_preflight(home: Path, env: dict[str, str], cwd: Path) -> dict[str, Any]:
    return codex.mcp_preflight(home, env, cwd)


def subprocess_executor(req: ProcessRequest) -> ProcessResult:
    """Run in its own process group; on timeout kill the whole group."""
    start = time.monotonic()
    with req.stdout_path.open("w") as out, req.stderr_path.open("w") as err:
        proc = subprocess.Popen(
            req.argv,
            env=req.env,
            cwd=req.cwd,
            stdin=subprocess.PIPE if req.stdin_text is not None else subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            text=True,
            start_new_session=True,
        )
        timed_out = False
        try:
            proc.communicate(input=req.stdin_text, timeout=req.timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
        except BaseException:
            _kill_group(proc)  # an interrupt must not leave the child's process group running
            raise
    return ProcessResult(proc.returncode, timed_out, round(time.monotonic() - start, 3))


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


KILL_GRACE_S = 10.0


def _kill_group(proc: subprocess.Popen[str]) -> None:
    """SIGTERM the whole group, then SIGKILL whatever is left of it, parent exit or not: a
    descendant that ignores SIGTERM must not outlive the run (a leftover zombie gets a harmless
    SIGKILL)."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + KILL_GRACE_S
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=KILL_GRACE_S)
    while _group_alive(proc.pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _group_alive(proc.pid):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5.0)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def prepare_run(
    spec: RunSpec,
    run_dir: Path,
    parent_env: dict[str, str],
    codex_auth: Path,
    claude_config_dir: Path | None = None,
) -> ProcessRequest:
    cwd = run_dir / "cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    if spec.agent == "codex":
        home = run_dir / "codex-home"
        codex.prepare_home(home, spec.arm, spec.config, cwd, codex_auth)
        extras = codex.env_extras(home, spec.config, parent_env)
    else:
        if claude_config_dir is None:
            # Fail closed (harness 1.5.1): a Claude child never falls back to ~/.claude.
            raise ValueError("a Claude Code run needs --claude-config-dir (no account chosen)")
        (run_dir / "mcp-config.json").write_text(
            json.dumps(claude_code.mcp_config(spec.arm), indent=2), encoding="utf-8"
        )
        extras = claude_code.env_extras(spec.config, parent_env, claude_config_dir)
    argv, stdin_text = arm_commands(spec, run_dir)
    env = child_env(parent_env, extras)
    (run_dir / "command.json").write_text(
        json.dumps(
            {
                "argv": argv,
                "stdin": stdin_text,
                "env_keys": sorted(env),
                "env": {k: v for k, v in env.items() if k not in SECRET_ENV},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return ProcessRequest(
        argv=argv,
        env=env,
        cwd=cwd,
        stdin_text=stdin_text,
        stdout_path=run_dir / "stdout.jsonl",
        stderr_path=run_dir / "stderr.log",
        timeout_s=float(spec.config.timeout_s),
    )


# meta.json written by the 1.2.0 runner carries this; such a directory is an attempt only once
# it has ``dispatched_at`` (or was reconciled as interrupted). Older directories (the smoke runs)
# have no version and are extracted as before.
META_VERSION = 2


def write_meta(run_dir: Path, spec: RunSpec, run_id: str, attempt: int, **extra: Any) -> None:
    meta = {
        "run_id": run_id,
        "attempt": attempt,
        "agent": spec.agent,
        "arm": spec.arm,
        "question_id": spec.question_id,
        "stratum": spec.stratum,
        "repetition": spec.repetition,
        "prompt": spec.prompt,
        "config": asdict(spec.config),
        **extra,
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def update_meta(run_dir: Path, **fields: Any) -> dict[str, Any]:
    """Merge fields into a run's ``meta.json`` (launch, reconciliation and guard state)."""
    path = run_dir / "meta.json"
    meta: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    meta.update(fields)
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []


def extract_dir(run_dir: Path) -> tuple[Extraction, dict[str, Any]]:
    """Read a run directory's raw evidence into an extraction (agent specific)."""
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    if meta["agent"] == "codex":
        rollout = codex.find_rollout(run_dir / "codex-home")
        if rollout is None and (run_dir / "rollout.jsonl").exists():
            rollout = run_dir / "rollout.jsonl"
        ex = codex.parse_rollout(_read_lines(rollout) if rollout else [])
        codex.parse_exec_stdout(_read_lines(run_dir / "stdout.jsonl"), ex)
        stderr = run_dir / "stderr.log"
        codex.parse_stderr(stderr.read_text(errors="replace") if stderr.exists() else "", ex)
        last = run_dir / "last-message.md"
        if last.exists() and last.read_text(encoding="utf-8").strip():
            ex.answer = last.read_text(encoding="utf-8")
        preflight_path = run_dir / "mcp-preflight.json"
        if preflight_path.exists():
            codex.apply_preflight(json.loads(preflight_path.read_text(encoding="utf-8")), ex)
        auth = run_dir / "codex-home" / "auth.json"
        if auth.exists() and not auth.is_symlink():
            ex.errors.append("auth.json was replaced inside the run home; check the login")
    else:
        ex = claude_code.parse_stream(_read_lines(run_dir / "stdout.jsonl"))
    return ex, meta


def classify(
    arm: Arm, agent: str, ex: Extraction, timed_out: bool, returncode: int | None
) -> tuple[str, str | None]:
    violations = (
        codex.isolation_violations(arm, ex)
        if agent == "codex"
        else claude_code.isolation_violations(arm, ex)
    )
    if violations:  # before infra: a broken arm must stop the batch, not be retried
        return STATUS_INVALID, "arm_isolation: " + "; ".join(violations)
    if ex.infra_reason:
        return STATUS_INFRA_ERROR, ex.infra_reason
    if timed_out:
        return STATUS_TIMEOUT, "wall time limit"
    if ex.turn_limit:
        return STATUS_TURN_LIMIT, "max turns reached"
    if ex.incomplete_reason:
        return STATUS_INCOMPLETE, ex.incomplete_reason
    if returncode not in (0, None):
        return STATUS_INCOMPLETE, f"exit {returncode}"
    if not ex.answer.strip():
        return STATUS_INCOMPLETE, "no_answer"
    return STATUS_COMPLETED, None


def kept_classification(run_dir: Path, meta: dict[str, Any]) -> tuple[str, str] | None:
    """(harness version, reason) when this run's ``invalid`` classification was recorded under
    an earlier rule set and must stay. ``classified_by`` in meta.json says which harness
    classified the run; a legacy run (no ``classified_by``) whose existing record is invalid is
    stamped now, the same way ``reconciled`` is persisted."""
    if not meta.get("classified_by"):
        prior = run_dir / "record.json"
        if not prior.exists():
            return None
        try:
            recorded = json.loads(prior.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if recorded.get("status") != STATUS_INVALID:
            return None
        legacy = "1.2.0" if int(meta.get("meta_version") or 1) >= META_VERSION else "1.1.0"
        meta = update_meta(
            run_dir,
            classified_by=legacy,
            classified_status=STATUS_INVALID,
            classified_reason=recorded.get("status_reason"),
        )
    if meta.get("classified_status") == STATUS_INVALID and meta.get("classified_by") != __version__:
        return str(meta["classified_by"]), str(meta.get("classified_reason") or "arm_isolation")
    return None


def stamp_classifications(evidence_root: Path) -> list[str]:
    """Persist ``classified_by`` for every recorded invalid run that lacks it (see
    ``kept_classification``); returns the run ids stamped."""
    stamped = []
    for path in sorted((evidence_root / "runs").glob("*/meta.json")):
        meta = json.loads(path.read_text(encoding="utf-8"))
        if not meta.get("classified_by") and kept_classification(path.parent, meta):
            stamped.append(path.parent.name)
    return stamped


def build_record(run_dir: Path) -> RunRecord:
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    if meta.get("not_dispatched"):
        raise ValueError(f"{run_dir.name} was never launched: it is not an attempt")
    reconciled = meta.get("reconciled") == "interrupted"
    if (
        int(meta.get("meta_version") or 1) >= META_VERSION
        and not meta.get("dispatched_at")
        and not reconciled
    ):
        raise ValueError(
            f"{run_dir.name} is a preparation without launch evidence (no dispatched_at): "
            "it is not an attempt"
        )
    kept = kept_classification(run_dir, meta)
    ex, meta = extract_dir(run_dir)
    if ex.plan_window is not None:
        ex.plan_window["observed_at"] = meta.get("finished_at") or meta.get("started_at")
    cfg = meta["config"]
    agent, arm = meta["agent"], meta["arm"]
    timed_out = bool(meta.get("timed_out"))
    status, reason = classify(arm, agent, ex, timed_out, meta.get("returncode"))
    if reconciled:
        # Reconciled on resume: whatever telemetry the run left is kept (usage, plan window,
        # credits, guard notes); only the classification is forced.
        status, reason = STATUS_INFRA_ERROR, "interrupted"
    elif kept is not None:
        # An invalid classification recorded under an earlier rule set stays: the batch stopped
        # on it and the deviation that fixed the cause applies to new runs only.
        status, reason = STATUS_INVALID, kept[1]
    hits = contamination_hits(arm, ex.web_urls, ex.answer)
    usage = ex.usage.as_dict() if ex.usage else None
    model = ex.model or cfg["model"]
    cost, basis = run_cost(
        agent,
        model,
        usage,
        ex.web_actions,
        ex.per_call_usage,
        ex.client_cost_usd,
        cfg["auth_mode"],
    )
    answer_file = None
    if ex.answer:
        (run_dir / "answer.md").write_text(ex.answer, encoding="utf-8")
        answer_file = str(run_dir / "answer.md")
    notes = list(ex.errors) + [str(n) for n in meta.get("guard_notes") or []]
    if reconciled:
        notes.append("interrupted: dispatched, never recorded; reconciled on resume")
    if kept is not None:
        notes.append(f"classification kept: invalid as recorded by harness {kept[0]}")
    if hits:
        notes.append("contaminated: " + ", ".join(hits))
    if ex.api_key_source is not None:
        notes.append(f"apiKeySource={ex.api_key_source}")
    if ex.init_tools is not None:
        notes.append("init_tools=" + ",".join(ex.init_tools))
    if ex.mcp_servers:
        notes.append(
            "mcp_servers="
            + ",".join(
                f"{k}:{v}" + (f"({ex.mcp_evidence[k]})" if k in ex.mcp_evidence else "")
                for k, v in sorted(ex.mcp_servers.items())
            )
        )
    record = RunRecord(
        run_id=meta["run_id"],
        run_key=f"{agent}.{arm}.{meta['question_id']}.r{meta['repetition']}",
        agent=agent,
        arm=arm,
        question_id=meta["question_id"],
        stratum=meta["stratum"],
        repetition=int(meta["repetition"]),
        attempt=int(meta["attempt"]),
        model=model,
        effort=ex.effort or cfg["effort"],
        service_tier=ex.service_tier or cfg["service_tier"],
        auth_mode=cfg["auth_mode"],
        billing_mode=_billing(agent, cfg["auth_mode"]),
        status=status,
        status_reason=reason,
        usage=usage,
        model_calls=ex.model_calls,
        tool_calls=ex.tool_calls,
        web_actions=ex.web_actions,
        compactions=ex.compactions,
        truncations=ex.truncations,
        turn_limit=ex.turn_limit,
        timeout=timed_out,
        wall_s=float(meta.get("wall_s") or 0.0),
        cost_usd=cost,
        cost_basis=basis,
        mcp_loaded=(ex.mcp_servers.get("archivist") == "connected")
        if arm != "web"
        else ("archivist" in ex.mcp_servers),
        contaminated=bool(hits),
        started_at=str(meta.get("started_at", "")),
        cli_version=ex.cli_version,
        num_turns=ex.num_turns,
        answer_file=answer_file,
        truncated_calls=ex.truncated_calls,
        paged_results=ex.paged_results,
        notes=notes,
        plan_window=ex.plan_window,
        claude_config_dir=_account(agent, meta),
    )
    (run_dir / "record.json").write_text(json.dumps(record.as_dict(), indent=2), "utf-8")
    return record


def _account(agent: str, meta: dict[str, Any]) -> str | None:
    """The Claude login used by a Claude Code run (``meta.json``, harness 1.5.1); None for Codex
    and for runs made before 1.5.1."""
    value = meta.get("claude_config_dir")
    return str(value) if agent == "claude_code" and value else None


def _billing(agent: str, auth_mode: str) -> str:
    from .model import BILLING_MODES

    return BILLING_MODES.get((agent, auth_mode), "unknown")


def window_percents(plan_window: dict[str, Any] | None) -> list[tuple[str, float]]:
    """Each plan window's use in percent: Codex ``used_percent`` of the primary and secondary
    windows, Claude Code ``unifiedWindows`` utilization (a fraction) times 100."""
    if not plan_window:
        return []
    out: list[tuple[str, float]] = []
    if plan_window.get("source") == "codex_rollout_rate_limits":
        for key in ("primary", "secondary"):
            window = plan_window.get(key)
            if isinstance(window, dict) and isinstance(window.get("used_percent"), int | float):
                name = f"codex {key} ({window.get('window_minutes')} min)"
                out.append((name, float(window["used_percent"])))
    else:
        for name, value in sorted((plan_window.get("windows") or {}).items()):
            if isinstance(value, int | float):
                out.append((f"claude {name}", round(float(value) * 100, 4)))
    return out


def credit_balance(plan_window: dict[str, Any] | None, key: str = "balance_min") -> float | None:
    """A Codex credit balance a record saw (the rollout reports it as a string): by default the
    minimum of the run, falling back to the last balance."""
    credits = (plan_window or {}).get("credits")
    if not isinstance(credits, dict):
        return None
    value = credits.get(key)
    if value is None:
        value = credits.get("balance")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class Budget:
    """Thread safe attempt counter against ``--max-runs`` plus the batch stops: after an
    ``invalid`` (arm isolation) record, a plan or quota limit, a plan window at or above
    ``stop_at_window`` percent, a sign of metered fallback, a refused launch (the plan gate,
    ``probe.PlanGate``: pre launch headroom, not an account level prevention) or a finding of
    the closing probe, no new attempt starts. ``admit`` makes the final launch decision
    atomic with ``stop``."""

    def __init__(
        self,
        max_runs: int,
        stop_at_window: float | None = DEFAULT_STOP_AT_WINDOW,
        max_window_step: float = probe.DEFAULT_MAX_WINDOW_STEP,
    ):
        if stop_at_window is not None and not 1 <= stop_at_window <= 100:
            raise ValueError("--stop-at-window must be 1 to 100")
        self.max_runs = max_runs
        self.stop_at_window = stop_at_window
        self.gate = probe.PlanGate(
            stop_at_window if stop_at_window is not None else 100.0, max_window_step
        )
        self.codex_dispatched = 0
        self.closing: dict[str, Any] | None = None
        self.used = 0
        self.stopped_by: str | None = None
        self.stop_kind: str | None = None
        self.stops: list[tuple[str, str, str]] = []  # (run id, kind, detail)
        self._lock = threading.RLock()

    def take(self) -> bool:
        with self._lock:
            if self.stopped_by is not None or self.used >= self.max_runs:
                return False
            self.used += 1
            return True

    def release(self) -> None:
        """Give back a reservation that never launched (it is not an attempt)."""
        with self._lock:
            self.used = max(0, self.used - 1)

    def refuse(self, run_id: str, detail: str, kind: str = "plan_gate") -> None:
        """A blocking plan (or Archivist quota) reading: release this reservation and latch the
        batch stop in one step under the admission lock, before any evidence is written."""
        with self._lock:
            self.used = max(0, self.used - 1)
            self.stop(run_id, kind, detail)

    def admit(self, run_dir: Path, agent: str) -> str | None:
        """Final launch admission, atomic with ``stop``: if the batch stopped since the
        reservation, release it and return None; else write ``dispatched_at`` into meta.json
        under the same lock and return it."""
        with self._lock:
            if self.stopped_by is not None:
                self.used = max(0, self.used - 1)
                return None
            dispatched_at = _now()
            update_meta(run_dir, dispatched_at=dispatched_at)
            if agent == "codex":
                self.codex_dispatched += 1
            return dispatched_at

    def stop(self, run_id: str, kind: str = "invalid", detail: str = "") -> None:
        with self._lock:
            if self.stopped_by is None:
                self.stopped_by, self.stop_kind = run_id, kind
            self.stops.append((run_id, kind, detail))


def metered_fallback(record: RunRecord, budget: Budget) -> list[str]:
    """Signs that a run drew on metered billing instead of the plan allowance: the run's
    minimum Codex credit balance below the batch baseline (the first probe's balance, else the
    run's first balance), or below the run's own first balance (each a sign), a Codex
    ``rate_limit_reached_type`` seen in the run, Claude overage use."""
    window = record.plan_window
    if not window:
        return []
    signs: list[str] = []
    reached = window.get("rate_limit_reached_seen") or window.get("rate_limit_reached_type")
    if reached:
        signs.append(f"Codex rate_limit_reached_type={reached}")
    lowest = credit_balance(window, "balance_min")
    first = credit_balance(window, "balance_first")
    if lowest is not None:
        # Two independent comparisons: against the batch baseline (the first probe) and
        # against the run's own first reading.
        baseline = budget.gate.baseline_for(first if first is not None else lowest)
        if lowest < baseline:
            signs.append(f"Codex credit balance {lowest:g} below the batch baseline {baseline:g}")
        if first is not None and lowest < first:
            signs.append(f"Codex credit balance dropped from {first:g} to {lowest:g}")
    if window.get("is_using_overage") is True:
        signs.append("Claude isUsingOverage true")
    return signs


def apply_guards(record: RunRecord, budget: Budget) -> list[str]:
    """Check one finished attempt against the batch stops; stop the budget and return the notes
    to keep on the record."""
    notes: list[str] = []
    if record.agent == "claude_code":
        budget.gate.observe_claude(record.plan_window)
    signs = metered_fallback(record, budget)
    if signs:
        detail = "; ".join(signs)
        notes.append(f"metered_fallback_guard: {detail}")
        budget.stop(record.run_id, "metered_fallback_guard", detail)
    if record.status == STATUS_INFRA_ERROR and stops_batch(record.status_reason):
        budget.stop(record.run_id, "plan_or_quota_limit", str(record.status_reason))
    window = record.plan_window or {}
    if window.get("rejected_seen") or window.get("status") == "rejected":
        # The run keeps its classification; the batch stops whatever the result subtype.
        notes.append("plan_rejected_guard: Claude rate_limit_event status rejected")
        budget.stop(record.run_id, "plan_or_quota_limit", "Claude rate_limit_event rejected")
    if budget.stop_at_window is not None:
        hot = [(n, p) for n, p in window_percents(record.plan_window) if p >= budget.stop_at_window]
        if hot:
            detail = ", ".join(f"{n} at {p:g}%" for n, p in hot)
            notes.append(f"plan_window_guard: {detail} (threshold {budget.stop_at_window:g}%)")
            budget.stop(record.run_id, "plan_window", detail)
    return notes


# (environment, working directory) -> probe report; see ``probe.app_server_probe``.
Probe = Callable[[dict[str, str], Path], dict[str, Any]]

ARCHIVIST_USAGE_ARGV: tuple[str, ...] = ("archivist", "usage", "--format", "json")
ARCHIVIST_USAGE_TIMEOUT_S = 60.0


def archivist_usage(env: dict[str, str]) -> dict[str, Any]:
    """Read the benchmark account's monthly CLI counter (``archivist usage --format json``,
    ``usage.cli_this_month``; the read does not count as a call). Returns ``{"ok": True,
    "cli_this_month", "cli_limit", "reset_date"}`` or ``{"ok": False, "error"}``; no account
    identifier is kept. Tests replace this function."""
    try:
        done = subprocess.run(
            list(ARCHIVIST_USAGE_ARGV),
            env=env,
            capture_output=True,
            text=True,
            timeout=ARCHIVIST_USAGE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return parse_archivist_usage(done.returncode, done.stdout, done.stderr)


def parse_archivist_usage(returncode: int | None, stdout: str, stderr: str = "") -> dict[str, Any]:
    if returncode not in (0, None):
        return {"ok": False, "error": f"archivist usage exit {returncode}: {stderr.strip()[:200]}"}
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "archivist usage output is not JSON"}
    usage = data.get("usage") if isinstance(data, dict) else None
    count = usage.get("cli_this_month") if isinstance(usage, dict) else None
    if isinstance(count, bool) or not isinstance(count, int):
        return {"ok": False, "error": "archivist usage has no integer usage.cli_this_month"}
    assert isinstance(usage, dict)
    return {
        "ok": True,
        "cli_this_month": count,
        "cli_limit": usage.get("cli_limit"),
        "reset_date": usage.get("reset_date"),
    }


class ArchivistGate:
    """Pre launch Archivist quota gate (``--archivist-ceiling``): before each
    launch that can call Archivist, read ``cli_this_month``; refuse (and stop the batch) at
    or above the ceiling, and on a failed read (fail closed). Every reading is kept in
    ``readings``. Thread safe."""

    def __init__(
        self,
        ceiling: int = DEFAULT_ARCHIVIST_CEILING,
        env: dict[str, str] | None = None,
        reader: Callable[[dict[str, str]], dict[str, Any]] | None = None,
    ) -> None:
        if ceiling < 1:
            raise ValueError("--archivist-ceiling must be at least 1")
        self.ceiling = ceiling
        self.env = env
        self.reader = reader
        self.readings: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def check(self) -> list[str]:
        """Reasons to refuse the next launch (empty: go)."""
        with self._lock:
            env = self.env if self.env is not None else child_env(dict(os.environ), {})
            reading = (self.reader or archivist_usage)(env)
            self.readings.append({"time": _now(), **reading})
            if not reading.get("ok"):
                return [f"archivist usage read failed: {reading.get('error')}"]
            count = int(reading["cli_this_month"])
            if count >= self.ceiling:
                return [f"archivist cli_this_month {count} at or above the ceiling {self.ceiling}"]
            return []


def codex_plan_probe(env: dict[str, str], cwd: Path) -> dict[str, Any]:
    """The live pre launch probe (looked up at call time, so tests can replace it)."""
    return probe.app_server_probe(env, cwd)


def execute_spec(
    spec: RunSpec,
    evidence_root: Path,
    budget: Budget,
    executor: ProcessExecutor,
    parent_env: dict[str, str],
    codex_auth: Path,
    ledger: Path,
    lock: threading.Lock,
    preflight: Preflight = codex_preflight,
    first_attempt: int = 1,
    plan_probe: Probe | None = None,
    max_attempts: int | None = None,
    archivist_gate: ArchivistGate | None = None,
    claude_config_dir: Path | None = None,
) -> list[RunRecord]:
    """One cell's attempts. Any exception latches the batch stop here, in the worker, before
    it propagates: no queued cell is admitted after a worker fails, whatever order the main
    thread collects the futures in."""
    try:
        return _execute_spec(
            spec,
            evidence_root,
            budget,
            executor,
            parent_env,
            codex_auth,
            ledger,
            lock,
            preflight,
            first_attempt,
            plan_probe,
            max_attempts,
            archivist_gate,
            claude_config_dir,
        )
    except BaseException as exc:
        budget.stop(spec.run_key, "aborted", f"{type(exc).__name__}: {exc}"[:200])
        raise


def _execute_spec(
    spec: RunSpec,
    evidence_root: Path,
    budget: Budget,
    executor: ProcessExecutor,
    parent_env: dict[str, str],
    codex_auth: Path,
    ledger: Path,
    lock: threading.Lock,
    preflight: Preflight,
    first_attempt: int,
    plan_probe: Probe | None,
    max_attempts: int | None = None,
    archivist_gate: ArchivistGate | None = None,
    claude_config_dir: Path | None = None,
) -> list[RunRecord]:
    records: list[RunRecord] = []
    # The chosen Claude account is kept in meta.json (and so in record.json) of Claude runs only.
    account: dict[str, Any] = (
        {"claude_config_dir": str(claude_config_dir)}
        if spec.agent == "claude_code" and claude_config_dir is not None
        else {}
    )
    # The rerun sequence of an invalid cell gets only the attempts it has left (at most three
    # in all), numbered after its last; any other cell its usual three attempts.
    last = first_attempt + max_attempts - 1 if max_attempts else MAX_INFRA_RERUNS + 1
    for attempt in range(first_attempt, last + 1):
        if not budget.take():
            break
        if spec.agent == "claude_code":
            refused = budget.gate.check_claude()
            if refused:
                budget.refuse(f"before {spec.run_key}", "; ".join(refused))
                break
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        run_id = f"{stamp}-{spec.run_key}-a{attempt}"
        run_dir = evidence_root / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        started = _now()
        # meta.json before anything is prepared; ``dispatched_at`` is added only at launch, so
        # a directory with it and no record is an interrupted attempt (``reconcile_interrupted``)
        # and one without it never ran.
        write_meta(
            run_dir,
            spec,
            run_id,
            attempt,
            started_at=started,
            meta_version=META_VERSION,
            **account,
        )
        request = prepare_run(spec, run_dir, parent_env, codex_auth, claude_config_dir)
        if spec.agent == "codex":
            report = preflight(run_dir / "codex-home", request.env, request.cwd)
            (run_dir / "mcp-preflight.json").write_text(json.dumps(report, indent=2), "utf-8")
            plan = (plan_probe or codex_plan_probe)(request.env, request.cwd)
            refused = budget.gate.check_codex(plan)
            if refused:
                # Publish the refusal under the admission lock first (latched for the batch),
                # then write the evidence: no other worker is admitted in between.
                detail = "; ".join(refused)
                budget.refuse(run_id, detail)
                (run_dir / "plan-probe.json").write_text(json.dumps(plan, indent=2), "utf-8")
                (run_dir / REFUSED_MARKER).write_text(
                    json.dumps({"refused_at": _now(), "reasons": refused}, indent=2), "utf-8"
                )
                update_meta(run_dir, not_dispatched=True, not_dispatched_reason=detail)
                break
            (run_dir / "plan-probe.json").write_text(json.dumps(plan, indent=2), "utf-8")
        if archivist_gate is not None and spec.arm != "web":
            # Only the archivist and both arms can call Archivist (the quota gate).
            blocked = archivist_gate.check()
            if blocked:
                detail = "; ".join(blocked)
                budget.refuse(run_id, detail, "archivist_ceiling")
                (run_dir / ARCHIVIST_REFUSED).write_text(
                    json.dumps(
                        {
                            "refused_at": _now(),
                            "reasons": blocked,
                            "ceiling": archivist_gate.ceiling,
                        },
                        indent=2,
                    ),
                    "utf-8",
                )
                update_meta(run_dir, not_dispatched=True, not_dispatched_reason=detail)
                break
        dispatched_at = budget.admit(run_dir, spec.agent)
        if dispatched_at is None:  # another worker stopped the batch after the reservation
            update_meta(
                run_dir, not_dispatched=True, not_dispatched_reason="batch stopped before dispatch"
            )
            break
        result = executor(request)
        finished = {
            "meta_version": META_VERSION,
            "started_at": started,
            "dispatched_at": dispatched_at,
            "finished_at": _now(),
            "wall_s": result.wall_s,
            "timed_out": result.timed_out,
            "returncode": result.returncode,
            **account,
        }
        write_meta(run_dir, spec, run_id, attempt, **finished)
        record = build_record(run_dir)
        classified = {
            "classified_by": __version__,
            "classified_status": record.status,
            "classified_reason": record.status_reason,
        }
        finished |= classified
        update_meta(run_dir, **classified)
        guard_notes = apply_guards(record, budget)
        if guard_notes:  # kept in meta.json so a re-extraction keeps them
            write_meta(run_dir, spec, run_id, attempt, **finished, guard_notes=guard_notes)
            record = build_record(run_dir)
        records.append(record)
        with lock, ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record.as_dict()) + "\n")
        if record.status == STATUS_INVALID:
            budget.stop(record.run_id, "invalid", str(record.status_reason))
        if record.status != STATUS_INFRA_ERROR:
            break
    return records


REFUSED_MARKER = "dispatch-refused.json"
ARCHIVIST_REFUSED = "archivist-gate-refused.json"
STOP_MESSAGES = {
    "archivist_ceiling": (
        "the Archivist quota gate refused the next launch (nothing ran): cli_this_month at "
        "or above --archivist-ceiling, or the usage read failed; reset the counter with a hand "
        "receipt (the authors' authorization) or wait, then continue with run --resume"
    ),
    "aborted": "the batch ended on an exception; in flight work settled, closing probe run",
    "closing_probe": (
        "the closing probe after the batch found a credit drop, a hot window or a window step: "
        "list the batch for the operator before any further batch"
    ),
    "plan_gate": (
        "the pre launch plan gate refused the next launch (nothing ran); wait for the window "
        "or check the billing state, then continue with run --resume"
    ),
    "invalid": "fix the cause, record a deviation, then rerun the affected cells",
    "plan_or_quota_limit": "the attempt is infra_error; continue later with run --resume",
    "plan_window": "wait for the window to reset, then continue with run --resume",
    "metered_fallback_guard": (
        "a run may have drawn on metered billing: check the credit balance or overage, list the "
        "run for the operator, then continue with run --resume"
    ),
}


def execute_plan(
    specs: Sequence[RunSpec],
    evidence_root: Path,
    max_runs: int,
    concurrency: int = 1,
    executor: ProcessExecutor = subprocess_executor,
    parent_env: dict[str, str] | None = None,
    codex_auth: Path | None = None,
    preflight: Preflight = codex_preflight,
    budget: Budget | None = None,
    start_attempts: dict[str, int] | None = None,
    plan_probe: Probe | None = None,
    rerun_attempts: dict[str, int] | None = None,
    archivist_gate: ArchivistGate | None = None,
    claude_config_dir: Path | None = None,
) -> list[RunRecord]:
    """Run the plan. ``budget`` carries ``--max-runs`` and ``--stop-at-window`` and, after the
    call, why the batch stopped (``stopped_by``, ``stop_kind``, ``stops``); ``start_attempts``
    (from ``resume_plan``) continues infra only cells at their next attempt number;
    ``archivist_gate`` (``--archivist-ceiling``) reads the Archivist counter before each
    archivist or both arm launch. ``claude_config_dir`` (``--claude-config-dir``, harness
    1.5.1) is the Claude login of every Claude Code run and of the gate's Claude probe; a plan
    with a Claude Code run needs it."""
    if max_runs < 1:
        raise ValueError("--max-runs must be at least 1")
    if len(specs) > max_runs:
        raise ValueError(f"plan has {len(specs)} runs, more than --max-runs {max_runs}; refused")
    if not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ValueError(f"concurrency must be 1 to {MAX_CONCURRENCY}")
    for spec in specs:
        if spec.config.service_tier == "fast":
            raise ValueError("the fast service tier is not allowed")
    keys = [s.run_key for s in specs]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        raise ValueError("duplicate planned run keys: " + ", ".join(duplicates))
    if claude_config_dir is None and any(s.agent == "claude_code" for s in specs):
        raise ValueError("a plan with Claude Code runs needs --claude-config-dir")
    env = dict(os.environ if parent_env is None else parent_env)
    auth = codex_auth or Path(env.get("HOME", str(Path.home()))) / ".codex" / "auth.json"
    evidence_root.mkdir(parents=True, exist_ok=True)
    ledger = evidence_root / "ledger.jsonl"
    budget = budget or Budget(max_runs)
    if budget.gate.env is None or claude_config_dir is not None:
        # The probe reads the account the runs use: one chosen value for both environments.
        budget.gate.configure(
            probe.claude_probe_env(env, claude_config_dir),
            budget.gate.scratch or evidence_root / "plan-probe-scratch",
            budget.gate.probe_log or evidence_root / PROBE_LEDGER,
        )
    if budget.gate.claude_window is None:
        budget.gate.observe_claude(probe.latest_claude_window(evidence_root, claude_config_dir))
    starts = start_attempts or {}
    reruns = dict(rerun_attempts or {})
    lock = threading.Lock()
    records: list[RunRecord] = []
    pool = ThreadPoolExecutor(max_workers=concurrency)
    try:
        futures = [
            pool.submit(
                execute_spec,
                s,
                evidence_root,
                budget,
                executor,
                env,
                auth,
                ledger,
                lock,
                preflight,
                starts.get(s.run_key, 1),
                plan_probe,
                reruns.get(s.run_key),
                archivist_gate,
                claude_config_dir,
            )
            for s in specs
        ]
        for future in futures:
            records.extend(future.result())
    except BaseException as exc:
        # Stop admission first, so queued attempts start nothing; the original exception is
        # re-raised after the cleanup below.
        budget.stop("batch", "aborted", f"{type(exc).__name__}: {exc}"[:200])
        raise
    finally:
        # Settle in flight work, then the closing probe, whatever ended the batch.
        pool.shutdown(wait=True, cancel_futures=True)
        if budget.codex_dispatched:
            try:
                closing_probe(evidence_root, env, auth, budget, plan_probe)
            except Exception as probe_exc:  # never mask the batch's own outcome
                print(f"closing probe failed: {probe_exc!r}", file=sys.stderr)
                budget.stop("closing probe", "closing_probe", f"failed: {probe_exc!r}"[:200])
    for run_id, kind, detail in budget.stops:
        print(
            f"batch stopped after {kind} run {run_id}"
            + (f" ({detail})" if detail else "")
            + f": {STOP_MESSAGES.get(kind, 'see the record')}",
            file=sys.stderr,
        )
    return records


PROBE_LEDGER = "plan-probes.jsonl"


def closing_probe(
    evidence_root: Path,
    env: dict[str, str],
    auth: Path,
    budget: Budget,
    plan_probe: Probe | None = None,
) -> dict[str, Any]:
    """One more Codex probe after the batch's last launch: a credit drop, a window at or
    above the threshold or a rise past ``--max-window-step`` is reported and stops the batch
    (``run`` exits 1). Saved as ``closing-probe.json`` and appended to the probe ledger."""
    base = evidence_root / "closing-probe"
    home, cwd = base / "codex-home", base / "cwd"
    home.mkdir(parents=True, exist_ok=True)
    cwd.mkdir(parents=True, exist_ok=True)
    link = home / "auth.json"
    if auth.exists() and not link.is_symlink():
        link.symlink_to(auth)
    report = (plan_probe or codex_plan_probe)(child_env(env, {"CODEX_HOME": str(home)}), cwd)
    reasons = budget.gate.check_codex(report)
    at = _now()
    closing = {"probed_at": at, "probe": report, "reasons": reasons}
    (evidence_root / "closing-probe.json").write_text(json.dumps(closing, indent=2), "utf-8")
    with (evidence_root / PROBE_LEDGER).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"time": at, "kind": "codex_closing", **closing}) + "\n")
    budget.closing = closing
    if reasons:
        budget.stop("closing probe", "closing_probe", "; ".join(reasons))
    return closing


def _meta(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def interrupted_dirs(evidence_root: Path) -> list[Path]:
    """Run directories that were launched (``dispatched_at`` in meta.json) and never
    recorded: interrupted attempts, each counted toward the three."""
    out = []
    for path in sorted((evidence_root / "runs").glob("*/meta.json")):
        meta = _meta(path)
        if (
            meta.get("dispatched_at")
            and not meta.get("not_dispatched")
            and not (path.parent / "record.json").exists()
        ):
            out.append(path.parent)
    return out


def undispatched_dirs(evidence_root: Path) -> list[Path]:
    """Prepared run directories that never reached launch (no ``dispatched_at``, no record):
    evidence only, never an attempt, a ledger row or a cell record."""
    out = []
    for path in sorted((evidence_root / "runs").glob("*/meta.json")):
        meta = _meta(path)
        if not meta.get("dispatched_at") and not (path.parent / "record.json").exists():
            out.append(path.parent)
    return out


def interrupted_record(run_dir: Path) -> RunRecord:
    """The ``infra_error`` record of an interrupted attempt (it counts toward the three)."""
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    cfg = meta["config"]
    agent, arm = meta["agent"], meta["arm"]
    return RunRecord(
        run_id=meta["run_id"],
        run_key=f"{agent}.{arm}.{meta['question_id']}.r{meta['repetition']}",
        agent=agent,
        arm=arm,
        question_id=meta["question_id"],
        stratum=meta["stratum"],
        repetition=int(meta["repetition"]),
        attempt=int(meta["attempt"]),
        model=cfg["model"],
        effort=cfg["effort"],
        service_tier=cfg["service_tier"],
        auth_mode=cfg["auth_mode"],
        billing_mode=_billing(agent, cfg["auth_mode"]),
        status=STATUS_INFRA_ERROR,
        status_reason="interrupted",
        usage=None,
        model_calls=0,
        tool_calls={},
        web_actions={},
        compactions=0,
        truncations=0,
        turn_limit=False,
        timeout=False,
        wall_s=float(meta.get("wall_s") or 0.0),
        cost_usd=None,
        cost_basis="none (interrupted before a record was written)",
        mcp_loaded=None,
        contaminated=False,
        started_at=str(meta.get("started_at", "")),
        notes=["interrupted: started, never recorded; raw evidence kept in the run directory"],
        claude_config_dir=_account(agent, meta),
    )


def reconciliation_signs(run_dir: Path, record: RunRecord) -> list[str]:
    """Metered billing signs an interrupted attempt left: its credits compared with its own
    saved pre launch probe (``plan-probe.json``) and with its first rollout balance, a
    ``rate_limit_reached_type``, Claude overage use."""
    window = record.plan_window or {}
    signs: list[str] = []
    reached = window.get("rate_limit_reached_seen") or window.get("rate_limit_reached_type")
    if reached:
        signs.append(f"Codex rate_limit_reached_type={reached}")
    lowest = credit_balance(window, "balance_min")
    first = credit_balance(window, "balance_first")
    saved = run_dir / "plan-probe.json"
    baseline = None
    if saved.exists():
        try:
            limits = (json.loads(saved.read_text(encoding="utf-8")).get("result") or {}).get(
                "rateLimits"
            ) or {}
            baseline = probe._number((limits.get("credits") or {}).get("balance"))
        except (json.JSONDecodeError, AttributeError):
            baseline = None
    if lowest is not None and baseline is not None and lowest < baseline:
        signs.append(f"Codex credit balance {lowest:g} below the pre dispatch probe {baseline:g}")
    if lowest is not None and first is not None and lowest < first:
        signs.append(f"Codex credit balance dropped from {first:g} to {lowest:g}")
    if window.get("is_using_overage") is True:
        signs.append("Claude isUsingOverage true")
    return signs


def reconcile_interrupted(evidence_root: Path) -> list[RunRecord]:
    """Mark prepared but never launched directories ``not_dispatched`` (evidence only), and
    write ``reconciled: interrupted`` plus a record and a ledger row (``infra_error``, reason
    ``interrupted``) for every launched but unrecorded attempt; run only when no batch is
    running in this evidence directory."""
    out = []
    ledger = evidence_root / "ledger.jsonl"
    for run_dir in undispatched_dirs(evidence_root):
        if not _meta(run_dir / "meta.json").get("not_dispatched"):
            update_meta(run_dir, not_dispatched=True, not_dispatched_reason="never dispatched")
    for run_dir in interrupted_dirs(evidence_root):
        update_meta(run_dir, reconciled="interrupted")  # extraction keeps it from now on
        record = build_record(run_dir)
        signs = reconciliation_signs(run_dir, record)
        if signs:
            prior = list(_meta(run_dir / "meta.json").get("guard_notes") or [])
            note = f"metered_fallback_guard: {'; '.join(signs)}"
            update_meta(run_dir, guard_notes=[*prior, note] if note not in prior else prior)
            record = build_record(run_dir)
            print(
                f"interrupted attempt {record.run_id} shows metered billing signs: "
                + "; ".join(signs),
                file=sys.stderr,
            )
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record.as_dict()) + "\n")
        out.append(record)
    return out


def campaign_records(evidence_root: Path) -> list[RunRecord]:
    """Every attempt of a campaign: the ledger rows, with a run's own (re-extracted)
    ``record.json`` winning over its ledger copy, run directories the ledger lacks, and each
    interrupted attempt as an ``infra_error`` (``interrupted``) record."""
    by_id: dict[str, RunRecord] = {r.run_id: r for r in load_ledger(evidence_root / "ledger.jsonl")}
    for path in sorted((evidence_root / "runs").glob("*/record.json")):
        record = RunRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        by_id[record.run_id] = record
    for run_dir in interrupted_dirs(evidence_root):
        record = interrupted_record(run_dir)
        by_id.setdefault(record.run_id, record)
    return sorted(by_id.values(), key=lambda r: r.run_id)


@dataclass
class ResumePlan:
    """What ``run --resume`` does with each planned cell."""

    pending: list[RunSpec]
    start_attempts: dict[str, int]
    done: list[str]
    excluded: list[str]
    reruns: list[str] = field(default_factory=list)
    excluded_invalid: list[str] = field(default_factory=list)
    # Attempts left in each rerun sequence (``reruns``), for ``execute_plan(rerun_attempts=)``.
    rerun_attempts: dict[str, int] = field(default_factory=dict)


def resume_plan(
    specs: Sequence[RunSpec], records: Sequence[RunRecord], rerun_invalid: bool = False
) -> ResumePlan:
    """Cells with a final record (neither ``infra_error`` nor ``invalid``) are done; infra only
    cells continue at the next attempt number, never past three attempts (a cell at three is
    excluded and listed); cells without a record run from attempt 1.

    A cell whose only non ``infra_error`` records are ``invalid`` (pre registration section
    10: the cause fixed and recorded as a deviation) gets one rerun sequence with
    ``rerun_invalid``: the attempts after its invalid record, numbered after its last, up to
    three when the earlier ones end ``infra_error`` (a plan or quota stop ends the batch as
    usual and a later ``--resume --rerun-invalid`` continues the same sequence). A second
    ``invalid`` within the sequence, or a sequence of three ``infra_error`` attempts, excludes
    and lists the cell; a cell is never given a second sequence. Without ``rerun_invalid`` an
    invalid cell is left as it is (done)."""
    by_key: dict[str, list[RunRecord]] = {}
    for r in records:
        by_key.setdefault(r.run_key, []).append(r)
    plan = ResumePlan([], {}, [], [])
    for spec in specs:
        rows = by_key.get(spec.run_key, [])
        last = max((int(r.attempt) for r in rows), default=0)
        finals = [r for r in rows if r.status not in (STATUS_INFRA_ERROR, STATUS_INVALID)]
        invalids = [r for r in rows if r.status == STATUS_INVALID]
        if finals:
            plan.done.append(spec.run_key)
            continue
        if invalids:
            first_invalid = min(int(r.attempt) for r in invalids)
            sequence = [r for r in rows if int(r.attempt) > first_invalid]
            if len(invalids) >= 2 or len(sequence) >= MAX_INFRA_RERUNS + 1:
                plan.excluded.append(spec.run_key)
                plan.excluded_invalid.append(spec.run_key)
            elif rerun_invalid:
                plan.pending.append(spec)
                plan.start_attempts[spec.run_key] = last + 1
                plan.reruns.append(spec.run_key)
                plan.rerun_attempts[spec.run_key] = MAX_INFRA_RERUNS + 1 - len(sequence)
            else:
                plan.done.append(spec.run_key)
            continue
        if last >= MAX_INFRA_RERUNS + 1:
            plan.excluded.append(spec.run_key)
            continue
        plan.pending.append(spec)
        if last:
            plan.start_attempts[spec.run_key] = last + 1
    return plan


def load_ledger(path: Path) -> list[RunRecord]:
    return [RunRecord.from_dict(json.loads(line)) for line in _read_lines(path) if line.strip()]


def ledger_markdown(records: Sequence[RunRecord]) -> str:
    head = (
        "| run | agent | arm | question | model / effort / tier | auth / billing | status | "
        "total tokens | uncached in | cached in | output | reasoning | model calls | tool calls | "
        "wall s | cost USD | MCP loaded | events |\n"
        "|---|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---|---|\n"
    )
    rows = []
    for r in records:
        u = r.usage or {}
        tools = ", ".join(f"{k} {v}" for k, v in r.tool_calls.items()) or "none"
        status = r.status + (f" ({r.status_reason})" if r.status_reason else "")
        if r.contaminated:
            status += ", contaminated"
        cost = f"{r.cost_usd:.4f}" if r.cost_usd is not None else "n/a"
        rows.append(
            f"| {r.run_id} | {r.agent} | {r.arm} | {r.question_id} | "
            f"{r.model} / {r.effort} / {r.service_tier} | {r.auth_mode} / {r.billing_mode} | "
            f"{status} | {u.get('total', 'n/a')} | {u.get('uncached_input', 'n/a')} | "
            f"{u.get('cached_input', 'n/a')} | {u.get('output', 'n/a')} | "
            f"{u.get('reasoning', 'n/a')} | {r.model_calls} | {tools} | {r.wall_s:.1f} | "
            f"{cost} | {_mcp_cell(r)} | {_events(r)} |"
        )
    return head + "\n".join(rows) + "\n"


def _events(r: RunRecord) -> str:
    parts = [
        f"compactions {r.compactions}",
        f"truncated calls {r.truncations}"
        + (
            " (" + ", ".join(str(c.get("original_tokens")) for c in r.truncated_calls) + " tok)"
            if r.truncated_calls
            else ""
        ),
        f"paged results {r.paged_results}",
    ]
    if r.turn_limit:
        parts.append("turn limit")
    if r.timeout:
        parts.append("timeout")
    return "; ".join(parts)


def _mcp_cell(r: RunRecord) -> str:
    if r.arm == "web":
        return "not configured" if not r.mcp_loaded else "LOADED (violation)"
    return "yes" if r.mcp_loaded else "no"
