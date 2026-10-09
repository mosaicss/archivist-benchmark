"""Plan gate: pre launch headroom plus before and after probes (pre registration section 11,
D2; review rounds 1 and 2).

No CLI switch disables credit or overage use, so this is not an account level prevention. The
control is headroom: no launch starts unless the plan shows room below the threshold
(``--stop-at-window``, default 90), and probes before each launch and after each batch
detect any spend past it.

Codex (every agent attempt and judge pass B call): a fresh ``codex app-server`` answers
``account/rateLimits/read`` over stdio (``initialize``, ``initialized``, then the read; no model
call, nothing spent, never ``account/rateLimitResetCredit/consume`` or any mutating method).
The launch is refused, and the batch stops, on a failed, timed out or incomplete probe (fail
closed), ``ordinaryUsageAllowed`` false, a window at or above the threshold, a window that rose
more than ``--max-window-step`` points since the previous probe of the batch,
``rateLimitReachedType`` set, ``spendControlReached`` true, or a credit balance below the batch
baseline (the first probe's balance). A closing probe after each batch reports any drop.

Claude Code (every agent attempt and judge pass A call): the latest plan window (agent records,
judge call metas, probe ledger) must be at most 30 minutes old, else a minimal plan probe call
(``claude -p --restricted`` on a small model, ``Reply with OK.``) refreshes it; the launch
needs no rejection, no window at or above the threshold, ``isUsingOverage`` false and overage
``rejected`` because ``org_level_disabled`` (any other state, a missing reason included, fails
closed). A hot or rejected reading whose windows have reset since is refreshed by a probe.

Residual risk: a launch admitted just under the threshold can spend past it; with two
concurrent launches and other consumers of the same login, the last 10 points of a window
are the exposure, which the after probes report.
"""

from __future__ import annotations

import json
import math
import os
import queue
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .model import DEFAULT_STOP_AT_WINDOW

APP_SERVER_ARGV: tuple[str, ...] = ("codex", "app-server")
PROBE_TIMEOUT_S = 30.0
READ_METHOD = "account/rateLimits/read"
# Keys dropped from a saved probe: account identifiers (anywhere) and the ids of the reset
# credits (``rateLimitResetCredits``).
ACCOUNT_KEYS = frozenset(
    {
        "accountid",
        "account_id",
        "userid",
        "user_id",
        "email",
        "organizationid",
        "organization_id",
        "orgid",
        "org_id",
        "creator_user_id",
        "creator_account_id",
        "chatgpt_account_id",
    }
)


def _sanitize(value: Any, in_credits: bool = False) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            low = str(key).lower()
            if low in ACCOUNT_KEYS or "accountid" in low.replace("_", ""):
                continue
            if in_credits and (low in ("id", "ids") or low.endswith(("id", "ids"))):
                continue
            out[key] = _sanitize(item, in_credits or key == "rateLimitResetCredits")
        return out
    if isinstance(value, list):
        return [_sanitize(v, in_credits) for v in value]
    return value


def sanitize(result: Any) -> Any:
    """A probe result without account identifiers or reset credit ids."""
    return _sanitize(result)


def exchange(
    argv: list[str], env: dict[str, str], cwd: Path, timeout_s: float = PROBE_TIMEOUT_S
) -> dict[str, Any]:
    """Talk to an app-server over stdio: ``initialize``, ``initialized``, then the rate limit
    read. Returns ``{"ok": True, "result": <sanitized result>}`` or ``{"ok": False, "error"}``.
    Only these three messages are ever sent."""
    try:
        proc = subprocess.Popen(
            argv,
            env=env,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    lines: queue.Queue[str] = queue.Queue()

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)

    threading.Thread(target=pump, daemon=True).start()

    def send(obj: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def wait_for(request_id: int) -> dict[str, Any]:
        while True:
            text = lines.get(timeout=timeout_s).strip()
            if not text:
                continue
            obj = json.loads(text)
            if isinstance(obj, dict) and obj.get("id") == request_id and "method" not in obj:
                return obj

    try:
        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "archivist_bench_probe", "version": "1"}},
            }
        )
        init = wait_for(1)
        if "error" in init:
            return {"ok": False, "error": f"initialize: {str(init['error'])[:300]}"}
        send({"method": "initialized"})
        send({"id": 2, "method": READ_METHOD})
        read = wait_for(2)
        if "error" in read or not isinstance(read.get("result"), dict):
            return {"ok": False, "error": f"{READ_METHOD}: {str(read.get('error'))[:300]}"}
        return {"ok": True, "result": sanitize(read["result"])}
    except (queue.Empty, json.JSONDecodeError, OSError, ValueError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        proc.kill()
        proc.wait()


def app_server_probe(env: dict[str, str], cwd: Path) -> dict[str, Any]:
    """The live probe: ``codex app-server`` in the launch's environment (its ``CODEX_HOME``
    with the auth symlink). Tests replace this function; they never reach a real server."""
    return exchange(list(APP_SERVER_ARGV), env, cwd)


def _number(value: Any) -> float | None:
    try:
        number = float(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None
    return number if number is not None and math.isfinite(number) else None


def now_utc() -> datetime:
    return datetime.now(UTC)


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: Any) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def validate_codex(probe: dict[str, Any]) -> list[str]:
    """Reasons a Codex probe result is incomplete (fail closed): ``ordinaryUsageAllowed`` a
    boolean, ``rateLimits.primary.usedPercent`` a finite number, ``credits.hasCredits`` a
    boolean and, when true, a finite ``balance``."""
    if not probe.get("ok"):
        return [f"plan probe failed: {probe.get('error', 'no result')}"]
    result = probe.get("result")
    if not isinstance(result, dict):
        return ["plan probe returned no result"]
    limits = result.get("rateLimits")
    if not isinstance(limits, dict):
        return ["plan probe returned no rateLimits"]
    problems: list[str] = []
    if not isinstance(result.get("ordinaryUsageAllowed"), bool):
        problems.append("plan probe: ordinaryUsageAllowed is not a boolean")
    primary = limits.get("primary")
    if not isinstance(primary, dict) or _number(primary.get("usedPercent")) is None:
        problems.append("plan probe: rateLimits.primary.usedPercent is not a finite number")
    secondary = limits.get("secondary")
    if secondary is not None and (
        not isinstance(secondary, dict) or _number(secondary.get("usedPercent")) is None
    ):
        problems.append("plan probe: rateLimits.secondary.usedPercent is not a finite number")
    credits = limits.get("credits")
    if not isinstance(credits, dict) or not isinstance(credits.get("hasCredits"), bool):
        problems.append("plan probe: rateLimits.credits.hasCredits is not a boolean")
    elif credits["hasCredits"] and _number(credits.get("balance")) is None:
        problems.append("plan probe: credits.balance is not a finite number")
    return problems


# --- Claude Code plan probe (pre registration section 11, D2; review round 2, R2) -----------------

CLAUDE_PROBE_MODEL = "claude-haiku-4-5-20251001"
CLAUDE_PROBE_PROMPT = "Reply with OK."
CLAUDE_PROBE_TIMEOUT_S = 120.0
CLAUDE_READING_MAX_AGE_S = 30 * 60
DEFAULT_MAX_WINDOW_STEP = 5.0
# The only overage state that proves no paid fallback: overage rejected because the
# organization disabled it. A temporary rejection (``fetch_error`` and the like) proves nothing.
DURABLE_OVERAGE = ("rejected", "org_level_disabled")


def claude_probe_command() -> list[str]:
    return [
        "claude",
        "-p",
        "--restricted",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        CLAUDE_PROBE_MODEL,
        "--tools",
        "",
        "--strict-mcp-config",
        "--max-turns",
        "1",
        "--no-session-persistence",
        "--disable-slash-commands",
    ]


def claude_probe_call(env: dict[str, str], cwd: Path) -> list[str]:
    """The live Claude probe: one minimal plan model call (not a scored run) whose stream
    carries the ``rate_limit_event``. Tests replace this function."""
    done = subprocess.run(
        claude_probe_command(),
        input=CLAUDE_PROBE_PROMPT,
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=CLAUDE_PROBE_TIMEOUT_S,
        check=False,
    )
    return done.stdout.splitlines()


def parse_claude_probe(lines: list[str], observed_at: str) -> dict[str, Any]:
    """A Claude probe's reading: ``{"ok": True, "reading": <plan window>, "usage"}`` or
    ``{"ok": False, "error"}``; fails closed without ``apiKeySource: none`` or without a
    ``rate_limit_event``."""
    from . import claude_code

    init: dict[str, Any] | None = None
    info: dict[str, Any] | None = None
    usage: Any = None
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "system" and obj.get("subtype") == "init":
            init = obj
        elif obj.get("type") == "rate_limit_event" and isinstance(obj.get("rate_limit_info"), dict):
            info = obj["rate_limit_info"]
        elif obj.get("type") == "result":
            usage = obj.get("modelUsage") or obj.get("usage")
    if init is None:
        return {"ok": False, "error": "no system/init event"}
    if init.get("apiKeySource") != "none":
        return {"ok": False, "error": f"apiKeySource {init.get('apiKeySource')!r}, not none"}
    if info is None:
        return {"ok": False, "error": "no rate_limit_event"}
    reading = claude_code.plan_window(info)
    reading["observed_at"] = observed_at
    reading["source"] = "claude_probe"
    return {"ok": True, "reading": reading, "usage": usage}


def _probe_row(
    kind: str,
    observed_at: str,
    report: dict[str, Any],
    claude_config_dir: str | None = None,
) -> dict[str, Any]:
    reading = report.get("reading") or {}
    row: dict[str, Any] = {
        "time": observed_at,
        "kind": kind,
        "ok": bool(report.get("ok")),
        "error": report.get("error"),
        "status": reading.get("status"),
        "windows": {
            name: {"utilization": value, "resetsAt": (reading.get("window_resets") or {}).get(name)}
            for name, value in (reading.get("windows") or {}).items()
        },
        "overage_status": reading.get("overage_status"),
        "overage_disabled_reason": reading.get("overage_disabled_reason"),
        "is_using_overage": reading.get("is_using_overage"),
        "usage": report.get("usage"),
        "reading": reading or None,
    }
    if claude_config_dir is not None:
        # The Claude account the probe read (harness 1.5.1); ``latest_claude_window`` counts
        # only readings of the chosen account.
        row["claude_config_dir"] = claude_config_dir
    return row


CLAUDE_DISABLED = "Claude calls are disabled in this batch (judge --passes B, amendment 2)"
NO_CLAUDE_ACCOUNT = (
    "no Claude account chosen: the probe environment has no CLAUDE_CONFIG_DIR "
    "(pass --claude-config-dir); the probe would read the default ~/.claude login"
)


class PlanGate:
    """Pre launch checks shared by one batch (thread safe).

    Codex: the threshold, the credit baseline (the first probe's balance) and the previous
    probe's ``usedPercent`` per window (a rise of more than ``max_window_step`` points between
    consecutive probes stops the batch: one launch spent more than the headroom assumes).
    Claude Code: the latest plan window, refreshed by a minimal plan probe call when it is
    missing, older than 30 minutes, or blocking only through windows whose reset has passed.

    This is pre launch headroom plus before and after probes, not an account level
    prevention: no CLI switch disables credit use, so a launch admitted inside the last
    points of a window can still spend past it (residual risk: two concurrent launches plus
    other consumers of the login within the last 10 points)."""

    def __init__(
        self,
        threshold: float = DEFAULT_STOP_AT_WINDOW,
        max_window_step: float = DEFAULT_MAX_WINDOW_STEP,
        clock: Callable[[], datetime] = now_utc,
        allow_claude: bool = True,
    ) -> None:
        if not 1 <= threshold <= 100:
            raise ValueError("--stop-at-window must be 1 to 100")
        if not max_window_step > 0:
            raise ValueError("--max-window-step must be above 0")
        self.threshold = threshold
        self.max_window_step = max_window_step
        self.clock = clock
        self.baseline: float | None = None
        self.previous_used: dict[str, float] = {}
        self.claude_window: dict[str, Any] | None = None
        # Where a Claude probe runs and is logged (set by the runner or the judge command).
        self.env: dict[str, str] | None = None
        self.scratch: Path | None = None
        self.probe_log: Path | None = None
        self.claude_probes = 0
        # False for a Codex only batch (``judge --passes B``, amendment 2): every Claude check
        # refuses and no Claude probe call is ever made.
        self.allow_claude = allow_claude
        self._lock = threading.RLock()

    def configure(
        self, env: dict[str, str] | None, scratch: Path | None, probe_log: Path | None
    ) -> None:
        with self._lock:
            if (env or {}).get("CLAUDE_CONFIG_DIR") != (self.env or {}).get("CLAUDE_CONFIG_DIR"):
                # Another Claude account: a cached reading belongs to the old one (harness
                # 1.5.1), so the new account's own reading is loaded or probed.
                self.claude_window = None
            self.env, self.scratch, self.probe_log = env, scratch, probe_log

    def baseline_for(self, balance: float) -> float:
        """The batch credit baseline, set by the first balance seen."""
        with self._lock:
            if self.baseline is None:
                self.baseline = balance
            return self.baseline

    def check_codex(self, probe: dict[str, Any]) -> list[str]:
        """Reasons to refuse a Codex launch (empty: go)."""
        invalid = validate_codex(probe)
        if invalid:
            return invalid
        result = probe["result"]
        limits = result["rateLimits"]
        reasons: list[str] = []
        if result["ordinaryUsageAllowed"] is not True:
            reasons.append("ordinaryUsageAllowed=False")
        with self._lock:
            for key in ("primary", "secondary"):
                window = limits.get(key)
                if not isinstance(window, dict):
                    continue
                used = _number(window.get("usedPercent"))
                assert used is not None
                minutes = window.get("windowDurationMins")
                if used >= self.threshold:
                    reasons.append(
                        f"codex {key} ({minutes} min) at {used:g}% (threshold {self.threshold:g}%)"
                    )
                before = self.previous_used.get(key)
                if before is not None and used - before > self.max_window_step:
                    reasons.append(
                        f"codex {key} rose {used - before:g} points between probes "
                        f"(max step {self.max_window_step:g})"
                    )
                self.previous_used[key] = used
        if limits.get("rateLimitReachedType"):
            reasons.append(f"rateLimitReachedType={limits['rateLimitReachedType']}")
        if limits.get("spendControlReached") is True:
            reasons.append("spendControlReached=true")
        balance = _number((limits.get("credits") or {}).get("balance"))
        if balance is not None:
            baseline = self.baseline_for(balance)
            if balance < baseline:
                reasons.append(
                    f"Codex credit balance {balance:g} below the batch baseline {baseline:g}"
                )
        return reasons

    def observe_claude(self, window: dict[str, Any] | None) -> None:
        if window:
            with self._lock:
                self.claude_window = window

    def _incomplete(self, window: dict[str, Any]) -> list[str]:
        """What keeps a reading from establishing usable allowance (fail closed): ``status``
        must be ``allowed`` (a rejection is a blocking reason of its own), at least one window
        with every utilization finite, and ``is_using_overage`` explicitly False."""
        problems: list[str] = []
        if window.get("status") not in ("allowed", "rejected"):
            problems.append(f"Claude reading status {window.get('status')!r} is not allowed")
        windows = window.get("windows")
        if not isinstance(windows, dict) or not windows:
            problems.append("Claude reading has no plan windows")
        else:
            for name, value in sorted(windows.items()):
                if _number(value) is None:
                    problems.append(f"Claude window {name} utilization {value!r} is not finite")
        if (
            window.get("is_using_overage") is not False
            and window.get("is_using_overage") is not True
        ):
            problems.append(
                f"Claude isUsingOverage {window.get('is_using_overage')!r} is not explicitly false"
            )
        return problems

    def _blocking(self, window: dict[str, Any]) -> tuple[list[str], list[str], list[Any]]:
        """(reasons that a window reset can clear, reasons that only a new reading can, the
        reset times of the blocking windows; None where a blocking window has none)."""
        resettable: list[str] = []
        lasting: list[str] = []
        resets: list[Any] = []
        if window.get("status") == "rejected" or window.get("rejected_seen"):
            resettable.append("Claude plan window rejected")
            resets.append(window.get("resets_at"))
        for name, value in sorted((window.get("windows") or {}).items()):
            used = _number(value)
            if used is not None and used * 100 >= self.threshold:
                resettable.append(
                    f"claude {name} at {used * 100:g}% (threshold {self.threshold:g}%)"
                )
                resets.append((window.get("window_resets") or {}).get(name))
        if window.get("is_using_overage") is True:
            lasting.append("Claude isUsingOverage true")
        state = (window.get("overage_status"), window.get("overage_disabled_reason"))
        if state != DURABLE_OVERAGE:
            lasting.append(
                f"Claude overage {state[0]!r}/{state[1]!r} is not rejected/org_level_disabled: "
                "a paid fallback is not ruled out"
            )
        return resettable, lasting, resets

    def _stale(self, window: dict[str, Any] | None) -> bool:
        if not window:
            return True
        observed = parse_time(window.get("observed_at"))
        now = self.clock()
        if observed is None or (now - observed).total_seconds() > CLAUDE_READING_MAX_AGE_S:
            return True
        if self._incomplete(window):
            return True  # a fresh probe may replace an incomplete reading
        resettable, _, resets = self._blocking(window)
        if not resettable:
            return False
        # Blocking only through windows (or a rejection) whose reset has passed since: a fresh
        # probe decides. Reset times of windows that do not block are irrelevant.
        times = [_number(r) for r in resets]
        return all(t is not None and t <= now.timestamp() for t in times)

    @property
    def claude_config_dir(self) -> str | None:
        """The Claude account this gate's probe reads (``CLAUDE_CONFIG_DIR`` of its
        environment), None when no account was chosen."""
        value = (self.env or {}).get("CLAUDE_CONFIG_DIR")
        return value or None

    def probe_claude(self) -> dict[str, Any]:
        """Run one Claude probe call, log it and keep its reading. Fails closed, with no
        subprocess, when the probe environment names no Claude account (harness 1.5.1)."""
        if not self.allow_claude:
            return {"ok": False, "error": CLAUDE_DISABLED}
        env = self.env if self.env is not None else _default_claude_env()
        account = env.get("CLAUDE_CONFIG_DIR") or None
        if account is None:
            return {"ok": False, "error": NO_CLAUDE_ACCOUNT}
        observed_at = stamp(self.clock())
        base = self.scratch
        if base is not None:
            base.mkdir(parents=True, exist_ok=True)
        cwd = Path(tempfile.mkdtemp(prefix="claude-probe-", dir=base))
        try:
            lines = claude_probe_call(env, cwd)
            report = parse_claude_probe(lines, observed_at)
        except (OSError, subprocess.SubprocessError) as exc:
            report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            shutil.rmtree(cwd, ignore_errors=True)
        self.claude_probes += 1
        if self.probe_log is not None:
            self.probe_log.parent.mkdir(parents=True, exist_ok=True)
            with self.probe_log.open("a", encoding="utf-8") as fh:
                row = _probe_row("claude_probe", observed_at, report, account)
                fh.write(json.dumps(row) + "\n")
        if report.get("ok"):
            self.observe_claude(report["reading"])
        return report

    def check_claude(self) -> list[str]:
        """Reasons to refuse a Claude Code launch: the latest reading, refreshed by a probe
        call when missing, stale or blocking only through windows that have reset."""
        if not self.allow_claude:
            return [CLAUDE_DISABLED]
        with self._lock:
            window = self.claude_window
            if self._stale(window):
                report = self.probe_claude()
                if not report.get("ok"):
                    return [f"Claude plan probe failed: {report.get('error')}"]
                window = self.claude_window
            assert window is not None
            return self.claude_reasons(window)

    def claude_reasons(self, window: dict[str, Any]) -> list[str]:
        """Reasons a given Claude reading refuses a launch at this gate's threshold, without
        any probe call (``check_claude`` after its refresh; ``plan-probe`` on its one reading)."""
        resettable, lasting, _ = self._blocking(window)
        return self._incomplete(window) + resettable + lasting


def _default_claude_env() -> dict[str, str]:
    """The allowlisted environment without any Claude account: ``CLAUDE_CONFIG_DIR`` is never
    supplied here (the parent's value is not inherited), so a probe on it refuses."""
    from .runner import child_env

    return child_env(dict(os.environ), CLAUDE_PROBE_ENV)


CLAUDE_PROBE_ENV = {"CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "DISABLE_AUTOUPDATER": "1"}


def claude_probe_env(
    parent: dict[str, str], config_dir: str | os.PathLike[str] | None
) -> dict[str, str]:
    """The Claude probe environment: the allowlisted parent plus the probe switches and, when an
    account was chosen, its ``CLAUDE_CONFIG_DIR`` from ``claude_code.account_extras`` (the same
    source as the agent runs and judge pass A calls)."""
    from . import claude_code
    from .runner import child_env

    extras = dict(CLAUDE_PROBE_ENV)
    if config_dir is not None:
        extras |= claude_code.account_extras(config_dir)
    return child_env(parent, extras)


def _observed(window: dict[str, Any], fallback: Any) -> str:
    return str(window.get("observed_at") or fallback or "")


def latest_claude_window(
    evidence: Path, config_dir: str | os.PathLike[str] | None
) -> dict[str, Any] | None:
    """The newest Claude plan window of a campaign for the chosen account: agent records
    (``record.json``), Claude judge calls (``judge-ledger.jsonl`` metas) and Claude probes
    (``plan-probes.jsonl``), by observation time. Only readings whose recorded
    ``claude_config_dir`` equals ``config_dir`` count (harness 1.5.1): untagged readings (made
    before 1.5.1, on the default account) and readings of another account are ignored, and no
    chosen account means no reading."""
    if config_dir is None:
        return None
    account = os.fspath(config_dir)
    seen: list[tuple[str, dict[str, Any]]] = []
    for path in sorted((evidence / "runs").glob("*/record.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        window = rec.get("plan_window")
        if rec.get("claude_config_dir") != account:
            continue
        if rec.get("agent") == "claude_code" and isinstance(window, dict):
            seen.append((_observed(window, rec.get("started_at")), window))
    for name, kind in (("judge-ledger.jsonl", "judge"), ("plan-probes.jsonl", "probe")):
        path = evidence / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if kind == "judge":
                meta = row.get("meta") or {}
                window = meta.get("plan_window") if isinstance(meta, dict) else None
                if row.get("cli") != "claude_code" or not isinstance(meta, dict):
                    continue
                if meta.get("claude_config_dir") != account:
                    continue
            else:
                window = row.get("reading") if row.get("kind") == "claude_probe" else None
                if row.get("claude_config_dir") != account:
                    continue
            if isinstance(window, dict) and window:
                seen.append((_observed(window, row.get("started_at") or row.get("time")), window))
    if not seen:
        return None
    return max(seen, key=lambda item: item[0])[1]
