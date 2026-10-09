"""Claude Code arms: command line, MCP config, environment and stream-json extraction.

Facts checked against Claude Code 2.1.289 (``claude --help``) and its headless docs: stream-json
needs ``--verbose``; usage is deduplicated by ``message.id`` (one API response can span several
stream events and per step ``output_tokens`` is a placeholder), so session totals come from the
final ``result`` event (``usage``, ``modelUsage``, ``total_cost_usd``, ``num_turns``); compaction
is a ``system`` event with ``subtype == "compact_boundary"``; ``--bare`` reads only
``ANTHROPIC_API_KEY`` or an apiKeyHelper, so subscription runs use ``--restricted`` instead.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .model import AgentConfig, Arm, Extraction, Usage, flatten_text, merge_infra

ARCHIVIST_SERVER = "archivist"
WEB_TOOLS: tuple[str, ...] = ("WebSearch", "WebFetch")
# Tools that neither fetch information nor run code; allowed in every arm identically.
NEUTRAL_TOOLS = frozenset({"TodoWrite", "ExitPlanMode", "EnterPlanMode"})
MCP_RESOURCE_TOOLS = frozenset({"ListMcpResourcesTool", "ReadMcpResourceTool"})
# Claude Code client side cuts: a result over MAX_MCP_OUTPUT_TOKENS is refused or truncated,
# and a large result is saved to a file with a pointer message instead of the content.
CLIENT_TRUNCATION = re.compile(
    r"exceeds maximum allowed tokens|MAX_MCP_OUTPUT_TOKENS|Output too large|"
    r"saved to (?:a )?file|output (?:has been |was )?truncated|\[truncated",
    re.IGNORECASE,
)
TOKEN_COUNT = re.compile(r"\((\d[\d,]*) tokens\)|(\d[\d,]*) tokens")
PAGED_MARKERS = ('"truncated": true', '"truncated":true')
INFRA_TEXT = re.compile(
    r"rate limit|overloaded|usage limit|API Error: 5\d\d|API Error: 429", re.IGNORECASE
)
# Plan usage limits (a subscription 429 is the plan limit): the batch stops instead of
# rerunning (pre registration section 11, D2). Overload and 5xx stay transient.
PLAN_LIMIT_TEXT = re.compile(
    r"usage limit|rate limit|limit reached|hit your limit|API Error: 429", re.IGNORECASE
)
TRANSIENT_STATUS = frozenset({500, 502, 503, 529})
# Explicit quota or rate limit markers only, on tool results flagged ``is_error``. ``CLI_QUOTA``
# is the Archivist monthly quota (the batch stops); the rest is a burst limit (rerun).
ARCHIVIST_429 = re.compile(
    r"CLI_QUOTA|RATE_LIMITED|Too Many Requests|HTTP 429|status(?: code)?:? 429", re.IGNORECASE
)
ARCHIVIST_QUOTA = re.compile(r"CLI_QUOTA", re.IGNORECASE)


def plan_window(info: dict[str, Any]) -> dict[str, Any]:
    """The plan window one ``rate_limit_event.rate_limit_info`` reports (shared by the agent
    runs, the judge and the Claude probe): status, the ``unifiedWindows`` utilization (a
    fraction, 0.23 is 23%) and reset times, ``isUsingOverage``, ``overageStatus`` and
    ``overageDisabledReason``."""
    windows = info.get("unifiedWindows")
    return {
        "source": "claude_rate_limit_event",
        "status": info.get("status"),
        "rate_limit_type": info.get("rateLimitType"),
        "windows": {k: (v or {}).get("utilization") for k, v in windows.items()}
        if isinstance(windows, dict)
        else {},
        "window_resets": {k: (v or {}).get("resetsAt") for k, v in windows.items()}
        if isinstance(windows, dict)
        else {},
        "resets_at": info.get("resetsAt"),
        "is_using_overage": info.get("isUsingOverage"),
        "overage_status": info.get("overageStatus"),
        "overage_disabled_reason": info.get("overageDisabledReason"),
    }


def builtin_tools(arm: Arm) -> list[str]:
    return list(WEB_TOOLS) if arm in ("web", "both") else []


def allowed_tool_rules(arm: Arm) -> list[str]:
    rules = builtin_tools(arm)
    if arm in ("archivist", "both"):
        rules.append(f"mcp__{ARCHIVIST_SERVER}")
    return rules


def mcp_config(arm: Arm) -> dict[str, Any]:
    servers: dict[str, Any] = {}
    if arm in ("archivist", "both"):
        servers[ARCHIVIST_SERVER] = {
            "type": "stdio",
            "command": "archivist",
            "args": ["mcp", "serve"],
        }
    return {"mcpServers": servers}


def command(cfg: AgentConfig, arm: Arm, mcp_config_path: str) -> list[str]:
    """``claude -p`` argv. The prompt goes on stdin so no variadic flag can swallow it."""
    if cfg.max_turns is None:
        raise ValueError("Claude Code runs need max_turns")
    argv = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        cfg.model,
        "--effort",
        cfg.effort,
        "--max-turns",
        str(cfg.max_turns),
        "--strict-mcp-config",
        "--mcp-config",
        mcp_config_path,
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--tools",
        ",".join(builtin_tools(arm)),
        "--allowedTools",
        ",".join(allowed_tool_rules(arm)),
    ]
    if cfg.auth_mode == "api_key":
        argv.append("--bare")
    elif cfg.auth_mode == "subscription":
        argv.append("--restricted")
    else:
        raise ValueError(f"unknown Claude Code auth mode {cfg.auth_mode!r}")
    return argv


def resolve_config_dir(value: str | os.PathLike[str]) -> Path:
    """The chosen Claude account directory (``--claude-config-dir``) as an absolute resolved
    path; it must name an existing directory (ValueError naming the problem otherwise)."""
    text = os.fspath(value)
    if not text.strip():
        raise ValueError("--claude-config-dir is empty")
    path = Path(text).expanduser()
    if not path.exists():
        raise ValueError(f"--claude-config-dir {text} does not exist")
    if not path.is_dir():
        raise ValueError(f"--claude-config-dir {text} is not a directory")
    return path.resolve()


def account_extras(config_dir: str | os.PathLike[str] | None) -> dict[str, str]:
    """The one source of a Claude child's account (harness 1.5.1): ``CLAUDE_CONFIG_DIR`` set
    to the explicitly chosen directory. Agent runs, judge pass A calls and the plan gate's Claude
    probe all build from it, so the probe reads the account the calls use. Never read from the
    environment; no choice is an error (fail closed), never a fallback to ``~/.claude``."""
    if config_dir is None or not os.fspath(config_dir).strip():
        raise ValueError("no Claude account chosen: pass --claude-config-dir")
    return {"CLAUDE_CONFIG_DIR": os.fspath(config_dir)}


def env_extras(
    cfg: AgentConfig,
    parent: dict[str, str],
    config_dir: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    extras = {
        # Load MCP tool definitions up front, like Codex, instead of deferring them.
        "ENABLE_TOOL_SEARCH": "false",
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
        "DISABLE_AUTOUPDATER": "1",
    }
    if config_dir is not None:
        extras |= account_extras(config_dir)
    if cfg.auth_mode == "api_key":
        key = parent.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "auth mode api_key needs an existing ANTHROPIC_API_KEY; no new key is created"
            )
        extras["ANTHROPIC_API_KEY"] = key
    return extras


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


def _sum_model_usage(model_usage: dict[str, Any]) -> Usage | None:
    if not model_usage:
        return None
    inp = cached = write = out = thinking = 0
    saw_thinking = False
    for entry in model_usage.values():
        if not isinstance(entry, dict):
            continue
        fresh = int(entry.get("inputTokens") or 0)
        read = int(entry.get("cacheReadInputTokens") or 0)
        created = int(entry.get("cacheCreationInputTokens") or 0)
        inp += fresh + read + created
        cached += read
        write += created
        out += int(entry.get("outputTokens") or 0)
        if entry.get("thinkingTokens") is not None:
            saw_thinking = True
            thinking += int(entry.get("thinkingTokens") or 0)
    return Usage(
        total=inp + out,
        input=inp,
        cached_input=cached,
        cache_write_input=write,
        uncached_input=inp - cached,
        output=out,
        reasoning=thinking if saw_thinking else None,
    )


USAGE_KEYS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)


def _thinking(usage: dict[str, Any]) -> int | None:
    details = usage.get("output_tokens_details")
    if isinstance(details, dict) and details.get("thinking_tokens") is not None:
        return int(details["thinking_tokens"] or 0)
    return None


def _server_searches(usage: dict[str, Any]) -> int:
    tool_use = usage.get("server_tool_use")
    return int(tool_use.get("web_search_requests") or 0) if isinstance(tool_use, dict) else 0


def _usage_from_result(usage: dict[str, Any]) -> Usage:
    fresh = int(usage.get("input_tokens") or 0)
    read = int(usage.get("cache_read_input_tokens") or 0)
    created = int(usage.get("cache_creation_input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    inp = fresh + read + created
    return Usage(
        total=inp + out,
        input=inp,
        cached_input=read,
        cache_write_input=created,
        uncached_input=inp - read,
        output=out,
        reasoning=_thinking(usage),
    )


def parse_stream(lines: Iterable[str]) -> Extraction:
    """Extract usage and events from one ``--output-format stream-json`` transcript."""
    ex = Extraction()
    message_ids: set[str] = set()
    tool_names: dict[str, str] = {}
    tools: Counter[str] = Counter()
    web: Counter[str] = Counter()
    result: dict[str, Any] | None = None
    last_text = ""
    step_usage: dict[str, dict[str, Any]] = {}
    rejected = False
    overage = False
    for ev in _json_lines(lines):
        etype = ev.get("type")
        if etype == "rate_limit_event" and isinstance(ev.get("rate_limit_info"), dict):
            info = ev["rate_limit_info"]
            ex.plan_window = plan_window(info)
            rejected = rejected or info.get("status") == "rejected"
            overage = overage or info.get("isUsingOverage") is True
            # Overage use and a rejection are sticky: any event of the run keeps the flag (a
            # rejection stops the batch whatever the result subtype, pre registration D2).
            ex.plan_window["is_using_overage"] = overage or ex.plan_window["is_using_overage"]
            ex.plan_window["rejected_seen"] = rejected
        elif etype == "system" and ev.get("subtype") == "init":
            ex.model = ev.get("model") or ex.model
            ex.cli_version = ev.get("claude_code_version") or ex.cli_version
            ex.api_key_source = ev.get("apiKeySource")
            ex.init_tools = [str(t) for t in ev.get("tools") or []]
            for server in ev.get("mcp_servers") or []:
                if isinstance(server, dict) and server.get("name"):
                    ex.mcp_servers[str(server["name"])] = str(server.get("status", "unknown"))
        elif etype == "system" and ev.get("subtype") == "compact_boundary":
            ex.compactions += 1
        elif etype == "assistant":
            msg = ev.get("message") or {}
            mid = msg.get("id")
            if isinstance(mid, str):
                message_ids.add(mid)
                if isinstance(msg.get("usage"), dict):
                    step_usage[mid] = msg["usage"]  # last event of a message wins
            for part in msg.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and part.get("text"):
                    last_text = str(part["text"])
                if part.get("type") in ("tool_use", "server_tool_use"):
                    pid = str(part.get("id"))
                    if pid in tool_names:
                        continue
                    name = str(part.get("name"))
                    tool_names[pid] = name
                    tools[name] += 1
                    inp = part.get("input") or {}
                    if name == "WebSearch":
                        web["search"] += 1
                        if isinstance(inp.get("query"), str):
                            ex.web_urls.append(inp["query"])
                    elif name == "WebFetch":
                        web["fetch"] += 1
                        if isinstance(inp.get("url"), str):
                            ex.web_urls.append(inp["url"])
            if msg.get("error") or ev.get("error"):
                ex.errors.append(f"assistant_error:{msg.get('error') or ev.get('error')}")
        elif etype == "user":
            msg = ev.get("message") or {}
            for part in msg.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    text = flatten_text(part.get("content"))
                    call_id = str(part.get("tool_use_id"))
                    tool = tool_names.get(call_id, "")
                    if CLIENT_TRUNCATION.search(text):
                        count = TOKEN_COUNT.search(text)
                        ex.truncated_calls.append(
                            {
                                "call_id": call_id,
                                "tool": tool or None,
                                "original_tokens": int(
                                    (count.group(1) or count.group(2)).replace(",", "")
                                )
                                if count
                                else None,
                            }
                        )
                    if any(marker in text for marker in PAGED_MARKERS):
                        ex.paged_results += 1
                    if part.get("is_error") and tool.startswith(f"mcp__{ARCHIVIST_SERVER}__"):
                        if ARCHIVIST_QUOTA.search(text):
                            ex.infra_reason = merge_infra(ex.infra_reason, "archivist_quota")
                        elif ARCHIVIST_429.search(text):
                            ex.infra_reason = merge_infra(ex.infra_reason, "archivist_429")
        elif etype == "result":
            result = ev
    ex.model_calls = len(message_ids)
    ex.truncations = len(ex.truncated_calls)
    ex.tool_calls = dict(sorted(tools.items()))
    ex.web_actions = dict(sorted(web.items()))
    if result is None:
        ex.answer = last_text
        ex.incomplete_reason = "no_result"
        if rejected:
            ex.infra_reason = merge_infra(ex.infra_reason, "usage_limit:rate_limit_event")
        if step_usage:
            # Partial fallback: per message usage, deduplicated by message.id. Input, cache read
            # and cache creation are real; per step output_tokens is a placeholder.
            summed: dict[str, Any] = {
                key: sum(int(u.get(key) or 0) for u in step_usage.values()) for key in USAGE_KEYS
            }
            thinking = [t for t in map(_thinking, step_usage.values()) if t is not None]
            if thinking:
                summed["output_tokens_details"] = {"thinking_tokens": sum(thinking)}
            ex.usage = _usage_from_result(summed)
            searches = sum(_server_searches(u) for u in step_usage.values())
            if searches:
                web["server_searches"] = searches
                ex.web_actions = dict(sorted(web.items()))
            ex.errors.append("partial usage from assistant messages; output is a placeholder")
        return ex
    ex.answer = str(result.get("result") or last_text)
    ex.num_turns = result.get("num_turns")
    ex.client_cost_usd = result.get("total_cost_usd")
    ex.model_usage = dict(result.get("modelUsage") or {})
    result_usage = result.get("usage")
    summed_usage = _sum_model_usage(ex.model_usage)
    searches = 0
    if summed_usage is not None:
        ex.usage = summed_usage
        searches = sum(
            int(v.get("webSearchRequests") or 0)
            for v in ex.model_usage.values()
            if isinstance(v, dict)
        )
    elif isinstance(result_usage, dict) and any(k in result_usage for k in USAGE_KEYS):
        ex.usage = _usage_from_result(result_usage)
        searches = _server_searches(result_usage)
    else:  # no accounting at all: never let missing usage look like zero tokens
        ex.usage = None
        ex.incomplete_reason = "no_usage"
    if searches:
        web["server_searches"] = searches
        ex.web_actions = dict(sorted(web.items()))
    subtype = str(result.get("subtype", ""))
    if subtype == "error_max_turns":
        ex.turn_limit = True
    elif subtype != "success" or result.get("is_error"):
        status = result.get("api_error_status")
        text = str(result.get("result") or "") + " ".join(map(str, result.get("errors") or []))
        transient = status in TRANSIENT_STATUS
        if status == 429 or rejected or (not transient and PLAN_LIMIT_TEXT.search(text)):
            ex.infra_reason = merge_infra(ex.infra_reason, f"usage_limit:{status or 'text'}")
        elif transient or INFRA_TEXT.search(text):
            ex.infra_reason = merge_infra(ex.infra_reason, f"api_error:{status or 'text'}")
        else:
            ex.incomplete_reason = f"result:{subtype or 'error'}"
    return ex


def isolation_violations(arm: Arm, ex: Extraction) -> list[str]:
    problems: list[str] = []
    if ex.init_tools is None:
        problems.append("no system/init event")
    else:
        allowed = set(builtin_tools(arm)) | NEUTRAL_TOOLS
        for tool in ex.init_tools:
            is_archivist = tool.startswith(f"mcp__{ARCHIVIST_SERVER}__")
            if arm in ("archivist", "both") and (is_archivist or tool in MCP_RESOURCE_TOOLS):
                continue
            if tool not in allowed:
                problems.append(f"tool {tool} outside the arm allowlist")
    used_allowed = set(builtin_tools(arm)) | NEUTRAL_TOOLS
    for tool in ex.tool_calls:
        is_archivist = tool.startswith(f"mcp__{ARCHIVIST_SERVER}__")
        if arm in ("archivist", "both") and (is_archivist or tool in MCP_RESOURCE_TOOLS):
            continue
        if tool not in used_allowed:
            problems.append(f"tool {tool} used outside the arm allowlist")
    status = ex.mcp_servers.get(ARCHIVIST_SERVER)
    if arm in ("archivist", "both") and status != "connected":
        problems.append(f"archivist MCP not connected ({status or 'unseen'})")
    for name in ex.mcp_servers:
        if name != ARCHIVIST_SERVER or arm == "web":
            problems.append(f"MCP server {name} loaded outside the arm allowlist")
    return sorted(set(problems))
