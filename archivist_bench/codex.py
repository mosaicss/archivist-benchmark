"""Codex CLI arms: config rendering, command line, environment and rollout extraction.

Facts checked against Codex CLI 0.160.0 (``codex exec --help``, ``codex features list`` and
real rollouts): ``exec`` has no ``--search``/``-a`` flags, so web search and approvals go in
``config.toml``; ``web_search`` defaults to ``cached`` and is always set explicitly; usage is the
last cumulative ``token_count.info.total_token_usage``; every model request writes one
``token_usage_record``; web search is an ``item_completed`` ``Extension`` item with
``kind == "web.search"`` and an ``action.type`` of ``search``, ``openPage``, ``findInPage`` or
``other``; context compaction writes a top level ``compacted`` record. Codex has no turn limit.
"""

from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .model import AgentConfig, Arm, Extraction, Usage, flatten_text, merge_infra

ARCHIVIST_SERVER = "archivist"
# archivist-cli MCP tools; every one is pre approved so ``approval_policy = "never"`` cannot
# stall a run. v0.2.33 (the tested CLI, pre registration section 11, D1) serves 8 tools and adds
# ``filings`` and ``find``; the 0.2.32 names stay listed (an approval for a tool the server does
# not serve is inert). Tools a later CLI adds are still served (approval falls back to policy).
ARCHIVIST_TOOLS: tuple[str, ...] = (
    "auth_status",
    "auth_whoami",
    "companies_get",
    "companies_search",
    "doctor",
    "filings",
    "find",
    "read_passage",
    "read_section",
    "search",
    "toc",
    "usage",
    "version",
)
# Pilot list plus the 0.160.0 features that add tools or discovery overhead.
DISABLED_FEATURES: tuple[str, ...] = (
    "apps",
    "plugins",
    "remote_plugin",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "in_app_browser",
    "multi_agent",
    "image_generation",
    "goals",
    "hooks",
    "tool_suggest",
    "skill_search",
    "sleep_tool",
    # No shell or file tools in any arm: the read only sandbox still lets a shell read local
    # files, including the answer keys in this repository.
    "shell_tool",
    "unified_exec",
)
WEB_SEARCH_MODE: dict[str, str] = {"web": "live", "archivist": "disabled", "both": "live"}
INFRA_ERROR_CODES = frozenset(
    {"usage_limit_exceeded", "server_overloaded", "internal_server_error", "rate_limit_exceeded"}
)
# Explicit quota or rate limit markers only (a bare "429" can be a figure in a passage), and
# only on a tool result flagged as an error. ``CLI_QUOTA`` is the monthly CLI quota (the batch
# stops); the rest is a burst limit (immediate rerun).
ARCHIVIST_429 = re.compile(
    r"CLI_QUOTA|RATE_LIMITED|Too Many Requests|HTTP 429|status(?: code)?:? 429", re.IGNORECASE
)
ARCHIVIST_QUOTA = re.compile(r"CLI_QUOTA", re.IGNORECASE)
# Codex exec cuts large tool results and says so in the output text (seen in 0.160.0 rollouts:
# "Warning: truncated output (original token count: 12620)").
CLIENT_TRUNCATION = re.compile(r"Warning: truncated output \(original token count: (\d+)\)")
# Archivist's own response budget: whole trailing items move behind a cursor.
PAGED_MARKERS = ('"truncated": true', '"truncated":true')
# Tool names that run commands or edit files when they appear as response_item calls. Code
# mode's own ``exec`` wrapper (which calls MCP tools) is not one of them.
SHELL_CALL_NAMES = frozenset(
    {"exec_command", "shell", "shell_command", "local_shell", "container.exec", "write_stdin"}
)
PATCH_CALL_NAMES = frozenset({"apply_patch"})
CATALOG_TOOL = re.compile(r"\bmcp__([A-Za-z0-9-]+?)__([A-Za-z0-9_]+)")
# Codex's built in MCP resource tools, reported as ``McpToolCall`` items of the pseudo server
# ``codex``: they only reach the configured MCP servers (Archivist alone in the archivist and
# both arms). Like Claude Code's ``MCP_RESOURCE_TOOLS`` they are allowed in those arms and stay
# violations in the web arm (pre registration section 11, D5; harness 1.2.1). They are resource
# listings, not Archivist ``/research`` calls.
CODEX_PSEUDO_SERVER = "codex"
CODEX_RESOURCE_TOOLS = frozenset(
    {"list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"}
)


def is_resource_builtin(server: str, tool: str) -> bool:
    return server == CODEX_PSEUDO_SERVER and tool in CODEX_RESOURCE_TOOLS


MCP_STDERR = re.compile(r"MCP (?:client|server) for `?(?P<server>[\w-]+)`? (?P<what>failed[^\n]*)")


def _toml_str(value: str) -> str:
    # JSON string escapes are a subset of TOML basic string escapes.
    return json.dumps(value, ensure_ascii=False)


def render_config(arm: Arm, cfg: AgentConfig, cwd: Path) -> str:
    """Render one arm's ``config.toml``. Identical model, effort and tier in every arm."""
    if cfg.service_tier == "fast":
        raise ValueError("the fast service tier is not allowed in this benchmark")
    lines = [
        "# Rendered by archivist_bench. Do not edit; re-render instead.",
        f"model = {_toml_str(cfg.model)}",
        f"model_reasoning_effort = {_toml_str(cfg.effort)}",
        'approval_policy = "never"',
        'sandbox_mode = "read-only"',
        f"web_search = {_toml_str(WEB_SEARCH_MODE[arm])}",
        "",
        f"[projects.{_toml_str(str(cwd))}]",
        'trust_level = "trusted"',
        "",
        "[features]",
    ]
    lines += [f"{name} = false" for name in DISABLED_FEATURES]
    if arm in ("archivist", "both"):
        lines += [
            "",
            f"[mcp_servers.{ARCHIVIST_SERVER}]",
            'command = "archivist"',
            'args = ["mcp", "serve"]',
        ]
        for tool in ARCHIVIST_TOOLS:
            lines += [
                "",
                f"[mcp_servers.{ARCHIVIST_SERVER}.tools.{tool}]",
                'approval_mode = "approve"',
            ]
    return "\n".join(lines) + "\n"


def command(cfg: AgentConfig, prompt: str, cwd: Path, last_message: Path) -> list[str]:
    """``codex exec`` argv. The prompt is the final positional argument."""
    argv = [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-s",
        "read-only",
        "-C",
        str(cwd),
        "-o",
        str(last_message),
        "-m",
        cfg.model,
        "-c",
        f"model_reasoning_effort={_toml_str(cfg.effort)}",
        "-c",
        'approval_policy="never"',
    ]
    if cfg.service_tier != "default":
        argv += ["-c", f"service_tier={_toml_str(cfg.service_tier)}"]
    return [*argv, prompt]


def env_extras(home: Path, cfg: AgentConfig, parent: dict[str, str]) -> dict[str, str]:
    extras = {"CODEX_HOME": str(home)}
    if cfg.auth_mode == "api_key":
        key = parent.get("CODEX_API_KEY")
        if not key:
            raise RuntimeError("auth mode api_key needs CODEX_API_KEY in the environment")
        extras["CODEX_API_KEY"] = key
    elif cfg.auth_mode != "chatgpt":
        raise ValueError(f"unknown Codex auth mode {cfg.auth_mode!r}")
    return extras


def prepare_home(home: Path, arm: Arm, cfg: AgentConfig, cwd: Path, auth_source: Path) -> None:
    """Create a fresh ``CODEX_HOME`` with the arm config and an auth symlink (refresh writes
    through to the operator's login)."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(render_config(arm, cfg, cwd), encoding="utf-8")
    if cfg.auth_mode == "chatgpt":
        link = home / "auth.json"
        if not auth_source.exists():
            raise RuntimeError(f"Codex login not found at {auth_source}")
        if not link.exists():
            link.symlink_to(auth_source)


def find_rollout(home: Path) -> Path | None:
    files = sorted((home / "sessions").glob("**/rollout-*.jsonl"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def _json_lines(lines: Iterable[str]) -> Iterable[dict[str, Any]]:
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def _status_word(status: Any) -> str:
    if isinstance(status, str):
        return status.lower()
    if isinstance(status, dict):
        for key in ("state", "status", "type"):
            if isinstance(status.get(key), str):
                return str(status[key]).lower()
        if len(status) == 1:
            return str(next(iter(status))).lower()
    return "unknown"


def _apply_mcp_event(payload: dict[str, Any], servers: dict[str, str]) -> None:
    """Startup events, if a Codex build emits them (0.160.0 exec persists none)."""
    kind = payload.get("type")
    if kind == "mcp_startup_update" and isinstance(payload.get("server"), str):
        word = _status_word(payload.get("status"))
        servers[payload["server"]] = "connected" if word == "ready" else word
    elif kind == "mcp_startup_complete":
        for name in payload.get("ready") or []:
            servers[str(name)] = "connected"
        for item in payload.get("failed") or []:
            name = item.get("server") if isinstance(item, dict) else item
            servers[str(name)] = "failed"
        for name in payload.get("cancelled") or []:
            servers[str(name)] = "cancelled"


def _window(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    return {k: raw.get(k) for k in ("used_percent", "window_minutes", "resets_at")}


def _balance(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def plan_window(
    rate_limits: dict[str, Any], previous: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The plan window a rollout ``token_count`` reports: primary and secondary windows (used
    percent, window minutes), credits (``has_credits``; after a plan limit Codex draws on them),
    ``plan_type`` and ``rate_limit_reached_type``. The credit balance keeps the first, the
    minimum and the last value of the run (``balance_first``, ``balance_min``, ``balance``), and
    a ``rate_limit_reached_type`` seen once stays (``rate_limit_reached_seen``)."""
    credits = rate_limits.get("credits")
    prior = (previous or {}).get("credits") or {}
    balance = credits.get("balance") if isinstance(credits, dict) else None
    first = prior.get("balance_first", balance)
    lowest = prior.get("balance_min", balance)
    now, low = _balance(balance), _balance(lowest)
    if now is not None and (low is None or now < low):
        lowest = balance
    if first is None:
        first = balance
    reached = rate_limits.get("rate_limit_reached_type")
    seen = (previous or {}).get("rate_limit_reached_seen") or reached
    return {
        "source": "codex_rollout_rate_limits",
        "limit_id": rate_limits.get("limit_id"),
        "plan_type": rate_limits.get("plan_type"),
        "primary": _window(rate_limits.get("primary")),
        "secondary": _window(rate_limits.get("secondary")),
        "credits": {
            **{k: credits.get(k) for k in ("has_credits", "unlimited", "balance")},
            "balance_first": first,
            "balance_min": lowest,
        }
        if isinstance(credits, dict)
        else None,
        "rate_limit_reached_type": reached,
        "rate_limit_reached_seen": seen,
    }


def _output_text(payload: dict[str, Any]) -> str:
    return flatten_text(payload.get("output"))


def parse_rollout(lines: Iterable[str]) -> Extraction:
    """Extract usage and events from one rollout JSONL."""
    ex = Extraction()
    tools: Counter[str] = Counter()
    web: Counter[str] = Counter()
    last_total: dict[str, Any] | None = None
    last_message = ""
    call_names: dict[str, str] = {}
    requested: dict[str, set[str]] = {"shell": set(), "file_change": set()}
    for rec in _json_lines(lines):
        rtype = rec.get("type")
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        assert isinstance(payload, dict)
        if rtype == "session_meta":
            ex.cli_version = payload.get("cli_version") or ex.cli_version
        elif rtype == "turn_context":
            ex.model = payload.get("model") or ex.model
            ex.effort = payload.get("effort") or ex.effort
        elif rtype == "token_usage_record":
            ex.model_calls += 1
            usage = payload.get("usage") or {}
            ex.per_call_usage.append({k: int(v or 0) for k, v in usage.items()})
        elif rtype == "compacted":
            ex.compactions += 1
        elif rtype == "response_item":
            ptype = payload.get("type")
            if ptype in ("custom_tool_call", "function_call", "local_shell_call"):
                call_id = str(payload.get("call_id"))
                name = str(payload.get("name") or ptype)
                call_names[call_id] = name
                if name in SHELL_CALL_NAMES or ptype == "local_shell_call":
                    requested["shell"].add(call_id)
                elif name in PATCH_CALL_NAMES:
                    requested["file_change"].add(call_id)
            if ptype in ("custom_tool_call_output", "function_call_output"):
                text = _output_text(payload)
                call_id = str(payload.get("call_id"))
                for match in CLIENT_TRUNCATION.finditer(text):
                    ex.truncated_calls.append(
                        {
                            "call_id": call_id,
                            "tool": call_names.get(call_id),
                            "original_tokens": int(match.group(1)),
                        }
                    )
                for server in {
                    m.group(1)
                    for m in CATALOG_TOOL.finditer(text)
                    if not is_resource_builtin(m.group(1), m.group(2))
                }:
                    ex.mcp_evidence.setdefault(server, "tool_catalog")
            elif ptype == "message" and payload.get("role") == "assistant":
                parts = payload.get("content") or []
                last_message = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))
        elif rtype == "event_msg":
            ptype = payload.get("type")
            if ptype == "token_count":
                if isinstance(payload.get("info"), dict):
                    total = payload["info"].get("total_token_usage")
                    if isinstance(total, dict):
                        last_total = total
                if isinstance(payload.get("rate_limits"), dict):
                    ex.plan_window = plan_window(payload["rate_limits"], ex.plan_window)
            elif ptype == "thread_settings_applied":
                settings = payload.get("thread_settings") or {}
                ex.service_tier = settings.get("service_tier") or ex.service_tier
            elif ptype in ("mcp_startup_update", "mcp_startup_complete"):
                _apply_mcp_event(payload, ex.mcp_servers)
            elif ptype == "task_complete":
                if payload.get("last_agent_message"):
                    last_message = str(payload["last_agent_message"])
                err = payload.get("error")
                if isinstance(err, dict):
                    code = str(err.get("codex_error_info") or "error")
                    ex.errors.append(f"task_complete:{code}")
                    reason = error_reason(code, str(err.get("message") or ""))
                    if reason:
                        ex.infra_reason = merge_infra(ex.infra_reason, reason)
                    else:
                        ex.incomplete_reason = f"error:{code}"
            elif ptype in ("error", "stream_error"):
                message = str(payload.get("message") or "")
                code = str(payload.get("codex_error_info") or "")
                reason = error_reason(code, message)
                ex.errors.append(f"{ptype}:{(code or message)[:200]}")
                if reason:
                    ex.infra_reason = merge_infra(ex.infra_reason, reason)
            elif ptype == "item_completed":
                _apply_item(payload.get("item") or {}, ex, tools, web)
    # A requested call and its completed item describe the same call, and an interrupted call
    # never writes the item: count the larger of the two (call ids differ between them).
    for name, ids in requested.items():
        if ids:
            tools[name] = max(tools[name], len(ids))
    ex.tool_calls = dict(sorted(tools.items()))
    ex.web_actions = dict(sorted(web.items()))
    ex.truncations = len(ex.truncated_calls)
    for server, basis in ex.mcp_evidence.items():
        if server not in ex.mcp_servers:
            ex.mcp_servers[server] = "connected"
            ex.mcp_evidence[server] = basis
    ex.answer = last_message
    if last_total is None:
        ex.incomplete_reason = ex.incomplete_reason or "no_usage"
    else:
        inp = int(last_total.get("input_tokens") or 0)
        cached = int(last_total.get("cached_input_tokens") or 0)
        out = int(last_total.get("output_tokens") or 0)
        ex.usage = Usage(
            total=int(last_total.get("total_tokens") or inp + out),
            input=inp,
            cached_input=cached,
            cache_write_input=int(last_total.get("cache_write_input_tokens") or 0),
            uncached_input=inp - cached,
            output=out,
            reasoning=int(last_total.get("reasoning_output_tokens") or 0),
        )
    return ex


def _apply_item(
    item: dict[str, Any], ex: Extraction, tools: Counter[str], web: Counter[str]
) -> None:
    itype = item.get("type")
    if itype == "McpToolCall":
        server = str(item.get("server"))
        tools[f"mcp:{server}.{item.get('tool')}"] += 1
        result_text = flatten_text(item.get("result"))
        # The pseudo server seen only through its resource built ins is not a loaded server.
        if item.get("status") == "completed" and not is_resource_builtin(
            server, str(item.get("tool"))
        ):
            ex.mcp_evidence.setdefault(server, "tool_call")
        if any(marker in result_text for marker in PAGED_MARKERS):
            ex.paged_results += 1
        result = item.get("result")
        failed = item.get("status") == "failed" or (
            isinstance(result, dict) and bool(result.get("isError") or result.get("is_error"))
        )
        if server == ARCHIVIST_SERVER and failed:
            if ARCHIVIST_QUOTA.search(result_text):
                ex.infra_reason = merge_infra(ex.infra_reason, "archivist_quota")
            elif ARCHIVIST_429.search(result_text):
                ex.infra_reason = merge_infra(ex.infra_reason, "archivist_429")
    elif itype == "Extension":
        kind = str(item.get("kind"))
        if kind == "web.search":
            action = item.get("action") or {}
            name = str(action.get("type") or "other")
            tools["web.search"] += 1
            web[name] += 1
            for url in [action.get("url")] + [
                r.get("url") for r in item.get("results") or [] if isinstance(r, dict)
            ]:
                if isinstance(url, str):
                    ex.web_urls.append(url)
            if isinstance(item.get("query"), str):
                ex.web_urls.append(str(item["query"]))
        else:
            tools[f"ext:{kind}"] += 1
    elif itype == "CommandExecution":
        tools["shell"] += 1
    elif itype == "FileChange":
        tools["file_change"] += 1
    elif itype == "ContextCompaction":
        pass  # counted from the top level ``compacted`` record


def parse_exec_stdout(lines: Iterable[str], ex: Extraction) -> None:
    """Fold ``codex exec --json`` stdout into an extraction: MCP startup and failure events."""
    for obj in _json_lines(lines):
        kind = str(obj.get("type", ""))
        if kind in ("mcp_startup_update", "mcp_startup_complete"):
            _apply_mcp_event(obj, ex.mcp_servers)
        msg = obj.get("msg")
        if isinstance(msg, dict) and msg.get("type") in (
            "mcp_startup_update",
            "mcp_startup_complete",
        ):
            _apply_mcp_event(msg, ex.mcp_servers)
        if kind in ("turn.failed", "error"):
            err = obj.get("error") if isinstance(obj.get("error"), dict) else obj
            assert isinstance(err, dict)
            message = str(err.get("message", ""))
            code = str(err.get("codex_error_info") or obj.get("codex_error_info") or "")
            ex.errors.append(f"{kind}:{message[:200]}")
            reason = error_reason(code, message)
            if reason:
                ex.infra_reason = merge_infra(ex.infra_reason, reason)
            else:  # e.g. context length exceeded: the run did not complete
                ex.incomplete_reason = ex.incomplete_reason or f"{kind}: {message[:120]}"


RATE_LIMIT_TEXT = re.compile(r"rate limit reached|rate limit exceeded|rate_limit", re.IGNORECASE)
USAGE_LIMIT_TEXT = re.compile(r"usage limit|usage_limit", re.IGNORECASE)
OVERLOAD_TEXT = re.compile(r"capacity|overloaded", re.IGNORECASE)


def error_reason(code: str, message: str) -> str | None:
    """The infra reason of a Codex error: the structured ``codex_error_info`` first, then the
    recognized text (plan limits stop the batch; overload is transient); None otherwise."""
    if code in INFRA_ERROR_CODES:
        return code
    if USAGE_LIMIT_TEXT.search(message):
        return "usage_limit_exceeded"
    if RATE_LIMIT_TEXT.search(message):
        return "rate_limit_exceeded"
    if OVERLOAD_TEXT.search(message):
        return "server_overloaded"
    return None


def parse_stderr(text: str, ex: Extraction) -> None:
    for match in MCP_STDERR.finditer(text):
        ex.mcp_servers[match.group("server")] = "failed"
        ex.errors.append(f"stderr:{match.group(0)[:200]}")


def isolation_violations(arm: Arm, ex: Extraction) -> list[str]:
    """Reasons a run broke its arm's isolation (empty when clean)."""
    problems: list[str] = []
    servers = ex.mcp_servers
    if arm in ("archivist", "both") and servers.get(ARCHIVIST_SERVER) != "connected":
        problems.append(f"archivist MCP not connected ({servers.get(ARCHIVIST_SERVER, 'unseen')})")
    for name in servers:
        if name != ARCHIVIST_SERVER or arm == "web":
            problems.append(f"MCP server {name} loaded outside the arm allowlist")
    for tool in ex.tool_calls:
        resource = tool in {f"mcp:{CODEX_PSEUDO_SERVER}.{t}" for t in CODEX_RESOURCE_TOOLS}
        allowed = tool.startswith(f"mcp:{ARCHIVIST_SERVER}.") or resource
        if tool.startswith("mcp:") and (arm == "web" or not allowed):
            problems.append(f"tool {tool} outside the arm allowlist")
        if tool == "web.search" and arm == "archivist":
            problems.append("web search used in the archivist arm")
        if tool in ("shell", "file_change") or tool.startswith("ext:"):
            problems.append(f"tool {tool} outside the arm allowlist")
    return sorted(set(problems))


def _stdio_exchange(
    argv: list[str], env: dict[str, str], cwd: Path, timeout_s: float
) -> dict[str, Any]:
    """MCP stdio handshake: initialize, then tools/list; returns tool names or the error."""
    proc = subprocess.Popen(
        argv,
        env=env,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
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
            obj = json.loads(lines.get(timeout=timeout_s))
            if isinstance(obj, dict) and obj.get("id") == request_id:
                return obj

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "archivist_bench_preflight", "version": "1"},
                },
            }
        )
        init = wait_for(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = wait_for(2)
        tools = [t.get("name") for t in (listed.get("result") or {}).get("tools", [])]
        server = (init.get("result") or {}).get("serverInfo") or {}
        return {"ok": bool(tools), "tools": tools, "server": server}
    except (queue.Empty, json.JSONDecodeError, OSError, ValueError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        proc.kill()
        proc.wait()


def mcp_preflight(
    home: Path, env: dict[str, str], cwd: Path, timeout_s: float = 30.0
) -> dict[str, Any]:
    """Prove, just before a run and without a model call, which MCP servers the run's Codex
    home configures and that each one answers a stdio handshake in the run's environment.

    Codex 0.160.0 ``exec`` persists no MCP startup event, so this plus in-run evidence (the
    session's tool catalog or an MCP tool call) stands in for an init event.
    """
    listed = subprocess.run(
        ["codex", "mcp", "list", "--json"],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    try:
        servers = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError:
        return {"configured": None, "error": f"codex mcp list: {listed.stderr[:300]}"}
    configured = [s["name"] for s in servers if isinstance(s, dict) and s.get("enabled")]
    result: dict[str, Any] = {"configured": configured, "handshake": {}}
    for entry in servers:
        transport = entry.get("transport") or {}
        if entry.get("enabled") and transport.get("type") == "stdio":
            argv = [str(transport["command"]), *map(str, transport.get("args") or [])]
            result["handshake"][entry["name"]] = _stdio_exchange(argv, env, cwd, timeout_s)
    return result


def apply_preflight(preflight: dict[str, Any], ex: Extraction) -> None:
    """Fold a preflight into an extraction: configured servers count as loaded when their
    handshake passed and nothing in the run reported them failed."""
    configured = preflight.get("configured")
    if configured is None:
        ex.errors.append("mcp preflight failed: " + str(preflight.get("error")))
        return
    for name in configured:
        shake = (preflight.get("handshake") or {}).get(name) or {}
        if name in ex.mcp_servers:
            continue
        if shake.get("ok"):
            ex.mcp_servers[name] = "connected"
            ex.mcp_evidence[name] = "preflight"
        else:
            ex.mcp_servers[name] = "failed"
            ex.errors.append(f"mcp preflight {name}: {shake.get('error', 'no tools')}")
