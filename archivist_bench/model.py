"""Shared types, constants and the pre registered defaults."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

Agent = Literal["codex", "claude_code"]
Arm = Literal["web", "archivist", "both"]

AGENTS: tuple[Agent, ...] = ("codex", "claude_code")
ARMS: tuple[Arm, ...] = ("web", "archivist", "both")

# Pre registered constants (preregistration.md section "Statistics" and "Scale").
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 8105
REPETITIONS = 3
CLAUDE_SUBSET_PER_STRATUM = 5
MAX_CONCURRENCY = 2
MAX_INFRA_RERUNS = 2

PROMPT_TEMPLATE = (
    "Research question (use your research tools; do not run shell commands): {question} "
    "Be concise and cite your sources."
)

# Run status vocabulary. Only ``completed`` runs are scored for tokens and accuracy; H5 scores
# completion over every non excluded run (see preregistration.md).
STATUS_COMPLETED = "completed"
STATUS_INCOMPLETE = "incomplete"
STATUS_INVALID = "invalid"
STATUS_INFRA_ERROR = "infra_error"
STATUS_TIMEOUT = "timeout"
STATUS_TURN_LIMIT = "turn_limit"
STATUS_PLANNED = "planned"

EXCLUDED_STATUSES = frozenset({STATUS_INVALID, STATUS_INFRA_ERROR})

# Infra reasons that stop the batch instead of rerunning at once (pre registration section 11,
# D2): a plan usage limit (Codex ``usage_limit_exceeded`` or ``rate_limit_exceeded``, Claude Code
# ``usage_limit:*``) or the Archivist monthly quota. The attempt stays ``infra_error`` and counts
# toward the three attempts; ``run --resume`` continues later. Transient reasons (overload, 5xx,
# an Archivist burst ``RATE_LIMITED``) keep the immediate rerun.
STOP_INFRA_REASONS = frozenset({"usage_limit_exceeded", "rate_limit_exceeded", "archivist_quota"})
STOP_INFRA_PREFIXES: tuple[str, ...] = ("usage_limit:",)
DEFAULT_STOP_AT_WINDOW = 90.0
# Pre launch Archivist quota gate (harness 1.4.0): no launch that can call
# Archivist starts once the benchmark account's ``cli_this_month`` reaches this ceiling.
DEFAULT_ARCHIVIST_CEILING = 9800


def stops_batch(reason: str | None) -> bool:
    """True when an ``infra_error`` reason is a plan or quota limit (stop, do not rerun now)."""
    return bool(reason) and (
        reason in STOP_INFRA_REASONS or str(reason).startswith(STOP_INFRA_PREFIXES)
    )


def merge_infra(current: str | None, new: str) -> str:
    """Combine infra reasons seen in one run: a plan or quota stop reason wins and is sticky (a
    later transient reason never replaces it, and it replaces an earlier transient one);
    otherwise the first reason stays."""
    if current is None:
        return new
    if stops_batch(current):
        return current
    return new if stops_batch(new) else current


DEFAULT_EVIDENCE_ROOT = (
    Path(os.environ.get("MOSAIC_EVIDENCE_ROOT", str(Path.home() / "archivist-bench-evidence")))
    / "codex"
    / "campaign"
)


@dataclass(frozen=True)
class AgentConfig:
    """Identical per agent across arms: model, effort, tier, auth, bounds."""

    agent: Agent
    model: str
    effort: str
    auth_mode: str
    service_tier: str = "default"
    max_turns: int | None = None
    timeout_s: int = 1200

    @property
    def billing_mode(self) -> str:
        return BILLING_MODES[(self.agent, self.auth_mode)]


BILLING_MODES: dict[tuple[str, str], str] = {
    ("codex", "chatgpt"): "chatgpt_plan_allowance",
    ("codex", "api_key"): "openai_api_metered",
    ("claude_code", "subscription"): "claude_plan_allowance",
    ("claude_code", "api_key"): "anthropic_console_metered",
}

DEFAULT_CONFIGS: dict[Agent, AgentConfig] = {
    "codex": AgentConfig(
        agent="codex",
        model="gpt-6.1-sol",
        effort="medium",
        auth_mode="chatgpt",
        service_tier="default",
        max_turns=None,
        timeout_s=1200,
    ),
    "claude_code": AgentConfig(
        agent="claude_code",
        model="claude-opus-5-5",
        effort="medium",
        auth_mode="subscription",
        service_tier="default",
        max_turns=50,
        timeout_s=1200,
    ),
}


@dataclass(frozen=True)
class RunSpec:
    """One planned run: agent, arm, question and repetition."""

    agent: Agent
    arm: Arm
    question_id: str
    stratum: str
    repetition: int
    prompt: str
    config: AgentConfig

    @property
    def run_key(self) -> str:
        return f"{self.agent}.{self.arm}.{self.question_id}.r{self.repetition}"


@dataclass
class Usage:
    """Token usage. ``output`` includes ``reasoning`` for both agents."""

    total: int = 0
    input: int = 0
    cached_input: int = 0
    cache_write_input: int = 0
    uncached_input: int = 0
    output: int = 0
    reasoning: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Extraction:
    """What an extractor reads out of one run's raw evidence."""

    usage: Usage | None = None
    model: str | None = None
    effort: str | None = None
    service_tier: str | None = None
    model_calls: int = 0
    tool_calls: dict[str, int] = field(default_factory=dict)
    web_actions: dict[str, int] = field(default_factory=dict)
    web_urls: list[str] = field(default_factory=list)
    compactions: int = 0
    truncations: int = 0
    truncated_calls: list[dict[str, Any]] = field(default_factory=list)
    paged_results: int = 0
    turn_limit: bool = False
    num_turns: int | None = None
    mcp_servers: dict[str, str] = field(default_factory=dict)
    mcp_evidence: dict[str, str] = field(default_factory=dict)
    init_tools: list[str] | None = None
    api_key_source: str | None = None
    answer: str = ""
    errors: list[str] = field(default_factory=list)
    infra_reason: str | None = None
    incomplete_reason: str | None = None
    client_cost_usd: float | None = None
    model_usage: dict[str, Any] = field(default_factory=dict)
    per_call_usage: list[dict[str, Any]] = field(default_factory=list)
    cli_version: str | None = None
    # The agent's plan window as the run last saw it (Codex rollout ``rate_limits``, Claude Code
    # ``rate_limit_event``); None when the run carried no such data.
    plan_window: dict[str, Any] | None = None


@dataclass
class RunRecord:
    """One ledger row: everything the analysis and the run ledger need."""

    run_id: str
    run_key: str
    agent: str
    arm: str
    question_id: str
    stratum: str
    repetition: int
    attempt: int
    model: str
    effort: str
    service_tier: str
    auth_mode: str
    billing_mode: str
    status: str
    status_reason: str | None
    usage: dict[str, Any] | None
    model_calls: int
    tool_calls: dict[str, int]
    web_actions: dict[str, int]
    compactions: int
    truncations: int
    turn_limit: bool
    timeout: bool
    wall_s: float
    cost_usd: float | None
    cost_basis: str
    mcp_loaded: bool | None
    contaminated: bool
    started_at: str
    cli_version: str | None = None
    num_turns: int | None = None
    answer_file: str | None = None
    truncated_calls: list[dict[str, Any]] = field(default_factory=list)
    paged_results: int = 0
    notes: list[str] = field(default_factory=list)
    plan_window: dict[str, Any] | None = None
    # The Claude account (``CLAUDE_CONFIG_DIR``) a Claude Code run used (harness 1.5.1); never
    # set for Codex, and left out of the record when unset so Codex records keep their keys.
    claude_config_dir: str | None = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if data.get("claude_config_dir") is None:
            data.pop("claude_config_dir", None)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunRecord:
        names = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in names})


def flatten_text(obj: Any) -> str:
    """Every string inside a JSON value, joined (tool results nest text in content parts)."""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return "\n".join(flatten_text(v) for v in obj.values())
    if isinstance(obj, list):
        return "\n".join(flatten_text(v) for v in obj)
    return ""
