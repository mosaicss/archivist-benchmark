"""Hermetic tests for the Archivist benchmark harness (no agent, network or quota)."""

from __future__ import annotations

import copy
import csv
import io
import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Collection, Generator, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]  # the repository root
BENCH = ROOT  # the archivist_bench package sits at the root
HARNESS_README = ROOT / "docs" / "HARNESS.md"
RUNBOOK = ROOT / "RUNBOOK.md"  # the step by step command sequences of each amendment


def runbook_section(heading: str) -> str:
    """One numbered section of RUNBOOK.md, from its heading to the next one."""
    _, _, section = RUNBOOK.read_text().partition(heading)
    assert section, heading
    return section.split("\n## ", 1)[0]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "archivist_bench"
QUESTION_SET = ROOT / "preregistration" / "questions.json"
# Built at runtime so no upstream host literal sits in a committed file.
UPSTREAM_URL = "https://www." + "sec" + ".gov/Archives/edgar/data/320193/x.htm"

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def bench() -> Generator[dict[str, ModuleType], None, None]:
    sys.path.insert(0, str(BENCH))
    import archivist_bench.__main__ as cli
    from archivist_bench import (
        claude_code,
        codex,
        cost,
        crosscheck,
        grounding,
        judge,
        model,
        precheck,
        probe,
        questions,
        redact,
        report,
        runner,
        stats,
    )

    yield {
        "cli": cli,
        "claude_code": claude_code,
        "codex": codex,
        "cost": cost,
        "crosscheck": crosscheck,
        "grounding": grounding,
        "judge": judge,
        "model": model,
        "questions": questions,
        "probe": probe,
        "redact": redact,
        "report": report,
        "runner": runner,
        "stats": stats,
        "precheck": precheck,
    }
    sys.path.remove(str(BENCH))


# A healthy ``account/rateLimits/read`` result (shape of Codex app-server 0.160.0, ids dropped).
OK_PROBE: dict[str, Any] = {
    "ok": True,
    "result": {
        "ordinaryUsageAllowed": True,
        "rateLimits": {
            "limitId": "codex",
            "primary": {"usedPercent": 5, "windowDurationMins": 10080, "resetsAt": 1791609630},
            "secondary": None,
            "credits": {"hasCredits": True, "unlimited": False, "balance": "62500"},
            "spendControlReached": False,
            "planType": "pro",
            "rateLimitReachedType": None,
        },
    },
}


def claude_probe_stream(
    utilization: float = 0.3,
    overage: str = "rejected",
    reason: str | None = "org_level_disabled",
    api_key_source: str = "none",
    status: str = "allowed",
    resets_at: int = 4102444800,  # 2100-01-01: never passed in a test
) -> list[str]:
    """A healthy Claude probe stream (shape of Claude Code 2.1.289 stream-json)."""
    info: dict[str, Any] = {
        "status": status,
        "resetsAt": resets_at,
        "rateLimitType": "five_hour",
        "overageStatus": overage,
        "isUsingOverage": False,
        "unifiedWindows": {
            "five_hour": {"utilization": utilization, "resetsAt": resets_at},
            "seven_day": {"utilization": 0.5, "resetsAt": resets_at},
        },
    }
    if reason is not None:
        info["overageDisabledReason"] = reason
    events = [
        {"type": "system", "subtype": "init", "apiKeySource": api_key_source, "tools": []},
        {"type": "rate_limit_event", "rate_limit_info": info},
        {"type": "result", "subtype": "success", "result": "OK", "usage": {"output_tokens": 2}},
    ]
    return [json.dumps(e) for e in events]


@pytest.fixture(autouse=True)
def fake_plan_probe(
    bench: dict[str, ModuleType], monkeypatch: pytest.MonkeyPatch
) -> list[dict[str, str]]:
    """No test reaches a real ``codex app-server`` or a real ``claude`` probe call: the live
    probes answer a healthy plan."""
    seen: list[dict[str, str]] = []

    def fake(env: dict[str, str], cwd: Path) -> dict[str, Any]:
        seen.append(dict(env))
        return copy.deepcopy(OK_PROBE)

    def fake_claude(env: dict[str, str], cwd: Path) -> list[str]:
        assert not any(cwd.iterdir()), "the Claude probe runs in a fresh empty directory"
        return claude_probe_stream()

    def no_process(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a test reached a real probe process")

    def fake_usage(env: dict[str, str]) -> dict[str, Any]:
        return {"ok": True, "cli_this_month": 100, "cli_limit": 10000, "reset_date": "2026-11-01"}

    monkeypatch.setattr(bench["probe"], "app_server_probe", fake)
    monkeypatch.setattr(bench["probe"], "claude_probe_call", fake_claude)
    monkeypatch.setattr(bench["probe"].subprocess, "run", no_process)
    # No test reads the real Archivist counter (the quota gate gets a low count).
    monkeypatch.setattr(bench["runner"], "archivist_usage", fake_usage)
    return seen


def fixture_lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text().splitlines()


# --- question sets -----------------------------------------------------------------------------

STRATA = ("lookup", "multi_hop_document", "multi_document", "multi_period", "breadth", "reach")
PREFIX = {
    "lookup": "LK",
    "multi_hop_document": "MH",
    "multi_document": "MD",
    "multi_period": "MP",
    "breadth": "BR",
    "reach": "RC",
}


def _uuid(n: int, salt: int) -> str:
    return f"{salt:08x}-0000-4000-8000-{n:012x}"


def _sourced(fid: str, n: int, salt: int, doc: bool = True) -> dict[str, Any]:
    filing, chunk = _uuid(n, salt), _uuid(n, salt + 1)
    src: dict[str, Any] = {
        "filing_uuid": filing,
        "chunk_id": chunk,
        "permalink": f"https://mosaic-finance.com/filings/{filing}/p/{chunk}/k1.{'A' * 43}/",
        "quote": "Total net sales 391,035",
    }
    if doc:
        src["exchange_document_id"] = "0000320193-24-000123"
        src["exchange_document_kind"] = "sec_accession_number"
    else:
        src["exchange_document_id"] = None
        src["document_id_absent_reason"] = "SEDAR+ rows carry no stored id"
    return {
        "id": fid,
        "kind": "sourced",
        "statement": "net sales",
        "value": "391,035",
        "source": src,
    }


def valid_set() -> dict[str, Any]:
    qs: list[dict[str, Any]] = []
    for s_index, stratum in enumerate(STRATA):
        for i in range(1, 16):
            qid = f"{PREFIX[stratum]}{i:02d}"
            tags = ["non_us", "sedar"] if i <= 3 else ["us"]
            if stratum == "reach":
                facts = [_sourced(f"{qid}.f{n}", n, 1000 * s_index + 50 * i) for n in range(1, 21)]
            else:
                facts = [
                    _sourced(f"{qid}.f1", 1, 1000 * s_index + 50 * i, doc=i % 2 == 0),
                    {
                        "id": f"{qid}.f2",
                        "kind": "absence",
                        "statement": "not mentioned",
                        "value": "not mentioned",
                        "source": {"filing_uuid": _uuid(9, 7)},
                        "absent_terms": ["TSMC"],
                        "absence_reason": "no chunk matched",
                    },
                    {
                        "id": f"{qid}.f3",
                        "kind": "computed",
                        "statement": "difference",
                        "value": "1",
                        "formula": f"{qid}.f1 - {qid}.f1",
                        "computed_from": [f"{qid}.f1"],
                    },
                ]
            qs.append(
                {
                    "id": qid,
                    "stratum": stratum,
                    "tags": tags,
                    "question": f"Q {qid}?",
                    "facts": facts,
                }
            )
    for i in (1, 2):
        qs.append(
            {
                "id": f"CT{i:02d}",
                "stratum": "control",
                "tags": ["control"],
                "question": "What is 17 times 23?",
                "facts": [
                    {
                        "id": f"CT{i:02d}.f1",
                        "kind": "control",
                        "statement": "product",
                        "value": "391",
                    }
                ],
            }
        )
    return {"schema_version": 1, "questions": qs}


def test_valid_question_set_passes(bench: dict[str, ModuleType]) -> None:
    assert bench["questions"].validate(valid_set()) == []


def test_question_set_violations_are_each_named(bench: dict[str, ModuleType]) -> None:
    q = bench["questions"]
    data = valid_set()
    data["questions"][1]["id"] = data["questions"][0]["id"]  # duplicate id
    data["questions"].pop(20)  # multi_hop_document now 14
    reach = next(x for x in data["questions"] if x["stratum"] == "reach")
    reach["facts"] = reach["facts"][:5]  # 5 filings
    fact = data["questions"][2]["facts"][0]
    del fact["source"]  # sourced fact without source
    absence = data["questions"][3]["facts"][1]
    absence["absence_reason"] = ""
    data["questions"][4]["question"] = f"See {UPSTREAM_URL}"
    errors = q.validate(data)
    text = "\n".join(errors)
    assert "duplicate question id" in text
    assert "stratum multi_hop_document: 14 questions" in text
    assert "reach question cites 5 filings" in text
    assert "sourced fact without source" in text
    assert "absence fact without absence_reason" in text
    assert "upstream host text present" in text


def test_question_set_requires_non_us_and_permalink_match(bench: dict[str, ModuleType]) -> None:
    q = bench["questions"]
    data = valid_set()
    for item in data["questions"]:
        item["tags"] = ["us"]
    first = data["questions"][0]["facts"][0]["source"]
    first["permalink"] = first["permalink"].replace(first["chunk_id"], _uuid(77, 77))
    data["questions"][5]["facts"][0]["source"].pop("exchange_document_id")
    errors = "\n".join(q.validate(data))
    assert "non_us tagged questions: 0" in errors
    assert "permalink does not match" in errors
    assert "exchange_document_id or document_id_absent_reason required" in errors


def test_validate_cli_exit_codes(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = tmp_path / "good.json"
    good.write_text(json.dumps(valid_set()))
    assert bench["cli"].main(["validate-questions", "--questions", str(good)]) == 0
    bad = valid_set()
    bad["questions"].pop()
    bad_path = tmp_path / "bad.json"
    bad_path.write_text(json.dumps(bad))
    assert bench["cli"].main(["validate-questions", "--questions", str(bad_path)]) == 1
    assert "controls: 1 questions" in capsys.readouterr().out


def test_claude_subset_is_fixed_and_stratified(bench: dict[str, ModuleType]) -> None:
    q = bench["questions"]
    data = valid_set()
    subset = q.claude_subset(data)
    shuffled = copy.deepcopy(data)
    shuffled["questions"].reverse()
    assert subset == q.claude_subset(shuffled)
    assert len(subset) == 6 * 5 + 2
    strata = {x["id"]: x["stratum"] for x in data["questions"]}
    for stratum in STRATA:
        assert sum(1 for i in subset if strata[i] == stratum) == 5


@pytest.mark.skipif(not QUESTION_SET.exists(), reason="committed question set not present")
def test_committed_question_set_is_valid(bench: dict[str, ModuleType]) -> None:
    q = bench["questions"]
    assert q.validate(q.load(QUESTION_SET)) == []


# --- Codex ---------------------------------------------------------------------------------------


def test_codex_rollout_extraction(bench: dict[str, ModuleType]) -> None:
    ex = bench["codex"].parse_rollout(fixture_lines("codex_rollout_archivist.jsonl"))
    assert ex.usage is not None
    assert ex.usage.total == 91200  # last cumulative value, never a sum of token_count events
    assert ex.usage.input == 90000
    assert ex.usage.cached_input == 76000
    assert ex.usage.uncached_input == 14000
    assert ex.usage.cache_write_input == 1000
    assert ex.usage.output == 1200
    assert ex.usage.reasoning == 200
    assert ex.model_calls == 3
    assert [c["input_tokens"] for c in ex.per_call_usage] == [20000, 30000, 40000]
    assert ex.tool_calls == {
        "mcp:archivist.companies_search": 1,
        "mcp:archivist.search": 2,
    }
    assert ex.compactions == 1
    assert ex.truncated_calls == [
        {"call_id": "call_big", "tool": "exec", "original_tokens": 12620},
        {"call_id": "call_fn", "tool": "noop_function", "original_tokens": 10400},
    ]
    assert ex.truncations == 2
    assert ex.paged_results == 1
    assert (ex.model, ex.effort, ex.service_tier, ex.cli_version) == (
        "gpt-6.1-sol",
        "medium",
        "default",
        "0.160.0",
    )
    assert ex.mcp_servers == {"archivist": "connected"}
    assert ex.mcp_evidence == {"archivist": "tool_catalog"}
    assert ex.answer.startswith("Total net sales were 391,035")
    assert bench["codex"].isolation_violations("archivist", ex) == []


def test_codex_web_actions_are_counted(bench: dict[str, ModuleType]) -> None:
    ex = bench["codex"].parse_rollout(fixture_lines("codex_rollout_web.jsonl"))
    assert ex.web_actions == {"findInPage": 1, "openPage": 1, "search": 1}
    assert ex.tool_calls == {"web.search": 3}
    assert "https://investor.example.com/10k" in ex.web_urls
    assert bench["codex"].isolation_violations("web", ex) == []
    assert "web search used in the archivist arm" in bench["codex"].isolation_violations(
        "archivist", ex
    )


def test_codex_mcp_failure_and_web_arm_mcp_are_isolation_violations(
    bench: dict[str, ModuleType],
) -> None:
    codex = bench["codex"]
    failed = codex.parse_rollout(fixture_lines("codex_rollout_mcp_failed.jsonl"))
    assert codex.isolation_violations("both", failed) == ["archivist MCP not connected (failed)"]
    loaded = codex.parse_rollout(fixture_lines("codex_rollout_archivist.jsonl"))
    problems = codex.isolation_violations("web", loaded)
    assert "MCP server archivist loaded outside the arm allowlist" in problems
    assert "tool mcp:archivist.search outside the arm allowlist" in problems


def test_codex_exec_stdout_and_stderr_fold_in(bench: dict[str, ModuleType]) -> None:
    codex = bench["codex"]
    model = bench["model"]
    ex = model.Extraction()
    codex.parse_exec_stdout(
        [
            json.dumps(
                {"type": "mcp_startup_update", "server": "archivist", "status": {"state": "ready"}}
            ),
            json.dumps(
                {"type": "turn.failed", "error": {"message": "Selected model is at capacity"}}
            ),
            "not json",
        ],
        ex,
    )
    assert ex.mcp_servers == {"archivist": "connected"}
    assert ex.infra_reason == "server_overloaded"
    codex.parse_stderr("ERROR MCP client for `archivist` failed to start: boom", ex)
    assert ex.mcp_servers["archivist"] == "failed"


def test_codex_archivist_429_is_infra(bench: dict[str, ModuleType]) -> None:
    line = json.dumps(
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "McpToolCall",
                    "server": "archivist",
                    "tool": "search",
                    "status": "failed",
                    "result": {"content": [{"type": "text", "text": "HTTP 429 CLI_QUOTA"}]},
                },
            },
        }
    )
    ex = bench["codex"].parse_rollout([line])
    # Codex campaign (D2): the monthly CLI quota stops the batch; a burst limit keeps the
    # immediate rerun.
    assert ex.infra_reason == "archivist_quota"
    assert ex.incomplete_reason == "no_usage"
    burst_line = line.replace("CLI_QUOTA", "RATE_LIMITED")
    assert bench["codex"].parse_rollout([burst_line]).infra_reason == "archivist_429"
    assert bench["codex"].parse_rollout([burst_line, line]).infra_reason == "archivist_quota"


@pytest.mark.parametrize("arm", ["web", "archivist", "both"])
def test_codex_config_rendering_per_arm(
    bench: dict[str, ModuleType], arm: str, tmp_path: Path
) -> None:
    codex = bench["codex"]
    cfg = bench["model"].DEFAULT_CONFIGS["codex"]
    text = codex.render_config(arm, cfg, tmp_path)
    assert 'model = "gpt-6.1-sol"' in text
    assert 'model_reasoning_effort = "medium"' in text
    assert 'approval_policy = "never"' in text
    assert f'web_search = "{"disabled" if arm == "archivist" else "live"}"' in text
    for feature in codex.DISABLED_FEATURES:
        assert f"{feature} = false" in text
    assert ("[mcp_servers.archivist]" in text) == (arm != "web")
    if arm != "web":
        assert text.count('approval_mode = "approve"') == len(codex.ARCHIVIST_TOOLS)
    assert "service_tier" not in text
    with pytest.raises(ValueError, match="fast"):
        codex.render_config(arm, replace(cfg, service_tier="fast"), tmp_path)


def test_codex_command_and_home(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    codex = bench["codex"]
    cfg = bench["model"].DEFAULT_CONFIGS["codex"]
    argv = codex.command(cfg, "PROMPT", tmp_path / "cwd", tmp_path / "last.md")
    assert argv[:4] == ["codex", "exec", "--json", "--skip-git-repo-check"]
    assert argv[argv.index("-s") + 1] == "read-only"
    assert argv[-1] == "PROMPT"
    assert "--search" not in argv and "-a" not in argv
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": {}}))
    home = tmp_path / "home"
    codex.prepare_home(home, "both", cfg, tmp_path / "cwd", auth)
    assert (home / "auth.json").is_symlink()
    assert (home / "auth.json").resolve() == auth.resolve()
    assert codex.env_extras(home, cfg, {"CODEX_API_KEY": "x"}) == {"CODEX_HOME": str(home)}
    with pytest.raises(RuntimeError):
        codex.env_extras(home, replace(cfg, auth_mode="api_key"), {})


# --- Claude Code -------------------------------------------------------------------------------


def test_claude_stream_extraction(bench: dict[str, ModuleType]) -> None:
    cc = bench["claude_code"]
    ex = cc.parse_stream(fixture_lines("claude_stream_both.jsonl"))
    assert ex.model_calls == 5  # msg_1 and msg_2 repeat; deduplicated by message.id
    assert ex.tool_calls == {
        "WebFetch": 1,
        "WebSearch": 1,
        "mcp__archivist__read_section": 1,
        "mcp__archivist__search": 1,
    }
    assert ex.web_actions == {"fetch": 1, "search": 1, "server_searches": 3}
    assert ex.compactions == 1
    assert ex.truncated_calls == [
        {"call_id": "tu_5", "tool": "mcp__archivist__read_section", "original_tokens": 31250}
    ]
    assert ex.truncations == 1
    assert ex.paged_results == 1
    assert ex.num_turns == 4
    assert ex.client_cost_usd == pytest.approx(0.4321)
    assert ex.usage is not None
    # modelUsage summed over models: opus 100 + 30000 + 2000 input, haiku 5000 input.
    assert ex.usage.input == 37100
    assert ex.usage.cached_input == 30000
    assert ex.usage.cache_write_input == 2000
    assert ex.usage.uncached_input == 7100
    assert ex.usage.output == 1100
    assert ex.usage.total == 38200
    assert ex.usage.reasoning == 300
    assert ex.api_key_source == "none"
    assert ex.mcp_servers == {"archivist": "connected"}
    assert cc.isolation_violations("both", ex) == []
    assert ex.answer.startswith("Net sales were 391,035")


def test_claude_turn_limit_no_result_and_overload(bench: dict[str, ModuleType]) -> None:
    cc = bench["claude_code"]
    limited = cc.parse_stream(fixture_lines("claude_stream_max_turns.jsonl"))
    assert limited.turn_limit is True
    assert limited.usage is not None and limited.usage.total == 15
    missing = cc.parse_stream(fixture_lines("claude_stream_no_result.jsonl"))
    assert missing.incomplete_reason == "no_result"
    overloaded = cc.parse_stream(fixture_lines("claude_stream_overloaded.jsonl"))
    assert overloaded.infra_reason == "api_error:529"


def test_claude_archivist_429_is_infra(bench: dict[str, ModuleType]) -> None:
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "m1",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "mcp__archivist__search",
                            "input": {},
                        }
                    ],
                },
            }
        ),
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "is_error": True,
                            "content": [{"type": "text", "text": "Error: HTTP 429 RATE_LIMITED"}],
                        }
                    ]
                },
            }
        ),
    ]
    ex = bench["claude_code"].parse_stream(lines)
    assert ex.infra_reason == "archivist_429"
    success = [
        lines[0],
        lines[1]
        .replace('"is_error": true, ', "")
        .replace("Error: HTTP 429 RATE_LIMITED", "Revenue was 429 million"),
    ]
    assert bench["claude_code"].parse_stream(success).infra_reason is None


def test_claude_isolation(bench: dict[str, ModuleType]) -> None:
    cc = bench["claude_code"]
    bad = cc.parse_stream(fixture_lines("claude_stream_mcp_failed.jsonl"))
    problems = cc.isolation_violations("both", bad)
    assert "archivist MCP not connected (failed)" in problems
    assert "tool Bash outside the arm allowlist" in problems
    both = cc.parse_stream(fixture_lines("claude_stream_both.jsonl"))
    web_problems = cc.isolation_violations("web", both)
    assert "tool mcp__archivist__search outside the arm allowlist" in web_problems
    assert "MCP server archivist loaded outside the arm allowlist" in web_problems
    assert "tool WebSearch outside the arm allowlist" in cc.isolation_violations("archivist", both)


@pytest.mark.parametrize(
    ("arm", "tools", "allowed"),
    [
        ("web", "WebSearch,WebFetch", "WebSearch,WebFetch"),
        ("archivist", "", "mcp__archivist"),
        ("both", "WebSearch,WebFetch", "WebSearch,WebFetch,mcp__archivist"),
    ],
)
def test_claude_command_per_arm(
    bench: dict[str, ModuleType], arm: str, tools: str, allowed: str
) -> None:
    cc = bench["claude_code"]
    cfg = bench["model"].DEFAULT_CONFIGS["claude_code"]
    argv = cc.command(cfg, arm, "/x/mcp.json")
    assert argv[argv.index("--tools") + 1] == tools
    assert argv[argv.index("--allowedTools") + 1] == allowed
    for flag in (
        "--strict-mcp-config",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--verbose",
    ):
        assert flag in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    assert "--restricted" in argv and "--bare" not in argv
    servers = cc.mcp_config(arm)["mcpServers"]
    assert ("archivist" in servers) == (arm != "web")
    api = cc.command(replace(cfg, auth_mode="api_key"), arm, "/x/mcp.json")
    assert "--bare" in api and "--restricted" not in api


def test_claude_api_key_mode_needs_existing_key(bench: dict[str, ModuleType]) -> None:
    cc = bench["claude_code"]
    cfg = replace(bench["model"].DEFAULT_CONFIGS["claude_code"], auth_mode="api_key")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        cc.env_extras(cfg, {})
    extras = cc.env_extras(cfg, {"ANTHROPIC_API_KEY": "sk-test"})
    assert extras["ENABLE_TOOL_SEARCH"] == "false"
    assert extras["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
    assert extras["DISABLE_AUTOUPDATER"] == "1"


# --- environment, redaction, contamination --------------------------------------------------------


def test_child_env_is_allowlisted(bench: dict[str, ModuleType]) -> None:
    runner = bench["runner"]
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "LANG": "C.UTF-8",
        "CLAUDE_CODE_SESSION_ID": "s",
        "CLAUDE_EFFORT": "max",
        "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/sock",
        "CLAUDE_CODE_MESSAGING_TOKEN": "secret",
        "CLAUDECODE": "1",
        "CODEX_HOME": "/home/x/.codex",
        "CODEX_API_KEY": "secret",
        "ANTHROPIC_API_KEY": "secret",
        "OPENAI_API_KEY": "secret",
        "GOOGLE_APPLICATION_CREDENTIALS": "/k.json",
        "HERDR_ENV": "1",
        "ARCHIVIST_TOKEN": "ak_x",
    }
    env = runner.child_env(parent, {"CODEX_HOME": "/run/home"})
    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "LANG": "C.UTF-8",
        "CODEX_HOME": "/run/home",
    }
    with pytest.raises(ValueError):
        runner.child_env(parent, {"CLAUDE_EFFORT": "max"})


def test_redaction_blinding_and_upstream_hosts(bench: dict[str, ModuleType]) -> None:
    redact = bench["redact"]
    permalink = "https://mosaic-finance.com/filings/a/p/b/k1.tok/"
    text = (
        f"Sales were 391,035 ([10-K]({permalink})); see {UPSTREAM_URL} and mcp__archivist__search."
    )
    redacted = redact.redact_links(text)
    assert "k1.tok" not in redacted and "Archives" not in redacted
    assert "[10-K](<url:mosaic-finance.com>)" in redacted
    assert "<url:filing-source>" in redacted
    assert redact.find_upstream_hosts(redacted) == []
    assert redact.redact_links("[10-Q](" + UPSTREAM_URL + ")") == "[10-Q](<url:filing-source>)"
    assert redact.find_upstream_hosts(text) == ["sec" + ".gov"]
    blinded = redact.blind("Per Archivist and Mosaic, " + text + " Used WebSearch on example.com.")
    lowered = blinded.lower()
    for leak in ("archivist", "mosaic", "websearch", "sec" + ".gov", "example.com", "https://"):
        assert leak not in lowered
    assert "[link]" in blinded and "[tool]" in blinded


def test_contamination(bench: dict[str, ModuleType]) -> None:
    redact = bench["redact"]
    assert redact.contamination_hits("web", ["https://mosaic-finance.com/filings/x/"], "") == [
        "mosaic-finance.com"
    ]
    assert redact.contamination_hits("both", ["https://mosaic-finance.com/filings/x/"], "") == []
    repo = "https://private-source-forge.invalid/owner/mosaic/questions.json"
    assert "private-source-forge.invalid" in redact.contamination_hits("both", [repo], "")


# --- runner: records, statuses, dry run, ceilings ------------------------------------------------


def _spec(bench: dict[str, ModuleType], agent: str = "codex", arm: str = "web") -> Any:
    model = bench["model"]
    return model.RunSpec(
        agent=agent,
        arm=arm,
        question_id="LK01",
        stratum="lookup",
        repetition=1,
        prompt=bench["runner"].prompt_for("What were total net sales?"),
        config=model.DEFAULT_CONFIGS[agent],
    )


def _run_dir(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    fixture: str,
    agent: str = "codex",
    arm: str = "web",
    **meta: Any,
) -> Path:
    run_dir = tmp_path / f"run-{agent}-{arm}-{fixture}"
    run_dir.mkdir()
    spec = _spec(bench, agent, arm)
    bench["runner"].write_meta(run_dir, spec, run_dir.name, 1, started_at="t", **meta)
    if agent == "codex":
        sessions = run_dir / "codex-home" / "sessions" / "2026" / "10" / "06"
        sessions.mkdir(parents=True)
        (sessions / "rollout-x.jsonl").write_text((FIXTURES / fixture).read_text())
    else:
        (run_dir / "stdout.jsonl").write_text((FIXTURES / fixture).read_text())
    return run_dir


@pytest.mark.parametrize(
    ("fixture", "agent", "arm", "status", "reason"),
    [
        ("codex_rollout_web.jsonl", "codex", "web", "completed", None),
        ("codex_rollout_archivist.jsonl", "codex", "archivist", "completed", None),
        ("codex_rollout_no_usage.jsonl", "codex", "web", "incomplete", "no_usage"),
        ("codex_rollout_infra.jsonl", "codex", "web", "infra_error", "usage_limit_exceeded"),
        ("codex_rollout_mcp_failed.jsonl", "codex", "both", "invalid", "arm_isolation"),
        ("claude_stream_both.jsonl", "claude_code", "both", "completed", None),
        ("claude_stream_max_turns.jsonl", "claude_code", "web", "turn_limit", "max turns"),
        ("claude_stream_no_result.jsonl", "claude_code", "both", "incomplete", "no_result"),
        ("claude_stream_mcp_failed.jsonl", "claude_code", "both", "invalid", "arm_isolation"),
        ("claude_stream_overloaded.jsonl", "claude_code", "both", "infra_error", "api_error"),
    ],
)
def test_record_status(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    fixture: str,
    agent: str,
    arm: str,
    status: str,
    reason: str | None,
) -> None:
    run_dir = _run_dir(bench, tmp_path, fixture, agent, arm, wall_s=12.5, returncode=0)
    record = bench["runner"].build_record(run_dir)
    assert record.status == status
    if reason is None:
        assert record.status_reason is None
    else:
        assert reason in (record.status_reason or "")
    assert json.loads((run_dir / "record.json").read_text())["status"] == status
    assert record.wall_s == 12.5


def test_completed_record_holds_every_field(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    run_dir = _run_dir(
        bench, tmp_path, "codex_rollout_archivist.jsonl", arm="archivist", wall_s=30.0, returncode=0
    )
    record = bench["runner"].build_record(run_dir)
    usage = record.usage
    assert usage is not None
    for key in (
        "total",
        "cached_input",
        "cache_write_input",
        "uncached_input",
        "output",
        "reasoning",
    ):
        assert usage[key] is not None
    assert record.model_calls == 3
    assert record.tool_calls["mcp:archivist.search"] == 2
    assert record.compactions == 1 and record.truncations == 2
    assert record.truncated_calls[0]["original_tokens"] == 12620
    assert record.turn_limit is False and record.timeout is False
    assert record.cost_usd is not None and record.cost_usd > 0
    assert "API list price equivalent" in record.cost_basis
    assert record.mcp_loaded is True
    assert record.billing_mode == "chatgpt_plan_allowance"
    assert (run_dir / "answer.md").read_text().startswith("Total net sales")
    ledger = bench["runner"].ledger_markdown([record])
    assert "| codex | archivist | LK01 |" in ledger and "| yes |" in ledger
    assert "mcp_servers=archivist:connected(tool_catalog)" in record.notes


def test_contaminated_run_is_flagged(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    run_dir = _run_dir(
        bench, tmp_path, "codex_rollout_contaminated.jsonl", wall_s=1.0, returncode=0
    )
    record = bench["runner"].build_record(run_dir)
    assert record.contaminated is True
    assert "contaminated: mosaic-finance.com" in record.notes


def _alive(pid: int) -> bool:
    """True while ``pid`` runs. A zombie counts as dead: in the CI job container PID 1 is
    ``tail -f /dev/null``, which never reaps the orphaned grandchild after the group kill."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def test_timeout_kills_process_group_and_keeps_partial_usage(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    runner = bench["runner"]
    marker = tmp_path / "grandchild.pid"
    script = f"sleep 60 & echo $! > {marker}; wait"
    started = time.monotonic()
    result = runner.subprocess_executor(
        runner.ProcessRequest(
            argv=["bash", "-c", script],
            env={"PATH": os.environ["PATH"]},
            cwd=tmp_path,
            stdin_text=None,
            stdout_path=tmp_path / "out",
            stderr_path=tmp_path / "err",
            timeout_s=0.5,
        )
    )
    assert result.timed_out is True
    assert time.monotonic() - started < 20
    grandchild = int(marker.read_text())
    for _ in range(50):
        if not _alive(grandchild):
            break
        time.sleep(0.1)
    else:
        pytest.fail("grandchild survived the process group kill")
    run_dir = _run_dir(
        bench, tmp_path, "codex_rollout_partial.jsonl", wall_s=0.5, timed_out=True, returncode=-15
    )
    record = runner.build_record(run_dir)
    assert record.status == "timeout"
    assert record.timeout is True
    assert record.usage is not None and record.usage["total"] == 15200


def no_servers(home: Path, env: dict[str, str], cwd: Path) -> dict[str, Any]:
    return {"configured": [], "handshake": {}}


class FakeExecutor:
    """Writes a fixture where the real agent would, then reports success."""

    def __init__(self, fixtures: list[str]) -> None:
        self.fixtures = list(fixtures)
        self.calls: list[Any] = []
        self.lock = threading.Lock()

    def __call__(self, req: Any) -> Any:
        from archivist_bench.runner import ProcessResult

        with self.lock:
            self.calls.append(req)
            name = self.fixtures.pop(0) if len(self.fixtures) > 1 else self.fixtures[0]
        text = (FIXTURES / name).read_text()
        if req.argv[0] == "codex":
            home = Path(req.env["CODEX_HOME"])
            sessions = home / "sessions" / "2026"
            sessions.mkdir(parents=True, exist_ok=True)
            (sessions / "rollout-fake.jsonl").write_text(text)
            req.stdout_path.write_text("")
        else:
            req.stdout_path.write_text(text)
        return ProcessResult(returncode=0, timed_out=False, wall_s=1.0)


def test_preflight_folds_into_isolation(bench: dict[str, ModuleType]) -> None:
    codex = bench["codex"]
    model = bench["model"]
    ok = {"configured": ["archivist"], "handshake": {"archivist": {"ok": True, "tools": ["s"]}}}
    ex = model.Extraction()
    codex.apply_preflight(ok, ex)
    assert (
        ex.mcp_servers == {"archivist": "connected"} and ex.mcp_evidence["archivist"] == "preflight"
    )
    assert codex.isolation_violations("archivist", ex) == []
    assert "MCP server archivist loaded outside the arm allowlist" in codex.isolation_violations(
        "web", ex
    )
    bad = model.Extraction()
    codex.apply_preflight(
        {"configured": ["archivist"], "handshake": {"archivist": {"ok": False, "error": "x"}}}, bad
    )
    assert codex.isolation_violations("both", bad) == ["archivist MCP not connected (failed)"]
    failed_in_run = model.Extraction(mcp_servers={"archivist": "failed"})
    codex.apply_preflight(ok, failed_in_run)
    assert failed_in_run.mcp_servers["archivist"] == "failed"


FAKE_MCP_SERVER = """
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        reply = {"serverInfo": {"name": "fake", "version": "1"}, "capabilities": {}}
    elif msg.get("method") == "tools/list":
        reply = {"tools": [{"name": "search"}, {"name": "read_passage"}]}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": reply}), flush=True)
"""


def test_stdio_handshake_against_fake_server(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    codex = bench["codex"]
    script = tmp_path / "server.py"
    script.write_text(FAKE_MCP_SERVER)
    env = {"PATH": os.environ["PATH"]}
    result = codex._stdio_exchange([sys.executable, str(script)], env, tmp_path, 10.0)
    assert result == {
        "ok": True,
        "tools": ["search", "read_passage"],
        "server": {"name": "fake", "version": "1"},
    }
    silent = codex._stdio_exchange(
        [sys.executable, "-c", "import time; time.sleep(5)"], env, tmp_path, 0.5
    )
    assert silent["ok"] is False and "Empty" in silent["error"]


def test_infra_failures_rerun_at_most_twice(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    runner = bench["runner"]
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": {}}))
    env = {"PATH": "/usr/bin", "HOME": str(tmp_path)}
    # A transient infra error (server_overloaded) reruns at once; a plan limit stops instead.
    fake = FakeExecutor(["codex_rollout_overloaded.jsonl"])
    records = runner.execute_plan(
        [_spec(bench)],
        tmp_path / "ev",
        max_runs=5,
        executor=fake,
        parent_env=env,
        codex_auth=auth,
        preflight=no_servers,
    )
    assert [r.attempt for r in records] == [1, 2, 3]
    assert all(r.status == "infra_error" for r in records)
    ledger = runner.load_ledger(tmp_path / "ev" / "ledger.jsonl")
    assert len(ledger) == 3
    recovered = FakeExecutor(["codex_rollout_overloaded.jsonl", "codex_rollout_web.jsonl"])
    again = runner.execute_plan(
        [_spec(bench)],
        tmp_path / "ev2",
        max_runs=5,
        executor=recovered,
        parent_env=env,
        codex_auth=auth,
        preflight=no_servers,
    )
    assert [r.status for r in again] == ["infra_error", "completed"]
    command = json.loads((tmp_path / "ev2" / "runs" / again[1].run_id / "command.json").read_text())
    assert "CLAUDE_EFFORT" not in command["env_keys"]
    assert command["argv"][0] == "codex"


def test_execute_ceilings(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    runner = bench["runner"]
    specs = [_spec(bench), _spec(bench, arm="archivist")]
    fake = FakeExecutor(["codex_rollout_web.jsonl"])
    with pytest.raises(ValueError, match="more than --max-runs"):
        runner.execute_plan(specs, tmp_path / "a", max_runs=1, executor=fake)
    with pytest.raises(ValueError, match="concurrency"):
        runner.execute_plan(specs, tmp_path / "b", max_runs=2, concurrency=3, executor=fake)
    fast = replace(specs[0], config=replace(specs[0].config, service_tier="fast"))
    with pytest.raises(ValueError, match="fast"):
        runner.execute_plan([fast], tmp_path / "c", max_runs=1, executor=fake)
    assert fake.calls == []


def test_attempt_budget_counts_reruns(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    runner = bench["runner"]
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": {}}))
    fake = FakeExecutor(["codex_rollout_overloaded.jsonl"])
    records = runner.execute_plan(
        [_spec(bench), replace(_spec(bench), repetition=2)],
        tmp_path / "ev",
        max_runs=3,
        concurrency=2,
        executor=fake,
        parent_env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
        codex_auth=auth,
        preflight=no_servers,
    )
    assert len(records) == 3 and len(fake.calls) == 3


@pytest.fixture
def question_file(tmp_path: Path) -> Path:
    path = tmp_path / "questions.json"
    path.write_text(json.dumps(valid_set()))
    return path


def test_plan_and_run_dry_run_spawn_nothing(
    bench: dict[str, ModuleType],
    question_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("dry run spawned a process")

    monkeypatch.setattr(bench["runner"].subprocess, "Popen", refuse)
    cli = bench["cli"]
    common = ["--questions", str(question_file), "--evidence-dir", str(tmp_path / "ev")]
    assert cli.main(["plan", *common]) == 0
    out = capsys.readouterr().out
    assert "== codex: model=gpt-6.1-sol effort=medium" in out
    assert "== claude_code: model=claude-opus-5-5 effort=medium" in out
    for arm in ("web", "archivist", "both"):
        assert f"-- arm {arm}: isolated home" in out
    assert "-- control questions: CT01, CT02" in out
    assert "total runs: 1116" in out  # 92 x 3 x 3 Codex plus 32 x 3 x 3 Claude Code
    assert "nothing executed" in out
    assert cli.main(["run", *common, "--agent", "codex", "--question", "LK01"]) == 0
    out = capsys.readouterr().out
    assert "total runs: 9" in out and "dry run" in out
    with pytest.raises(SystemExit, match="--max-runs"):
        cli.main(["run", *common, "--agent", "codex", "--question", "LK01", "--execute"])
    with pytest.raises(SystemExit, match="more than --max-runs"):
        cli.main(
            [
                "run",
                *common,
                "--agent",
                "codex",
                "--question",
                "LK01",
                "--execute",
                "--max-runs",
                "3",
            ]
        )
    assert not (tmp_path / "ev" / "runs").exists()


def test_prompt_is_identical_across_arms(bench: dict[str, ModuleType]) -> None:
    runner = bench["runner"]
    data = valid_set()
    cfg = bench["model"].DEFAULT_CONFIGS["codex"]
    specs = runner.build_plan(data["questions"][:2], "codex", cfg, 1)
    by_question: dict[str, set[str]] = {}
    for spec in specs:
        by_question.setdefault(spec.question_id, set()).add(spec.prompt)
    assert all(len(p) == 1 for p in by_question.values())
    assert next(iter(by_question["LK01"])).startswith(
        "Research question (use your research tools; do not run shell commands): Q LK01?"
    )


# --- judge ---------------------------------------------------------------------------------------


QUESTION = {
    "id": "LK01",
    "question": "What were Apple's total net sales in fiscal 2024?",
    "facts": [
        {"id": "LK01.f1", "statement": "net sales FY2024", "value": "391,035", "unit": "USD m"},
        {"id": "LK01.f2", "statement": "net sales FY2023", "value": "383,285", "unit": "USD m"},
    ],
}


def _verdict(f1: str, f2: str, complete: bool = True) -> str:
    return json.dumps(
        {"facts": [{"id": "F1", "verdict": f1}, {"id": "F2", "verdict": f2}], "complete": complete}
    )


def _same(reply: Any) -> dict[str, Any]:
    return {"A": reply, "B": reply}


def test_judge_agreement_and_dispute(bench: dict[str, ModuleType]) -> None:
    judge = bench["judge"]
    prompts: dict[str, str] = {}

    def make(name: str, reply: str) -> Any:
        def execute(prompt: str) -> str:
            prompts[name] = prompt
            return reply

        return execute

    executors = {
        "A": make("A", _verdict("correct", "correct")),
        "B": make("B", _verdict("correct", "missing", False)),
    }
    result = judge.judge(
        QUESTION, "Sales [link](https://mosaic-finance.com/x) via Archivist", "run-1", executors
    )
    assert result.status == "ok"
    assert result.fact_verdicts == {"LK01.f1": "correct", "LK01.f2": "disputed"}
    assert result.fact_scores == {"LK01.f1": 1.0, "LK01.f2": 0.5}
    assert result.disputed == ["LK01.f2"]
    assert result.accuracy == 0.75
    assert result.complete == 0.5
    # Both judges get the identical blinded prompt: key first, then the answer.
    assert prompts["A"] == prompts["B"]
    prompt = prompts["A"]
    assert prompt.index("Answer key:") < prompt.index("Answer to grade:")
    assert "mosaic" not in prompt.lower().split("answer to grade:")[1]
    assert "archivist" not in prompt.lower()
    out = result.as_dict()
    assert out["passes"]["A"]["judge"]["cli"] == "claude_code"
    assert out["passes"]["B"]["judge"]["cli"] == "codex"
    assert out["passes"]["B"]["fact_verdicts"] == {"LK01.f1": "correct", "LK01.f2": "missing"}
    assert judge.single_judge_accuracy(out, "A") == 1.0
    assert judge.single_judge_accuracy(out, "B") == 0.5


def test_judges_are_the_owner_plans(bench: dict[str, ModuleType]) -> None:
    judge = bench["judge"]
    a, b = judge.JUDGES["A"], judge.JUDGES["B"]
    assert (a.cli, a.family, a.auth_mode, a.billing_mode) == (
        "claude_code",
        "anthropic",
        "subscription",
        "claude_plan_allowance",
    )
    assert (b.cli, b.family, b.auth_mode, b.billing_mode) == (
        "codex",
        "openai",
        "chatgpt",
        "chatgpt_plan_allowance",
    )
    assert a.effort == b.effort == "medium"
    for name in ("vertex_executor", "gcloud_token", "VERTEX_BASE", "JUDGE_MODEL"):
        assert not hasattr(judge, name)


def test_judge_retries_malformed_once_then_errors(bench: dict[str, ModuleType]) -> None:
    judge = bench["judge"]
    replies = iter(["not json", _verdict("correct", "incorrect")])
    ok = judge.judge(
        QUESTION,
        "answer",
        "run-2",
        {"A": lambda _p: next(replies), "B": lambda _p: _verdict("correct", "incorrect")},
    )
    assert ok.status == "ok" and ok.passes["A"].attempts == 2
    assert ok.accuracy == 0.5
    bad = judge.judge(QUESTION, "answer", "run-3", _same(lambda _p: '{"facts": []}'))
    assert bad.status == "judge_error" and bad.accuracy is None
    assert bad.passes["A"].attempts == 2


def test_judge_call_error_retries_and_infra_error_raises(bench: dict[str, ModuleType]) -> None:
    judge = bench["judge"]

    def failing(_p: str) -> str:
        raise judge.JudgeCallError("exit 1")

    result = judge.judge(QUESTION, "answer", "run-c", _same(failing))
    assert result.status == "judge_error" and result.passes["B"].attempts == 2
    assert result.passes["A"].error == "call: exit 1"

    def limited(_p: str) -> str:
        raise judge.JudgeInfraError("plan limit")

    with pytest.raises(judge.JudgeInfraError):
        judge.judge(QUESTION, "answer", "run-i", _same(limited))


def test_judge_shuffle_is_seeded(bench: dict[str, ModuleType]) -> None:
    judge = bench["judge"]
    many = dict(
        QUESTION,
        facts=[
            {"id": f"LK01.f{i}", "statement": f"fact {i}", "value": str(i)} for i in range(1, 9)
        ],
    )
    a = judge.build_prompt(many, "x", "run-9")
    b = judge.build_prompt(many, "x", "run-9")
    assert a == b
    key = a.split("Answer key:\n")[1].split("\n\nAnswer to grade:")[0]
    order = [line.split(":")[0] for line in key.splitlines() if line]
    assert sorted(order) == sorted(f"F{i}" for i in range(1, 9))
    assert order != [f"F{i}" for i in range(1, 9)]


CLAUDE_JUDGE_INIT = {
    "type": "system",
    "subtype": "init",
    "tools": ["StructuredOutput"],
    "mcp_servers": [],
    "model": "claude-opus-5-5",
    "apiKeySource": "none",
    "claude_code_version": "2.1.289",
}


def _claude_judge_stream(
    init: dict[str, Any] | None = None, result: dict[str, Any] | None = None
) -> str:
    events = [
        init or CLAUDE_JUDGE_INIT,
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed",
                "overageStatus": "rejected",
                "overageDisabledReason": "org_level_disabled",
                "unifiedWindows": {"five_hour": {"utilization": 0.25}},
            },
        },
        result
        or {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "",
            "structured_output": json.loads(_verdict("correct", "missing")),
            "num_turns": 2,
            "total_cost_usd": 0.05,
            "modelUsage": {
                "claude-opus-5-5": {
                    "inputTokens": 10,
                    "outputTokens": 40,
                    "cacheReadInputTokens": 0,
                    "cacheCreationInputTokens": 3000,
                }
            },
        },
    ]
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _fake_process(
    bench: dict[str, ModuleType], stdout: str, seen: list[Any], last: str | None = None
) -> Any:
    runner = bench["runner"]

    def process(req: Any) -> Any:
        seen.append(req)
        req.stdout_path.write_text(stdout)
        req.stderr_path.write_text("")
        if last is not None:
            out = Path(req.argv[req.argv.index("-o") + 1])
            out.write_text(last)
            home = Path(req.env["CODEX_HOME"])
            seen.append((home / "config.toml").read_text())
            seen.append((home / "auth.json").is_symlink())
        return runner.ProcessResult(0, False, 1.5)

    return process


def test_claude_judge_executor(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    judge = bench["judge"]
    seen: list[Any] = []
    parent = {"PATH": "/usr/bin", "HOME": str(tmp_path), "ANTHROPIC_API_KEY": "sk-not-passed"}
    ex = judge.CliExecutor(
        judge.JUDGES["A"],
        tmp_path / "logs",
        process=_fake_process(bench, _claude_judge_stream(), seen),
        parent_env=parent,
        claude_config_dir=tmp_path,
    )
    reply = ex("PROMPT")
    req = seen[0]
    assert req.stdin_text == "PROMPT"
    argv = req.argv
    assert argv[:2] == ["claude", "-p"] and "--restricted" in argv and "--bare" not in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    assert argv[argv.index("--effort") + 1] == "medium"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == judge.VERDICT_SCHEMA
    assert "--strict-mcp-config" in argv and "--mcp-config" not in argv
    assert "ANTHROPIC_API_KEY" not in req.env
    assert req.cwd.name == "cwd" and not req.cwd.exists()  # fresh per call, removed after
    assert json.loads(reply.text)["facts"][1]["verdict"] == "missing"
    assert reply.usage["total"] == 3050 and reply.cost_usd == 0.05
    assert reply.meta["plan_window"]["windows"] == {"five_hour": 0.25}
    assert reply.meta["api_key_source"] == "none"
    assert (tmp_path / "logs" / "A-1" / "stdout.jsonl").exists()


@pytest.mark.parametrize(
    ("init", "match"),
    [
        (dict(CLAUDE_JUDGE_INIT, tools=["StructuredOutput", "Bash"]), "isolation"),
        (dict(CLAUDE_JUDGE_INIT, mcp_servers=[{"name": "x"}]), "isolation"),
        (dict(CLAUDE_JUDGE_INIT, apiKeySource="ANTHROPIC_API_KEY"), "API key"),
    ],
)
def test_claude_judge_isolation(
    bench: dict[str, ModuleType], init: dict[str, Any], match: str
) -> None:
    judge = bench["judge"]
    with pytest.raises(judge.JudgeCallError, match=match):
        judge.parse_claude_judge(_claude_judge_stream(init=init).splitlines())


def test_claude_judge_accepts_delivered_verdict_flagged_as_error(
    bench: dict[str, ModuleType],
) -> None:
    judge = bench["judge"]
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "result": "",
        "structured_output": json.loads(_verdict("correct", "correct")),
    }
    reply = judge.parse_claude_judge(_claude_judge_stream(result=result).splitlines())
    assert json.loads(reply.text)["complete"] is True
    failed = dict(result, subtype="error_during_execution", structured_output=None)
    with pytest.raises(judge.JudgeCallError, match="result error"):
        judge.parse_claude_judge(_claude_judge_stream(result=failed).splitlines())


def test_claude_judge_plan_limit_is_infra(bench: dict[str, ModuleType]) -> None:
    judge = bench["judge"]
    limited = {
        "type": "rate_limit_event",
        "rate_limit_info": {"status": "rejected", "rateLimitType": "seven_day"},
    }
    with pytest.raises(judge.JudgeInfraError, match="seven_day"):
        judge.parse_claude_judge([json.dumps(CLAUDE_JUDGE_INIT), json.dumps(limited)])
    error = {"type": "result", "is_error": True, "result": "API Error: 429 rate limit"}
    with pytest.raises(judge.JudgeInfraError):
        judge.parse_claude_judge([json.dumps(CLAUDE_JUDGE_INIT), json.dumps(error)])


CODEX_JUDGE_STREAM = "\n".join(
    json.dumps(e)
    for e in (
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.completed", "item": {"id": "i0", "type": "reasoning", "text": "..."}},
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "{}"}},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 12000,
                "cached_input_tokens": 8000,
                "cache_write_input_tokens": 0,
                "output_tokens": 300,
                "reasoning_output_tokens": 100,
            },
        },
    )
)


def test_codex_judge_executor(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    judge = bench["judge"]
    seen: list[Any] = []
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": {}}))
    parent = {"PATH": "/usr/bin", "HOME": str(tmp_path), "OPENAI_API_KEY": "sk-not-passed"}
    ex = judge.CliExecutor(
        judge.JUDGES["B"],
        tmp_path / "logs",
        process=_fake_process(bench, CODEX_JUDGE_STREAM, seen, last=_verdict("correct", "correct")),
        parent_env=parent,
        codex_auth=auth,
    )
    reply = ex("PROMPT")
    req, config, symlinked = seen
    argv = req.argv
    assert argv[:2] == ["codex", "exec"] and argv[-1] == "-"
    assert req.stdin_text == "PROMPT"
    for flag in ("--ephemeral", "--json", "--output-schema"):
        assert flag in argv
    assert argv[argv.index("-s") + 1] == "read-only"
    assert 'web_search="disabled"' in argv and 'model_reasoning_effort="medium"' in argv
    assert "shell_tool = false" in config and "unified_exec = false" in config
    assert "mcp_servers" not in config and 'web_search = "disabled"' in config
    assert 'forced_login_method = "chatgpt"' in config
    assert symlinked is True
    assert not any(k.startswith("OPENAI_") for k in req.env)
    assert json.loads(reply.text)["complete"] is True
    assert reply.usage["input_tokens"] == 12000
    # 4,000 uncached x 2.00 + 8,000 cached x 0.10 + 300 output x 10.00 per million.
    assert reply.cost_usd == pytest.approx(0.0118)
    assert not req.cwd.exists()  # scratch home and cwd are removed after the call


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-x"}, "not a ChatGPT login"),
        ({"auth_mode": "chatgpt", "OPENAI_API_KEY": "sk-x"}, "API key"),
        (None, "not found"),
    ],
)
def test_codex_judge_refuses_non_plan_login(
    bench: dict[str, ModuleType], tmp_path: Path, content: dict[str, Any] | None, match: str
) -> None:
    judge = bench["judge"]
    auth = tmp_path / "auth.json"
    if content is not None:
        auth.write_text(json.dumps(content))
    seen: list[Any] = []
    ex = judge.CliExecutor(
        judge.JUDGES["B"],
        tmp_path / "logs",
        process=_fake_process(bench, CODEX_JUDGE_STREAM, seen, last="{}"),
        parent_env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
        codex_auth=auth,
    )
    with pytest.raises(judge.JudgeCallError, match=match) as info:
        ex("PROMPT")
    assert "sk-x" not in str(info.value)
    assert seen == []  # Codex never started, so it never saw (or logged out) the login
    if content is not None:
        assert json.loads(auth.read_text()) == content


def test_judge_call_dirs_are_never_reused(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    judge = bench["judge"]
    seen: list[Any] = []
    ex = judge.CliExecutor(
        judge.JUDGES["A"],
        tmp_path / "logs",
        process=_fake_process(bench, _claude_judge_stream(), seen),
        parent_env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
        claude_config_dir=tmp_path,
    )
    ex("one")
    ex.begin(tmp_path / "logs")  # a resumed batch points at the same directory
    ex("two")
    assert sorted(p.name for p in (tmp_path / "logs").iterdir()) == ["A-1", "A-2"]


def test_claude_judge_infra_from_status_and_errors(bench: dict[str, ModuleType]) -> None:
    judge = bench["judge"]
    for result in (
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "api_error_status": 529,
            "result": "",
        },
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "errors": ["API Error: 429 rate limit"],
            "result": "",
        },
    ):
        with pytest.raises(judge.JudgeInfraError):
            judge.parse_claude_judge(_claude_judge_stream(result=result).splitlines())


def test_claude_judge_usage_fallback_and_unavailable(bench: dict[str, ModuleType]) -> None:
    judge = bench["judge"]
    base = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "",
        "structured_output": json.loads(_verdict("correct", "correct")),
    }
    with_usage = dict(
        base, usage={"input_tokens": 5, "cache_creation_input_tokens": 3000, "output_tokens": 50}
    )
    reply = judge.parse_claude_judge(_claude_judge_stream(result=with_usage).splitlines())
    assert reply.usage["total"] == 3055 and reply.meta["usage_unavailable"] is False
    bare = judge.parse_claude_judge(_claude_judge_stream(result=base).splitlines())
    assert bare.usage == {} and bare.meta["usage_unavailable"] is True
    empty = judge.parse_claude_judge(_claude_judge_stream(result=dict(base, usage={})).splitlines())
    assert empty.usage == {} and empty.meta["usage_unavailable"] is True


def test_codex_judge_isolation_and_limits(bench: dict[str, ModuleType]) -> None:
    judge = bench["judge"]
    shell = json.dumps({"type": "item.completed", "item": {"id": "x", "type": "command_execution"}})
    with pytest.raises(judge.JudgeCallError, match="isolation"):
        judge.parse_codex_judge([shell, *CODEX_JUDGE_STREAM.splitlines()], "{}")
    limit = json.dumps({"type": "turn.failed", "error": {"message": "usage_limit_exceeded"}})
    with pytest.raises(judge.JudgeInfraError):
        judge.parse_codex_judge([limit], "")
    other = json.dumps({"type": "turn.failed", "error": {"message": "bad request"}})
    with pytest.raises(judge.JudgeCallError, match="turn failed") as info:
        judge.parse_codex_judge([other], "")
    assert not isinstance(info.value, judge.JudgeInfraError)


def _bias_judgement(run_id: str, a: list[str], b: list[str]) -> dict[str, Any]:
    labels = [f"F{i}" for i in range(1, len(a) + 1)]
    return {
        "run_id": run_id,
        "status": "ok",
        "fact_verdicts": {
            f"Q.f{i}": (x if x == y else "disputed")
            for i, (x, y) in enumerate(zip(a, b, strict=True), 1)
        },
        "passes": {
            name: {
                "verdicts": dict(zip(labels, v, strict=True)),
                "fact_verdicts": {f"Q.f{i}": x for i, x in enumerate(v, 1)},
                "complete": True,
            }
            for name, v in (("A", a), ("B", b))
        },
    }


def test_judge_bias_report(bench: dict[str, ModuleType]) -> None:
    judge = bench["judge"]
    c, m = "correct", "missing"
    judgements = [
        # Claude Code answers: the Claude judge says correct, the Codex judge half.
        _bias_judgement("cc1", [c, c], [c, m]),
        _bias_judgement("cc2", [c, c], [c, m]),
        # Codex answers to the same questions: both judges agree.
        _bias_judgement("cx1", [c, m], [c, m]),
        _bias_judgement("cx2", [c, c], [c, c]),
        # A Codex only question: in the rates, not in the interaction.
        _bias_judgement("cx3", [m, m], [m, m]),
    ]
    runs = {
        "cc1": {"agent": "claude_code", "question_id": "Q1"},
        "cc2": {"agent": "claude_code", "question_id": "Q2"},
        "cx1": {"agent": "codex", "question_id": "Q1"},
        "cx2": {"agent": "codex", "question_id": "Q2"},
        "cx3": {"agent": "codex", "question_id": "Q3"},
    }
    report = judge.bias_report(judgements, runs, resamples=200)
    rates = {(r["judge_family"], r["answer_family"]): r for r in report["verdict_rates"]}
    assert rates[("anthropic", "anthropic")]["correct_rate"] == 1.0
    assert rates[("openai", "anthropic")]["correct_rate"] == 0.5
    assert rates[("openai", "anthropic")]["missing_rate"] == 0.5
    assert rates[("openai", "openai")]["runs"] == 3
    agreement = {r["answer_family"]: r["agreement_rate"] for r in report["agreement"]}
    assert agreement == {"anthropic": 0.5, "openai": 1.0}
    inter = report["family_interaction"]
    # Shared Q1, Q2: Claude gap (1.0 - 0.75) minus Codex gap (0.5 - 0.75) = 0.5.
    assert inter["estimate"] == 0.5 and inter["shared_questions"] == 2
    assert inter["runs"] == {"anthropic": 2, "openai": 2}
    assert inter == judge.bias_report(judgements, runs, resamples=200)["family_interaction"]
    # No question answered by both families: no interaction.
    only = judge.bias_report(judgements[2:], runs, resamples=10)
    assert only["family_interaction"] is None


def test_bias_bootstrap_resamples_questions_not_runs(bench: dict[str, ModuleType]) -> None:
    judge = bench["judge"]
    c, m = "correct", "missing"
    judgements, runs = [], {}
    for q in range(4):
        for rep_ in range(9):  # nine correlated runs per question and family
            a_cc = [c, c] if q % 2 else [c, m]
            for agent, verdicts in (("claude_code", (a_cc, [c, m])), ("codex", ([c, m], [c, m]))):
                rid = f"{agent}-{q}-{rep_}"
                judgements.append(_bias_judgement(rid, *verdicts))
                runs[rid] = {"agent": agent, "question_id": f"Q{q}"}
    inter = judge.bias_report(judgements, runs, resamples=500)["family_interaction"]
    assert inter["shared_questions"] == 4
    # With 4 clusters the interval stays wide; treating 36 runs as independent would not.
    assert inter["ci95"][0] <= 0.0 < inter["ci95"][1]
    assert inter["flagged"] is False


def test_audit_queue_takes_disputes_and_a_seeded_sample(bench: dict[str, ModuleType]) -> None:
    judge = bench["judge"]
    judgements = [
        {"run_id": f"r{i}", "fact_verdicts": {f"Q.f{j}": "correct" for j in range(10)}}
        for i in range(10)
    ]
    judgements[3]["fact_verdicts"]["Q.f1"] = "disputed"
    queue = judge.audit_queue(judgements)
    assert queue[0] == {"run_id": "r3", "fact_id": "Q.f1", "verdict": "disputed"}
    assert len(queue) == 1 + round(99 * 0.05)
    assert queue == judge.audit_queue(list(reversed(judgements)))


def test_control_scoring(bench: dict[str, ModuleType]) -> None:
    q = {"facts": [{"id": "CT01.f1", "value": "391"}]}
    assert bench["judge"].control_score(q, "The product is 391.") == 1.0
    assert bench["judge"].control_score(q, "I do not know") == 0.0


# --- statistics and cost -----------------------------------------------------------------------


def test_bootstrap_is_deterministic(bench: dict[str, ModuleType]) -> None:
    stats = bench["stats"]
    pairs = [(100.0, 50.0), (200.0, 80.0), (150.0, 100.0), (90.0, 60.0), (300.0, 120.0)]
    first = stats.paired_bootstrap(pairs, stats.token_ratio, resamples=2000, seed=8105)
    second = stats.paired_bootstrap(pairs, stats.token_ratio, resamples=2000, seed=8105)
    other = stats.paired_bootstrap(pairs, stats.token_ratio, resamples=2000, seed=1)
    assert first == second
    assert first != other
    assert first.estimate == pytest.approx(840 / 410)
    assert first.low <= first.estimate <= first.high
    assert stats.percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5
    assert stats.BOOTSTRAP_RESAMPLES == 10_000 and stats.BOOTSTRAP_SEED == 8105


def test_analyze_pairs_by_question(bench: dict[str, ModuleType]) -> None:
    stats = bench["stats"]
    rows = []
    for q, (web, arc) in {"A": (100, 50), "B": (200, 100), "C": (300, 100)}.items():
        for rep in (1, 2):
            rows.append(
                {
                    "agent": "codex",
                    "stratum": "lookup",
                    "arm": "web",
                    "question_id": q,
                    "total_tokens": web + rep,
                    "accuracy": 1.0,
                }
            )
            rows.append(
                {
                    "agent": "codex",
                    "stratum": "lookup",
                    "arm": "archivist",
                    "question_id": q,
                    "total_tokens": arc + rep,
                    "accuracy": 0.5,
                }
            )
    rows.append(
        {
            "agent": "codex",
            "stratum": "lookup",
            "arm": "archivist",
            "question_id": "D",
            "total_tokens": 10,
            "accuracy": 1.0,
        }
    )  # unpaired: excluded
    result = stats.analyze(rows, resamples=500, seed=8105)
    entry = next(r for r in result if r["arm"] == "archivist" and r["baseline"] == "web")
    ratio = entry["token_ratio_baseline_over_arm"]
    assert ratio["n_questions"] == 3
    assert ratio["estimate"] == pytest.approx(round(604.5 / 254.5, 4))
    assert entry["accuracy_diff_arm_minus_baseline"]["estimate"] == -0.5
    assert entry["dropped_questions"]["token_ratio_baseline_over_arm"] == ["D"]
    completion = stats.completion_rates(
        [
            {
                "agent": "codex",
                "stratum": "reach",
                "arm": "web",
                "completion": 0.0,
                "timeout": True,
            },
            {"agent": "codex", "stratum": "reach", "arm": "web", "completion": 1.0},
        ]
    )
    assert completion[0]["completion_rate"] == 0.5 and completion[0]["timeouts"] == 1


def test_cost_math(bench: dict[str, ModuleType]) -> None:
    cost = bench["cost"]
    usage = {
        "uncached_input": 1_000_000,
        "cache_write_input": 0,
        "cached_input": 1_000_000,
        "output": 100_000,
    }
    assert cost.token_cost("gpt-6.1-sol", usage) == pytest.approx(2.0 + 0.10 + 1.0)
    calls = [
        {"input_tokens": 300_000, "cached_input_tokens": 0, "output_tokens": 0},
        {"input_tokens": 100_000, "cached_input_tokens": 0, "output_tokens": 100_000},
    ]
    # 300K call at the long rate: 0.6 x 2 = 1.20; 100K/100K call standard: 0.20 + 1.00.
    assert cost.token_cost("gpt-6.1-sol", usage, per_call_usage=calls) == pytest.approx(2.40)
    usd, basis = cost.run_cost(
        "codex", "gpt-6.1-sol", usage, {"search": 3, "openPage": 4}, None, None, "chatgpt"
    )
    assert usd == pytest.approx(3.1 + 0.03)
    assert "plan allowance" in basis
    usd, basis = cost.run_cost(
        "claude_code", "claude-opus-5-5", usage, {}, None, 0.5, "subscription"
    )
    assert usd == 0.5 and "client estimate" in basis
    opus = cost.token_cost(
        "claude-opus-5-5",
        {
            "uncached_input": 2_000_000,
            "cache_write_input": 1_000_000,
            "cached_input": 0,
            "output": 0,
        },
    )
    assert opus == pytest.approx(4.0 + 5.0)
    assert cost.web_search_calls("claude_code", {"search": 1, "server_searches": 4}) == 4
    assert cost.run_cost("codex", "gpt-6.1-sol", None, {}, None, None, "chatgpt")[0] is None


def test_estimate_uses_smoke_then_pilot(bench: dict[str, ModuleType]) -> None:
    cost = bench["cost"]
    smoke = [
        {
            "agent": "codex",
            "arm": arm,
            "stratum": "lookup",
            "question_id": "LK01",
            "uncached_input": 10_000,
            "cached_input": 40_000,
            "cache_write": 0,
            "output": 1_000,
            "total": 51_000,
            "web_searches": 2 if arm != "archivist" else 0,
            "archivist_calls": 3 if arm != "web" else 0,
        }
        for arm in ("web", "archivist", "both")
    ] + [
        {
            "agent": "claude_code",
            "arm": "both",
            "stratum": "lookup",
            "question_id": "LK01",
            "uncached_input": 0,
            "cached_input": 0,
            "cache_write": 0,
            "output": 0,
            "total": 102_000,
            "web_searches": 0,
            "archivist_calls": 0,
        }
    ]
    pilot = cost.pilot_rows(
        [
            {
                "question_id": "couche-tard-8q",
                "arm": "web",
                "uncached_input_tokens": 100_000,
                "cached_input_tokens": 600_000,
                "output_tokens": 5_000,
                "total_tokens": 705_000,
                "tool_calls_observed": {"web_search_query": 6, "web_open": 6},
            },
            {
                "question_id": "couche-tard-8q",
                "arm": "mosaic",
                "uncached_input_tokens": 40_000,
                "cached_input_tokens": 170_000,
                "output_tokens": 3_000,
                "total_tokens": 213_000,
                "tool_calls_observed": {"companies_search": 1, "search": 2},
            },
            {
                "question_id": "apple-fy25-headline",
                "arm": "tuned",
                "uncached_input_tokens": 1,
                "cached_input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 3,
                "tool_calls_observed": {},
            },
        ]
    )
    assert len(pilot) == 2
    result = cost.build_estimate(
        {"codex": {"lookup": 15, "multi_period": 15, "reach": 15}, "claude_code": {"lookup": 5}},
        smoke,
        pilot,
        {"codex": "gpt-6.1-sol", "claude_code": "claude-opus-5-5"},
    )
    codex = result["agents"]["codex"]
    assert codex["runs"] == 3 * 3 * 45
    lines = {(line["stratum"], line["arm"]): line for line in codex["lines"]}
    assert lines[("lookup", "web")]["basis"].startswith("smoke")
    assert lines[("multi_period", "archivist")]["basis"].startswith("pilot")
    assert lines[("multi_period", "both")]["basis"].startswith("max(")
    assert lines[("reach", "web")]["tokens_per_run"] == 2 * 705_000
    assert result["claude_ratio"] == {"lookup/both": 2.0}
    claude = {(x["stratum"], x["arm"]): x for x in result["agents"]["claude_code"]["lines"]}
    assert "measured Claude ratio 2.00" in claude[("lookup", "both")]["basis"]
    assert "assumed equal to Codex" in claude[("lookup", "web")]["basis"]
    assert result["agents"]["claude_code"]["runs"] == 45
    assert codex["archivist_calls"] > 0


# --- review fixes -------------------------------------------------------------------------------


def _mcp_line(status: str, text: str) -> str:
    return json.dumps(
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "McpToolCall",
                    "server": "archivist",
                    "tool": "search",
                    "status": status,
                    "result": {"content": [{"type": "text", "text": text}]},
                },
            },
        }
    )


def test_codex_429_only_on_failed_results(bench: dict[str, ModuleType]) -> None:
    codex = bench["codex"]
    assert (
        codex.parse_rollout([_mcp_line("completed", "Revenue was 429 million")]).infra_reason
        is None
    )
    assert codex.parse_rollout([_mcp_line("completed", "HTTP 429 CLI_QUOTA")]).infra_reason is None
    assert codex.parse_rollout([_mcp_line("failed", "429 million")]).infra_reason is None
    assert codex.parse_rollout([_mcp_line("failed", "status 429")]).infra_reason == "archivist_429"


def test_codex_shell_and_extensions_break_isolation(bench: dict[str, ModuleType]) -> None:
    codex = bench["codex"]
    model = bench["model"]
    for tool in ("shell", "file_change", "ext:clock.sleep"):
        ex = model.Extraction(tool_calls={tool: 1})
        assert f"tool {tool} outside the arm allowlist" in codex.isolation_violations("web", ex)
    text = codex.render_config("web", model.DEFAULT_CONFIGS["codex"], Path("/x"))
    assert "shell_tool = false" in text and "unified_exec = false" in text


def test_codex_non_infra_turn_failure_is_incomplete(bench: dict[str, ModuleType]) -> None:
    codex = bench["codex"]
    ex = bench["model"].Extraction()
    codex.parse_exec_stdout(
        [json.dumps({"type": "turn.failed", "error": {"message": "context length exceeded"}})],
        ex,
    )
    assert ex.infra_reason is None
    assert ex.incomplete_reason is not None and "context length" in ex.incomplete_reason


def test_nonzero_exit_with_text_is_incomplete(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    run_dir = _run_dir(bench, tmp_path, "codex_rollout_web.jsonl", wall_s=1.0, returncode=1)
    record = bench["runner"].build_record(run_dir)
    assert record.status == "incomplete" and record.status_reason == "exit 1"


def test_invalid_run_stops_the_batch(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    runner = bench["runner"]
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": {}}))
    fake = FakeExecutor(["codex_rollout_mcp_failed.jsonl"])
    specs = [_spec(bench, arm="both"), _spec(bench), _spec(bench, arm="archivist")]
    records = runner.execute_plan(
        specs,
        tmp_path / "ev",
        max_runs=5,
        executor=fake,
        parent_env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
        codex_auth=auth,
        preflight=no_servers,
    )
    assert [r.status for r in records] == ["invalid"]
    assert len(fake.calls) == 1


def test_claude_used_tool_outside_allowlist(bench: dict[str, ModuleType]) -> None:
    cc = bench["claude_code"]
    ex = cc.parse_stream(fixture_lines("claude_stream_both.jsonl"))
    ex.init_tools = ["WebSearch", "WebFetch"]
    ex.tool_calls = dict(ex.tool_calls, Bash=1)
    problems = cc.isolation_violations("both", ex)
    assert "tool Bash used outside the arm allowlist" in problems
    assert "tool WebFetch used outside the arm allowlist" in cc.isolation_violations(
        "archivist", ex
    )


def test_claude_partial_usage_without_result(bench: dict[str, ModuleType]) -> None:
    cc = bench["claude_code"]
    lines = fixture_lines("claude_stream_both.jsonl")[:-1]  # drop the result event
    ex = cc.parse_stream(lines)
    assert ex.incomplete_reason == "no_result"
    assert ex.usage is not None
    # msg_1..msg_4 carry input 3 and output 1 (msg_5 has no usage), deduplicated by id.
    assert ex.usage.input == 12 and ex.usage.output == 4
    assert any("placeholder" in e for e in ex.errors)


def test_judge_rejects_duplicate_and_null_items(bench: dict[str, ModuleType]) -> None:
    judge = bench["judge"]
    labels = {"F1", "F2"}
    dup = json.dumps(
        {
            "facts": [
                {"id": "F1", "verdict": "correct"},
                {"id": "F1", "verdict": "missing"},
                {"id": "F2", "verdict": "correct"},
            ],
            "complete": True,
        }
    )
    with pytest.raises(ValueError, match="duplicate"):
        judge.parse_verdict(dup, labels)
    with pytest.raises(ValueError, match="not an object"):
        judge.parse_verdict(json.dumps({"facts": [None], "complete": True}), labels)
    result = judge.judge(
        QUESTION,
        "answer",
        "run-n",
        _same(lambda _p: json.dumps({"facts": [None], "complete": True})),
    )
    assert result.status == "judge_error"


def test_blind_neutralizes_codex_identifiers(bench: dict[str, ModuleType]) -> None:
    blinded = bench["redact"].blind("I called tools.web__run, then functions.exec and web__run.")
    for leak in ("web__run", "functions.exec", "tools."):
        assert leak not in blinded
    assert blinded.count("[tool]") == 3


def _judge_setup(bench: dict[str, ModuleType], tmp_path: Path, n: int) -> tuple[Path, Path]:
    evidence = tmp_path / "ev"
    data = valid_set()
    qpath = tmp_path / "q.json"
    qpath.write_text(json.dumps(data))
    for i in range(n):
        run_dir = evidence / "runs" / f"r{i}"
        run_dir.mkdir(parents=True)
        (run_dir / "answer.md").write_text("391,035")
        (run_dir / "record.json").write_text(
            json.dumps(
                {
                    "run_id": f"r{i}",
                    "run_key": f"codex.web.LK01.r{i}",
                    "status": "completed",
                    "question_id": "LK01",
                    "agent": "codex",
                    "arm": "web",
                    "stratum": "lookup",
                    "contaminated": False,
                    "usage": {"total": 100},
                    "answer_file": str(run_dir / "answer.md"),
                }
            )
        )
    return evidence, qpath


def test_judge_ceiling_and_error_batches(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = bench["cli"]
    judge = bench["judge"]
    evidence, qpath = _judge_setup(bench, tmp_path, 2)
    good = json.dumps(
        {"facts": [{"id": f"F{i}", "verdict": "correct"} for i in (1, 2, 3)], "complete": True}
    )
    calls: list[str] = []

    def execute(prompt: str) -> str:
        calls.append(prompt)
        return good

    monkeypatch.setattr(judge, "make_executors", lambda _log, **_k: _same(execute))
    base = [
        "judge",
        "--questions",
        str(qpath),
        "--evidence-dir",
        str(evidence),
        "--execute",
        "--claude-config-dir",
        str(tmp_path),
    ]
    with pytest.raises(SystemExit, match="at least 4"):
        cli.main([*base, "--max-calls", "2"])
    # Ceiling 4 with two answers: both judged (2 calls each), exit 0.
    assert cli.main([*base, "--max-calls", "4"]) == 0
    assert len(calls) == 4
    ledger = [json.loads(x) for x in (evidence / "judge-ledger.jsonl").read_text().splitlines()]
    assert [(r["pass"], r["cli"], r["status"]) for r in ledger[:4]] == [
        ("A", "claude_code", "started"),
        ("A", "claude_code", "ok"),
        ("B", "codex", "started"),
        ("B", "codex", "ok"),
    ]
    assert {r["billing_mode"] for r in ledger} == {
        "claude_plan_allowance",
        "chatgpt_plan_allowance",
    }
    # A judge_error is retried in one more batch, then left alone.
    path = evidence / "runs" / "r0" / "judgement.json"
    path.write_text(json.dumps({"run_id": "r0", "status": "judge_error", "batches": 1}))
    assert cli.main([*base, "--max-calls", "2"]) == 0
    assert json.loads(path.read_text())["batches"] == 2
    path.write_text(json.dumps({"run_id": "r0", "status": "judge_error", "batches": 2}))
    before = len(calls)
    assert cli.main([*base, "--max-calls", "10", "--force"]) == 0  # r1 forced once more
    assert len(calls) == before + 2
    assert cli.main([*base, "--max-calls", "10", "--force"]) == 0  # both at the cap
    assert len(calls) == before + 2


def test_judge_exits_nonzero_when_ceiling_leaves_answers(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = bench["cli"]
    judge = bench["judge"]
    evidence, qpath = _judge_setup(bench, tmp_path, 1)
    replies = iter(["bad", json.dumps({"facts": [], "complete": True}), "bad", "bad"])
    monkeypatch.setattr(judge, "make_executors", lambda _log, **_k: _same(lambda _p: next(replies)))
    # Two calls allowed, pass A needs a retry: the third call hits the ceiling.
    rc = cli.main(
        [
            "judge",
            "--questions",
            str(qpath),
            "--evidence-dir",
            str(evidence),
            "--execute",
            "--max-calls",
            "2",
            "--claude-config-dir",
            str(tmp_path),
        ]
    )
    assert rc == 1
    # A call ceiling says nothing about the answer: its state is restored (never judged), so
    # the ceiling cannot use up the answer's two judge batches.
    assert not (evidence / "runs" / "r0" / "judgement.json").exists()


def test_judge_crash_keeps_the_pending_batch(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = bench["cli"]
    judge = bench["judge"]
    evidence, qpath = _judge_setup(bench, tmp_path, 1)

    def crash(_p: str) -> str:
        raise RuntimeError("killed")

    monkeypatch.setattr(judge, "make_executors", lambda _log, **_k: _same(crash))
    base = [
        "judge",
        "--questions",
        str(qpath),
        "--evidence-dir",
        str(evidence),
        "--execute",
        "--claude-config-dir",
        str(tmp_path),
    ]
    with pytest.raises(RuntimeError):
        cli.main([*base, "--max-calls", "2"])
    # Persisted before the calls, so a crash cannot reset the answer's batch count.
    pending = json.loads((evidence / "runs" / "r0" / "judgement.json").read_text())
    assert pending["status"] == "judge_error" and pending["batches"] == 1
    rows = [json.loads(x) for x in (evidence / "judge-ledger.jsonl").read_text().splitlines()]
    assert [r["status"] for r in rows] == ["started"]


def test_judge_plan_limit_stops_batch_without_counting(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = bench["cli"]
    judge = bench["judge"]
    evidence, qpath = _judge_setup(bench, tmp_path, 2)
    calls: list[str] = []

    def limited(prompt: str) -> str:
        calls.append(prompt)
        raise judge.JudgeInfraError("Claude plan limit: seven_day")

    monkeypatch.setattr(judge, "make_executors", lambda _log, **_k: _same(limited))
    prior = {"run_id": "r1", "status": "judge_error", "batches": 1}
    (evidence / "runs" / "r1" / "judgement.json").write_text(json.dumps(prior))
    base = [
        "judge",
        "--questions",
        str(qpath),
        "--evidence-dir",
        str(evidence),
        "--execute",
        "--claude-config-dir",
        str(tmp_path),
    ]
    assert cli.main([*base, "--max-calls", "10"]) == 1
    assert len(calls) == 1  # first call of the first answer, then the batch stops
    # r0 had no judgement and still has none; r1 keeps its prior batch count.
    assert not (evidence / "runs" / "r0" / "judgement.json").exists()
    assert json.loads((evidence / "runs" / "r1" / "judgement.json").read_text()) == prior
    rows = [json.loads(x) for x in (evidence / "judge-ledger.jsonl").read_text().splitlines()]
    assert [r["status"] for r in rows] == ["started", "infra_error"]


def test_judge_login_error_stops_without_counting(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = bench["cli"]
    judge = bench["judge"]
    evidence, qpath = _judge_setup(bench, tmp_path, 1)
    good = json.dumps(
        {"facts": [{"id": f"F{i}", "verdict": "correct"} for i in (1, 2, 3)], "complete": True}
    )

    def bad_login(_p: str) -> str:
        raise judge.JudgeLoginError("Codex login is not a ChatGPT login")

    monkeypatch.setattr(
        judge, "make_executors", lambda _log, **_k: {"A": lambda _p: good, "B": bad_login}
    )
    base = [
        "judge",
        "--questions",
        str(qpath),
        "--evidence-dir",
        str(evidence),
        "--execute",
        "--claude-config-dir",
        str(tmp_path),
    ]
    for _ in range(3):  # repeated batches never use up the answer's judge batches
        assert cli.main([*base, "--max-calls", "4"]) == 1
    assert not (evidence / "runs" / "r0" / "judgement.json").exists()


def test_make_executors_checks_the_codex_login_first(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = bench["cli"]
    judge = bench["judge"]
    evidence, qpath = _judge_setup(bench, tmp_path, 1)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text(json.dumps({"auth_mode": "apikey"}))
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(judge.JudgeLoginError):
        judge.make_executors(tmp_path / "logs")
    base = [
        "judge",
        "--questions",
        str(qpath),
        "--evidence-dir",
        str(evidence),
        "--execute",
        "--claude-config-dir",
        str(tmp_path),
    ]
    with pytest.raises(SystemExit, match="login check failed"):
        cli.main([*base, "--max-calls", "2"])
    assert not (evidence / "judge-ledger.jsonl").exists()


def test_kill_group_escalates_past_a_sigterm_ignoring_descendant(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    runner = bench["runner"]
    monkeypatch.setattr(runner, "KILL_GRACE_S", 0.5)
    pid_file = tmp_path / "pid"
    proc = subprocess.Popen(
        ["sh", "-c", f'(trap "" TERM; echo $$ > {pid_file}; exec sleep 30) & wait'],
        start_new_session=True,
        text=True,
    )
    deadline = time.monotonic() + 5
    while not (pid_file.exists() and pid_file.read_text().strip()):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    runner._kill_group(proc)
    time.sleep(0.2)
    assert not runner._group_alive(proc.pid) or _only_zombies(proc.pid)


def _only_zombies(pgid: int) -> bool:
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if int(fields[2]) == pgid and fields[0] != "Z":
            return False
    return True


def test_judge_run_id_filter(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = bench["cli"]
    judge = bench["judge"]
    evidence, qpath = _judge_setup(bench, tmp_path, 3)
    good = json.dumps(
        {"facts": [{"id": f"F{i}", "verdict": "correct"} for i in (1, 2, 3)], "complete": True}
    )
    monkeypatch.setattr(judge, "make_executors", lambda _log, **_k: _same(lambda _p: good))
    base = [
        "judge",
        "--questions",
        str(qpath),
        "--evidence-dir",
        str(evidence),
        "--execute",
        "--claude-config-dir",
        str(tmp_path),
    ]
    assert cli.main([*base, "--max-calls", "2", "--run-id", "r1"]) == 0
    judged = sorted(p.parent.name for p in (evidence / "runs").glob("*/judgement.json"))
    assert judged == ["r1"]


def test_analyze_outputs(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = bench["cli"]
    evidence, qpath = _judge_setup(bench, tmp_path, 1)
    runs = evidence / "runs"
    specs = [
        ("w-ok", "web", "completed", 100),
        ("a-ok", "archivist", "completed", 50),
        ("a-to", "archivist", "timeout", 400),
        ("c-web", "web", "completed", 30),
    ]
    for run_id, arm, status, total in specs:
        d = runs / run_id
        d.mkdir()
        (d / "answer.md").write_text("391")
        qid = "CT01" if run_id.startswith("c-") else "LK02"
        stratum = "control" if qid == "CT01" else "lookup"
        (d / "record.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "run_key": f"codex.{arm}.{run_id}",
                    "status": status,
                    "question_id": qid,
                    "agent": "codex",
                    "arm": arm,
                    "stratum": stratum,
                    "contaminated": False,
                    "usage": {"total": total},
                    "timeout": status == "timeout",
                    "answer_file": str(d / "answer.md"),
                }
            )
        )
    (runs / "w-ok" / "judgement.json").write_text(
        json.dumps(
            {
                "run_id": "w-ok",
                "status": "ok",
                "accuracy": 1.0,
                "complete": 1.0,
                "batches": 1,
                "fact_verdicts": {},
            }
        )
    )
    (runs / "a-ok" / "judgement.json").write_text(
        json.dumps(
            {
                "run_id": "a-ok",
                "status": "ok",
                "accuracy": 1.0,
                "complete": 1.0,
                "batches": 1,
                "fact_verdicts": {},
            }
        )
    )
    assert (
        cli.main(
            [
                "analyze",
                "--questions",
                str(qpath),
                "--evidence-dir",
                str(evidence),
                "--resamples",
                "50",
            ]
        )
        == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert out["control_overhead"] == [
        {"agent": "codex", "arm": "web", "runs": 1, "mean_total_tokens": 30}
    ]
    assert [r["run_id"] for r in out["runs_without_valid_judgement"]] == ["r0"]
    # Hand written judgements carry no per pass verdicts: no bias rows, no interaction.
    assert out["judge_bias"]["family_interaction"] is None
    assert out["judge_bias"]["verdict_rates"] == []
    assert out["single_judge_sensitivity"] == []
    sens = next(
        e
        for e in out["token_sensitivity_with_partial_usage"]
        if e["stratum"] == "lookup" and e["arm"] == "archivist" and e["baseline"] == "web"
    )
    assert sens["token_ratio_baseline_over_arm"]["estimate"] == pytest.approx(100 / 225, abs=1e-4)
    main = next(
        e
        for e in out["comparisons"]
        if e["stratum"] == "lookup" and e["arm"] == "archivist" and e["baseline"] == "web"
    )
    assert main["token_ratio_baseline_over_arm"]["estimate"] == pytest.approx(2.0)
    assert main["dropped_questions"]["token_ratio_baseline_over_arm"] == ["LK01"]
    # LK01's only web run is unjudged: completion None, so LK01 is not a 0 in the comparison.
    assert main["completion_diff_arm_minus_baseline"]["estimate"] == -0.5
    assert main["completion_diff_arm_minus_baseline"]["n_questions"] == 1
    completion = {(c["stratum"], c["arm"]): c for c in out["completion"]}
    assert completion[("lookup", "web")]["runs_without_valid_judgement"] == 1
    assert completion[("lookup", "archivist")]["completion_rate"] == 0.5
    both = [e for e in out["comparisons"] if e["arm"] == "both"]
    assert {e["baseline"] for e in both} == {"web", "archivist"}
    assert all("token_ratio_baseline_over_arm" not in e for e in both)  # no both runs


def test_both_against_archivist_and_completion_difference(bench: dict[str, ModuleType]) -> None:
    stats = bench["stats"]
    rows = []
    for q in ("A", "B", "C"):
        for arm, acc, comp in (("web", 1.0, 0.0), ("archivist", 0.5, 1.0), ("both", 1.0, 1.0)):
            rows.append(
                {
                    "agent": "codex",
                    "stratum": "reach",
                    "arm": arm,
                    "question_id": q,
                    "total_tokens": 100,
                    "accuracy": acc,
                    "completion": comp,
                }
            )
    result = {(r["arm"], r["baseline"]): r for r in stats.analyze(rows, resamples=200)}
    assert result[("both", "archivist")]["accuracy_diff_arm_minus_baseline"]["estimate"] == 0.5
    assert result[("archivist", "web")]["completion_diff_arm_minus_baseline"]["estimate"] == 1.0
    assert result[("both", "web")]["token_ratio_baseline_over_arm"]["estimate"] == 1.0


# --- second review round ------------------------------------------------------------------------


def _result_line(**fields: Any) -> str:
    base = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 1,
        "result": "391",
    }
    return json.dumps(base | fields)


def test_claude_result_without_usage_is_incomplete(bench: dict[str, ModuleType]) -> None:
    cc = bench["claude_code"]
    init = fixture_lines("claude_stream_both.jsonl")[0]
    ex = cc.parse_stream([init, _result_line()])
    assert ex.usage is None and ex.incomplete_reason == "no_usage"
    ex = cc.parse_stream([init, _result_line(usage={}, modelUsage={})])
    assert ex.usage is None and ex.incomplete_reason == "no_usage"


def test_claude_result_usage_reads_thinking_and_searches(bench: dict[str, ModuleType]) -> None:
    cc = bench["claude_code"]
    init = fixture_lines("claude_stream_both.jsonl")[0]
    usage = {
        "input_tokens": 10,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "output_tokens": 50,
        "output_tokens_details": {"thinking_tokens": 30},
        "server_tool_use": {"web_search_requests": 4},
    }
    ex = cc.parse_stream([init, _result_line(usage=usage)])
    assert ex.usage is not None and ex.usage.reasoning == 30
    assert ex.web_actions["server_searches"] == 4
    step = json.dumps({"type": "assistant", "message": {"id": "m1", "content": [], "usage": usage}})
    partial = cc.parse_stream([init, step])
    assert partial.incomplete_reason == "no_result"
    assert partial.usage is not None and partial.usage.reasoning == 30
    assert partial.web_actions["server_searches"] == 4


def _call(ptype: str, name: str, call_id: str) -> str:
    return json.dumps(
        {"type": "response_item", "payload": {"type": ptype, "name": name, "call_id": call_id}}
    )


def test_codex_requested_shell_and_patch_calls_count(bench: dict[str, ModuleType]) -> None:
    codex = bench["codex"]
    lines = [
        _call("function_call", "exec_command", "c1"),
        _call("custom_tool_call", "apply_patch", "c2"),
        _call("custom_tool_call", "exec", "c3"),  # code mode wrapper: allowed
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {"type": "CommandExecution", "id": "exec-x"},
                },
            }
        ),
    ]
    ex = codex.parse_rollout(lines)
    assert ex.tool_calls == {"file_change": 1, "shell": 1}  # c1 and its item are one call
    problems = codex.isolation_violations("web", ex)
    assert "tool shell outside the arm allowlist" in problems
    assert "tool file_change outside the arm allowlist" in problems
    assert codex.parse_rollout([_call("custom_tool_call", "exec", "c3")]).tool_calls == {}


def test_isolation_wins_over_infra(bench: dict[str, ModuleType]) -> None:
    runner = bench["runner"]
    ex = bench["model"].Extraction(infra_reason="server_overloaded", tool_calls={"shell": 1})
    status, reason = runner.classify("web", "codex", ex, False, 0)
    assert status == "invalid" and "shell" in (reason or "")


def test_dropped_lists_questions_missing_in_both_arms(bench: dict[str, ModuleType]) -> None:
    stats = bench["stats"]
    rows = [
        {
            "agent": "codex",
            "stratum": "lookup",
            "arm": arm,
            "question_id": q,
            "total_tokens": tokens,
            "accuracy": None,
            "completion": None,
        }
        for q, arm, tokens in (
            ("A", "web", 100),
            ("A", "archivist", 50),
            ("B", "web", None),
            ("B", "archivist", None),
            ("C", "web", 80),
        )
    ]
    entry = next(
        e
        for e in stats.analyze(rows, resamples=50)
        if e["arm"] == "archivist" and e["baseline"] == "web"
    )
    assert entry["dropped_questions"]["token_ratio_baseline_over_arm"] == ["B", "C"]
    assert entry["dropped_questions"]["accuracy_diff_arm_minus_baseline"] == ["A", "B", "C"]


def test_analyze_refuses_duplicate_cells(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = bench["cli"]
    evidence, qpath = _judge_setup(bench, tmp_path, 1)
    dup = evidence / "runs" / "r0-again"
    dup.mkdir()
    record = json.loads((evidence / "runs" / "r0" / "record.json").read_text())
    (dup / "record.json").write_text(json.dumps(record | {"run_id": "r0-again"}))
    infra = evidence / "runs" / "r0-infra"
    infra.mkdir()
    (infra / "record.json").write_text(
        json.dumps(record | {"run_id": "r0-infra", "status": "infra_error"})
    )
    rc = cli.main(["analyze", "--questions", str(qpath), "--evidence-dir", str(evidence)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "duplicate cell codex.web.LK01.r0: r0, r0-again" in err


def test_evidence_dir_is_explicit_for_execute_judge_and_analyze(
    bench: dict[str, ModuleType], question_file: Path
) -> None:
    cli = bench["cli"]
    q = ["--questions", str(question_file)]
    with pytest.raises(SystemExit, match="evidence-dir"):
        cli.main(
            ["run", *q, "--agent", "codex", "--question", "LK01", "--execute", "--max-runs", "9"]
        )
    for cmd in (["judge", *q], ["analyze", *q]):
        with pytest.raises(SystemExit):
            cli.main(cmd)  # argparse: --evidence-dir is required
    assert cli.main(["plan", *q, "--agent", "codex", "--question", "LK01"]) == 0


def test_run_reports_skipped_cells(
    bench: dict[str, ModuleType],
    question_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    runner = bench["runner"]
    auth = tmp_path / ".codex" / "auth.json"
    auth.parent.mkdir()
    auth.write_text("{}")
    real = runner.execute_plan

    def fake_plan(specs: Any, root: Path, max_runs: int, concurrency: int, **kw: Any) -> Any:
        return real(
            specs,
            root,
            max_runs,
            concurrency,
            executor=FakeExecutor(["codex_rollout_overloaded.jsonl"]),
            parent_env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
            codex_auth=auth,
            preflight=no_servers,
            **kw,
        )

    monkeypatch.setattr(runner, "execute_plan", fake_plan)
    rc = cli.main(
        [
            "run",
            "--questions",
            str(question_file),
            "--agent",
            "codex",
            "--question",
            "LK01",
            "--arms",
            "web",
            "--repetitions",
            "2",
            "--execute",
            "--max-runs",
            "3",
            "--evidence-dir",
            str(tmp_path / "ev"),
        ]
    )
    assert rc == 1
    assert "codex.web.LK01.r2" in capsys.readouterr().err


def test_more_upstream_hosts_are_redacted(bench: dict[str, ModuleType]) -> None:
    redact = bench["redact"]
    hosts = ["financial" + "filings.com", "financial" + "reports.eu", "tmxinfo" + "services.com"]
    for host in hosts:
        text = f"see [doc](https://www.{host}/x) or {host}"
        assert redact.find_upstream_hosts(text) == [host]
        out = redact.redact_links(text)
        assert host not in out and "<url:filing-source>" in out


def test_estimate_without_multi_period_profile_raises(bench: dict[str, ModuleType]) -> None:
    cost = bench["cost"]
    with pytest.raises(ValueError, match="multi_period, arm web"):
        cost.build_estimate({"codex": {"breadth": 15}}, [], [], {"codex": "gpt-6.1-sol"})


# --- Codex campaign: v0.2.33 tools, plan window guards, resume, audit, report ---------------------


def test_v0233_tools_are_pre_approved(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    codex = bench["codex"]
    cfg = bench["model"].DEFAULT_CONFIGS["codex"]
    for arm in ("archivist", "both"):
        text = codex.render_config(arm, cfg, tmp_path)
        for tool in ("filings", "find", "companies_get", "read_passage", "search", "toc"):
            assert f'[mcp_servers.archivist.tools.{tool}]\napproval_mode = "approve"' in text
    assert "tools.filings" not in codex.render_config("web", cfg, tmp_path)


def _rollout(used: float = 41.0, balance: str = "62500", reached: str | None = None) -> str:
    """The rate limits fixture with its last window, balance and reached type replaced."""
    lines = fixture_lines("codex_rollout_rate_limits.jsonl")
    out = []
    for line in lines:
        obj = json.loads(line)
        limits = (obj.get("payload") or {}).get("rate_limits")
        if limits and line is lines[-2]:
            limits["primary"]["used_percent"] = used
            limits["credits"]["balance"] = balance
            limits["rate_limit_reached_type"] = reached
        out.append(json.dumps(obj))
    return "\n".join(out) + "\n"


def test_codex_plan_window_is_captured(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    codex = bench["codex"]
    runner = bench["runner"]
    ex = codex.parse_rollout(fixture_lines("codex_rollout_rate_limits.jsonl"))
    window = ex.plan_window
    assert window is not None
    assert window["primary"] == {
        "used_percent": 41.0,
        "window_minutes": 10080,
        "resets_at": 1791609630,
    }
    assert window["secondary"]["used_percent"] == 12.5
    assert window["credits"] == {
        "has_credits": True,
        "unlimited": False,
        "balance": "62500",
        "balance_first": "62500",
        "balance_min": "62500",
    }
    assert window["plan_type"] == "pro" and window["rate_limit_reached_type"] is None
    assert runner.window_percents(window) == [
        ("codex primary (10080 min)", 41.0),
        ("codex secondary (300 min)", 12.5),
    ]
    assert runner.credit_balance(window) == 62500.0
    run_dir = _run_dir(bench, tmp_path, "codex_rollout_rate_limits.jsonl", wall_s=1.0, returncode=0)
    record = runner.build_record(run_dir)
    assert record.status == "completed" and record.plan_window == window | {"observed_at": "t"}
    plain = runner.build_record(
        _run_dir(bench, tmp_path, "codex_rollout_web.jsonl", wall_s=1.0, returncode=0)
    )
    assert plain.plan_window is None
    budget = runner.Budget(5)
    assert runner.apply_guards(plain, budget) == [] and budget.stopped_by is None


def test_claude_plan_window_is_captured(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    cc = bench["claude_code"]
    runner = bench["runner"]
    ex = cc.parse_stream(fixture_lines("claude_stream_rate_limits.jsonl"))
    assert ex.plan_window == {
        "source": "claude_rate_limit_event",
        "status": "allowed",
        "rate_limit_type": "five_hour",
        "windows": {"five_hour": 0.41, "seven_day": 0.62},
        "window_resets": {"five_hour": 1791322800, "seven_day": 1791698400},
        "resets_at": 1791322800,
        "is_using_overage": False,
        "overage_status": "rejected",
        "overage_disabled_reason": "org_level_disabled",
        "rejected_seen": False,
    }
    assert runner.window_percents(ex.plan_window) == [
        ("claude five_hour", 41.0),
        ("claude seven_day", 62.0),
    ]
    run_dir = _run_dir(
        bench,
        tmp_path,
        "claude_stream_rate_limits.jsonl",
        agent="claude_code",
        arm="both",
        wall_s=1.0,
        returncode=0,
    )
    assert runner.build_record(run_dir).plan_window == ex.plan_window | {"observed_at": "t"}
    assert cc.parse_stream(fixture_lines("claude_stream_both.jsonl")).plan_window is None


class TextExecutor(FakeExecutor):
    """Like FakeExecutor, with the raw evidence given as text instead of a fixture name."""

    def __init__(self, texts: list[str]) -> None:
        super().__init__([])
        self.texts = list(texts)

    def __call__(self, req: Any) -> Any:
        from archivist_bench.runner import ProcessResult

        with self.lock:
            self.calls.append(req)
            text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        if req.argv[0] == "codex":
            sessions = Path(req.env["CODEX_HOME"]) / "sessions" / "2026"
            sessions.mkdir(parents=True, exist_ok=True)
            (sessions / "rollout-fake.jsonl").write_text(text)
            req.stdout_path.write_text("")
        else:
            req.stdout_path.write_text(text)
        return ProcessResult(returncode=0, timed_out=False, wall_s=1.0)


def _auth(tmp_path: Path) -> Path:
    auth = tmp_path / ".codex" / "auth.json"
    auth.parent.mkdir(exist_ok=True)
    auth.write_text(json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": {}}))
    return auth


def _execute(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    specs: list[Any],
    executor: Any,
    evidence: str = "ev",
    **kw: Any,
) -> list[Any]:
    if any(s.agent == "claude_code" for s in specs):
        kw.setdefault("claude_config_dir", tmp_path)  # the chosen Claude account (1.5.1)
    result: list[Any] = bench["runner"].execute_plan(
        specs,
        tmp_path / evidence,
        max_runs=kw.pop("max_runs", 9),
        executor=executor,
        parent_env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
        codex_auth=_auth(tmp_path),
        preflight=no_servers,
        **kw,
    )
    return result


def _reps(
    bench: dict[str, ModuleType], n: int, agent: str = "codex", arm: str = "web"
) -> list[Any]:
    return [replace(_spec(bench, agent, arm), repetition=i) for i in range(1, n + 1)]


def test_window_threshold_stops_the_batch(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = bench["runner"]
    budget = runner.Budget(9, stop_at_window=90)
    fake = TextExecutor([_rollout(41.0), _rollout(91.0), _rollout(41.0)])
    records = _execute(bench, tmp_path, _reps(bench, 3), fake, budget=budget)
    assert [r.status for r in records] == ["completed", "completed"]
    assert len(fake.calls) == 2
    assert budget.stop_kind == "plan_window" and budget.stopped_by == records[1].run_id
    err = capsys.readouterr().err
    assert records[1].run_id in err and "codex primary (10080 min) at 91%" in err
    assert any(n.startswith("plan_window_guard:") for n in records[1].notes)
    relaxed = runner.Budget(9, stop_at_window=95)
    again = _execute(
        bench, tmp_path, _reps(bench, 3), TextExecutor([_rollout(91.0)]), "ev2", budget=relaxed
    )
    assert len(again) == 3 and relaxed.stopped_by is None
    with pytest.raises(ValueError, match="1 to 100"):
        runner.Budget(9, stop_at_window=0)


def test_window_stop_exits_one_with_skipped_cells(
    bench: dict[str, ModuleType],
    question_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    runner = bench["runner"]
    real = runner.execute_plan
    fake = TextExecutor([_rollout(95.0)])

    def fake_plan(specs: Any, root: Path, max_runs: int, concurrency: int, **kw: Any) -> Any:
        return real(
            specs,
            root,
            max_runs,
            concurrency,
            executor=fake,
            parent_env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
            codex_auth=_auth(tmp_path),
            preflight=no_servers,
            **kw,
        )

    monkeypatch.setattr(runner, "execute_plan", fake_plan)
    base = ["run", "--questions", str(question_file), "--agent", "codex", "--question", "LK01"]
    rc = cli.main(
        [
            *base,
            "--arms",
            "web",
            "--execute",
            "--max-runs",
            "3",
            "--evidence-dir",
            str(tmp_path / "ev"),
        ]
    )
    assert rc == 1 and len(fake.calls) == 1
    err = capsys.readouterr().err
    assert "batch stopped after plan_window run" in err and "at 95%" in err
    assert "codex.web.LK01.r2" in err and "codex.web.LK01.r3" in err
    with pytest.raises(SystemExit):
        cli.main([*base, "--stop-at-window", "101"])


def test_metered_fallback_guard_stops_at_once(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    runner = bench["runner"]
    budget = runner.Budget(9)
    fake = TextExecutor([_rollout(balance="62500"), _rollout(balance="62400"), _rollout()])
    records = _execute(bench, tmp_path, _reps(bench, 3), fake, budget=budget)
    assert len(records) == 2 and budget.stop_kind == "metered_fallback_guard"
    note = "Codex credit balance dropped from 62500 to 62400"
    assert any(note in n and n.startswith("metered_fallback_guard:") for n in records[1].notes)
    run_dir = tmp_path / "ev" / "runs" / records[1].run_id
    assert any(note in n for n in runner.build_record(run_dir).notes)  # kept on re-extraction
    ledger = runner.load_ledger(tmp_path / "ev" / "ledger.jsonl")
    assert (
        any(note in n for n in ledger[-1].notes)
        and ledger[-1].plan_window["credits"]["balance"] == "62400"
    )

    reached = runner.Budget(9)
    one = _execute(
        bench,
        tmp_path,
        _reps(bench, 2),
        TextExecutor([_rollout(reached="primary")]),
        "ev2",
        budget=reached,
    )
    assert len(one) == 1 and reached.stop_kind == "metered_fallback_guard"
    assert any("rate_limit_reached_type=primary" in n for n in one[0].notes)

    stream = (FIXTURES / "claude_stream_rate_limits.jsonl").read_text()
    overage = stream.replace('"isUsingOverage":false', '"isUsingOverage":true', 1)
    claude = runner.Budget(9)
    out = _execute(
        bench,
        tmp_path,
        _reps(bench, 2, "claude_code", "both"),
        TextExecutor([overage]),
        "ev3",
        budget=claude,
    )
    assert len(out) == 1 and claude.stop_kind == "metered_fallback_guard"
    assert out[0].plan_window["is_using_overage"] is True  # sticky across later events
    assert any("Claude isUsingOverage true" in n for n in out[0].notes)


def test_plan_usage_limit_stops_without_rerun(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    runner = bench["runner"]
    model = bench["model"]
    budget = runner.Budget(9)
    fake = FakeExecutor(["codex_rollout_infra.jsonl"])
    records = _execute(bench, tmp_path, _reps(bench, 2), fake, budget=budget)
    assert [(r.status, r.status_reason, r.attempt) for r in records] == [
        ("infra_error", "usage_limit_exceeded", 1)
    ]
    assert budget.stop_kind == "plan_or_quota_limit" and len(fake.calls) == 1
    cc = bench["claude_code"]
    init = fixture_lines("claude_stream_both.jsonl")[0]
    cases = {
        "usage_limit:429": _result_line(subtype="error", is_error=True, api_error_status=429),
        "usage_limit:text": _result_line(
            subtype="error", is_error=True, result="Claude AI usage limit reached"
        ),
        "api_error:529": _result_line(subtype="error", is_error=True, api_error_status=529),
    }
    for reason, line in cases.items():
        assert cc.parse_stream([init, line]).infra_reason == reason
    rejected = json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}})
    assert cc.parse_stream([init, rejected]).infra_reason == "usage_limit:rate_limit_event"
    assert model.stops_batch("usage_limit:429") and model.stops_batch("usage_limit_exceeded")
    assert model.stops_batch("rate_limit_exceeded") and model.stops_batch("archivist_quota")
    for transient in ("api_error:529", "server_overloaded", "archivist_429", None):
        assert not model.stops_batch(transient)


def test_archivist_quota_stops_and_burst_reruns(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    runner = bench["runner"]
    base = (FIXTURES / "codex_rollout_archivist.jsonl").read_text()
    quota = base + _mcp_line("failed", "Error: CLI_QUOTA monthly quota exhausted") + "\n"
    budget = runner.Budget(9)
    records = _execute(
        bench, tmp_path, _reps(bench, 2, arm="archivist"), TextExecutor([quota]), budget=budget
    )
    assert [(r.status, r.status_reason) for r in records] == [("infra_error", "archivist_quota")]
    assert budget.stop_kind == "plan_or_quota_limit"
    burst = base + _mcp_line("failed", "Error: HTTP 429 RATE_LIMITED") + "\n"
    again = _execute(
        bench, tmp_path, _reps(bench, 1, arm="archivist"), TextExecutor([burst]), "ev2"
    )
    assert [r.attempt for r in again] == [1, 2, 3]
    assert all(r.status_reason == "archivist_429" for r in again)
    tool_use = json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "content": [{"type": "tool_use", "id": "t1", "name": "mcp__archivist__find"}],
            },
        }
    )
    result = json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "is_error": True,
                        "content": "Error: CLI_QUOTA exceeded",
                    }
                ]
            },
        }
    )
    assert bench["claude_code"].parse_stream([tool_use, result]).infra_reason == "archivist_quota"


def test_claude_judge_overage_is_a_stop(bench: dict[str, ModuleType]) -> None:
    judge = bench["judge"]
    event = {
        "type": "rate_limit_event",
        "rate_limit_info": {"status": "allowed", "isUsingOverage": True},
    }
    with pytest.raises(judge.JudgeInfraError, match="metered_fallback_guard"):
        judge.parse_claude_judge([json.dumps(CLAUDE_JUDGE_INIT), json.dumps(event)])
    reply = judge.parse_claude_judge(_claude_judge_stream().splitlines())
    assert reply.meta["plan_window"]["is_using_overage"] is None


def test_resume_continues_a_campaign(
    bench: dict[str, ModuleType],
    question_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    runner = bench["runner"]
    reps = _reps(bench, 4)
    _execute(bench, tmp_path, [reps[0]], FakeExecutor(["codex_rollout_web.jsonl"]))
    _execute(bench, tmp_path, [reps[1]], FakeExecutor(["codex_rollout_infra.jsonl"]))
    _execute(bench, tmp_path, [reps[2]], FakeExecutor(["codex_rollout_overloaded.jsonl"]))
    evidence = tmp_path / "ev"
    plan = runner.resume_plan(reps, runner.campaign_records(evidence))
    assert plan.done == ["codex.web.LK01.r1"]
    assert plan.excluded == ["codex.web.LK01.r3"]
    assert [s.run_key for s in plan.pending] == ["codex.web.LK01.r2", "codex.web.LK01.r4"]
    assert plan.start_attempts == {"codex.web.LK01.r2": 2}
    # Never past three attempts: a cell continuing at attempt 3 gets one more try only.
    last = _execute(
        bench,
        tmp_path,
        [reps[0]],
        FakeExecutor(["codex_rollout_overloaded.jsonl"]),
        "ev3",
        start_attempts={"codex.web.LK01.r1": 3},
    )
    assert [r.attempt for r in last] == [3]

    real = runner.execute_plan
    fake = FakeExecutor(["codex_rollout_web.jsonl"])

    def fake_plan(specs: Any, root: Path, max_runs: int, concurrency: int, **kw: Any) -> Any:
        return real(
            specs,
            root,
            max_runs,
            concurrency,
            executor=fake,
            parent_env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
            codex_auth=_auth(tmp_path),
            preflight=no_servers,
            **kw,
        )

    monkeypatch.setattr(runner, "execute_plan", fake_plan)
    base = [
        "run",
        "--questions",
        str(question_file),
        "--agent",
        "codex",
        "--question",
        "LK01",
        "--arms",
        "web",
        "--repetitions",
        "4",
        "--evidence-dir",
        str(evidence),
    ]
    with pytest.raises(SystemExit, match="--resume"):
        cli.main([*base, "--execute", "--max-runs", "9"])
    assert fake.calls == []
    capsys.readouterr()
    assert cli.main([*base, "--resume"]) == 0  # dry run shows the resume summary
    assert "1 cells done, 2 to run" in capsys.readouterr().err
    assert cli.main([*base, "--execute", "--max-runs", "2", "--resume"]) == 0
    err = capsys.readouterr().err
    assert "excluded (three infra_error attempts): codex.web.LK01.r3" in err
    new = [
        r
        for r in runner.campaign_records(evidence)
        if r.run_key in ("codex.web.LK01.r2", "codex.web.LK01.r4") and r.status == "completed"
    ]
    assert sorted((r.run_key, r.attempt) for r in new) == [
        ("codex.web.LK01.r2", 2),
        ("codex.web.LK01.r4", 1),
    ]
    assert len(fake.calls) == 2
    assert cli.main([*base, "--execute", "--max-runs", "2", "--resume"]) == 0
    assert "nothing to run" in capsys.readouterr().out


def _judged_campaign(
    bench: dict[str, ModuleType], tmp_path: Path, n: int = 5, disputed_every: int = 0
) -> tuple[Path, Path]:
    """``n`` judged LK01 runs; judge B calls F1 incorrect on r0 only (one disputed fact), or on
    every ``disputed_every``-th run."""
    judge = bench["judge"]
    evidence, qpath = _judge_setup(bench, tmp_path, n)
    (evidence / "runs" / "r0" / "answer.md").write_text(
        f"Net sales 391,035 [10-K]({UPSTREAM_URL})  \nsee {UPSTREAM_URL}\n"
    )
    question = json.loads(qpath.read_text())["questions"][0]
    all_correct = json.dumps(
        {"facts": [{"id": f"F{i}", "verdict": "correct"} for i in (1, 2, 3)], "complete": True}
    )
    one_wrong = json.dumps(
        {
            "facts": [
                {"id": "F1", "verdict": "incorrect"},
                {"id": "F2", "verdict": "correct"},
                {"id": "F3", "verdict": "correct"},
            ],
            "complete": True,
        }
    )
    for i in range(n):
        run_id = f"r{i}"
        executors = {
            "A": lambda _p: all_correct,
            "B": lambda _p, i=i: (
                one_wrong
                if (i == 0 or (disputed_every and i % disputed_every == 0))
                else all_correct
            ),
        }
        result = judge.judge(question, "391,035", run_id, executors)
        (evidence / "runs" / run_id / "judgement.json").write_text(
            json.dumps(dict(result.as_dict(), batches=1))
        )
    return evidence, qpath


def test_audit_export_queue(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import csv

    cli = bench["cli"]
    evidence, qpath = _judged_campaign(bench, tmp_path)
    out = tmp_path / "results"
    common = ["--questions", str(qpath), "--evidence-dir", str(evidence)]
    assert cli.main(["audit-export", *common, "--out", str(out)]) == 0
    with (out / "audit-queue-001.csv").open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2  # one disputed fact, round(14 * 0.05) = 1 sampled agreed fact
    disputed = rows[0]
    assert disputed["selection"] == "disputed" and disputed["run_id"] == "r0"
    assert disputed["fact_id"] == "LK01.f1" and disputed["fact_kind"] == "sourced"
    assert disputed["judge_A_verdict"] == "correct" and disputed["judge_B_verdict"] == "incorrect"
    assert disputed["permalink"].startswith("https://mosaic-finance.com/filings/")
    assert disputed["document_id_absent_reason"] and disputed["quote"]
    assert disputed["expected_value"] == "391,035" and disputed["question"] == "Q LK01?"
    assert disputed["answer_file"] == "answers/r0.md"
    assert disputed["audit_verdict"] == "" and disputed["audit_note"] == ""
    assert rows[1]["selection"] == "sample"
    answer = (out / "answers" / "r0.md").read_text()
    assert "<url:filing-source>" in answer and bench["redact"].find_upstream_hosts(answer) == []
    assert not any(line != line.rstrip() for line in answer.splitlines())
    md = (out / "audit-queue-001.md").read_text()
    assert "## r0" in md and "[permalink](https://mosaic-finance.com/filings/" in md
    assert "[answers/r0.md](answers/r0.md)" in md
    for path in out.rglob("*"):
        if path.is_file():
            assert bench["redact"].find_upstream_hosts(path.read_text()) == []

    judgement = evidence / "runs" / "r1" / "judgement.json"
    data = json.loads(judgement.read_text())
    data["fact_verdicts"]["LK01.f9"] = "disputed"
    judgement.write_text(json.dumps(data))
    capsys.readouterr()
    assert cli.main(["audit-export", *common, "--out", str(tmp_path / "bad")]) == 1
    assert "unknown fact id LK01.f9" in capsys.readouterr().err


def _write_audit(path: Path, source: Path, fills: dict[tuple[str, str], str]) -> None:
    import csv

    with source.open(newline="") as fh:
        reader = csv.DictReader(fh)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    for row in rows:
        row["audit_verdict"] = fills.pop((row["run_id"], row["fact_id"]), "")
    extra = [
        {"run_id": run_id, "fact_id": fact_id, "audit_verdict": verdict}
        for (run_id, fact_id), verdict in fills.items()
    ]
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows + extra)


def test_analyze_applies_the_audit(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = bench["cli"]
    evidence, qpath = _judged_campaign(bench, tmp_path)
    out = tmp_path / "results"
    common = ["--questions", str(qpath), "--evidence-dir", str(evidence)]
    assert cli.main(["audit-export", *common, "--out", str(out)]) == 0
    queue = out / "audit-queue-001.csv"
    analyze = ["analyze", *common, "--resamples", "20"]
    capsys.readouterr()
    assert cli.main(analyze) == 0
    plain = json.loads(capsys.readouterr().out)
    assert plain["audit_corrections"] == []

    filled = tmp_path / "filled.csv"
    _write_audit(filled, queue, {("r0", "LK01.f1"): "correct"})
    assert cli.main([*analyze, "--audit", str(filled)]) == 0
    audited = json.loads(capsys.readouterr().out)
    [correction] = audited["audit_corrections"]
    assert correction["run_id"] == "r0" and correction["fact_id"] == "LK01.f1"
    assert correction["judge_verdict"] == "disputed" and correction["judge_score"] == 0.5
    assert correction["audit_score"] == 1.0
    assert correction["run_accuracy_judged"] == pytest.approx(0.833333)
    assert correction["run_accuracy_audited"] == 1.0
    rows = bench["cli"].analysis_rows(
        evidence,
        json.loads(qpath.read_text()),
        bench["report"].corrections_by_run(audited["audit_corrections"]),
    )
    assert {r["run_id"]: r["accuracy"] for r in rows}["r0"] == 1.0
    wrong = tmp_path / "wrong.csv"
    _write_audit(wrong, queue, {("r0", "LK01.f1"): "incorrect"})
    assert cli.main([*analyze, "--audit", str(wrong)]) == 0
    assert json.loads(capsys.readouterr().out)["audit_corrections"][0]["run_accuracy_audited"] == (
        pytest.approx(0.666667)
    )

    for fills, message in (
        ({("r0", "LK01.f1"): "maybe"}, "invalid audit_verdict"),
        ({("r0", "LK01.f1"): "correct", ("nope", "LK01.f1"): "correct"}, "unknown run"),
        ({("r0", "LK01.f7"): "correct"}, "unknown fact"),
    ):
        bad = tmp_path / "bad.csv"
        _write_audit(bad, queue, dict(fills))
        assert cli.main([*analyze, "--audit", str(bad)]) == 1
        captured = capsys.readouterr()
        assert message in captured.err and captured.out == ""


def _report_campaign(
    bench: dict[str, ModuleType], tmp_path: Path
) -> tuple[Path, Path, dict[str, Any]]:
    """A small Codex campaign (LK01 and MP01, web and archivist) with raw rollouts that carry an
    upstream link, an account id and an over long string."""
    runner = bench["runner"]
    data = valid_set()
    qpath = tmp_path / "q.json"
    qpath.write_text(json.dumps(data))
    lookup = {q["id"]: q for q in data["questions"]}
    cfg = bench["model"].DEFAULT_CONFIGS["codex"]
    specs = runner.build_plan(
        [lookup["LK01"], lookup["MP01"]], "codex", cfg, 1, ("web", "archivist")
    )
    meta = json.dumps(
        {
            "type": "session_meta",
            "payload": {
                "id": "t1",
                "cli_version": "0.160.0",
                "creator_user_id": "user-SECRETID",
                "base_instructions": "x" * 5000,
                "note": f"opened {UPSTREAM_URL} for owner@example.com",
            },
        }
    )
    web = "\n".join([meta, *_rollout().splitlines()[1:]]) + "\n"
    archivist = (FIXTURES / "codex_rollout_archivist.jsonl").read_text()
    fake = TextExecutor([web, archivist, web, archivist])
    _execute(bench, tmp_path, specs, fake)
    return tmp_path / "ev", qpath, data


def test_report_is_deterministic_and_host_free(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import csv
    import gzip
    import hashlib

    cli = bench["cli"]
    redact = bench["redact"]
    evidence, qpath, _data = _report_campaign(bench, tmp_path)
    common = ["--questions", str(qpath), "--evidence-dir", str(evidence)]
    capsys.readouterr()
    assert cli.main(["analyze", *common, "--resamples", "20"]) == 0
    analysis = tmp_path / "analysis.json"
    analysis.write_text(capsys.readouterr().out)
    out_a, out_b = tmp_path / "a", tmp_path / "b"
    for out in (out_a, out_b):
        assert cli.main(["report", *common, "--analysis", str(analysis), "--out", str(out)]) == 0
    files_a = sorted(p.relative_to(out_a) for p in out_a.rglob("*") if p.is_file())
    files_b = sorted(p.relative_to(out_b) for p in out_b.rglob("*") if p.is_file())
    assert files_a == files_b
    for rel in files_a:
        assert (out_a / rel).read_bytes() == (out_b / rel).read_bytes(), rel
    names = {str(p) for p in files_a}
    for expected in (
        "runs.csv",
        "cells.csv",
        "comparisons.csv",
        "multi_period_tokens_by_filings.csv",
        "analysis.json",
        "logs/manifest.csv",
    ):
        assert expected in names
    for rel in files_a:
        raw = (out_a / rel).read_bytes()
        text = (gzip.decompress(raw) if rel.suffix == ".gz" else raw).decode()
        assert redact.find_upstream_hosts(text) == [], rel
        assert "SECRETID" not in text and "owner@example.com" not in text
        assert len(raw) < 1000 * 1024
    with (out_a / "runs.csv").open(newline="") as fh:
        runs = list(csv.DictReader(fh))
    assert len(runs) == 4 and {r["status"] for r in runs} == {"completed"}
    web_row = next(r for r in runs if r["arm"] == "web")
    assert web_row["plan_window_max_percent"] == "41"
    assert web_row["plan_windows"] == "primary (10080 min) 41; secondary (300 min) 12.5"
    assert web_row["credit_balance"] == "62500" and web_row["metered_signs"] == ""
    assert web_row["answer_file"].startswith("answers/") and web_row["log_file"].startswith("logs/")
    with (out_a / "logs" / "manifest.csv").open(newline="") as fh:
        manifest = {r["run_id"]: r for r in csv.DictReader(fh)}
    entry = manifest[web_row["run_id"]]
    raw_source = evidence / "runs" / web_row["run_id"] / entry["source"]
    assert entry["sha256"] == hashlib.sha256(raw_source.read_bytes()).hexdigest()
    assert int(entry["bytes"]) == raw_source.stat().st_size and entry["clipped_strings"] == "1"
    log = gzip.decompress((out_a / entry["log_file"]).read_bytes()).decode()
    head = json.loads(log.splitlines()[0])["payload"]
    assert head["creator_user_id"] == "<redacted>"
    digest = hashlib.sha256(("x" * 5000).encode()).hexdigest()
    assert (
        head["base_instructions"] == "x" * 4000 + f"[clipped: original 5000 chars, sha256 {digest}]"
    )
    assert "<url:filing-source>" in head["note"] and "<email>" in head["note"]
    with (out_a / "multi_period_tokens_by_filings.csv").open(newline="") as fh:
        series = list(csv.DictReader(fh))
    assert {(r["question_id"], r["arm"]) for r in series} == {
        ("MP01", "web"),
        ("MP01", "archivist"),
    }
    assert all(r["distinct_key_filings"] == "2" and r["completed_reps"] == "1" for r in series)
    with (out_a / "cells.csv").open(newline="") as fh:
        cells = list(csv.DictReader(fh))
    assert {(c["stratum"], c["arm"]) for c in cells} == {
        ("lookup", "web"),
        ("lookup", "archivist"),
        ("multi_period", "web"),
        ("multi_period", "archivist"),
    }
    with (out_a / "comparisons.csv").open(newline="") as fh:
        comparisons = list(csv.DictReader(fh))
    ratio = next(
        c
        for c in comparisons
        if c["source"] == "main"
        and c["stratum"] == "lookup"
        and c["arm"] == "archivist"
        and c["metric"] == "token_ratio_baseline_over_arm"
    )
    assert ratio["baseline"] == "web" and ratio["n_questions"] == "1"
    assert (out_a / "analysis.json").read_text() == analysis.read_text()

    capsys.readouterr()
    missing = ["report", *common, "--analysis", str(tmp_path / "none.json"), "--out", str(out_a)]
    assert cli.main(missing) == 1
    assert "analysis file not found" in capsys.readouterr().err


# --- Codex campaign review round 1 (Codex): F1 to F12 ---------------------------------------------


FAKE_APP_SERVER = """
import json, sys
log = open(sys.argv[1], "a")
mode = sys.argv[2]
for line in sys.stdin:
    msg = json.loads(line)
    log.write(json.dumps(msg) + "\\n"); log.flush()
    if mode == "hang":
        continue
    if msg.get("method") == "initialize":
        print(json.dumps({"id": msg["id"], "result": {"userAgent": "fake"}}), flush=True)
    elif msg.get("method") == "account/rateLimits/read":
        if mode == "error":
            print(json.dumps({"id": msg["id"], "error": {"message": "not logged in"}}), flush=True)
            continue
        print(json.dumps({"method": "account/rateLimits/updated", "params": {}}), flush=True)
        print(json.dumps({"id": msg["id"], "result": {
            "ordinaryUsageAllowed": True,
            "accountId": "acct-SECRET",
            "rateLimits": {"limitId": "codex", "primary": {"usedPercent": 5,
                "windowDurationMins": 10080}, "secondary": None,
                "credits": {"hasCredits": True, "unlimited": False, "balance": "62500"},
                "spendControlReached": False, "planType": "pro", "rateLimitReachedType": None},
            "rateLimitResetCredits": {"available": 1, "creditIds": ["cred-SECRET"],
                "items": [{"id": "cred-SECRET2", "amount": 5}]},
        }}), flush=True)
"""


def test_f1_probe_protocol_against_a_fake_app_server(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    probe = bench["probe"]
    script = tmp_path / "server.py"
    script.write_text(FAKE_APP_SERVER)
    log = tmp_path / "sent.jsonl"
    env = {"PATH": os.environ["PATH"]}
    report = probe.exchange([sys.executable, str(script), str(log), "ok"], env, tmp_path, 10.0)
    assert report["ok"] is True
    text = json.dumps(report)
    assert "SECRET" not in text and "accountId" not in text
    assert report["result"]["rateLimits"]["credits"]["balance"] == "62500"
    assert report["result"]["rateLimitResetCredits"] == {"available": 1, "items": [{"amount": 5}]}
    sent = [json.loads(line) for line in log.read_text().splitlines()]
    assert [m.get("method") for m in sent] == [
        "initialize",
        "initialized",
        "account/rateLimits/read",
    ]
    assert sent[0]["params"]["clientInfo"]["name"] == "archivist_bench_probe"
    assert probe.PlanGate().check_codex(report) == []
    error = probe.exchange([sys.executable, str(script), str(log), "error"], env, tmp_path, 10.0)
    assert error["ok"] is False and "not logged in" in error["error"]
    hung = probe.exchange([sys.executable, str(script), str(log), "hang"], env, tmp_path, 0.5)
    assert hung["ok"] is False and "Empty" in hung["error"]
    missing = probe.exchange(["/nonexistent/codex", "app-server"], env, tmp_path, 1.0)
    assert missing["ok"] is False
    assert probe.APP_SERVER_ARGV == ("codex", "app-server")


def _probe(**limits: Any) -> dict[str, Any]:
    out = copy.deepcopy(OK_PROBE)
    allowed = limits.pop("ordinaryUsageAllowed", True)
    out["result"]["ordinaryUsageAllowed"] = allowed
    out["result"]["rateLimits"].update(limits)
    return out


def test_f1_codex_gate_rules(bench: dict[str, ModuleType]) -> None:
    probe = bench["probe"]
    gate = probe.PlanGate(90)
    assert gate.check_codex(copy.deepcopy(OK_PROBE)) == [] and gate.baseline == 62500.0
    cases = [
        ({"ok": False, "error": "timeout"}, "plan probe failed"),
        (_probe(ordinaryUsageAllowed=False), "ordinaryUsageAllowed=False"),
        (_probe(primary={"usedPercent": 90, "windowDurationMins": 10080}), "at 90%"),
        (_probe(secondary={"usedPercent": 97, "windowDurationMins": 300}), "secondary"),
        (_probe(rateLimitReachedType="primary"), "rateLimitReachedType=primary"),
        (_probe(spendControlReached=True), "spendControlReached"),
        (
            _probe(credits={"hasCredits": True, "balance": "62400"}),
            "62400 below the batch baseline 62500",
        ),
        ({"ok": True, "result": {"ordinaryUsageAllowed": True}}, "no rateLimits"),
    ]
    for report, reason in cases:
        found = gate.check_codex(report)
        assert any(reason in r for r in found), (reason, found)
    assert probe.PlanGate(95).check_codex(_probe(primary={"usedPercent": 94})) == []


def test_f1_codex_launch_is_refused_before_spending(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = bench["runner"]
    probe = bench["probe"]
    for name, report in (
        ("hot", _probe(primary={"usedPercent": 95, "windowDurationMins": 10080})),
        ("failed", {"ok": False, "error": "TimeoutExpired"}),
        ("spend", _probe(spendControlReached=True)),
    ):
        monkeypatch.setattr(probe, "app_server_probe", lambda _e, _c, r=report: copy.deepcopy(r))
        budget = runner.Budget(9)
        fake = FakeExecutor(["codex_rollout_web.jsonl"])
        records = _execute(bench, tmp_path, _reps(bench, 2), fake, name, budget=budget)
        assert records == [] and fake.calls == [], name
        assert budget.stop_kind == "plan_gate"
        [run_dir] = list((tmp_path / name / "runs").iterdir())
        assert (run_dir / "dispatch-refused.json").exists()
        assert json.loads((run_dir / "plan-probe.json").read_text()) == report
        # A refused launch ran nothing: it is not an attempt.
        assert runner.campaign_records(tmp_path / name) == []
        plan = runner.resume_plan(_reps(bench, 2), runner.campaign_records(tmp_path / name))
        assert plan.start_attempts == {} and len(plan.pending) == 2
    seen: list[str] = []
    monkeypatch.setattr(
        probe, "app_server_probe", lambda env, _c: seen.append(env["CODEX_HOME"]) or OK_PROBE
    )
    _execute(bench, tmp_path, _reps(bench, 2), FakeExecutor(["codex_rollout_web.jsonl"]), "ok")
    # Two pre launch probes, then the closing probe after the batch's last launch.
    assert len(seen) == 3 and all(h.endswith("codex-home") for h in seen)
    assert seen[-1].endswith("closing-probe/codex-home")
    assert json.loads((tmp_path / "ok" / "closing-probe.json").read_text())["reasons"] == []


NOW = datetime(2026, 10, 7, 2, 10, tzinfo=UTC)
GOOD_CLAUDE = {
    "status": "allowed",
    "windows": {"five_hour": 0.4, "seven_day": 0.6},
    "window_resets": {"five_hour": 4102444800, "seven_day": 4102444800},
    "is_using_overage": False,
    "overage_status": "rejected",
    "overage_disabled_reason": "org_level_disabled",
    "observed_at": "2026-10-07T02:00:00Z",
}


def test_f1_claude_gate_rules_and_seed(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    probe = bench["probe"]
    runner = bench["runner"]
    gate = probe.PlanGate(90, clock=lambda: NOW)
    gate.observe_claude(GOOD_CLAUDE)
    assert gate.check_claude() == [] and gate.claude_probes == 0  # fresh reading, no probe
    for change, reason in (
        ({"status": "rejected"}, "rejected"),
        ({"rejected_seen": True}, "rejected"),
        ({"windows": {"five_hour": 0.9}}, "five_hour at 90%"),
        ({"is_using_overage": True}, "isUsingOverage"),
        ({"overage_status": "allowed"}, "paid fallback is not ruled out"),
        ({"overage_status": None}, "paid fallback is not ruled out"),
    ):
        gate.observe_claude(GOOD_CLAUDE | change)
        assert any(reason in r for r in gate.check_claude()), change
    # Seeded from the campaign: the newest Claude reading (agent record or judge call) wins.
    evidence = tmp_path / "ev"
    old = evidence / "runs" / "a"
    old.mkdir(parents=True)
    early = GOOD_CLAUDE | {"observed_at": "2026-10-07T01:55:00Z"}
    account = str(tmp_path)  # the chosen Claude account (harness 1.5.1) tags every reading
    (old / "record.json").write_text(
        json.dumps(
            {
                "agent": "claude_code",
                "started_at": "x",
                "plan_window": early,
                "claude_config_dir": account,
            }
        )
    )
    (evidence / "judge-ledger.jsonl").write_text(
        json.dumps(
            {
                "cli": "claude_code",
                "started_at": "2026-10-07T02:00:00Z",
                "meta": {
                    "plan_window": GOOD_CLAUDE | {"overage_status": "allowed"},
                    "claude_config_dir": account,
                },
            }
        )
        + "\n"
    )
    assert probe.latest_claude_window(evidence, tmp_path)["overage_status"] == "allowed"
    budget = runner.Budget(9)
    budget.gate.clock = lambda: NOW
    fake = FakeExecutor(["claude_stream_both.jsonl"])
    records = _execute(
        bench, tmp_path, _reps(bench, 2, "claude_code", "both"), fake, "ev", budget=budget
    )
    assert records == [] and fake.calls == [] and budget.stop_kind == "plan_gate"
    assert [p.name for p in (evidence / "runs").iterdir()] == ["a"]  # nothing launched
    assert budget.used == 0  # the reservation was released


def test_f1_judges_are_gated(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    judge = bench["judge"]
    probe = bench["probe"]
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None}))
    seen: list[Any] = []
    parent = {"PATH": "/usr/bin", "HOME": str(tmp_path)}
    codex_judge = judge.CliExecutor(
        judge.JUDGES["B"],
        tmp_path / "logs",
        process=_fake_process(bench, CODEX_JUDGE_STREAM, seen, last=_verdict("correct", "missing")),
        parent_env=parent,
        codex_auth=auth,
        plan_probe=lambda _e, _c: _probe(spendControlReached=True),
    )
    with pytest.raises(judge.JudgeInfraError, match="spendControlReached"):
        codex_judge("PROMPT")
    assert seen == []  # nothing launched
    [call_dir] = list((tmp_path / "logs").iterdir())
    assert json.loads((call_dir / "plan-probe.json").read_text())["result"]["rateLimits"][
        "spendControlReached"
    ]
    ok = judge.CliExecutor(
        judge.JUDGES["B"],
        tmp_path / "logs2",
        process=_fake_process(bench, CODEX_JUDGE_STREAM, seen, last=_verdict("correct", "missing")),
        parent_env=parent,
        codex_auth=auth,
    )
    assert ok("PROMPT").text  # the default (faked) live probe passes
    gate = probe.PlanGate()
    gate.configure(probe.claude_probe_env(parent, tmp_path), None, None)  # the chosen account
    gate.observe_claude(
        GOOD_CLAUDE | {"overage_status": "allowed", "observed_at": probe.stamp(probe.now_utc())}
    )
    claude_judge = judge.CliExecutor(
        judge.JUDGES["A"],
        tmp_path / "logs3",
        process=_fake_process(bench, _claude_judge_stream(), seen),
        parent_env=parent,
        gate=gate,
        claude_config_dir=tmp_path,
    )
    with pytest.raises(judge.JudgeInfraError, match="paid fallback"):
        claude_judge("PROMPT")
    gate.observe_claude(GOOD_CLAUDE | {"observed_at": probe.stamp(probe.now_utc())})
    assert claude_judge("PROMPT").text
    assert gate.claude_window["windows"] == {"five_hour": 0.25}  # observed from the call


def test_f1_judge_command_shares_one_gate(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = bench["cli"]
    judge = bench["judge"]
    evidence, qpath = _judge_setup(bench, tmp_path, 1)
    good = json.dumps(
        {"facts": [{"id": f"F{i}", "verdict": "correct"} for i in (1, 2, 3)], "complete": True}
    )
    gates: list[Any] = []

    class Gated:
        gate: Any = None

        def __call__(self, prompt: str) -> str:
            gates.append(self.gate)
            return good

    monkeypatch.setattr(judge, "make_executors", lambda _log, **_k: {"A": Gated(), "B": Gated()})
    base = [
        "judge",
        "--questions",
        str(qpath),
        "--evidence-dir",
        str(evidence),
        "--execute",
        "--claude-config-dir",
        str(tmp_path),
    ]
    assert cli.main([*base, "--max-calls", "2", "--stop-at-window", "75"]) == 0
    assert len(gates) == 2 and gates[0] is gates[1] and gates[0].threshold == 75
    with pytest.raises(SystemExit):
        cli.main([*base, "--max-calls", "2", "--stop-at-window", "0"])


def test_f2_codex_keeps_first_min_and_last_balance(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    codex = bench["codex"]
    runner = bench["runner"]
    lines = fixture_lines("codex_rollout_rate_limits.jsonl")
    balances = iter(["62500", "62300", "62400"])
    out = []
    for line in lines:
        obj = json.loads(line)
        limits = (obj.get("payload") or {}).get("rate_limits")
        if limits:
            limits["credits"]["balance"] = next(balances)
        out.append(json.dumps(obj))
    credits = codex.parse_rollout(out).plan_window["credits"]
    assert (credits["balance_first"], credits["balance_min"], credits["balance"]) == (
        "62500",
        "62300",
        "62400",
    )
    budget = runner.Budget(9)
    records = _execute(
        bench, tmp_path, _reps(bench, 2), TextExecutor(["\n".join(out)]), budget=budget
    )
    # The first run of the batch catches its own drop against the probe baseline.
    assert len(records) == 1 and budget.stop_kind == "metered_fallback_guard"
    assert any("dropped from 62500 to 62300" in n for n in records[0].notes)


def test_f3_stop_reasons_win_and_stick(bench: dict[str, ModuleType]) -> None:
    model = bench["model"]
    codex = bench["codex"]
    cc = bench["claude_code"]
    assert model.merge_infra(None, "server_overloaded") == "server_overloaded"
    assert model.merge_infra("server_overloaded", "archivist_quota") == "archivist_quota"
    assert model.merge_infra("archivist_quota", "server_overloaded") == "archivist_quota"
    assert model.merge_infra("usage_limit:429", "archivist_quota") == "usage_limit:429"
    quota = _mcp_line("failed", "Error: CLI_QUOTA exhausted")
    overloaded = json.dumps(
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "error": {"message": "overloaded", "codex_error_info": "server_overloaded"},
            },
        }
    )
    assert codex.parse_rollout([quota, overloaded]).infra_reason == "archivist_quota"
    assert codex.parse_rollout([overloaded, quota]).infra_reason == "archivist_quota"
    ex = codex.parse_rollout([overloaded])
    codex.parse_exec_stdout(
        [json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit"}})],
        ex,
    )
    assert ex.infra_reason == "usage_limit_exceeded"
    init = fixture_lines("claude_stream_both.jsonl")[0]
    burst = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "m1",
                    "content": [{"type": "tool_use", "id": "t1", "name": "mcp__archivist__search"}],
                },
            }
        ),
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "is_error": True,
                            "content": "HTTP 429 RATE_LIMITED",
                        }
                    ]
                },
            }
        ),
    ]
    limited = _result_line(subtype="error", is_error=True, api_error_status=429)
    assert cc.parse_stream([init, *burst, limited]).infra_reason == "usage_limit:429"
    quota_stream = [burst[0], burst[1].replace("HTTP 429 RATE_LIMITED", "CLI_QUOTA")]
    transient = _result_line(subtype="error", is_error=True, api_error_status=529)
    assert cc.parse_stream([init, *quota_stream, transient]).infra_reason == "archivist_quota"


def test_f4_codex_rate_limit_errors_stop_the_batch(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    codex = bench["codex"]
    model = bench["model"]
    runner = bench["runner"]
    for event, reason in (
        (
            {
                "type": "turn.failed",
                "error": {
                    "message": "Rate limit reached",
                    "codex_error_info": "rate_limit_exceeded",
                },
            },
            "rate_limit_exceeded",
        ),
        (
            {"type": "turn.failed", "error": {"message": "Rate limit reached for gpt"}},
            "rate_limit_exceeded",
        ),
        ({"type": "error", "message": "You've hit your usage limit."}, "usage_limit_exceeded"),
        (
            {"type": "error", "message": "x", "codex_error_info": "usage_limit_exceeded"},
            "usage_limit_exceeded",
        ),
    ):
        ex = model.Extraction()
        codex.parse_exec_stdout([json.dumps(event)], ex)
        assert ex.infra_reason == reason and model.stops_batch(reason), event
    rollout_error = json.dumps(
        {"type": "event_msg", "payload": {"type": "error", "message": "Rate limit reached"}}
    )
    assert codex.parse_rollout([rollout_error]).infra_reason == "rate_limit_exceeded"
    text_only = json.dumps(
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "error": {"message": "usage limit hit"}},
        }
    )
    assert codex.parse_rollout([text_only]).infra_reason == "usage_limit_exceeded"
    base = (FIXTURES / "codex_rollout_web.jsonl").read_text()

    class StdoutExecutor(TextExecutor):
        def __call__(self, req: Any) -> Any:
            result = super().__call__(req)
            req.stdout_path.write_text(
                json.dumps(
                    {
                        "type": "turn.failed",
                        "error": {
                            "message": "Rate limit reached",
                            "codex_error_info": "rate_limit_exceeded",
                        },
                    }
                )
                + "\n"
            )
            return result

    budget = runner.Budget(9)
    records = _execute(bench, tmp_path, _reps(bench, 2), StdoutExecutor([base]), budget=budget)
    assert [(r.status, r.status_reason) for r in records] == [
        ("infra_error", "rate_limit_exceeded")
    ]
    assert budget.stop_kind == "plan_or_quota_limit"


def test_f5_claude_rejection_stops_even_after_success(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    runner = bench["runner"]
    stream = (FIXTURES / "claude_stream_rate_limits.jsonl").read_text()
    rejected = stream.replace('"status":"allowed"', '"status":"rejected"', 1)
    budget = runner.Budget(9)
    records = _execute(
        bench,
        tmp_path,
        _reps(bench, 2, "claude_code", "both"),
        TextExecutor([rejected]),
        budget=budget,
    )
    assert [r.status for r in records] == ["completed"]  # the answer stays completed
    assert records[0].plan_window["rejected_seen"] is True
    assert budget.stop_kind == "plan_or_quota_limit"
    assert any(n.startswith("plan_rejected_guard:") for n in records[0].notes)


def test_f6_interrupted_attempts_count_on_resume(
    bench: dict[str, ModuleType],
    question_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    runner = bench["runner"]
    evidence = tmp_path / "ev"
    reps = _reps(bench, 2)
    for spec, attempt in ((reps[0], 1), (reps[1], 3)):
        run_dir = evidence / "runs" / f"20261007T000000000000Z-{spec.run_key}-a{attempt}"
        run_dir.mkdir(parents=True)
        runner.write_meta(
            run_dir,
            spec,
            run_dir.name,
            attempt,
            started_at="2026-10-07T00:00:00Z",
            dispatched_at="2026-10-07T00:00:01Z",
        )
    refused = evidence / "runs" / "20261007T000000000001Z-codex.web.LK01.r1-a2"
    refused.mkdir()
    runner.write_meta(refused, reps[0], refused.name, 2, started_at="t")
    (refused / "dispatch-refused.json").write_text("{}")
    records = runner.campaign_records(evidence)
    assert sorted((r.run_key, r.attempt, r.status_reason) for r in records) == [
        ("codex.web.LK01.r1", 1, "interrupted"),
        ("codex.web.LK01.r2", 3, "interrupted"),
    ]
    plan = runner.resume_plan(reps, records)
    assert plan.start_attempts == {"codex.web.LK01.r1": 2}
    assert plan.excluded == ["codex.web.LK01.r2"]
    real = runner.execute_plan
    fake = FakeExecutor(["codex_rollout_web.jsonl"])

    def fake_plan(specs: Any, root: Path, max_runs: int, concurrency: int, **kw: Any) -> Any:
        return real(
            specs,
            root,
            max_runs,
            concurrency,
            executor=fake,
            parent_env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
            codex_auth=_auth(tmp_path),
            preflight=no_servers,
            **kw,
        )

    monkeypatch.setattr(runner, "execute_plan", fake_plan)
    base = [
        "run",
        "--questions",
        str(question_file),
        "--agent",
        "codex",
        "--question",
        "LK01",
        "--arms",
        "web",
        "--repetitions",
        "2",
        "--evidence-dir",
        str(evidence),
    ]
    assert cli.main([*base, "--execute", "--max-runs", "3", "--resume"]) == 0
    err = capsys.readouterr().err
    assert err.count("reconciled interrupted attempt") == 2
    ledger = runner.load_ledger(evidence / "ledger.jsonl")
    interrupted = [r for r in ledger if r.status_reason == "interrupted"]
    assert len(interrupted) == 2 and all(r.status == "infra_error" for r in interrupted)
    done = [r for r in ledger if r.status == "completed"]
    assert [(r.run_key, r.attempt) for r in done] == [("codex.web.LK01.r1", 2)]
    assert len(fake.calls) == 1 and not (refused / "record.json").exists()


def test_f7_duplicate_questions_and_keys_are_refused(
    bench: dict[str, ModuleType], question_file: Path, tmp_path: Path
) -> None:
    cli = bench["cli"]
    runner = bench["runner"]
    q = ["--questions", str(question_file), "--agent", "codex", "--arms", "web"]
    with pytest.raises(SystemExit, match="duplicate --question ids: LK04"):
        cli.main(["run", *q, "--question", "LK04", "--question", "LK04"])
    with pytest.raises(ValueError, match="duplicate planned run keys"):
        runner.execute_plan([_spec(bench), _spec(bench)], tmp_path / "ev", max_runs=5)
    assert not (tmp_path / "ev").exists()


def test_f8_audit_queue_is_sharded(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import csv

    cli = bench["cli"]
    report = bench["report"]
    evidence, qpath = _judged_campaign(bench, tmp_path, n=24, disputed_every=2)
    data = json.loads(qpath.read_text())
    records = {r["run_id"]: r for r in cli._records(evidence)}
    out = tmp_path / "results"
    rows = report.export_audit(evidence, data, records, out, 8105, shard_bytes=6000)
    shards = sorted(out.glob("audit-queue-*.csv"))
    assert len(shards) > 1 and [p.name for p in shards][0] == "audit-queue-001.csv"
    for path in shards + sorted(out.glob("audit-queue-*.md")):
        assert path.stat().st_size < 6000, path
    index = (out / "audit-index.md").read_text()
    assert all(p.name in index for p in shards)
    seen: list[tuple[str, str]] = []
    for path in shards:
        with path.open(newline="") as fh:
            seen += [(r["run_id"], r["fact_id"]) for r in csv.DictReader(fh)]
    assert sorted(seen) == sorted((r["run_id"], r["fact_id"]) for r in rows)
    again = tmp_path / "again"
    report.export_audit(evidence, data, records, again, 8105, shard_bytes=6000)
    for path in out.glob("audit-*"):
        assert path.read_bytes() == (again / path.name).read_bytes()  # deterministic
    # Fill one row in the second shard and apply the whole directory.
    with shards[1].open(newline="") as fh:
        reader = csv.DictReader(fh)
        fields, filled = list(reader.fieldnames or []), list(reader)
    target = next(r for r in filled if r["selection"] == "disputed")
    target["audit_verdict"] = "correct"
    with shards[1].open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(filled)
    common = ["--questions", str(qpath), "--evidence-dir", str(evidence), "--resamples", "20"]
    capsys.readouterr()
    assert cli.main(["analyze", *common, "--audit", str(out)]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert [(c["run_id"], c["fact_id"]) for c in applied["audit_corrections"]] == [
        (target["run_id"], target["fact_id"])
    ]
    assert [a["name"] for a in applied["audit_files"]] == [p.name for p in shards]
    # A conflicting verdict in another file applies nothing.
    conflict = tmp_path / "conflict.csv"
    conflict.write_text(
        f"run_id,fact_id,audit_verdict\n{target['run_id']},{target['fact_id']},incorrect\n"
    )
    assert cli.main(["analyze", *common, "--audit", str(out), str(conflict)]) == 1
    captured = capsys.readouterr()
    assert "conflicting audit verdicts" in captured.err and captured.out == ""
    # Human audit verdicts are never overwritten by a new export.
    with pytest.raises(report.AuditError, match="human audit verdicts"):
        report.export_audit(evidence, data, records, out, 8105, shard_bytes=6000)


def test_f9_report_is_bound_to_its_evidence(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = bench["cli"]
    evidence, qpath, _data = _report_campaign(bench, tmp_path)
    common = ["--questions", str(qpath), "--evidence-dir", str(evidence)]
    capsys.readouterr()
    assert cli.main(["analyze", *common, "--resamples", "20"]) == 0
    analysis = json.loads(capsys.readouterr().out)
    assert analysis["harness_version"] == sys.modules["archivist_bench"].__version__ == "1.6.0"
    assert len(analysis["evidence_fingerprint"]) == 64
    path = tmp_path / "analysis.json"
    out = ["--out", str(tmp_path / "out")]

    def report_with(content: dict[str, Any]) -> tuple[int, str]:
        path.write_text(json.dumps(content))
        capsys.readouterr()
        rc = cli.main(["report", *common, "--analysis", str(path), *out])
        return rc, capsys.readouterr().err

    assert report_with(analysis)[0] == 0
    assert "no comparisons" in report_with({"comparisons": []})[1]
    no_print = {k: v for k, v in analysis.items() if k != "evidence_fingerprint"}
    assert "no evidence_fingerprint" in report_with(no_print)[1]
    run_dir = next((evidence / "runs").iterdir())
    (run_dir / "judgement.json").write_text(json.dumps({"run_id": run_dir.name, "status": "ok"}))
    rc, err = report_with(analysis)
    assert rc == 1 and "evidence changed" in err
    (run_dir / "judgement.json").unlink()
    duplicate = evidence / "runs" / "zz-duplicate"
    duplicate.mkdir()
    record = json.loads((run_dir / "record.json").read_text())
    (duplicate / "record.json").write_text(json.dumps(record | {"run_id": "zz-duplicate"}))
    rc, err = report_with(analysis)
    assert rc == 1 and "duplicate cells" in err


def test_f10_scrub_masks_secrets_everywhere(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    import gzip

    report = bench["report"]
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl"  # gitleaks:allow (synthetic token: header alg HS256, payload sub 1234)
    raw_line = {
        "access_token": "plain-access-value",
        "api_key": 12345,
        "accountId": 987654,
        "account_id": 1,
        "userId": "u-77",
        "Cookie": "c=1",
        "auth": {"tokens": {"id_token": "idt", "refresh_token": "rft"}, "OPENAI_API_KEY": "k"},
        "input_tokens": 1200,
        "total_tokens": 1300,
        "nested": json.dumps({"accountId": 42, "note": "Authorization: Bearer abcdefghijkl"}),
        "text": (
            f"key sk-abcdefghijklmn and ak_ABCDEFGH1234 and mst_abcdefgh123 and {jwt} at "
            "https://x.example.com/cb?access_token=zzz&x=1 then refresh_token=qqq, mail a@b.co"
        ),
    }
    packed, clipped = report.scrub_log((json.dumps(raw_line) + "\n").encode())
    text = gzip.decompress(packed).decode()
    out = json.loads(text)
    for secret in (
        "plain-access-value",
        "12345",
        "987654",
        "u-77",
        "c=1",
        "idt",
        "rft",
        "abcdefghijkl",
        "sk-abcdefghijklmn",
        "ak_ABCDEFGH1234",
        "mst_abcdefgh123",
        jwt,
        "zzz",
        "qqq",
        "a@b.co",
    ):
        assert secret not in text, secret
    assert out["input_tokens"] == 1200 and out["total_tokens"] == 1300
    assert out["account_id"] == "<redacted>" and out["accountId"] == "<redacted>"
    assert json.loads(out["nested"])["accountId"] == "<redacted>"
    assert report.check_files([]) == []
    leaky = tmp_path / "leaky.jsonl.gz"
    leaky.write_bytes(
        gzip.compress(b'{"h": "Authorization: Bearer AbCdEfGhIjKlMn", "m": "x@y.io"}\n')
    )
    clean = tmp_path / "clean.jsonl.gz"
    clean.write_bytes(packed)
    csv_file = tmp_path / "runs.csv"
    csv_file.write_text("a,b\nsk-abcdefghijklmn,1\n")
    problems = report.check_files([leaky, clean, csv_file])
    assert any("leaky" in p and "bearer" in p and "email" in p for p in problems)
    assert any("runs.csv" in p and "sk key" in p for p in problems)
    assert not any("clean" in p for p in problems)


def test_f11_runs_csv_keeps_guard_evidence(bench: dict[str, ModuleType]) -> None:
    report = bench["report"]
    record = {
        "run_id": "r1",
        "agent": "codex",
        "arm": "web",
        "stratum": "lookup",
        "question_id": "LK01",
        "usage": {"total": 10},
        "billing_mode": "chatgpt_plan_allowance",
        "notes": [
            "mcp_servers=x",
            "metered_fallback_guard: Codex credit balance dropped from 62500 to 62300",
            "plan_window_guard: codex primary (10080 min) at 91% (threshold 90%)",
        ],
        "plan_window": {
            "source": "codex_rollout_rate_limits",
            "primary": {"used_percent": 91.0, "window_minutes": 10080},
            "credits": {"balance": "62400", "balance_first": "62500", "balance_min": "62300"},
        },
    }
    [row] = report.run_rows([record], {}, {}, {})
    assert "metered_fallback_guard: Codex credit balance dropped" in row["guard_notes"]
    assert "plan_window_guard" in row["guard_notes"] and "mcp_servers" not in row["guard_notes"]
    assert "credit balance dropped 62500 to 62300" in row["metered_signs"]
    assert "Codex credit balance dropped from 62500 to 62300" in row["metered_signs"]
    assert "guard_notes" in report.RUN_COLUMNS and "metered_signs" in report.RUN_COLUMNS


def test_f12_readme_reports_the_audited_analysis() -> None:
    text = HARNESS_README.read_text()
    assert "--analysis analysis-audited.json --audit" in text
    assert "not an account level prevention" in text or "cannot prevent paid fallback" in text
    for term in (
        "account/rateLimits/read",
        "judge --stop-at-window",
        "audit-queue-001.csv",
        "evidence_fingerprint",
    ):
        assert term in text, term


# --- Codex campaign review round 2 (Codex): R1 to R10 ---------------------------------------------


def _probe_sequence(
    monkeypatch: pytest.MonkeyPatch, bench: dict[str, ModuleType], reports: list[dict[str, Any]]
) -> list[str]:
    """Live probe replaced by a sequence (the last one repeats); returns the probed homes."""
    seen: list[str] = []
    queue = list(reports)

    def fake(env: dict[str, str], cwd: Path) -> dict[str, Any]:
        seen.append(env.get("CODEX_HOME", ""))
        return copy.deepcopy(queue.pop(0) if len(queue) > 1 else queue[0])

    monkeypatch.setattr(bench["probe"], "app_server_probe", fake)
    return seen


def test_r1a_closing_probe_reports_a_drop(
    bench: dict[str, ModuleType],
    question_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = bench["runner"]
    cli = bench["cli"]
    dropped = _probe(credits={"hasCredits": True, "balance": "62450"})
    seen = _probe_sequence(monkeypatch, bench, [OK_PROBE, OK_PROBE, dropped])
    budget = runner.Budget(9)
    records = _execute(
        bench, tmp_path, _reps(bench, 2), FakeExecutor(["codex_rollout_web.jsonl"]), budget=budget
    )
    assert [r.status for r in records] == ["completed", "completed"]
    assert seen[-1].endswith("closing-probe/codex-home") and len(seen) == 3
    closing = json.loads((tmp_path / "ev" / "closing-probe.json").read_text())
    assert any("62450 below the batch baseline 62500" in r for r in closing["reasons"])
    assert budget.stop_kind == "closing_probe"
    assert "batch stopped after closing_probe" in capsys.readouterr().err
    rows = [json.loads(x) for x in (tmp_path / "ev" / "plan-probes.jsonl").read_text().splitlines()]
    assert rows[-1]["kind"] == "codex_closing" and rows[-1]["reasons"]
    # No Codex launch, no closing probe.
    seen.clear()
    _execute(
        bench,
        tmp_path,
        _reps(bench, 1, "claude_code", "both"),
        FakeExecutor(["claude_stream_both.jsonl"]),
        "claude",
    )
    assert seen == [] and not (tmp_path / "claude" / "closing-probe.json").exists()
    # The command exits 1 and lists the closing probe with the batch.
    real = runner.execute_plan
    _probe_sequence(monkeypatch, bench, [OK_PROBE, dropped])

    def fake_plan(specs: Any, root: Path, max_runs: int, concurrency: int, **kw: Any) -> Any:
        return real(
            specs,
            root,
            max_runs,
            concurrency,
            executor=FakeExecutor(["codex_rollout_web.jsonl"]),
            parent_env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
            codex_auth=_auth(tmp_path),
            preflight=no_servers,
            **kw,
        )

    monkeypatch.setattr(runner, "execute_plan", fake_plan)
    rc = cli.main(
        [
            "run",
            "--questions",
            str(question_file),
            "--agent",
            "codex",
            "--question",
            "LK01",
            "--arms",
            "web",
            "--repetitions",
            "1",
            "--execute",
            "--max-runs",
            "1",
            "--evidence-dir",
            str(tmp_path / "cli"),
        ]
    )
    err = capsys.readouterr().err
    assert rc == 1 and "closing probe: Codex credit balance 62450 below" in err


def test_r1a_judge_closing_probe(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    judge = bench["judge"]
    cli = bench["cli"]
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None}))
    reports = iter([OK_PROBE, _probe(primary={"usedPercent": 93, "windowDurationMins": 10080})])
    seen: list[Any] = []
    codex_judge = judge.CliExecutor(
        judge.JUDGES["B"],
        tmp_path / "logs",
        process=_fake_process(bench, CODEX_JUDGE_STREAM, seen, last=_verdict("correct", "missing")),
        parent_env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
        codex_auth=auth,
        plan_probe=lambda _e, _c: copy.deepcopy(next(reports)),
    )
    assert codex_judge("PROMPT").text and codex_judge.dispatched == 1
    closing = codex_judge.closing_probe(tmp_path / "logs" / "closing")
    assert any("at 93%" in r for r in closing["reasons"])
    saved = json.loads((tmp_path / "logs" / "closing" / "closing-probe.json").read_text())
    assert saved["reasons"] == closing["reasons"]
    # cmd_judge runs it after the batch's last call and exits 1 on a finding.
    (tmp_path / "j").mkdir()
    evidence, qpath = _judge_setup(bench, tmp_path / "j", 1)
    good = json.dumps(
        {"facts": [{"id": f"F{i}", "verdict": "correct"} for i in (1, 2, 3)], "complete": True}
    )

    class Closing:
        gate: Any = None
        dispatched = 0

        def __call__(self, prompt: str) -> str:
            self.dispatched += 1
            return good

        def closing_probe(self, log_dir: Path) -> dict[str, Any]:
            log_dir.mkdir(parents=True)
            return {"probed_at": "t", "probe": {}, "reasons": ["Codex credit balance 1 below"]}

    monkeypatch.setattr(
        judge, "make_executors", lambda _log, **_k: {"A": Closing(), "B": Closing()}
    )
    base = [
        "judge",
        "--questions",
        str(qpath),
        "--evidence-dir",
        str(evidence),
        "--execute",
        "--claude-config-dir",
        str(tmp_path),
    ]
    capsys.readouterr()
    assert cli.main([*base, "--max-calls", "2"]) == 1
    assert "closing probe after the judge batch" in capsys.readouterr().err
    rows = (evidence / "plan-probes.jsonl").read_text().splitlines()
    assert [json.loads(r)["kind"] for r in rows] == ["codex_judge_closing"] * 2


def test_r1b_window_step_between_probes(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = bench["probe"]
    runner = bench["runner"]
    gate = probe.PlanGate(90, max_window_step=5)

    def used(value: float) -> dict[str, Any]:
        return _probe(primary={"usedPercent": value, "windowDurationMins": 10080})

    assert gate.check_codex(used(10)) == []
    assert gate.check_codex(used(15)) == []  # exactly the step
    assert any("rose 6 points" in r for r in gate.check_codex(used(21)))
    assert gate.check_codex(used(3)) == []  # a reset is not spending
    with pytest.raises(ValueError, match="max-window-step"):
        probe.PlanGate(90, max_window_step=0)
    _probe_sequence(monkeypatch, bench, [used(10), used(17)])
    budget = runner.Budget(9, max_window_step=5)
    fake = FakeExecutor(["codex_rollout_web.jsonl"])
    records = _execute(bench, tmp_path, _reps(bench, 2), fake, budget=budget)
    assert len(records) == 1 and len(fake.calls) == 1 and budget.stop_kind == "plan_gate"
    assert any("rose 7 points" in d for _r, _k, d in budget.stops)


def test_r2_claude_overage_must_be_org_disabled(bench: dict[str, ModuleType]) -> None:
    probe = bench["probe"]
    gate = probe.PlanGate(90, clock=lambda: NOW)
    for reason in ("fetch_error", None, "unknown"):
        gate.observe_claude(GOOD_CLAUDE | {"overage_disabled_reason": reason})
        assert any("not rejected/org_level_disabled" in r for r in gate.check_claude()), reason
    gate.observe_claude(GOOD_CLAUDE)
    assert gate.check_claude() == []
    window = bench["claude_code"].plan_window(
        json.loads(claude_probe_stream(reason="fetch_error")[1])["rate_limit_info"]
    )
    assert window["overage_disabled_reason"] == "fetch_error"


def test_r2_claude_probe_freshness_and_failures(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = bench["probe"]
    streams: list[list[str]] = []
    calls: list[tuple[dict[str, str], Path]] = []

    def fake_claude(env: dict[str, str], cwd: Path) -> list[str]:
        calls.append((env, cwd))
        return streams.pop(0)

    monkeypatch.setattr(probe, "claude_probe_call", fake_claude)
    log = tmp_path / "plan-probes.jsonl"
    gate = probe.PlanGate(90, clock=lambda: NOW)
    account = {"CLAUDE_CONFIG_DIR": str(tmp_path)}  # the chosen Claude account (1.5.1)
    gate.configure(
        {"PATH": "/usr/bin", "DISABLE_AUTOUPDATER": "1", **account}, tmp_path / "scratch", log
    )
    # No reading: a probe call decides (and is logged with its windows and overage fields).
    streams.append(claude_probe_stream())
    assert gate.check_claude() == [] and gate.claude_probes == 1
    row = json.loads(log.read_text().splitlines()[-1])
    assert row["kind"] == "claude_probe" and row["ok"] is True and row["status"] == "allowed"
    assert row["windows"]["five_hour"] == {"utilization": 0.3, "resetsAt": 4102444800}
    assert row["overage_disabled_reason"] == "org_level_disabled" and row["usage"]
    assert row["reading"]["observed_at"] == "2026-10-07T02:10:00Z"
    # Fresh (under 30 minutes): no new probe.
    assert gate.check_claude() == [] and gate.claude_probes == 1
    # Stale (31 minutes old): a new probe; failing ones fail closed.
    for stream, reason in (
        (claude_probe_stream(api_key_source="ANTHROPIC_API_KEY"), "apiKeySource"),
        ([claude_probe_stream()[0], claude_probe_stream()[2]], "no rate_limit_event"),
        ([], "no system/init"),
    ):
        gate.observe_claude(GOOD_CLAUDE | {"observed_at": "2026-10-07T01:39:00Z"})
        streams.append(stream)
        assert any(reason in r for r in gate.check_claude()), reason
    assert len(calls) == 4 and all(not c.exists() for _e, c in calls)  # scratch removed
    # A hot reading whose window reset has passed is refreshed; one still in force blocks.
    hot = GOOD_CLAUDE | {"windows": {"five_hour": 0.95}, "window_resets": {"five_hour": 1}}
    gate.observe_claude(hot)
    streams.append(claude_probe_stream())
    assert gate.check_claude() == [] and len(calls) == 5
    gate.observe_claude(hot | {"window_resets": {"five_hour": 4102444800}})
    assert any("five_hour at 95%" in r for r in gate.check_claude()) and len(calls) == 5
    assert probe.claude_probe_command() == [
        "claude",
        "-p",
        "--restricted",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "claude-haiku-4-5-20251001",
        "--tools",
        "",
        "--strict-mcp-config",
        "--max-turns",
        "1",
        "--no-session-persistence",
        "--disable-slash-commands",
    ]
    assert probe.CLAUDE_PROBE_PROMPT == "Reply with OK."
    assert probe.CLAUDE_PROBE_ENV == {
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
        "DISABLE_AUTOUPDATER": "1",
    }
    # The probe log feeds the next batch's reading.
    assert probe.latest_claude_window(tmp_path, tmp_path)["source"] == "claude_probe"


def test_r2_claude_probe_is_not_a_run(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    runner = bench["runner"]
    budget = runner.Budget(1)
    records = _execute(
        bench,
        tmp_path,
        _reps(bench, 1, "claude_code", "both"),
        FakeExecutor(["claude_stream_both.jsonl"]),
        max_runs=1,
        budget=budget,
    )
    assert [r.status for r in records] == ["completed"] and budget.used == 1
    assert budget.gate.claude_probes == 1
    rows = (tmp_path / "ev" / "plan-probes.jsonl").read_text().splitlines()
    assert [json.loads(r)["kind"] for r in rows] == ["claude_probe"]
    assert len(runner.load_ledger(tmp_path / "ev" / "ledger.jsonl")) == 1


def test_r3_early_credit_drop_against_the_baseline(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    runner = bench["runner"]
    text = (FIXTURES / "codex_rollout_rate_limits.jsonl").read_text().replace("62500", "62400")
    credits = bench["codex"].parse_rollout(text.splitlines()).plan_window["credits"]
    assert (credits["balance_first"], credits["balance_min"]) == ("62400", "62400")
    budget = runner.Budget(9)  # the probe baseline is 62500
    records = _execute(bench, tmp_path, _reps(bench, 2), TextExecutor([text]), budget=budget)
    assert len(records) == 1 and budget.stop_kind == "metered_fallback_guard"
    assert any("62400 below the batch baseline 62500" in n for n in records[0].notes)
    assert not any("dropped from" in n for n in records[0].notes)


@pytest.mark.parametrize(
    ("result", "problem"),
    [
        ({"rateLimits": OK_PROBE["result"]["rateLimits"]}, "ordinaryUsageAllowed"),
        ({"ordinaryUsageAllowed": "yes", "rateLimits": {}}, "ordinaryUsageAllowed"),
        ({"ordinaryUsageAllowed": True, "rateLimits": {}}, "primary.usedPercent"),
        ({"ordinaryUsageAllowed": True}, "no rateLimits"),
        (None, "no result"),
    ],
)
def test_r5_incomplete_probe_fails_closed(
    bench: dict[str, ModuleType], result: Any, problem: str
) -> None:
    gate = bench["probe"].PlanGate()
    reasons = gate.check_codex({"ok": True, "result": result})
    assert any(problem in r for r in reasons), reasons


def test_r5_malformed_numbers_fail_closed(bench: dict[str, ModuleType]) -> None:
    gate = bench["probe"].PlanGate()
    cases = [
        (_probe(primary={"usedPercent": "NaN"}), "primary.usedPercent"),
        (_probe(primary={"usedPercent": float("inf")}), "primary.usedPercent"),
        (_probe(primary={"usedPercent": None}), "primary.usedPercent"),
        (_probe(primary={"usedPercent": True}), "primary.usedPercent"),
        (_probe(secondary={"usedPercent": "x"}), "secondary.usedPercent"),
        (_probe(credits={"hasCredits": True, "balance": "abc"}), "credits.balance"),
        (_probe(credits={"hasCredits": True}), "credits.balance"),
        (_probe(credits={"balance": "5"}), "hasCredits"),
        (_probe(credits=None), "hasCredits"),
    ]
    for report, problem in cases:
        assert any(problem in r for r in gate.check_codex(report)), problem
    no_credits = _probe(credits={"hasCredits": False, "unlimited": False, "balance": None})
    assert gate.check_codex(no_credits) == []


def test_r6_r7_stopped_batch_never_launches(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    runner = bench["runner"]
    budget = runner.Budget(9)
    assert budget.take() and budget.used == 1
    budget.stop("other worker", "plan_or_quota_limit", "archivist_quota")
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    runner.write_meta(run_dir, _spec(bench), "r", 1, started_at="t")
    assert budget.admit(run_dir, "codex") is None and budget.used == 0
    assert "dispatched_at" not in json.loads((run_dir / "meta.json").read_text())

    # In a batch: another worker stops it while this one prepares; this one never launches.
    stopper = runner.Budget(9)

    def preflight_then_stop(home: Path, env: dict[str, str], cwd: Path) -> dict[str, Any]:
        stopper.stop("worker A", "plan_or_quota_limit", "archivist_quota")
        return {"configured": [], "handshake": {}}

    fake = FakeExecutor(["codex_rollout_web.jsonl"])
    records = runner.execute_plan(
        [_spec(bench)],
        tmp_path / "ev",
        max_runs=9,
        executor=fake,
        parent_env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
        codex_auth=_auth(tmp_path),
        preflight=preflight_then_stop,
        budget=stopper,
    )
    assert records == [] and fake.calls == [] and stopper.used == 0
    [prepared] = list((tmp_path / "ev" / "runs").iterdir())
    meta = json.loads((prepared / "meta.json").read_text())
    assert meta["not_dispatched"] is True and "dispatched_at" not in meta
    assert not (tmp_path / "ev" / "ledger.jsonl").exists()
    assert runner.campaign_records(tmp_path / "ev") == []
    plan = runner.resume_plan([_spec(bench)], runner.campaign_records(tmp_path / "ev"))
    assert plan.start_attempts == {} and len(plan.pending) == 1
    with pytest.raises(ValueError, match="never launched"):
        runner.build_record(prepared)
    # A launched run records when it was launched.
    done = _execute(
        bench, tmp_path, [_spec(bench)], FakeExecutor(["codex_rollout_web.jsonl"]), "ok"
    )
    meta = json.loads((tmp_path / "ok" / "runs" / done[0].run_id / "meta.json").read_text())
    assert meta["dispatched_at"] and meta["finished_at"] >= meta["dispatched_at"]


def test_r7_prepared_dirs_are_not_attempts(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    runner = bench["runner"]
    evidence = tmp_path / "ev"
    spec = _spec(bench)
    prepared = evidence / "runs" / "p-codex.web.LK01.r1-a1"
    prepared.mkdir(parents=True)
    runner.write_meta(prepared, spec, prepared.name, 1, started_at="t")  # no dispatched_at
    assert runner.interrupted_dirs(evidence) == []
    assert runner.campaign_records(evidence) == []
    assert runner.reconcile_interrupted(evidence) == []
    meta = json.loads((prepared / "meta.json").read_text())
    assert meta["not_dispatched"] is True and not (prepared / "record.json").exists()
    assert not (evidence / "ledger.jsonl").exists()


def test_r8_reconciliation_survives_extraction(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = bench["runner"]
    cli = bench["cli"]
    evidence = tmp_path / "ev"
    run_dir = _run_dir(bench, tmp_path, "codex_rollout_web.jsonl", wall_s=1.0, returncode=0)
    target = evidence / "runs" / run_dir.name
    target.parent.mkdir(parents=True)
    run_dir.rename(target)
    runner.update_meta(target, dispatched_at="2026-10-07T00:00:01Z")
    [record] = runner.reconcile_interrupted(evidence)
    assert (record.status, record.status_reason) == ("infra_error", "interrupted")
    assert json.loads((target / "meta.json").read_text())["reconciled"] == "interrupted"
    again = runner.build_record(target)  # completed raw output exists, yet it stays interrupted
    assert (again.status, again.status_reason) == ("infra_error", "interrupted")
    assert cli.main(["extract", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["status_reason"] == "interrupted"


def test_r9_fingerprint_binds_every_input(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = bench["cli"]
    report = bench["report"]
    evidence, qpath = _judged_campaign(bench, tmp_path)
    out = tmp_path / "results"
    common = ["--questions", str(qpath), "--evidence-dir", str(evidence)]
    assert cli.main(["audit-export", *common, "--out", str(out)]) == 0
    shard = out / "audit-queue-001.csv"
    _write_audit(shard, shard, {("r0", "LK01.f1"): "correct"})
    capsys.readouterr()
    assert cli.main(["analyze", *common, "--resamples", "20", "--audit", str(out)]) == 0
    analysis = json.loads(capsys.readouterr().out)
    path = tmp_path / "analysis.json"
    path.write_text(json.dumps(analysis))
    base = ["report", *common, "--analysis", str(path), "--out", str(tmp_path / "rep")]

    def run(*extra: str) -> tuple[int, str]:
        capsys.readouterr()
        rc = cli.main([*base, *extra])
        return rc, capsys.readouterr().err

    assert run("--audit", str(out))[0] == 0
    assert "not the ones this analysis applied" in run()[1]  # audit files not passed
    fingerprint = report.evidence_fingerprint(evidence, qpath, [shard])
    assert fingerprint == analysis["evidence_fingerprint"]
    answer = evidence / "runs" / "r1" / "answer.md"
    original = answer.read_text()
    answer.write_text("changed")
    assert report.evidence_fingerprint(evidence, qpath, [shard]) != fingerprint
    assert "evidence changed" in run("--audit", str(out))[1]
    answer.write_text(original)
    log = evidence / "runs" / "r1" / "stdout.jsonl"
    record = json.loads((evidence / "runs" / "r1" / "record.json").read_text())
    (evidence / "runs" / "r1" / "record.json").write_text(
        json.dumps(record | {"agent": "claude_code"})
    )
    log.write_text("{}\n")
    changed_log = report.evidence_fingerprint(evidence, qpath, [shard])
    log.write_text('{"x": 1}\n')
    assert report.evidence_fingerprint(evidence, qpath, [shard]) != changed_log
    (evidence / "runs" / "r1" / "record.json").write_text(json.dumps(record))
    log.unlink()
    assert report.evidence_fingerprint(evidence, qpath, [shard]) == fingerprint
    qpath.write_text(qpath.read_text() + " ")
    assert "evidence changed" in run("--audit", str(out))[1]
    qpath.write_text(qpath.read_text()[:-1])
    _write_audit(shard, shard, {("r0", "LK01.f1"): "incorrect"})
    assert "not the ones this analysis applied" in run("--audit", str(out))[1]


def test_r10_identifier_spellings_are_masked_and_checked(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    import gzip

    report = bench["report"]
    raw = {
        "organizationId": 5551,
        "orgId": 5552,
        "org-id": 5553,
        "ORG_ID": 5554,
        "accountid": 5555,
        "Account-Id": 5556,
        "creatorUserId": 5557,
        "payload": json.dumps({"organization_id": 5558, "items": [{"orgId": 5559}]}),
        "input_tokens": 10,
        "organization": "Apple Inc.",
    }
    packed, _ = report.scrub_log((json.dumps(raw) + "\n").encode())
    text = gzip.decompress(packed).decode()
    for n in range(5551, 5560):
        assert str(n) not in text, n
    out = json.loads(text)
    assert out["input_tokens"] == 10 and out["organization"] == "Apple Inc."
    assert report.normalize_key("Org-Id") == report.normalize_key("org_id") == "orgid"
    clean = tmp_path / "clean.jsonl.gz"
    clean.write_bytes(packed)
    leaky = tmp_path / "leaky.jsonl.gz"
    leaky.write_bytes(
        gzip.compress(
            (json.dumps({"payload": json.dumps({"orgId": 77}), "accountID": "x"}) + "\n").encode()
        )
    )
    analysis = tmp_path / "analysis.json"
    analysis.write_text(json.dumps({"comparisons": [], "nested": {"user-id": 3}}))
    problems = report.check_files([clean, leaky, analysis])
    assert not any("clean" in p for p in problems)
    assert any("leaky" in p and "unmasked sensitive keys" in p and "orgId" in p for p in problems)
    assert any("analysis.json" in p and "user-id" in p for p in problems)


# --- Codex campaign review round 3 (Codex): findings 1 to 10 --------------------------------------


def test_v3_1_refusal_is_published_before_evidence(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = bench["runner"]
    hot = _probe(primary={"usedPercent": 95, "windowDurationMins": 10080})
    _probe_sequence(monkeypatch, bench, [hot])
    evidence = tmp_path / "ev"
    files_at_stop: list[list[str]] = []

    class Watching(runner.Budget):  # type: ignore[name-defined, misc]
        def stop(self, run_id: str, kind: str = "invalid", detail: str = "") -> None:
            files_at_stop.append(sorted(p.name for p in evidence.rglob("*") if p.is_file()))
            super().stop(run_id, kind, detail)

    budget = Watching(9)
    fake = FakeExecutor(["codex_rollout_web.jsonl"])
    _execute(bench, tmp_path, [_spec(bench)], fake, budget=budget)
    assert fake.calls == [] and budget.stop_kind == "plan_gate"
    # At the moment of the stop no refusal evidence existed yet.
    assert "plan-probe.json" not in files_at_stop[0]
    assert "dispatch-refused.json" not in files_at_stop[0]
    [run_dir] = list((evidence / "runs").iterdir())
    assert (run_dir / "plan-probe.json").exists() and (run_dir / "dispatch-refused.json").exists()
    probe_dir = tmp_path / "latched"
    probe_dir.mkdir()
    runner.write_meta(probe_dir, _spec(bench), "x", 1, started_at="t")
    assert budget.admit(probe_dir, "codex") is None  # latched: nobody else is admitted


def test_v3_1_concurrent_worker_is_not_admitted_after_a_refusal(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = bench["runner"]
    refused = threading.Event()
    hot = _probe(primary={"usedPercent": 95, "windowDurationMins": 10080})

    def fake(env: dict[str, str], cwd: Path) -> dict[str, Any]:
        if ".r2-" in env["CODEX_HOME"]:
            return copy.deepcopy(hot)  # worker B sees the hot window
        assert refused.wait(10), "worker B never refused"
        return copy.deepcopy(OK_PROBE)  # worker A's probe passed before B's refusal landed

    monkeypatch.setattr(bench["probe"], "app_server_probe", fake)

    class Signalling(runner.Budget):  # type: ignore[name-defined, misc]
        def stop(self, run_id: str, kind: str = "invalid", detail: str = "") -> None:
            super().stop(run_id, kind, detail)
            refused.set()

    budget = Signalling(9)
    fake_exec = FakeExecutor(["codex_rollout_web.jsonl"])
    records = _execute(bench, tmp_path, _reps(bench, 2), fake_exec, budget=budget, concurrency=2)
    assert records == [] and fake_exec.calls == [] and budget.used == 0
    metas = [json.loads(p.read_text()) for p in (tmp_path / "ev" / "runs").glob("*/meta.json")]
    assert len(metas) == 2 and all(m["not_dispatched"] for m in metas)
    assert not any("dispatched_at" in m for m in metas)


@pytest.mark.parametrize(
    "change",
    [
        {"status": None},
        {"status": "allowed_warning"},
        {"windows": {}},
        {"windows": {"five_hour": float("nan")}},
        {"windows": {"five_hour": None, "seven_day": 0.2}},
        {"is_using_overage": None},
    ],
)
def test_v3_2_incomplete_claude_readings_fail_closed(
    bench: dict[str, ModuleType], monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    probe = bench["probe"]
    gate = probe.PlanGate(90, clock=lambda: NOW)
    gate.configure({"CLAUDE_CONFIG_DIR": "/claude-account"}, None, None)  # the chosen account
    bad = GOOD_CLAUDE | change
    # The probe returns the same incomplete reading: refused.
    original = probe.parse_claude_probe
    monkeypatch.setattr(probe, "parse_claude_probe", lambda _l, _t: {"ok": True, "reading": bad})
    gate.observe_claude(bad)
    reasons = gate.check_claude()
    assert reasons and gate.claude_probes == 1, change
    # A fresh complete probe reading (the faked healthy probe call) replaces it.
    monkeypatch.setattr(probe, "parse_claude_probe", original)
    gate.observe_claude(bad)
    assert gate.check_claude() == [] and gate.claude_probes == 2


def test_v3_3_extract_refuses_a_preparation(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = bench["runner"]
    cli = bench["cli"]
    evidence = tmp_path / "ev"
    prepared = evidence / "runs" / "20261007T000000000000Z-codex.web.LK01.r1-a1"
    prepared.mkdir(parents=True)
    runner.write_meta(
        prepared, _spec(bench), prepared.name, 1, started_at="t", meta_version=runner.META_VERSION
    )
    (prepared / "cwd").mkdir()
    with pytest.raises(ValueError, match="preparation without launch evidence"):
        runner.build_record(prepared)
    assert cli.main(["extract", str(prepared)]) == 1
    assert "not extracted" in capsys.readouterr().err
    assert not (prepared / "record.json").exists()
    assert runner.campaign_records(evidence) == []
    plan = runner.resume_plan([_spec(bench)], runner.campaign_records(evidence))
    assert plan.done == [] and len(plan.pending) == 1 and plan.start_attempts == {}


def _launched_dir(
    bench: dict[str, ModuleType], evidence: Path, rollout: str, probe_balance: str
) -> Path:
    runner = bench["runner"]
    run_dir = evidence / "runs" / "20261007T000000000000Z-codex.web.LK01.r1-a1"
    sessions = run_dir / "codex-home" / "sessions" / "2026"
    sessions.mkdir(parents=True)
    (sessions / "rollout-x.jsonl").write_text(rollout)
    runner.write_meta(
        run_dir,
        _spec(bench),
        run_dir.name,
        1,
        started_at="2026-10-07T00:00:00Z",
        dispatched_at="2026-10-07T00:00:01Z",
        meta_version=runner.META_VERSION,
    )
    (run_dir / "plan-probe.json").write_text(
        json.dumps(_probe(credits={"hasCredits": True, "balance": probe_balance}))
    )
    return run_dir


def test_v3_4_reconciliation_keeps_telemetry_and_billing_signs(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = bench["runner"]
    report = bench["report"]
    evidence = tmp_path / "ev"
    lines = fixture_lines("codex_rollout_rate_limits.jsonl")
    balances = iter(["62500", "62300", "62400"])
    rollout = []
    for line in lines:
        obj = json.loads(line)
        limits = (obj.get("payload") or {}).get("rate_limits")
        if limits:
            limits["credits"]["balance"] = next(balances)
        rollout.append(json.dumps(obj))
    run_dir = _launched_dir(bench, evidence, "\n".join(rollout) + "\n", "62600")
    [record] = runner.reconcile_interrupted(evidence)
    assert (record.status, record.status_reason) == ("infra_error", "interrupted")
    assert record.usage is not None and record.usage["total"] == 40500
    assert record.plan_window["credits"]["balance_min"] == "62300"
    signs = next(n for n in record.notes if n.startswith("metered_fallback_guard:"))
    assert "62300 below the pre dispatch probe 62600" in signs
    assert "dropped from 62500 to 62300" in signs
    assert "metered billing signs" in capsys.readouterr().err
    again = runner.build_record(run_dir)  # extraction keeps classification and the signs
    assert again.status_reason == "interrupted" and signs in again.notes
    [row] = report.run_rows([again.as_dict()], {}, {}, {})
    assert "pre dispatch probe" in row["guard_notes"] and "62300" in row["metered_signs"]
    clean = tmp_path / "clean"
    plain_dir = _launched_dir(
        bench, clean, (FIXTURES / "codex_rollout_rate_limits.jsonl").read_text(), "62500"
    )
    [plain] = runner.reconcile_interrupted(clean)
    assert not any(n.startswith("metered_fallback_guard") for n in plain.notes)
    assert plain_dir.exists()


def test_v3_5_closing_probe_runs_on_exceptions(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = bench["runner"]
    seen = _probe_sequence(monkeypatch, bench, [OK_PROBE])

    class Failing(FakeExecutor):
        def __init__(self, error: BaseException) -> None:
            super().__init__(["codex_rollout_web.jsonl"])
            self.error = error

        def __call__(self, req: Any) -> Any:
            super().__call__(req)
            raise self.error

    for name, error in (
        ("boom", RuntimeError("extraction exploded")),
        ("intr", KeyboardInterrupt()),
    ):
        seen.clear()
        budget = runner.Budget(9)
        fake = Failing(error)
        with pytest.raises(type(error)):
            _execute(bench, tmp_path, _reps(bench, 2), fake, name, budget=budget)
        assert len(fake.calls) == 1  # the queued attempt never started
        assert seen[-1].endswith("closing-probe/codex-home"), name
        assert (tmp_path / name / "closing-probe.json").exists()
        assert any(kind == "aborted" for _r, kind, _d in budget.stops)


def test_v3_5_judge_closing_probe_runs_on_exceptions(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    judge = bench["judge"]
    cli = bench["cli"]
    evidence, qpath = _judge_setup(bench, tmp_path, 1)

    class Exploding:
        gate: Any = None
        dispatched = 0

        def __call__(self, prompt: str) -> str:
            self.dispatched += 1
            raise KeyboardInterrupt

        def closing_probe(self, log_dir: Path) -> dict[str, Any]:
            log_dir.mkdir(parents=True)
            return {"probed_at": "t", "probe": {}, "reasons": []}

    monkeypatch.setattr(
        judge, "make_executors", lambda _log, **_k: {"A": Exploding(), "B": Exploding()}
    )
    base = [
        "judge",
        "--questions",
        str(qpath),
        "--evidence-dir",
        str(evidence),
        "--execute",
        "--claude-config-dir",
        str(tmp_path),
    ]
    with pytest.raises(KeyboardInterrupt):
        cli.main([*base, "--max-calls", "2"])
    rows = (evidence / "plan-probes.jsonl").read_text().splitlines()
    assert [json.loads(r)["kind"] for r in rows] == ["codex_judge_closing"]  # A only launched


def test_v3_6_one_canonical_answer(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = bench["cli"]
    report = bench["report"]
    evidence, qpath = _judge_setup(bench, tmp_path, 1)
    control = evidence / "runs" / "c-web"
    control.mkdir()
    (control / "answer.md").write_text("The product is 391.")
    outside = tmp_path / "elsewhere.md"
    outside.write_text("I do not know")
    (control / "record.json").write_text(
        json.dumps(
            {
                "run_id": "c-web",
                "run_key": "codex.web.CT01.r1",
                "status": "completed",
                "question_id": "CT01",
                "agent": "codex",
                "arm": "web",
                "stratum": "control",
                "contaminated": False,
                "usage": {"total": 30},
                "answer_file": str(outside),
            }
        )
    )
    data = json.loads(qpath.read_text())
    rows = {r["run_id"]: r for r in cli.analysis_rows(evidence, data)}
    assert rows["c-web"]["accuracy"] == 1.0  # read from the run directory, not the pointer
    capsys.readouterr()
    assert (
        cli.main(
            [
                "analyze",
                "--questions",
                str(qpath),
                "--evidence-dir",
                str(evidence),
                "--resamples",
                "20",
            ]
        )
        == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert out["answer_file_mismatches"] == [{"run_id": "c-web", "answer_file": str(outside)}]
    before = report.evidence_fingerprint(evidence, qpath, [])
    outside.write_text("changed elsewhere")
    assert report.evidence_fingerprint(evidence, qpath, []) == before
    (control / "answer.md").write_text("The product is 392.")
    assert report.evidence_fingerprint(evidence, qpath, []) != before
    assert {r["run_id"]: r for r in cli.analysis_rows(evidence, data)}["c-web"]["accuracy"] == 0.0
    assert report.answer_mismatch({"answer_file": str(control / "answer.md")}, control) is None


def test_v3_7_prose_assignments_and_embedded_json(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    import gzip

    report = bench["report"]
    prose = (
        'HTTP response body: {"organizationId": 7777, "access_token": "opaque-secret-value"} '
        "then token=abc123def session_id: s-9988 'api_key': 'k-4242' "
        'and "orgId": 6161, while input_tokens=1200 stays'
    )
    packed, _ = report.scrub_log((json.dumps({"msg": prose}) + "\n").encode())
    text = json.loads(gzip.decompress(packed).decode())["msg"]
    for secret in ("7777", "opaque-secret-value", "abc123def", "s-9988", "k-4242", "6161"):
        assert secret not in text, secret
    assert "input_tokens=1200" in text and text.startswith("HTTP response body: {")
    leaky = tmp_path / "leaky.jsonl.gz"
    leaky.write_bytes(gzip.compress((json.dumps({"msg": prose}) + "\n").encode()))
    clean = tmp_path / "clean.jsonl.gz"
    clean.write_bytes(packed)
    problems = report.check_files([leaky, clean])
    assert any("leaky" in p and "unmasked sensitive keys" in p for p in problems)
    assert not any("clean" in p for p in problems)
    found = report.text_findings(prose)
    assert any("organizationId" in f for f in found) and any("token=" in f for f in found)


def test_v3_8_report_checks_the_whole_folder(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = bench["cli"]
    evidence, qpath, _data = _report_campaign(bench, tmp_path)
    common = ["--questions", str(qpath), "--evidence-dir", str(evidence)]
    capsys.readouterr()
    assert cli.main(["analyze", *common, "--resamples", "20"]) == 0
    analysis = tmp_path / "analysis.json"
    analysis.write_text(capsys.readouterr().out)
    out = tmp_path / "out"
    shard = out / "audit-queue-001.csv"
    shard.parent.mkdir()
    upstream_host = "www." + "sec" + ".gov"
    shard.write_text(f"run_id,fact_id,audit_verdict,audit_note\nr,f,,see {upstream_host}\n")
    stale = out / "logs" / "old.bin"
    stale.parent.mkdir()
    stale.write_bytes(b"x" * (1000 * 1024 + 1))
    rc = cli.main(["report", *common, "--analysis", str(analysis), "--out", str(out)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "audit-queue-001.csv" in err and "upstream filing host" in err
    assert "old.bin" in err and "over the" in err


def test_v3_9_only_blocking_windows_decide_staleness(
    bench: dict[str, ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = bench["probe"]
    calls: list[int] = []

    def fake_claude(env: dict[str, str], cwd: Path) -> list[str]:
        calls.append(1)
        return claude_probe_stream()

    monkeypatch.setattr(probe, "claude_probe_call", fake_claude)
    gate = probe.PlanGate(90, clock=lambda: NOW)
    gate.configure({"CLAUDE_CONFIG_DIR": "/claude-account"}, None, None)  # the chosen account
    two = GOOD_CLAUDE | {
        "windows": {"five_hour": 0.95, "seven_day": 0.6},
        "window_resets": {"five_hour": 1, "seven_day": 4102444800},
    }
    gate.observe_claude(two)
    assert gate.check_claude() == [] and calls == [1]  # the hot window reset: a probe decides
    gate.observe_claude(two | {"window_resets": {"five_hour": 4102444800, "seven_day": 1}})
    assert any("five_hour at 95%" in r for r in gate.check_claude()) and calls == [1]


def test_v3_10_checker_reports_structured_sensitive_values(bench: dict[str, ModuleType]) -> None:
    report = bench["report"]
    assert report.unmasked_sensitive({"accountId": [123]}) == ["$.accountId"]
    assert report.unmasked_sensitive({"orgId": {"id": 77}}) == ["$.orgId"]
    assert report.unmasked_sensitive({"userId": 5, "email": "<email>", "token": None}) == [
        "$.userId"
    ]
    assert report.unmasked_sensitive({"accountId": "<redacted>"}) == []


# --- Codex campaign review round 4 (Codex): findings 1 to 4 ---------------------------------------


def test_v4_1_worker_failure_latches_the_stop(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    runner = bench["runner"]
    failed = threading.Event()
    a_running = threading.Event()

    class SlowAndFailing(FakeExecutor):
        def __call__(self, req: Any) -> Any:
            key = req.cwd.parent.name
            if ".r2-" in key:
                with self.lock:
                    self.calls.append(req)
                assert a_running.wait(10)  # worker A is launched and still running
                failed.set()
                raise RuntimeError("worker B failed")
            if ".r1-" in key:
                a_running.set()
                assert failed.wait(10)
                time.sleep(0.3)  # worker A is slow: B's thread is free to take queued cells
            return super().__call__(req)

    fake = SlowAndFailing(["codex_rollout_web.jsonl"])
    budget = runner.Budget(9)
    with pytest.raises(RuntimeError, match="worker B failed"):
        _execute(bench, tmp_path, _reps(bench, 4), fake, budget=budget, concurrency=2)
    launched = sorted(req.cwd.parent.name.split("-codex.")[1] for req in fake.calls)
    assert launched == ["web.LK01.r1-a1", "web.LK01.r2-a1"]  # r3 and r4 never admitted
    assert ("codex.web.LK01.r2", "aborted") in [(r, k) for r, k, _d in budget.stops]
    runs = {p.name.split("-codex.")[1] for p in (tmp_path / "ev" / "runs").iterdir()}
    assert not any(".r3-" in r or ".r4-" in r for r in runs)
    assert (tmp_path / "ev" / "closing-probe.json").exists()  # cleanup still probed


def test_v4_2_authorization_headers_and_quoted_values(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    import gzip

    report = bench["report"]
    basic = "dXNlcjpwYXNzd29yZA=="  # base64 of user:password (synthetic)
    texts = [
        f"Authorization: Basic {basic} sent",
        "Proxy-Authorization: Negotiate abc123xyz789",
        "api_key: 'opaque\"secret'",
        'token="a\\"bc-secret" tail',
        '"authorization": "Bearer zzzzzzzzzzzzzzzzzzzz"',
    ]
    packed, _ = report.scrub_log((json.dumps({"lines": texts}) + "\n").encode())
    out = gzip.decompress(packed).decode()
    for secret in (basic, "Negotiate", "abc123xyz789", 'opaque\\"secret', "bc-secret", "zzzz"):
        assert secret not in out, secret
    assert '"Authorization: <redacted>"' in out  # the whole value, to the end of the string
    raw = tmp_path / "raw.jsonl.gz"
    raw.write_bytes(gzip.compress((json.dumps({"lines": texts}) + "\n").encode()))
    clean = tmp_path / "clean.jsonl.gz"
    clean.write_bytes(packed)
    problems = report.check_files([raw, clean])
    assert any("raw" in p and "basic" in p for p in problems)
    assert any("raw" in p and "unmasked sensitive keys" in p for p in problems)
    assert not any("clean" in p for p in problems)
    md = tmp_path / "note.md"
    md.write_text(f"curl with Basic {basic} and Bearer abc.def123ghi\n")
    assert any("basic" in p and "bearer" in p for p in report.check_files([md]))
    prose = tmp_path / "prose.md"
    prose.write_text("Basic EPS rose; bearer instruments. Basic earnings per share were 6.08.\n")
    assert report.check_files([prose]) == []


def test_v4_3_every_text_format_is_validated(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = bench["report"]
    cli = bench["cli"]
    md = tmp_path / "a.md"
    md.write_text("line one\nconfig api_key=opaque-secret-value here\n")
    csv_file = tmp_path / "b.csv"
    csv_file.write_text('x,y\n1,"body {""access_token"": ""opaque""}"\n')
    jsonl = tmp_path / "c.json"
    jsonl.write_text(json.dumps({"note": 'prefix {"organizationId": 7777}'}))
    problems = report.check_files([md, csv_file, jsonl])
    assert any("a.md" in p and "line 2" in p and "api_key" in p for p in problems)
    assert any("b.csv" in p and "row 2 column 2" in p for p in problems)
    assert any("c.json" in p and "organizationId" in p for p in problems)
    # Generated free text is scrubbed: credentials masked, investor relations emails kept.
    out = tmp_path / "out"
    rel = report.write_answer(
        out, "r1", "Contact ir@apple.com. Debug: token=abc123def and password: hunter22x"
    )
    answer = (out / rel).read_text()
    assert "ir@apple.com" in answer and "abc123def" not in answer and "hunter22x" not in answer
    assert report.check_files([out / rel]) == []
    cells = report._csv(("note",), [{"note": "secret=s3cr3t-value, Email: ir@apple.com"}])
    assert "s3cr3t-value" not in cells and "ir@apple.com" in cells
    # An auditor edited audit shard is never rewritten: report fails and names file and row.
    (tmp_path / "camp").mkdir()
    evidence, qpath, _data = _report_campaign(bench, tmp_path / "camp")
    common = ["--questions", str(qpath), "--evidence-dir", str(evidence)]
    capsys.readouterr()
    assert cli.main(["analyze", *common, "--resamples", "20"]) == 0
    analysis = tmp_path / "analysis.json"
    analysis.write_text(capsys.readouterr().out)
    results = tmp_path / "results"
    results.mkdir()
    shard = results / "audit-queue-001.csv"
    shard.write_text("run_id,fact_id,audit_verdict,audit_note\nr,f,correct,ok\nr,g,,api_key=k-9\n")
    before = shard.read_bytes()
    rc = cli.main(["report", *common, "--analysis", str(analysis), "--out", str(results)])
    err = capsys.readouterr().err
    assert rc == 1 and "audit-queue-001.csv" in err and "row 3 column 4" in err
    assert shard.read_bytes() == before


def test_v4_4_billing_evidence_is_committed_and_bound(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import csv

    cli = bench["cli"]
    report = bench["report"]
    dropped = _probe(credits={"hasCredits": True, "balance": "62450"})
    _probe_sequence(monkeypatch, bench, [OK_PROBE] * 4 + [dropped])
    evidence, qpath, _data = _report_campaign(bench, tmp_path)
    closing = json.loads((evidence / "closing-probe.json").read_text())
    assert closing["reasons"]
    common = ["--questions", str(qpath), "--evidence-dir", str(evidence)]
    capsys.readouterr()
    assert cli.main(["analyze", *common, "--resamples", "20"]) == 0
    analysis = json.loads(capsys.readouterr().out)
    warnings = analysis["billing_warnings"]
    assert any(
        w["kind"] == "codex_closing" and "62450 below the batch baseline 62500" in w["detail"]
        for w in warnings
    )
    path = tmp_path / "analysis.json"
    path.write_text(json.dumps(analysis))
    out = tmp_path / "out"
    assert cli.main(["report", *common, "--analysis", str(path), "--out", str(out)]) == 0
    with (out / "plan-probes.csv").open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    closing_rows = [r for r in rows if r["kind"] == "codex_closing"]
    assert closing_rows and "62450 below" in closing_rows[0]["warning"]
    assert closing_rows[0]["credits_balance"] == "62450"
    assert "accountId" not in (out / "plan-probes.csv").read_text()
    # The billing evidence is bound to the fingerprint: a judge closing probe added later
    # changes it, the report refuses the stale analysis, and the new warning is reported.
    before = report.evidence_fingerprint(evidence, qpath, [])
    judge_dir = evidence / "judge-logs" / "closing-20261007T030000Z-B"
    judge_dir.mkdir(parents=True)
    (judge_dir / "closing-probe.json").write_text(
        json.dumps(
            {
                "probed_at": "2026-10-07T03:00:00Z",
                "probe": dropped,
                "reasons": ["Codex credit balance 62400 below the batch baseline 62500"],
            }
        )
    )
    assert report.evidence_fingerprint(evidence, qpath, []) != before
    capsys.readouterr()
    assert cli.main(["report", *common, "--analysis", str(path), "--out", str(out)]) == 1
    assert "evidence changed" in capsys.readouterr().err
    kinds = {w["kind"] for w in report.billing_warnings(evidence, [])}
    assert {"codex_closing", "codex_judge_closing"} <= kinds
    (evidence / "plan-probes.jsonl").write_text("")
    assert report.evidence_fingerprint(evidence, qpath, []) != before


# --- Codex campaign review pass 5 (Codex): P1 to P4 -----------------------------------------------


def test_p5_1_digest_and_multi_parameter_headers(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    import gzip

    report = bench["report"]
    digest = 'Authorization: Digest username="u", response="opaque-response"'
    proxy = 'Proxy-Authorization: Digest realm="r", nonce="n0nce-value", response="r3sp"'
    for text in (digest, proxy, f"{digest}\nnext line", f"prefix {proxy} tail"):
        for scrubbed in (report.mask_text(text), report.scrub_free_text(text)):
            for secret in ("opaque-response", "n0nce-value", "r3sp", "username"):
                assert secret not in scrubbed, (text, scrubbed)
            assert report.findings_at(scrubbed, report._credential) == []
    assert report.scrub_free_text(f"{digest}\nnext line").endswith("<redacted>\nnext line")
    # Inside a JSON string value: masked to the end of that value, the JSON stays valid.
    raw = json.dumps({"headers": digest, "n": 1})
    packed, _ = report.scrub_log((json.dumps({"body": raw, "msg": digest}) + "\n").encode())
    out = json.loads(gzip.decompress(packed).decode())
    assert json.loads(out["body"]) == {"headers": "Authorization: <redacted>", "n": 1}
    assert out["msg"] == "Authorization: <redacted>"
    leaky = tmp_path / "leaky.md"
    leaky.write_text(f"echo {digest}\n")
    assert any("Authorization header" in p for p in report.check_files([leaky]))


def test_p5_2_bearer_rule_is_shared(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    report = bench["report"]
    flagged = ["Bearer AbCdEfGhIjKl", "Bearer abc12345", "Bearer ab.cd.ef.gh", "bearer Xy_z0123"]
    prose = [
        "Bearer Bonds",
        "bearer instruments.",
        "Bearer bonds and bearer shares",
        "Bearer ABCDEFGHIJKLMN",
    ]
    for text in flagged:
        masked = report.scrub_free_text(f"Tool error echoed {text}")
        assert masked.endswith("<redacted>"), text
        path = tmp_path / "f.md"
        path.write_text(f"Tool error echoed {text}\n")
        assert any("bearer" in p for p in report.check_files([path])), text
    for text in prose:
        assert report.scrub_free_text(text) == text, text
        path = tmp_path / "p.md"
        path.write_text(text + "\n")
        assert report.check_files([path]) == [], text
    assert report.bearer_like("AbCdEfGhIjKl") and not report.bearer_like("instruments")


def test_p5_3_free_text_keys_are_exact(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    report = bench["report"]
    prose = "Trade secrets: technology\nand know-how"
    assert report.scrub_free_text(prose) == prose
    for text in (
        "Tokens: 3 per share",
        "passwords: none disclosed",
        "Session: annual meeting",
        "Email: ir@apple.com",
        "Secrets: kept",
    ):
        assert report.scrub_free_text(text) == text, text
    for text, value in (
        ("secret: s3cr3t", "s3cr3t"),
        ("client_secret=abcd1234", "abcd1234"),
        ("Set-Cookie: sid=zz99", "sid=zz99"),
        ("access_token: tok-1", "tok-1"),
    ):
        assert value not in report.scrub_free_text(text), text
    # JSON and logs keep the full key set.
    assert report.unmasked_sensitive({"sessionId": 5}) == ["$.sessionId"]
    out = tmp_path / "answers"
    rel = report.write_answer(out, "r1", prose)
    assert (out / rel).read_text() == prose + "\n"
    assert report.check_files([out / rel]) == []


def test_p5_4_markdown_boundaries_and_audit_quotes(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = bench["cli"]
    report = bench["report"]
    evidence, qpath = _judged_campaign(bench, tmp_path)
    data = json.loads(qpath.read_text())
    facts = data["questions"][0]["facts"]
    facts[0]["source"]["quote"] = "Login with password: hunter22x\nand continue"
    facts[1]["statement"] = "Trade secrets: technology\nand know-how"
    qpath.write_text(json.dumps(data))
    out = tmp_path / "results"
    common = ["--questions", str(qpath), "--evidence-dir", str(evidence)]
    capsys.readouterr()
    assert cli.main(["audit-export", *common, "--out", str(out)]) == 0, capsys.readouterr().err
    md = (out / "audit-queue-001.md").read_text()
    assert "hunter22x" not in md and "password: <redacted><br>and continue" in md
    assert "hunter22x" not in (out / "audit-queue-001.csv").read_text()
    assert report.check_files([out / "audit-queue-001.md"]) == []
    # A mask followed by <br> or | is standalone; a real value before <br> is still found.
    cell = tmp_path / "cells.md"
    cell.write_text("| a | token: <redacted><br>next | password: <redacted>| x |\n")
    assert report.check_files([cell]) == []
    bad = tmp_path / "bad.md"
    bad.write_text("| a | token: tok123<br>next |\n")
    assert any("line 1" in p and "token" in p for p in report.check_files([bad]))
    # report over the same folder (retained shards) passes too.
    capsys.readouterr()
    assert cli.main(["analyze", *common, "--resamples", "20"]) == 0
    analysis = tmp_path / "analysis.json"
    analysis.write_text(capsys.readouterr().out)
    rc = cli.main(["report", *common, "--analysis", str(analysis), "--out", str(out)])
    assert rc == 0, capsys.readouterr().err


# --- Codex campaign review pass 6 (Codex): Q1 and Q2 ----------------------------------------------


def test_p6_1_prefixed_credential_keys(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    import csv
    import io

    report = bench["report"]
    secret = "opaque-live-credential-987654321"
    inputs = [
        f"X-API-Key: {secret}",
        f"auth_token={secret}",
        f'{{"auth_token":"{secret}"}}',
        f"x-auth-token: {secret}",
        f"client-secret={secret}",
        f"db_password: {secret}",
        f'error body {{"meta": {{"X-API-Key": "{secret}"}}}} end',
    ]
    for text in inputs:
        assert secret not in report.scrub_free_text(text), text
        cells = report._csv(("note",), [{"note": text}])
        assert secret not in cells, text
        out = tmp_path / "answers"
        rel = report.write_answer(out, "r1", text)
        assert secret not in (out / rel).read_text(), text
        raw_csv = io.StringIO()
        csv.writer(raw_csv).writerow(["x", text])
        for name, content in (("raw.md", text + "\n"), ("raw.csv", raw_csv.getvalue())):
            path = tmp_path / name
            path.write_text(content)
            assert report.check_files([path]), (name, text)
    for key in ("X-API-Key", "auth_token", "x-auth-token", "client-secret", "db_password"):
        assert report._credential(key), key
    for key in ("secrets", "Tokens", "passwords", "keys", "API keys", "mytoken", "x-tokens"):
        assert not report._credential(key), key
    for prose in ("Trade secrets: technology", "Tokens: 3", "passwords: none", "API keys: none"):
        assert report.scrub_free_text(prose) == prose, prose
        path = tmp_path / "prose.md"
        path.write_text(prose + "\n")
        assert report.check_files([path]) == [], prose


def test_p6_2_title_case_is_not_a_bearer_token(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    report = bench["report"]
    prose = "Bearer Certificates are issued in denominations of $1,000."
    for text in (prose, "Bearer Bonds", "Bearer Instruments", "Bearer Debentures"):
        assert report.scrub_free_text(text) == text, text
        path = tmp_path / "p.md"
        path.write_text(text + "\n")
        assert report.check_files([path]) == [], text
        cells = report._csv(("note",), [{"note": text}])
        assert "<redacted>" not in cells
    assert not report.bearer_like("Certificates") and not report.bearer_like("Abcdefghijkl")
    assert report.bearer_like("AbCdEfGhIjKl") and report.bearer_like("abcdEFghijkl")
    masked = report.scrub_free_text("echoed Bearer AbCdEfGhIjKl")
    assert masked == "echoed Bearer <redacted>"
    path = tmp_path / "f.md"
    path.write_text("echoed Bearer AbCdEfGhIjKl\n")
    assert any("bearer" in p for p in report.check_files([path]))


# --- Codex campaign C1 isolation fix (harness 1.2.1, deviation D5) --------------------------------


RESOURCE_TOOLS = ("list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource")


def test_c1_codex_resource_builtins_are_allowed_in_archivist_arms(
    bench: dict[str, ModuleType],
) -> None:
    codex = bench["codex"]
    model = bench["model"]
    ex = codex.parse_rollout(fixture_lines("codex_rollout_resource_tools.jsonl"))
    for tool in RESOURCE_TOOLS:
        assert ex.tool_calls[f"mcp:codex.{tool}"] == 1  # still counted
    assert "codex" not in ex.mcp_servers and "codex" not in ex.mcp_evidence
    for arm in ("archivist", "both"):
        assert codex.isolation_violations(arm, ex) == [], arm
    web = codex.isolation_violations("web", ex)
    assert any("mcp:codex.list_mcp_resources outside" in p for p in web)
    # Any other codex.* tool, or any other server, is still a violation.
    other = codex.parse_rollout([_mcp_line("completed", "x").replace('"archivist"', '"codex"')])
    assert "codex" in other.mcp_servers
    assert any(
        "MCP server codex loaded" in p for p in codex.isolation_violations("archivist", other)
    )
    for tools in ({"mcp:codex.shell": 1}, {"mcp:github.search": 1}):
        violations = codex.isolation_violations(
            "archivist", model.Extraction(tool_calls=tools, mcp_servers={"archivist": "connected"})
        )
        assert any("outside the arm allowlist" in p for p in violations), tools
    # A catalog listing the built ins does not load the pseudo server; another tool does.
    catalog = json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c",
                "output": "mcp__codex__list_mcp_resources mcp__archivist__search",
            },
        }
    )
    listed = codex.parse_rollout([catalog])
    assert "codex" not in listed.mcp_evidence and listed.mcp_evidence["archivist"]
    assert (
        "codex"
        in codex.parse_rollout([catalog.replace("list_mcp_resources", "shell")]).mcp_evidence
    )


def test_c1_resource_calls_are_not_archivist_calls(bench: dict[str, ModuleType]) -> None:
    cost = bench["cost"]
    rows = cost.ledger_rows(
        [
            {
                "agent": "codex",
                "arm": "archivist",
                "question_id": "CT02",
                "stratum": "control",
                "status": "completed",
                "usage": {"total": 100, "input": 90, "cached_input": 0, "output": 10},
                "model_calls": 1,
                "tool_calls": {
                    "mcp:archivist.search": 2,
                    **{f"mcp:codex.{t}": 1 for t in RESOURCE_TOOLS},
                },
                "web_actions": {},
                "cost_usd": 0.01,
            }
        ]
    )
    assert rows[0]["archivist_calls"] == 2


def test_c1_reextraction_completes_in_archivist_arm_only(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    runner = bench["runner"]
    archivist = _run_dir(
        bench,
        tmp_path,
        "codex_rollout_resource_tools.jsonl",
        arm="archivist",
        wall_s=8.6,
        returncode=0,
    )
    record = runner.build_record(archivist)
    assert record.status == "completed" and record.mcp_loaded is True
    assert record.tool_calls["mcp:codex.list_mcp_resources"] == 1
    assert not any("codex:" in n for n in record.notes if n.startswith("mcp_servers="))
    web_text = (
        (FIXTURES / "codex_rollout_web.jsonl").read_text()
        + "\n".join(
            line
            for line in fixture_lines("codex_rollout_resource_tools.jsonl")
            if '"server":"codex"' in line
        )
        + "\n"
    )
    web_dir = tmp_path / "web-run"
    web_dir.mkdir()
    runner.write_meta(web_dir, _spec(bench, arm="web"), web_dir.name, 1, started_at="t")
    sessions = web_dir / "codex-home" / "sessions" / "2026"
    sessions.mkdir(parents=True)
    (sessions / "rollout-x.jsonl").write_text(web_text)
    web = runner.build_record(web_dir)
    assert web.status == "invalid" and "mcp:codex.list_mcp_resources" in str(web.status_reason)


def _legacy_invalid(bench: dict[str, ModuleType], evidence: Path) -> Path:
    """A C1 shaped run classified invalid by harness 1.2.0 (no classified_by in meta.json)."""
    runner = bench["runner"]
    run_dir = evidence / "runs" / "20261007T032906059008Z-codex.archivist.LK01.r1-a1"
    sessions = run_dir / "codex-home" / "sessions" / "2026"
    sessions.mkdir(parents=True)
    (sessions / "rollout-x.jsonl").write_text(
        (FIXTURES / "codex_rollout_resource_tools.jsonl").read_text()
    )
    runner.write_meta(
        run_dir,
        _spec(bench, arm="archivist"),
        run_dir.name,
        1,
        started_at="2026-10-07T03:29:06Z",
        dispatched_at="2026-10-07T03:29:06Z",
        finished_at="2026-10-07T03:29:15Z",
        meta_version=runner.META_VERSION,
        returncode=0,
        wall_s=8.6,
    )
    reason = (
        "arm_isolation: MCP server codex loaded outside the arm allowlist; tool "
        "mcp:codex.list_mcp_resources outside the arm allowlist"
    )
    record = runner.build_record(run_dir).as_dict() | {"status": "invalid", "status_reason": reason}
    (run_dir / "record.json").write_text(json.dumps(record))
    with (evidence / "ledger.jsonl").open("a") as fh:
        fh.write(json.dumps(record) + "\n")
    return run_dir


def test_c1_extract_keeps_the_recorded_invalid(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = bench["runner"]
    cli = bench["cli"]
    run_dir = _legacy_invalid(bench, tmp_path / "ev")
    assert cli.main(["extract", str(run_dir)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "invalid" and "mcp:codex.list_mcp_resources" in out["status_reason"]
    assert "classification kept: invalid as recorded by harness 1.2.0" in out["notes"]
    meta = json.loads((run_dir / "meta.json").read_text())
    assert (meta["classified_by"], meta["classified_status"]) == ("1.2.0", "invalid")
    assert runner.build_record(run_dir).status == "invalid"  # stable on every re-extraction
    # A run classified by the current harness records its version and re-extracts freely.
    done = _execute(
        bench,
        tmp_path,
        [_spec(bench, arm="archivist")],
        TextExecutor([(FIXTURES / "codex_rollout_resource_tools.jsonl").read_text()]),
        "new",
    )
    assert done[0].status == "completed"
    new_meta = json.loads((tmp_path / "new" / "runs" / done[0].run_id / "meta.json").read_text())
    assert new_meta["classified_by"] == "1.6.0" and new_meta["classified_status"] == "completed"


def test_c1_rerun_invalid_once(
    bench: dict[str, ModuleType],
    question_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    runner = bench["runner"]
    evidence = tmp_path / "ev"
    _legacy_invalid(bench, evidence)
    spec = _spec(bench, arm="archivist")
    records = runner.campaign_records(evidence)
    assert runner.resume_plan([spec], records).done == ["codex.archivist.LK01.r1"]
    plan = runner.resume_plan([spec], records, rerun_invalid=True)
    assert plan.reruns == ["codex.archivist.LK01.r1"]
    assert plan.start_attempts == {"codex.archivist.LK01.r1": 2}
    real = runner.execute_plan
    fake = TextExecutor([(FIXTURES / "codex_rollout_resource_tools.jsonl").read_text()])

    def fake_plan(specs: Any, root: Path, max_runs: int, concurrency: int, **kw: Any) -> Any:
        return real(
            specs,
            root,
            max_runs,
            concurrency,
            executor=fake,
            parent_env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
            codex_auth=_auth(tmp_path),
            preflight=no_servers,
            **kw,
        )

    monkeypatch.setattr(runner, "execute_plan", fake_plan)
    base = [
        "run",
        "--questions",
        str(question_file),
        "--agent",
        "codex",
        "--question",
        "LK01",
        "--arms",
        "archivist",
        "--repetitions",
        "1",
        "--evidence-dir",
        str(evidence),
    ]
    with pytest.raises(SystemExit, match="--rerun-invalid needs --resume"):
        cli.main([*base, "--execute", "--max-runs", "1", "--rerun-invalid"])
    capsys.readouterr()
    assert cli.main([*base, "--execute", "--max-runs", "1", "--resume", "--rerun-invalid"]) == 0
    assert "rerun once after an invalid record: codex.archivist.LK01.r1" in capsys.readouterr().err
    rows = sorted(runner.campaign_records(evidence), key=lambda r: r.attempt)
    assert [(r.attempt, r.status) for r in rows] == [(1, "invalid"), (2, "completed")]
    assert len(fake.calls) == 1
    # Never twice: the cell is done now, and a second resume runs nothing.
    capsys.readouterr()
    assert cli.main([*base, "--execute", "--max-runs", "1", "--resume", "--rerun-invalid"]) == 0
    assert "nothing to run" in capsys.readouterr().out and len(fake.calls) == 1
    # analyze accepts an invalid record plus its rerun; the invalid one is listed as excluded.
    capsys.readouterr()
    assert (
        cli.main(
            [
                "analyze",
                "--questions",
                str(question_file),
                "--evidence-dir",
                str(evidence),
                "--resamples",
                "20",
            ]
        )
        == 0
    )
    analysis = json.loads(capsys.readouterr().out)
    assert [e["status"] for e in analysis["excluded_runs"]] == ["invalid"]
    assert cli.duplicate_cells([r.as_dict() for r in rows]) == {}
    assert cli.duplicate_cells([r.as_dict() for r in rows] + [rows[1].as_dict() | {"run_id": "x"}])


def test_c1_a_second_invalid_or_a_used_rerun_is_excluded(bench: dict[str, ModuleType]) -> None:
    runner = bench["runner"]
    model = bench["model"]
    spec = _spec(bench, arm="archivist")

    def rec(attempt: int, status: str) -> Any:
        return model.RunRecord.from_dict(
            {
                "run_id": f"r{attempt}",
                "run_key": spec.run_key,
                "agent": "codex",
                "arm": "archivist",
                "question_id": "LK01",
                "stratum": "lookup",
                "repetition": 1,
                "attempt": attempt,
                "model": "m",
                "effort": "medium",
                "service_tier": "default",
                "auth_mode": "chatgpt",
                "billing_mode": "b",
                "status": status,
                "status_reason": None,
                "usage": None,
                "model_calls": 0,
                "tool_calls": {},
                "web_actions": {},
                "compactions": 0,
                "truncations": 0,
                "turn_limit": False,
                "timeout": False,
                "wall_s": 0.0,
                "cost_usd": None,
                "cost_basis": "",
                "mcp_loaded": None,
                "contaminated": False,
                "started_at": "t",
            }
        )

    cases = {
        "two invalid": [rec(1, "invalid"), rec(2, "invalid")],
        "invalid inside the sequence": [
            rec(1, "invalid"),
            rec(2, "infra_error"),
            rec(3, "invalid"),
        ],
        "sequence of three infra_error": [
            rec(1, "invalid"),
            rec(2, "infra_error"),
            rec(3, "infra_error"),
            rec(4, "infra_error"),
        ],
    }
    for name, rows in cases.items():
        plan = runner.resume_plan([spec], rows, rerun_invalid=True)
        assert plan.excluded == [spec.run_key] and plan.pending == [], name
    # A sequence that ended infra_error continues with the attempts it has left.
    plan = runner.resume_plan([spec], [rec(1, "invalid"), rec(2, "infra_error")], True)
    assert plan.start_attempts == {spec.run_key: 3} and plan.rerun_attempts == {spec.run_key: 2}
    # A completed rerun is final: never a second sequence.
    done = runner.resume_plan([spec], [rec(1, "invalid"), rec(2, "completed")], True)
    assert done.done == [spec.run_key] and done.pending == []
    # Numbered after the last attempt, infra errors before the invalid one included.
    plan = runner.resume_plan(
        [spec], [rec(1, "infra_error"), rec(2, "infra_error"), rec(3, "invalid")], True
    )
    assert plan.start_attempts == {spec.run_key: 4} and plan.reruns == [spec.run_key]
    assert plan.rerun_attempts == {spec.run_key: 3}

    for name, rows in cases.items():
        assert runner.resume_plan([spec], rows, rerun_invalid=True).excluded_invalid, name


def test_c1_rerun_sequence_uses_only_its_attempts_left(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    spec = _spec(bench)  # web arm: the overloaded fixture is a clean transient infra error
    for left, evidence in ((1, "one"), (3, "three")):
        fake = FakeExecutor(["codex_rollout_overloaded.jsonl"])
        records = _execute(
            bench,
            tmp_path,
            [spec],
            fake,
            evidence,
            start_attempts={spec.run_key: 2},
            rerun_attempts={spec.run_key: left},
        )
        assert [r.attempt for r in records] == list(range(2, 2 + left)), left
        assert all(r.status == "infra_error" for r in records) and len(fake.calls) == left


def _rerun_cli(
    bench: dict[str, ModuleType],
    question_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    texts: list[str],
) -> tuple[Callable[[], int], TextExecutor]:
    """``run --resume --rerun-invalid`` for codex.archivist.LK01.r1 with a faked executor."""
    cli = bench["cli"]
    runner = bench["runner"]
    real = runner.execute_plan
    fake = TextExecutor(texts)

    def fake_plan(specs: Any, root: Path, max_runs: int, concurrency: int, **kw: Any) -> Any:
        return real(
            specs,
            root,
            max_runs,
            concurrency,
            executor=fake,
            parent_env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
            codex_auth=_auth(tmp_path),
            preflight=no_servers,
            **kw,
        )

    monkeypatch.setattr(runner, "execute_plan", fake_plan)
    base = [
        "run",
        "--questions",
        str(question_file),
        "--agent",
        "codex",
        "--question",
        "LK01",
        "--arms",
        "archivist",
        "--repetitions",
        "1",
        "--evidence-dir",
        str(tmp_path / "ev"),
        "--execute",
        "--max-runs",
        "3",
        "--resume",
        "--rerun-invalid",
    ]

    def run() -> int:
        return int(cli.main(base))

    return run, fake


def test_c1_rerun_plan_stop_then_resume_completes(
    bench: dict[str, ModuleType],
    question_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = bench["runner"]
    _legacy_invalid(bench, tmp_path / "ev")
    archivist = (FIXTURES / "codex_rollout_resource_tools.jsonl").read_text()
    limited = (
        archivist
        + json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "error": {
                        "message": "You've hit your usage limit.",
                        "codex_error_info": "usage_limit_exceeded",
                    },
                },
            }
        )
        + "\n"
    )
    run, fake = _rerun_cli(bench, question_file, tmp_path, monkeypatch, [limited])
    assert run() == 1  # the plan stop ends the batch inside the rerun sequence
    assert "1 to run" in capsys.readouterr().err
    fake.texts = [archivist]
    assert run() == 0  # the same sequence continues
    err = capsys.readouterr().err
    assert "codex.archivist.LK01.r1 (2 attempts left in its sequence)" in err
    rows = sorted(runner.campaign_records(tmp_path / "ev"), key=lambda r: r.attempt)
    assert [(r.attempt, r.status, r.status_reason) for r in rows] == [
        (1, "invalid", rows[0].status_reason),
        (2, "infra_error", "usage_limit_exceeded"),
        (3, "completed", None),
    ]
    assert run() == 0 and "nothing to run" in capsys.readouterr().out  # no second sequence
    assert len(fake.calls) == 2


def test_c1_rerun_that_is_invalid_again_excludes_the_cell(
    bench: dict[str, ModuleType],
    question_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = bench["runner"]
    _legacy_invalid(bench, tmp_path / "ev")
    broken = (FIXTURES / "codex_rollout_mcp_failed.jsonl").read_text()
    run, fake = _rerun_cli(bench, question_file, tmp_path, monkeypatch, [broken])
    assert run() == 1  # invalid again: the batch stops
    rows = sorted(runner.campaign_records(tmp_path / "ev"), key=lambda r: r.attempt)
    assert [(r.attempt, r.status) for r in rows] == [(1, "invalid"), (2, "invalid")]
    capsys.readouterr()
    assert run() == 0
    captured = capsys.readouterr()
    assert (
        "excluded (invalid, its one rerun sequence used): codex.archivist.LK01.r1" in captured.err
    )
    assert "nothing to run" in captured.out and len(fake.calls) == 1


# --- Codex campaign amendment 2 (harness 1.3.0): Codex only, pass B scores every fact -------------


def _verdicts_for(judge: ModuleType, question: dict[str, Any], *verdicts: str) -> str:
    """A verdict JSON for every fact label of ``question``: the given verdicts first, the rest
    ``correct``."""
    labels = list(judge.fact_labels(question).values())
    chosen = list(verdicts) + ["correct"] * (len(labels) - len(verdicts))
    return json.dumps(
        {
            "facts": [{"id": lab, "verdict": v} for lab, v in zip(labels, chosen, strict=True)],
            "complete": True,
        }
    )


def test_a2_judge_one_pass_and_kept_passes(bench: dict[str, ModuleType]) -> None:
    judge = bench["judge"]
    only_b = judge.judge(
        QUESTION, "answer", "run-b", {"B": lambda _p: _verdict("correct", "missing")}, passes=("B",)
    )
    out = only_b.as_dict()
    assert (out["status"], out["rule"], out["accuracy"], out["complete"]) == ("ok", "B", 0.5, 1.0)
    assert out["fact_verdicts"] == {"LK01.f1": "correct", "LK01.f2": "missing"}
    assert out["disputed"] == [] and list(out["passes"]) == ["B"]
    assert (out["passes_requested"], out["passes_run"], out["passes_kept"]) == (["B"], ["B"], [])
    # Kept pass A stays byte for byte and scores beside the new pass B (combined rule).
    full = judge.judge(
        QUESTION, "answer", "run-k", _same(lambda _p: _verdict("correct", "correct"))
    )
    kept_a = full.as_dict()["passes"]["A"]
    merged = judge.judge(
        QUESTION,
        "answer",
        "run-k",
        {"B": lambda _p: _verdict("correct", "incorrect")},
        passes=("B",),
        keep={"A": copy.deepcopy(kept_a)},
    ).as_dict()
    assert merged["passes"]["A"] == kept_a and merged["rule"] == "combined"
    assert merged["fact_verdicts"] == {"LK01.f1": "correct", "LK01.f2": "disputed"}
    assert (merged["passes_run"], merged["passes_kept"]) == (["B"], ["A"])
    # A kept pass that errored does not score: the new pass alone does.
    broken = dict(kept_a, error="call: exit 1")
    alone = judge.judge(
        QUESTION,
        "answer",
        "run-k",
        {"B": lambda _p: _verdict("correct", "incorrect")},
        passes=("B",),
        keep={"A": broken},
    ).as_dict()
    assert alone["rule"] == "B" and alone["passes"]["A"] == broken and alone["accuracy"] == 0.5
    # Both passes (amendment 1) record what was requested and run.
    both = full.as_dict()
    assert (both["rule"], both["passes_requested"], both["passes_run"]) == (
        "combined",
        ["A", "B"],
        ["A", "B"],
    )
    # Scoring rules: combined needs a judgement graded by both; B reads pass B whatever the
    # status (correct 1, else 0; completion from pass B).
    assert judge.scoring(out, "combined") is None
    assert judge.scoring(out, "B").accuracy == 0.5
    disputed = judge.judge(
        QUESTION,
        "answer",
        "run-d",
        {
            "A": lambda _p: _verdict("correct", "correct"),
            "B": lambda _p: _verdict("correct", "missing", False),
        },
    ).as_dict()
    assert judge.scoring(disputed, "combined").accuracy == 0.75
    b = judge.scoring(disputed, "B")
    assert (b.accuracy, b.complete, b.scores) == (0.5, 0.0, {"LK01.f1": 1.0, "LK01.f2": 0.0})
    assert judge.scoring(dict(disputed, status="judge_error"), "B").accuracy == 0.5
    errored = judge.judge(
        QUESTION, "answer", "run-e", {"B": lambda _p: "not json"}, passes=("B",)
    ).as_dict()
    assert errored["status"] == "judge_error" and judge.scoring(errored, "B") is None
    with pytest.raises(ValueError, match="unknown primary judge"):
        judge.scoring(out, "A")


def test_a2_audit_queue_and_pass_agreement_under_pass_b(bench: dict[str, ModuleType]) -> None:
    judge = bench["judge"]

    def stored(run_id: str, a: list[str] | None, b: list[str]) -> dict[str, Any]:
        executors: dict[str, Any] = {"B": lambda _p: _verdict(*b)}
        passes: tuple[str, ...] = ("B",)
        if a is not None:
            executors["A"] = lambda _p: _verdict(*a)
            passes = ("A", "B")
        return judge.judge(QUESTION, "x", run_id, executors, passes=passes).as_dict()

    judgements = [
        stored(f"r{i:02d}", ["correct", "correct"], ["correct", "correct"]) for i in range(20)
    ]
    judgements.append(stored("r20", ["correct", "correct"], ["correct", "incorrect"]))
    judgements.append(stored("r21", None, ["missing", "correct"]))  # pass B only
    judgements.append({"run_id": "r22", "status": "judge_error", "batches": 2})
    queue = judge.audit_queue(judgements, 8105, "B")
    assert queue[0] == {
        "run_id": "r20",
        "fact_id": "LK01.f2",
        "verdict": "incorrect",
        "selection": "disputed",
    }
    others = 2 * 20 + 1 + 2  # every other fact with a pass B verdict, pass B only runs included
    assert len(queue) == 1 + round(others * 0.05)
    assert all(item["selection"] == "sample" for item in queue[1:])
    assert queue == judge.audit_queue(list(reversed(judgements)), 8105, "B")
    # The combined rule leaves judgements scored by one pass alone out.
    assert all(i["run_id"] != "r21" for i in judge.audit_queue(judgements, 8105))
    agreement = judge.pass_agreement(judgements)
    assert agreement["label"] == "exploratory"
    assert (agreement["answers"], agreement["facts"], agreement["agreed_facts"]) == (21, 42, 41)
    assert agreement["agreement_share"] == round(41 / 42, 4)
    assert agreement["cross_table_rows_A_columns_B"]["correct"] == {
        "correct": 41,
        "incorrect": 1,
        "missing": 0,
    }
    assert judge.pass_agreement(judgements, {"r20"})["answers"] == 1


def _b_only_executor(calls: list[str], reply: str) -> Any:
    class CodexJudge:
        gate: Any = None
        dispatched = 0

        def __call__(self, prompt: str) -> str:
            calls.append(prompt)
            self.dispatched += 1
            return reply

    return CodexJudge()


def test_a2_judge_passes_b_merges_and_skips(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    judge = bench["judge"]
    probe = bench["probe"]
    evidence, qpath = _judge_setup(bench, tmp_path, 5)
    question = json.loads(qpath.read_text())["questions"][0]
    runs = evidence / "runs"
    all_correct = _verdicts_for(judge, question)
    # r0: graded by both judges before the amendment: skipped, untouched.
    full = judge.judge(question, "391,035", "r0", _same(lambda _p: all_correct)).as_dict()
    (runs / "r0" / "judgement.json").write_text(json.dumps(dict(full, batches=1)))
    # r1: the judge_error the host restart left (no passes): judged in its second batch.
    interrupted = {
        "run_id": "r1",
        "status": "judge_error",
        "error": "interrupted before both passes finished",
        "batches": 1,
    }
    (runs / "r1" / "judgement.json").write_text(json.dumps(interrupted))
    # r2: never judged. r3: a judge_error after its second batch: excluded, skipped.
    (runs / "r3" / "judgement.json").write_text(
        json.dumps({"run_id": "r3", "status": "judge_error", "batches": 2})
    )
    # r4: pass A valid, pass B failed: pass B runs, pass A stays untouched.
    failed_b = judge.judge(
        question,
        "391,035",
        "r4",
        {"A": lambda _p: _verdicts_for(judge, question, "incorrect"), "B": lambda _p: "bad"},
    ).as_dict()
    (runs / "r4" / "judgement.json").write_text(json.dumps(dict(failed_b, batches=1)))
    before_r0 = (runs / "r0" / "judgement.json").read_bytes()

    def no_claude(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a Claude reading or probe was looked up under --passes B")

    monkeypatch.setattr(probe, "latest_claude_window", no_claude)
    monkeypatch.setattr(probe, "claude_probe_call", no_claude)
    calls: list[str] = []
    asked: list[Any] = []
    built: list[Any] = []

    def make(log: Path, passes: Any = None) -> dict[str, Any]:
        asked.append(passes)
        ex = _b_only_executor(calls, all_correct)
        built.append(ex)
        return {"B": ex}

    monkeypatch.setattr(judge, "make_executors", make)
    base = ["judge", "--questions", str(qpath), "--evidence-dir", str(evidence)]
    capsys.readouterr()
    assert cli.main([*base, "--passes", "B"]) == 0  # dry run
    out = capsys.readouterr().out
    assert "3 answers to judge, at least 3 judge calls (B codex gpt-6.1-sol medium)" in out
    with pytest.raises(SystemExit, match="at least 3"):
        cli.main([*base, "--passes", "B", "--execute", "--max-calls", "2"])
    assert cli.main([*base, "--passes", "B", "--execute", "--max-calls", "3"]) == 0
    assert asked == [("B",)] and len(calls) == 3
    gate = built[0].gate
    assert gate.allow_claude is False and gate.check_claude() == [probe.CLAUDE_DISABLED]
    ledger = [json.loads(x) for x in (evidence / "judge-ledger.jsonl").read_text().splitlines()]
    assert {(r["pass"], r["cli"]) for r in ledger} == {("B", "codex")}
    assert sorted({r["run_id"] for r in ledger}) == ["r1", "r2", "r4"]
    assert (runs / "r0" / "judgement.json").read_bytes() == before_r0
    r1 = json.loads((runs / "r1" / "judgement.json").read_text())
    assert (r1["status"], r1["rule"], r1["batches"], r1["accuracy"]) == ("ok", "B", 2, 1.0)
    assert (r1["passes_requested"], r1["passes_run"], list(r1["passes"])) == (["B"], ["B"], ["B"])
    r2 = json.loads((runs / "r2" / "judgement.json").read_text())
    assert (r2["status"], r2["rule"], r2["batches"]) == ("ok", "B", 1)
    r4 = json.loads((runs / "r4" / "judgement.json").read_text())
    assert r4["passes"]["A"] == failed_b["passes"]["A"]
    assert (r4["status"], r4["rule"], r4["batches"], r4["passes_kept"]) == (
        "ok",
        "combined",
        2,
        ["A"],
    )
    assert r4["disputed"] == ["LK01.f1"]
    assert json.loads((runs / "r3" / "judgement.json").read_text())["batches"] == 2
    # Every answer now has a valid pass B (or is excluded): nothing left to judge.
    capsys.readouterr()
    assert cli.main([*base, "--passes", "B", "--execute", "--max-calls", "3"]) == 0
    assert "0 answers to judge" in capsys.readouterr().out and len(calls) == 3


def test_a2_passes_b_keeps_pass_a_on_crash_and_limit(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = bench["cli"]
    judge = bench["judge"]
    evidence, qpath = _judge_setup(bench, tmp_path, 1)
    question = json.loads(qpath.read_text())["questions"][0]
    path = evidence / "runs" / "r0" / "judgement.json"
    prior = dict(
        judge.judge(
            question,
            "391,035",
            "r0",
            {"A": lambda _p: _verdicts_for(judge, question), "B": lambda _p: "bad"},
        ).as_dict(),
        batches=1,
    )
    path.write_text(json.dumps(prior))
    base = ["judge", "--questions", str(qpath), "--evidence-dir", str(evidence), "--execute"]
    base += ["--passes", "B", "--max-calls", "2"]

    def limited(_p: str) -> str:
        raise judge.JudgeInfraError("Codex infra: usage limit")

    monkeypatch.setattr(judge, "make_executors", lambda _log, passes=None: {"B": limited})
    assert cli.main(base) == 1
    assert json.loads(path.read_text()) == prior  # a plan limit restores the judgement

    def crash(_p: str) -> str:
        raise RuntimeError("killed")

    monkeypatch.setattr(judge, "make_executors", lambda _log, passes=None: {"B": crash})
    with pytest.raises(RuntimeError):
        cli.main(base)
    pending = json.loads(path.read_text())
    assert (pending["status"], pending["batches"]) == ("judge_error", 2)
    assert pending["passes"]["A"] == prior["passes"]["A"]  # pass A survives the crash
    assert pending["passes_requested"] == ["B"] and "B" not in pending["passes"]


def test_a2_make_executors_without_pass_a(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    judge = bench["judge"]
    probe = bench["probe"]
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt"}))
    monkeypatch.setenv("HOME", str(home))

    def no_claude(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("Claude probe called")

    monkeypatch.setattr(probe, "claude_probe_call", no_claude)
    only_b = judge.make_executors(tmp_path / "logs", passes=("B",))
    assert list(only_b) == ["B"] and only_b["B"].cfg.cli == "codex"
    gate = only_b["B"].gate
    assert gate.allow_claude is False
    assert gate.check_claude() == [probe.CLAUDE_DISABLED]
    assert gate.probe_claude() == {"ok": False, "error": probe.CLAUDE_DISABLED}
    assert gate.claude_probes == 0
    both = judge.make_executors(tmp_path / "logs")
    assert list(both) == ["A", "B"] and both["B"].gate.allow_claude is True
    (home / ".codex" / "auth.json").write_text(json.dumps({"auth_mode": "apikey"}))
    with pytest.raises(judge.JudgeLoginError):
        judge.make_executors(tmp_path / "logs", passes=("B",))


def _amendment2_campaign(bench: dict[str, ModuleType], tmp_path: Path) -> tuple[Path, Path]:
    """Five judged LK01 runs (r0 disputed: pass A correct, pass B incorrect on F1; complete per
    pass) plus r4 rejudged by pass B alone (amendment 2)."""
    judge = bench["judge"]
    evidence, qpath = _judged_campaign(bench, tmp_path)
    question = json.loads(qpath.read_text())["questions"][0]
    only_b = judge.judge(
        question,
        "391,035",
        "r4",
        {"B": lambda _p: _verdicts_for(judge, question, "missing")},
        passes=("B",),
    )
    (evidence / "runs" / "r4" / "judgement.json").write_text(
        json.dumps(dict(only_b.as_dict(), batches=1))
    )
    return evidence, qpath


def test_a2_analyze_primary_judge_b(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = bench["cli"]
    judge = bench["judge"]
    evidence, qpath = _amendment2_campaign(bench, tmp_path)
    common = ["analyze", "--questions", str(qpath), "--evidence-dir", str(evidence)]
    capsys.readouterr()
    assert cli.main([*common, "--resamples", "20", "--primary-judge", "B"]) == 0
    b = json.loads(capsys.readouterr().out)
    assert b["primary_judge"] == "B"
    assert b["judge_bias"] == {"deferred": judge.DEFERRED_WITH_CLAUDE_ARM}
    assert b["single_judge_sensitivity"] == {"deferred": judge.DEFERRED_WITH_CLAUDE_ARM}
    agreement = b["pass_agreement"]
    assert (agreement["answers"], agreement["facts"], agreement["agreed_facts"]) == (4, 12, 11)
    assert b["audit_queue"][0] == {
        "run_id": "r0",
        "fact_id": "LK01.f1",
        "verdict": "incorrect",
        "selection": "disputed",
    }
    assert len(b["audit_queue"]) == 1 + round(14 * 0.05)
    assert b["runs_without_valid_judgement"] == []
    data = json.loads(qpath.read_text())
    rows = {r["run_id"]: r for r in cli.analysis_rows(evidence, data, None, "B")}
    assert rows["r0"]["accuracy"] == pytest.approx(0.666667)  # pass B: incorrect scores 0
    assert rows["r4"]["accuracy"] == pytest.approx(0.666667)  # pass B only judgement
    assert rows["r1"]["accuracy"] == 1.0 and rows["r0"]["completion"] == 1.0
    # The default (amendment 1) rule is unchanged and refuses the pass B only judgement.
    assert cli.main([*common, "--resamples", "20"]) == 0
    combined = json.loads(capsys.readouterr().out)
    assert combined["primary_judge"] == "combined" and "pass_agreement" not in combined
    assert isinstance(combined["single_judge_sensitivity"], list)
    assert combined["runs_without_valid_judgement"] == [
        {
            "run_id": "r4",
            "judge_status": "no_valid_combined_judgement",
            "batches": 1,
            "excluded": False,
        }
    ]
    rows = {r["run_id"]: r for r in cli.analysis_rows(evidence, data)}
    assert rows["r0"]["accuracy"] == pytest.approx(0.833333) and rows["r4"]["accuracy"] is None
    with pytest.raises(SystemExit):
        cli.main([*common, "--primary-judge", "A"])


def test_a2_audit_export_and_application_under_pass_b(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import csv

    cli = bench["cli"]
    evidence, qpath = _amendment2_campaign(bench, tmp_path)
    common = ["--questions", str(qpath), "--evidence-dir", str(evidence)]
    out = tmp_path / "results"
    capsys.readouterr()
    assert cli.main(["analyze", *common, "--resamples", "20", "--primary-judge", "B"]) == 0
    analysis = tmp_path / "analysis-b.json"
    analysis.write_text(capsys.readouterr().out)
    assert cli.main(["analyze", *common, "--resamples", "20"]) == 0
    analysis_c = tmp_path / "analysis-c.json"
    analysis_c.write_text(capsys.readouterr().out)
    # audit-export takes the analysis' rule and refuses another one.
    export = ["audit-export", *common, "--out", str(out)]
    assert cli.main([*export, "--analysis", str(analysis), "--primary-judge", "combined"]) == 1
    assert "the analysis used B" in capsys.readouterr().err
    assert cli.main([*export, "--analysis", str(analysis)]) == 0
    assert "(B rule)" in capsys.readouterr().out
    with (out / "audit-queue-001.csv").open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    disputed = rows[0]
    assert (disputed["selection"], disputed["run_id"], disputed["fact_id"]) == (
        "disputed",
        "r0",
        "LK01.f1",
    )
    assert disputed["judge_verdict"] == "incorrect" and disputed["primary_judge"] == "B"
    assert (disputed["judge_A_verdict"], disputed["judge_B_verdict"]) == ("correct", "incorrect")
    assert {r["primary_judge"] for r in rows} == {"B"}
    assert "pass B (Codex) scores every fact" in (out / "audit-queue-001.md").read_text()
    assert "--primary-judge B" in (out / "audit-index.md").read_text()
    assert cli.main([*export, "--primary-judge", "B"]) == 0  # same rows without --analysis
    # Apply a human audit verdict under the B rule.
    filled = tmp_path / "filled.csv"
    _write_audit(filled, out / "audit-queue-001.csv", {("r0", "LK01.f1"): "correct"})
    analyze = ["analyze", *common, "--resamples", "20"]
    capsys.readouterr()
    assert cli.main([*analyze, "--primary-judge", "B", "--audit", str(filled)]) == 0
    [correction] = json.loads(capsys.readouterr().out)["audit_corrections"]
    assert (correction["judge_verdict"], correction["judge_score"]) == ("incorrect", 0.0)
    assert correction["audit_score"] == 1.0
    assert correction["run_accuracy_judged"] == pytest.approx(0.666667)
    assert correction["run_accuracy_audited"] == 1.0
    # An audit file of one rule never applies to an analysis of the other.
    assert cli.main([*analyze, "--audit", str(filled)]) == 1
    captured = capsys.readouterr()
    assert "another scoring rule than combined" in captured.err and captured.out == ""
    combined_out = tmp_path / "combined"
    assert cli.main(["audit-export", *common, "--out", str(combined_out)]) == 0
    old = tmp_path / "old.csv"
    _write_audit(old, combined_out / "audit-queue-001.csv", {("r0", "LK01.f1"): "correct"})
    assert cli.main([*analyze, "--primary-judge", "B", "--audit", str(old)]) == 1
    assert "another scoring rule than B" in capsys.readouterr().err
    # The queue must still be the analysis' queue: changed evidence is refused.
    judgement = evidence / "runs" / "r1" / "judgement.json"
    data = json.loads(judgement.read_text())
    data["passes"]["A"]["fact_verdicts"]["LK01.f2"] = "missing"
    judgement.write_text(json.dumps(data))
    capsys.readouterr()
    assert cli.main([*export[:-1], str(tmp_path / "again"), "--analysis", str(analysis)]) == 1
    assert "differs from the analysis' audit_queue" in capsys.readouterr().err
    assert analysis_c.exists()


def test_a2_report_uses_the_analysis_rule(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import csv

    cli = bench["cli"]
    judge = bench["judge"]
    evidence, qpath, data = _report_campaign(bench, tmp_path)
    lookup = {q["id"]: q for q in data["questions"]}
    for record in sorted((evidence / "runs").glob("*/record.json")):
        rec = json.loads(record.read_text())
        question = lookup[rec["question_id"]]
        wrong = "incorrect" if rec["arm"] == "web" else "correct"
        result = judge.judge(
            question,
            "answer",
            rec["run_id"],
            {"B": lambda _p, q=question, w=wrong: _verdicts_for(judge, q, w)},
            passes=("B",),
        )
        (record.parent / "judgement.json").write_text(json.dumps(dict(result.as_dict(), batches=1)))
    common = ["--questions", str(qpath), "--evidence-dir", str(evidence)]
    capsys.readouterr()
    assert cli.main(["analyze", *common, "--resamples", "20", "--primary-judge", "B"]) == 0
    analysis = tmp_path / "analysis.json"
    analysis.write_text(capsys.readouterr().out)
    out = tmp_path / "results"
    report_cmd = ["report", *common, "--analysis", str(analysis), "--out", str(out)]
    assert cli.main([*report_cmd, "--primary-judge", "combined"]) == 1
    assert "the analysis used B" in capsys.readouterr().err
    assert cli.main(report_cmd) == 0
    assert json.loads((out / "analysis.json").read_text())["primary_judge"] == "B"
    with (out / "cells.csv").open(newline="") as fh:
        cells = {(c["stratum"], c["arm"]): c for c in csv.DictReader(fh)}
    assert set(cells) == {
        ("lookup", "web"),
        ("lookup", "archivist"),
        ("multi_period", "web"),
        ("multi_period", "archivist"),
    }
    assert cells[("lookup", "archivist")]["question_mean_accuracy"] == "1"
    assert float(cells[("lookup", "web")]["question_mean_accuracy"]) < 1.0
    with (out / "comparisons.csv").open(newline="") as fh:
        comparisons = list(csv.DictReader(fh))
    assert {c["source"] for c in comparisons} == {"main", "tokens_with_partial_usage"}
    assert {c["agent"] for c in comparisons} == {"codex"}
    accuracy = next(
        c
        for c in comparisons
        if c["source"] == "main"
        and c["stratum"] == "lookup"
        and c["arm"] == "archivist"
        and c["baseline"] == "web"
        and c["metric"] == "accuracy_diff_arm_minus_baseline"
    )
    assert float(accuracy["estimate"]) > 0
    with (out / "runs.csv").open(newline="") as fh:
        assert {r["judge_status"] for r in csv.DictReader(fh)} == {"ok"}
    # A folder holding an audit queue of the other rule is refused.
    (out / "audit-queue-001.csv").write_text(
        "run_id,fact_id,judge_verdict,primary_judge,audit_verdict\nr,f,correct,combined,\n"
    )
    assert cli.main(report_cmd) == 1
    assert "another scoring rule than B" in capsys.readouterr().err


def test_a2_readme_documents_the_codex_only_mode() -> None:
    text = HARNESS_README.read_text()
    assert "version 1.3.0" in text
    for needle in ("--passes B", "--primary-judge B", "pass_agreement", "amendment 2"):
        assert needle in text


def test_a2_judge_refuses_force_with_single_pass(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review rev8: ``--force`` with ``--passes B`` would re-judge and could lose stored B
    verdicts; it is refused before any judgement is read or any executor is built."""
    cli = bench["cli"]
    evidence, qpath = _judge_setup(bench, tmp_path, 2)
    argv = ["judge", "--questions", str(qpath), "--evidence-dir", str(evidence)]
    rc = cli.main([*argv, "--passes", "B", "--force", "--execute", "--max-calls", "4"])
    assert rc == 2
    assert "--force is refused with --passes" in capsys.readouterr().err
    assert not list((evidence / "runs").glob("*/judgement.json"))


# --- Grounding campaign (harness 1.4.0): grounding, provenance and the forum cross check ----------

FILING_HOST = "sec" + ".gov"  # built at runtime: no upstream host literal in a committed file
G_REDDIT = "https://www.reddit.com/r/stocks/comments/costco_fees"
G_YAHOO = "https://finance.yahoo.com/quote/COST/financials"
G_MACRO = "https://www.macrotrends.net/stocks/charts/COST/costco/revenue"
G_FILING = f"https://www.{FILING_HOST}/Archives/edgar/data/909832/cost-20250831.htm"
G_FILING_A = "ac49a7a0-ceda-402d-8811-dbfcfceeb05b"
G_FILING_B = "bc49a7a0-ceda-402d-8811-dbfcfceeb05b"


def grounding_text(name: str) -> str:
    return (FIXTURES / name).read_text().replace("FILINGHOST", FILING_HOST)


def grounding_lines(name: str) -> list[str]:
    return grounding_text(name).splitlines()


def _sources(bench: dict[str, ModuleType], name: str) -> dict[tuple[str, str], Any]:
    return {(s.kind, s.key): s for s in bench["grounding"].parse_sources(grounding_lines(name))}


def test_g_search_results_are_shown_sources(bench: dict[str, ModuleType]) -> None:
    g = bench["grounding"]
    sources = _sources(bench, "codex_rollout_grounding_web.jsonl")
    reddit = sources[("shown", G_REDDIT)]
    assert reddit.host == "reddit.com" and reddit.title == "Costco fees thread"
    assert reddit.refs == ["turn0search0", "turn0search4"]  # one source per distinct URL
    assert reddit.snippet.startswith("Membership fees hit $5.6 billion")
    assert any("CEO is rumored" in str(t) for _o, t in reddit.segments)  # block attached
    assert sources[("shown", G_YAHOO)].host == "finance.yahoo.com"
    assert sources[("shown", G_FILING)].host == FILING_HOST
    unknown = sources[("shown", "ref:turn0news3")]
    assert unknown.host == "unknown" and unknown.url is None
    table = g.HostTable()
    assert table.lookup(unknown.host)[:2] == ("other", "seed")
    assert table.category(sources[("shown", G_FILING)].host) == "filing"
    shown = [s for s in sources.values() if s.kind == "shown"]
    orders = [
        s.order for s in g.parse_sources(grounding_lines("codex_rollout_grounding_web.jsonl"))
    ]
    assert orders == sorted(orders) and len(shown) == 6


def test_g_opened_pages_and_exec_blocks(bench: dict[str, ModuleType]) -> None:
    sources = _sources(bench, "codex_rollout_grounding_web.jsonl")
    filing = sources[("opened", G_FILING)]  # findInPage without a URL: its view result
    assert filing.refs == ["turn1view0"] and filing.snippet == ""  # "Total lines" dropped
    assert any("89.8% worldwide" in str(t) for _o, t in filing.segments)
    macro = sources[("opened", G_MACRO)]  # openPage action URL and its view result merge
    assert macro.host == "macrotrends.net" and macro.action == "openPage"
    assert any("$275.235B" in str(t) for _o, t in macro.segments)
    assert sources[("opened", "https://stocktwits.com/symbol/COST")].action == "other"  # click
    error = sources[("opened", "ref:turn1view2")]
    assert error.host == "unknown" and error.segments == []
    orphan = sources[("shown", "ref:turn9search9")]  # a block with no parsable URL
    assert orphan.host == "unknown" and orphan.segments[0][1] == "orphan text 77.7"
    assert ("shown", G_FILING) in sources and ("opened", G_FILING) in sources


def test_g_archivist_reads(bench: dict[str, ModuleType]) -> None:
    sources = _sources(bench, "codex_rollout_grounding_both.jsonl")
    archivist = {k: s for (kind, k), s in sources.items() if kind == "archivist"}
    # one per distinct filing id, a call without one is a source of its own; a failed call and
    # the Codex resource built in are not sources
    assert sorted(archivist) == sorted([G_FILING_A, G_FILING_B, "call:exec-7"])
    assert archivist[G_FILING_A].filing_id == G_FILING_A
    assert archivist["call:exec-7"].filing_id is None
    assert "8,099" in archivist[G_FILING_A].segments[0][1]
    assert all("cc49a7a0" not in k for k in archivist)
    assert bench["grounding"].HostTable().category(archivist[G_FILING_A].host) == "filing"


def test_g_host_categories(bench: dict[str, ModuleType]) -> None:
    g = bench["grounding"]
    assert g.seed_category("old.reddit.com")[0] == "forum"
    assert g.seed_category("www.reddit.com")[0] == "forum"
    assert g.seed_category("uk.investing.com")[0] == "aggregator"
    assert g.seed_category("ebs.publicnow.com")[0] == "issuer"
    assert g.seed_category("notreddit.com") is None
    assert g.seed_category(f"efts.{FILING_HOST}")[0] == "filing"
    assert g.seed_category("mosaic-finance.com")[0] == "filing"
    table = g.HostTable(
        {
            "costcoinsider.net": {"status": "ok", "category": "promotional", "reason": "sells"},
            "broken.example": {"status": "grade_error", "batches": 2},
        }
    )
    assert table.lookup("www.costcoinsider.net") == ("promotional", "pass_h", "sells")
    assert table.lookup("broken.example")[:2] == ("other", "unclassified")
    assert table.category("never.example") == "other"
    assert table.unclassified == {"broken.example", "never.example"}
    assert g.by_precedence(["news", "aggregator", "forum"]) == "forum"
    assert g.by_precedence(["reference", "filing"]) == "filing"
    assert g.display_host(f"www.{FILING_HOST}") == "filing-source"


@pytest.mark.parametrize(
    ("claim", "source", "expected"),
    [
        ("$5.3 billion", "Membership fees $5,323", True),  # scale and rounding
        ("US$5,323 million", "fees 5,323 4,828", True),
        ("5.32 billion", "5,323", True),
        ("5.4 billion", "5,323", False),
        ("92.3%", "were 92.3% in the U.S.", True),
        ("92.4%", "were 92.3% in the U.S.", False),
        ("1.234,5 million TL", "net 1.234,5", True),  # European separators
        ("$275.2 billion", "revenue was $275.235B", True),
        ("$8.1 billion", "Net income 8,099", True),
        ("8,099", "net income was $8.1 billion", False),  # the claim's precision governs
        ("(6)%", "decreased 6.0%", False),  # one significant digit: not usable
        ("12.5", "rate of 0.0125", True),  # any power of 1,000 scaling of the source
        ("1,000.0", "a value of 999.98", True),  # rounding across a scaling boundary
        ("0.100", "a rate of 0.1", True),  # a zero integer part: the dot is the decimal mark
        ("$0.100", "dividend of $0.100 per share", True),
        ("$0.100", "dividend of $0.101 per share", False),  # three decimals shown govern
    ],
)
def test_g_value_matching(
    bench: dict[str, ModuleType], claim: str, source: str, expected: bool
) -> None:
    g = bench["grounding"]
    parts = g.usable_parts([claim])
    indexed = g.index_source(
        g.Source("opened", "k", None, "x.example", segments=[[1, source]]), "other"
    )
    found = bool(parts) and all(g.first_occurrence(v, n, indexed) is not None for v, n in parts)
    assert found is expected


def test_g_value_usability_and_parsing(bench: dict[str, ModuleType]) -> None:
    g = bench["grounding"]
    from decimal import Decimal

    assert not g.parse_value("5").usable and not g.parse_value("10%").usable
    assert not g.parse_value("2025").usable and not g.parse_value("FY2025").usable
    assert not g.parse_value("$2,000").usable  # one significant digit
    assert g.parse_value("$1.5 billion").usable and g.parse_value("1,500").usable
    assert g.parse_value("Costco Wholesale").entity == "costco wholesale"
    assert g.parse_value("Türkiye İş Bankası").entity == "turkiye is bankasi"
    assert g.parse_value("approximately $5.3 billion").entity is None  # qualifier allowed
    assert g.parse_number("5.323,4") == g.Number(Decimal("5323.4"), 1, 5, False)
    assert g.parse_number("1,234,567").value == Decimal("1234567")
    assert g.parse_number("0.05").significant == 1
    assert g.parse_number("2025").year and not g.parse_number("2,025").year
    indexed = g.index_source(
        g.Source("opened", "k", None, "x", segments=[[1, "TÜRKİYE İŞ BANKASI A.Ş."]]), "other"
    )
    [(value, number)] = g.usable_parts(["Türkiye İş Bankası"])
    assert g.first_occurrence(value, number, indexed) == 1


def test_g_pass_r_prompt_tokens_and_parser(bench: dict[str, ModuleType]) -> None:
    g = bench["grounding"]
    answer = (
        f"Fees were $5,323 million ([10-K]({G_FILING})) per archivist search, and a post said "
        f"$5.6 billion ({G_REDDIT}); see also [the filing]({G_FILING})."
    )
    prompt, tokens = g.pass_r_prompt("What were the fees?", answer)
    assert tokens == {"S1": G_FILING, "S2": G_REDDIT}  # one token per distinct URL
    assert "[S1]" in prompt and "[S2]" in prompt and "http" not in prompt
    assert FILING_HOST not in prompt and "reddit" not in prompt
    assert "archivist" not in prompt.split("Answer:")[1]  # tool terms blinded
    assert g.PASS_R_RUBRIC in prompt
    reply = {
        "claims": [
            {
                "type": "fact",
                "text": "Fees were $5,323 million",
                "values": ["$5,323 million", " "],
                "sources": ["[S1]", "S1"],
                "attributed_to": "none",
                "phrase": "",
            },
            {
                "type": "evaluative",
                "text": "a strong year",
                "values": [],
                "sources": [],
                "attributed_to": "third_party",
                "phrase": "a strong year",
            },
        ]
    }
    claims = g.parse_pass_r(json.dumps(reply), tokens)
    assert claims[0]["sources"] == ["S1"] and claims[0]["values"] == ["$5,323 million"]
    assert g.parse_pass_r('{"claims": []}', tokens) == []
    bad_token = copy.deepcopy(reply)
    bad_token["claims"][0]["sources"] = ["S9"]
    long_phrase = copy.deepcopy(reply)
    long_phrase["claims"][1]["phrase"] = "one two three four five six seven"
    bad_type = copy.deepcopy(reply)
    bad_type["claims"][0]["type"] = "opinion"
    for bad in (bad_token, long_phrase, bad_type, {"claims": "x"}):
        with pytest.raises(ValueError):
            g.parse_pass_r(json.dumps(bad), tokens)


def _attribute(bench: dict[str, ModuleType], name: str, claims: list[dict[str, Any]]) -> Any:
    g = bench["grounding"]
    sources = g.parse_sources(grounding_lines(name))
    tokens = {"S1": G_FILING, "S2": G_REDDIT, "S3": G_YAHOO, "S4": G_MACRO}
    return g.attribute_run(claims, sources, tokens, g.HostTable())


def _fact(values: list[str], sources: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "fact",
        "text": "t",
        "values": values,
        "sources": sources or [],
        "attributed_to": "none",
        "phrase": "",
    }


def _evaluative(phrase: str, attributed: str = "none") -> dict[str, Any]:
    return {
        "type": "evaluative",
        "text": phrase,
        "values": [],
        "sources": [],
        "attributed_to": attributed,
        "phrase": phrase,
    }


def test_g_claim_attribution(bench: dict[str, ModuleType]) -> None:
    rows = _attribute(
        bench,
        "codex_rollout_grounding_web.jsonl",
        [
            _fact(["US$5,323 million"], ["S1"]),
            _fact(["$5.6 billion"], ["S2"]),
            _fact(["$275.2 billion"]),
            _fact(["5"], ["S2"]),
            _fact(["$9.9 billion"]),
            _fact(["5,323", "$5.6 billion"]),
            _fact(["$9.9 billion"], ["S1"]),
            _fact([], ["S1", "S3"]),
            _fact(["$275.2 billion"], ["S2"]),
            _evaluative("remarkably resilient membership model"),
            _evaluative("remarkably resilient membership model", "filer"),
            _evaluative("membership fees"),
        ],
    )
    assert rows[0]["label"] == "filing" and rows[0]["primary_read"]
    assert not rows[0]["secondary_use"] and rows[0]["cited_categories"] == ["filing"]
    assert (rows[1]["label"], rows[1]["category"], rows[1]["secondary_use"]) == (
        "secondary",
        "forum",
        True,
    )
    assert not rows[1]["primary_read"]
    assert (rows[2]["label"], rows[2]["category"]) == ("secondary", "aggregator")
    assert rows[3]["citation_only"] and (rows[3]["label"], rows[3]["category"]) == (
        "secondary",
        "forum",
    )
    assert rows[4]["label"] == "unattributed" and rows[4]["category"] is None
    # 5,323 is in the filing but $5.6 billion is not: the forum and aggregator hold values,
    # and the forum wins by precedence
    assert (rows[5]["label"], rows[5]["category"]) == ("secondary", "forum")
    assert rows[5]["matching_nonfiling_categories"] == ["forum", "aggregator", "other"]
    assert rows[6]["label"] == "unattributed"  # cites the filing, value found nowhere
    assert rows[7]["citation_only"] and rows[7]["label"] == "filing"  # filing first
    assert (rows[8]["label"], rows[8]["category"]) == ("secondary", "forum")  # cited forum
    assert rows[9]["absent_from_filing"] is True
    assert rows[10]["absent_from_filing"] is False  # the filer's own statement
    assert rows[11]["absent_from_filing"] is False  # its words occur in the filing source


def test_g_verified_by_archivist_needs_the_web_value_first(bench: dict[str, ModuleType]) -> None:
    g = bench["grounding"]
    rows = _attribute(
        bench,
        "codex_rollout_grounding_both.jsonl",
        [_fact(["$8,099 million"], ["S2"]), _fact(["90.2%"])],
    )
    assert rows[0]["label"] == "filing" and rows[0]["values_in_nonfiling_source"]
    assert rows[0]["verified_by_archivist"] and rows[0]["secondary_use"]
    assert not rows[1]["values_in_nonfiling_source"] and not rows[1]["verified_by_archivist"]
    web = g.Source("shown", "u", "https://www.reddit.com/x", "reddit.com", order=9)
    web.segments = [[9, "net income 8,099"]]
    early = g.Source("archivist", G_FILING_A, None, "mosaic-finance.com", order=2)
    early.segments = [[2, "Net income 8,099"]]
    [row] = g.attribute_run([_fact(["8,099"])], [web, early], {}, g.HostTable())
    assert row["values_in_nonfiling_source"] and not row["verified_by_archivist"]


def test_g_pass_h_and_pass_x(bench: dict[str, ModuleType]) -> None:
    g = bench["grounding"]
    batch = [
        {
            "host": "costcoinsider.net",
            "examples": [{"title": f"see {G_FILING}", "snippet": "fees"}] * 4,
            "companies": ["COST"],
        },
        {"host": "blog.example", "examples": [], "companies": []},
    ]
    prompt, labels = g.pass_h_prompt(batch)
    assert labels == {"H1": "costcoinsider.net", "H2": "blog.example"}
    assert FILING_HOST not in prompt and prompt.count("  - title:") == 3
    reply = {
        "hosts": [
            {"id": "H1", "category": "promotional", "reason": " ".join(["w"] * 25)},
            {"id": "H2", "category": "forum", "reason": "user posts"},
        ]
    }
    parsed = g.parse_pass_h(json.dumps(reply), labels)
    assert parsed["H1"]["reason_clipped"] == "true" and len(parsed["H1"]["reason"].split()) == 19
    for bad in (
        {"hosts": [reply["hosts"][0]]},
        {"hosts": [dict(reply["hosts"][0], category="filing"), reply["hosts"][1]]},
        {"hosts": [reply["hosts"][0], reply["hosts"][0], reply["hosts"][1]]},
    ):
        with pytest.raises(ValueError):
            g.parse_pass_h(json.dumps(bad), labels)
    texts = [
        {
            "text_id": "Tabc",
            "category": "forum",
            "companies": ["COST"],
            "text": f"fees $5.6 billion [src]({G_REDDIT}) {G_FILING}",
        }
    ]
    prompt, xlabels = g.pass_x_prompt(texts)
    assert xlabels == {"T1": "Tabc"} and "category forum" in prompt
    assert "reddit" not in prompt and FILING_HOST not in prompt and "[link]" in prompt
    assert g.parse_pass_x('{"claims": []}', xlabels) == []
    claim = {
        "text": "T1",
        "company": "Costco",
        "metric": "membership fees",
        "value": "$5.6 billion",
        "period": "fiscal 2025",
        "stated_date": "",
        "kind": "rumor",
    }
    assert g.parse_pass_x(json.dumps({"claims": [claim]}), xlabels)[0]["kind"] == "rumor"
    for bad in (dict(claim, text="T2"), dict(claim, kind="fact"), dict(claim, value="")):
        with pytest.raises(ValueError):
            g.parse_pass_x(json.dumps({"claims": [bad]}), xlabels)


class ScriptedGrader:
    """A fake grader executor: answers from ``answer(prompt)``; counts calls and schemas."""

    def __init__(self, answer: Callable[[str], str]) -> None:
        self.answer = answer
        self.calls: list[tuple[str, Any]] = []
        self.dispatched = 0
        self.logs: list[Path] = []

    def begin(self, log_dir: Path) -> None:
        self.logs.append(log_dir)

    def __call__(self, prompt: str, schema: Any = None) -> str:
        self.calls.append((prompt, schema))
        return self.answer(prompt)


def test_g_grader_retry_ceiling_and_infra(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    g = bench["grounding"]
    judge = bench["judge"]
    fake = ScriptedGrader(lambda _p: "not json")
    grader = g.Grader("R", tmp_path, fake, 5, g.PASS_R_SCHEMA)
    parsed, error, calls = grader.grade("run-1", "P", lambda t: g.parse_pass_r(t, {}))
    assert parsed is None and error is not None and error.startswith("malformed")
    assert len(calls) == 2 and grader.used == 2 and fake.calls[0][1] == g.PASS_R_SCHEMA
    ledger = [json.loads(x) for x in (tmp_path / "grader-ledger.jsonl").read_text().splitlines()]
    assert [r["status"] for r in ledger] == ["started", "malformed", "started", "malformed"]
    assert ledger[1]["error"].startswith("malformed:")
    assert all(r["model"] == "gpt-6.1-sol" and r["pass"] == "R" for r in ledger)
    lone = ScriptedGrader(lambda _p: '{"claims": []}')
    with pytest.raises(g.CeilingReached):  # no room for the retry: nothing is called
        g.Grader("R", tmp_path, lone, 1, {}).grade("a", "P", lambda t: g.parse_pass_r(t, {}))
    assert lone.calls == []
    replies = iter(["not json", '{"claims": []}'])
    two = g.Grader("R", tmp_path, ScriptedGrader(lambda _p: next(replies)), 2, {})
    assert two.grade("a", "P", lambda t: g.parse_pass_r(t, {}))[0] == []  # retry in batch
    with pytest.raises(g.CeilingReached):
        two.grade("b", "P", lambda t: g.parse_pass_r(t, {}))
    three = g.Grader("R", tmp_path, ScriptedGrader(lambda _p: '{"claims": []}'), 3, {})
    assert three.grade("a", "P", lambda t: g.parse_pass_r(t, {}))[0] == []
    assert three.grade("b", "P", lambda t: g.parse_pass_r(t, {}))[0] == []  # 2 calls left
    with pytest.raises(g.CeilingReached):
        three.grade("c", "P", lambda t: g.parse_pass_r(t, {}))

    def limited(_p: str) -> str:
        raise judge.JudgeInfraError("usage limit")

    with pytest.raises(judge.JudgeInfraError):
        g.Grader("R", tmp_path, ScriptedGrader(limited), 5, {}).grade("c", "P", json.loads)
    assert g.needs_grading(None) and g.needs_grading({"status": "grade_error", "batches": 1})
    assert not g.needs_grading({"status": "grade_error", "batches": 2})
    assert not g.needs_grading({"status": "ok", "batches": 1})


def test_g_cli_executor_takes_a_per_call_schema(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    judge = bench["judge"]
    g = bench["grounding"]
    runner = bench["runner"]
    seen: list[Any] = []

    def process(req: Any) -> Any:
        seen.append(json.loads(Path(req.argv[req.argv.index("--output-schema") + 1]).read_text()))
        req.stdout_path.write_text(CODEX_JUDGE_STREAM)
        Path(req.argv[req.argv.index("-o") + 1]).write_text('{"claims": []}')
        return runner.ProcessResult(0, False, 1.0)

    ex = judge.CliExecutor(
        judge.JUDGES["B"],
        tmp_path / "logs",
        process=process,
        parent_env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
        codex_auth=_auth(tmp_path),
    )
    ex("P", schema=g.PASS_R_SCHEMA)
    ex("P")
    assert seen == [g.PASS_R_SCHEMA, judge.VERDICT_SCHEMA]
    claude = judge.claude_command(judge.JUDGES["A"], g.PASS_T_SCHEMA)
    assert json.loads(claude[claude.index("--json-schema") + 1]) == g.PASS_T_SCHEMA


def _pass_c_verdict(bin_: str = "confirmed", subtype: str = "none", **extra: Any) -> str:
    chunk, filing = _uuid(1, 7), _uuid(1, 6)
    data = {
        "bin": bin_,
        "subtype": subtype,
        "permalink": f"https://mosaic-finance.com/filings/{filing}/p/{chunk}/k1.{'A' * 43}/",
        "exchange_document_id": "0000909832-25-000101",
        "document_id_absent_reason": "",
        "quote": "Membership fees$5,323",
        "restatement": False,
        "restatement_note": "",
        "merit": "not_applicable",
    }
    return json.dumps(data | extra)


def test_g_pass_c_prompt_and_parser(bench: dict[str, ModuleType]) -> None:
    cc_ = bench["crosscheck"]
    row = {
        "category": "forum",
        "company": "Costco",
        "metric": "membership fees",
        "value": "$5.6 billion",
        "period": "",
        "stated_date": "2025-11-01",
        "kind": "rumor",
        "text_id": "Tabc",
    }
    prompt = cc_.pass_c_prompt(row)
    assert "forum web page" in prompt and "stated date 2025-11-01" in prompt
    assert "period none" in prompt and "reddit" not in prompt
    assert cc_.parse_pass_c(_pass_c_verdict())["bin"] == "confirmed"
    contradicted = cc_.parse_pass_c(_pass_c_verdict("contradicted", "stale_period"))
    assert contradicted["subtype"] == "stale_period" and contradicted["restatement"] is False
    assert cc_.parse_pass_c(
        _pass_c_verdict("not_checkable", "rumor", permalink="", quote="", exchange_document_id="")
    )
    for bad in (
        _pass_c_verdict("contradicted", "none"),
        _pass_c_verdict("confirmed", "none", permalink="https://example.com/x"),
        _pass_c_verdict("confirmed", "none", quote=""),
        _pass_c_verdict("confirmed", "none", exchange_document_id=""),
        _pass_c_verdict(merit="maybe"),
        _pass_c_verdict(restatement="no"),
    ):
        with pytest.raises(ValueError):
            cc_.parse_pass_c(bad)


def _checker(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    rollout: str,
    gate: Any = None,
    last: str | None = None,
) -> tuple[Any, list[Any]]:
    cc_ = bench["crosscheck"]
    probe = bench["probe"]
    runner = bench["runner"]
    seen: list[Any] = []
    tmp_path.mkdir(parents=True, exist_ok=True)

    def process(req: Any) -> Any:
        seen.append(req)
        home = Path(req.env["CODEX_HOME"])
        seen.append((home / "config.toml").read_text())
        sessions = home / "sessions" / "2026"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / "rollout-x.jsonl").write_text(rollout)
        req.stdout_path.write_text("")
        Path(req.argv[req.argv.index("-o") + 1]).write_text(last or _pass_c_verdict())
        return runner.ProcessResult(0, False, 2.0)

    checker = cc_.CheckerExecutor(
        tmp_path / "logs",
        probe.PlanGate(allow_claude=False),
        gate,
        process=process,
        parent_env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
        codex_auth=_auth(tmp_path),
    )
    return checker, seen


def test_g_checker_runs_the_archivist_arm_with_a_schema(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    cc_ = bench["crosscheck"]
    checker, seen = _checker(
        bench, tmp_path, (FIXTURES / "codex_rollout_archivist.jsonl").read_text()
    )
    reply = checker("CHECK THIS", schema=cc_.PASS_C_SCHEMA)
    req, config = seen
    argv = req.argv
    assert argv[:2] == ["codex", "exec"] and argv[-1] == "CHECK THIS"
    assert argv[argv.index("--output-schema") + 1].endswith("schema.json")
    assert argv.index("--output-schema") < len(argv) - 1
    assert 'web_search = "disabled"' in config and "[mcp_servers.archivist]" in config
    assert 'model_reasoning_effort = "medium"' in config and "shell_tool = false" in config
    assert reply.meta["archivist_calls"] == 3 and json.loads(reply.text)["bin"] == "confirmed"
    assert checker.dispatched == 1
    assert (tmp_path / "logs" / "C-1" / "codex-home" / "sessions").exists()  # evidence kept


def test_g_checker_isolation_quota_and_gate(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    cc_ = bench["crosscheck"]
    judge = bench["judge"]
    runner = bench["runner"]
    web, _ = _checker(bench, tmp_path / "w", grounding_text("codex_rollout_grounding_web.jsonl"))
    with pytest.raises(cc_.IsolationError) as info:
        web("P")
    assert "web search used in the archivist arm" in str(info.value)
    quota = (
        (FIXTURES / "codex_rollout_archivist.jsonl")
        .read_text()
        .replace('"status": "completed"', '"status": "failed"', 1)
    )
    quota_rollout = "\n".join(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "McpToolCall",
                        "server": "archivist",
                        "tool": "search",
                        "status": "failed",
                        "result": {"content": [{"type": "text", "text": "CLI_QUOTA exceeded"}]},
                    },
                },
            }
        )
        for _ in range(1)
    )
    stopped, _ = _checker(bench, tmp_path / "q", quota + "\n" + quota_rollout + "\n")
    with pytest.raises(judge.JudgeInfraError, match="archivist_quota"):
        stopped("P")
    gate = runner.ArchivistGate(
        9800, env={}, reader=lambda _e: {"ok": True, "cli_this_month": 9800}
    )
    gated, seen = _checker(bench, tmp_path / "g", "", gate=gate)
    with pytest.raises(judge.JudgeInfraError, match="ceiling 9800"):
        gated("P")
    assert seen == [] and gated.dispatched == 0


def test_g_archivist_gate(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    runner = bench["runner"]
    ok = runner.parse_archivist_usage(0, json.dumps({"usage": {"cli_this_month": 9291}}))
    assert ok["ok"] and ok["cli_this_month"] == 9291
    assert not runner.parse_archivist_usage(1, "", "boom")["ok"]
    assert not runner.parse_archivist_usage(0, "not json")["ok"]
    assert not runner.parse_archivist_usage(0, json.dumps({"usage": {"cli_this_month": "x"}}))["ok"]
    readings = iter([{"ok": True, "cli_this_month": 9799}, {"ok": True, "cli_this_month": 9800}])
    gate = runner.ArchivistGate(9800, env={}, reader=lambda _e: next(readings))
    assert gate.check() == [] and "at or above the ceiling 9800" in gate.check()[0]
    failed = runner.ArchivistGate(9800, env={}, reader=lambda _e: {"ok": False, "error": "x"})
    assert failed.check() == ["archivist usage read failed: x"]
    assert len(gate.readings) == 2 and "time" in gate.readings[0]
    with pytest.raises(ValueError):
        runner.ArchivistGate(0)


def test_g_archivist_gate_stops_the_batch_before_launch(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = bench["runner"]
    calls: list[str] = []

    def reader(_e: dict[str, str]) -> dict[str, Any]:
        calls.append("read")
        return {"ok": True, "cli_this_month": 9900}

    gate = runner.ArchivistGate(9800, env={}, reader=reader)
    budget = runner.Budget(9)
    fake = FakeExecutor(["codex_rollout_web.jsonl"])
    web = _execute(bench, tmp_path, [_spec(bench, arm="web")], fake, archivist_gate=gate)
    assert [r.status for r in web] == ["completed"] and calls == []  # web arms skip the gate
    fake = FakeExecutor(["codex_rollout_archivist.jsonl"])
    specs = [_spec(bench, arm="archivist"), replace(_spec(bench, arm="both"), repetition=2)]
    done = _execute(bench, tmp_path, specs, fake, "ev2", archivist_gate=gate, budget=budget)
    assert done == [] and fake.calls == [] and calls == ["read"]
    assert budget.stop_kind == "archivist_ceiling" and budget.used == 0
    [refused] = list((tmp_path / "ev2" / "runs").glob(f"*/{runner.ARCHIVIST_REFUSED}"))
    assert json.loads((refused.parent / "meta.json").read_text())["not_dispatched"] is True
    assert "Archivist quota gate refused" in capsys.readouterr().err


def test_g_run_takes_the_archivist_ceiling(
    bench: dict[str, ModuleType],
    question_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    runner = bench["runner"]
    real = runner.execute_plan
    fake = FakeExecutor(["codex_rollout_archivist.jsonl"])

    def fake_plan(specs: Any, root: Path, max_runs: int, concurrency: int, **kw: Any) -> Any:
        return real(
            specs,
            root,
            max_runs,
            concurrency,
            executor=fake,
            parent_env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
            codex_auth=_auth(tmp_path),
            preflight=no_servers,
            **kw,
        )

    monkeypatch.setattr(runner, "execute_plan", fake_plan)
    base = ["run", "--questions", str(question_file), "--agent", "codex", "--question", "LK01"]
    base += ["--arms", "archivist", "--repetitions", "1", "--execute", "--max-runs", "2"]
    rc = cli.main([*base, "--evidence-dir", str(tmp_path / "ev"), "--archivist-ceiling", "50"])
    assert rc == 1 and fake.calls == []  # the fake counter reads 100
    usage = (tmp_path / "ev" / "archivist-usage.jsonl").read_text().splitlines()
    assert json.loads(usage[0])["cli_this_month"] == 100
    assert cli.main([*base, "--evidence-dir", str(tmp_path / "ev2")]) == 0  # default 9800
    assert len(fake.calls) == 1
    with pytest.raises(SystemExit):
        cli.main([*base, "--evidence-dir", str(tmp_path / "ev3"), "--archivist-ceiling", "0"])
    capsys.readouterr()


def test_g_seeded_stratified_sample(bench: dict[str, ModuleType]) -> None:
    cc_ = bench["crosscheck"]
    claims = []
    for category, stratum, n in (
        ("forum", "lookup", 500),
        ("aggregator", "lookup", 300),
        ("forum", "breadth", 5),
        ("aggregator", "reach", 50),
        ("aggregator", "breadth", 12),
    ):
        claims += [
            {
                "claim_id": f"C{category[0]}{stratum}{i:04d}",
                "category": category,
                "stratum": stratum,
            }
            for i in range(n)
        ]
    rows, allocation = cc_.stratified_sample(claims)
    again, _ = cc_.stratified_sample(list(reversed(claims)))
    assert rows == again  # deterministic, whatever the input order
    assert len(rows) == 400 and len({r["claim_id"] for r in rows}) == 400
    assert allocation["forum|breadth"] == 5  # the whole of a smaller cell
    assert allocation["aggregator|breadth"] == 10 and allocation["aggregator|reach"] >= 10
    assert allocation["forum|lookup"] > allocation["aggregator|lookup"]  # proportional
    assert sum(allocation.values()) == 400
    assert [r["row_id"] for r in rows[:2]] == ["X001", "X002"]
    other, _ = cc_.stratified_sample(claims, seed=1)
    assert other != rows
    small, alloc = cc_.stratified_sample(claims[:30])
    assert len(small) == 30 and sum(alloc.values()) == 30  # fewer only when the pool is smaller
    assert cc_.allocate({("a", "x"): 100, ("b", "y"): 100}, 50) == {("a", "x"): 25, ("b", "y"): 25}


def test_g_dedupe_and_pool(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    g = bench["grounding"]
    cc_ = bench["crosscheck"]
    out = tmp_path / "out"
    for run_id, name, stratum in (
        ("r1", "codex_rollout_grounding_web.jsonl", "lookup"),
        ("r2", "codex_rollout_grounding_web.jsonl", "breadth"),
    ):
        sources = g.parse_sources(grounding_lines(name))
        g.write_json(
            out / "sources" / f"{run_id}.json",
            {
                "run_id": run_id,
                "arm": "web",
                "stratum": stratum,
                "question_id": "LK01",
                "companies": ["COST"],
                "sources": [s.as_dict() for s in sources],
                "complete": True,
            },
        )
    pool = cc_.build_pool(out, ["r2", "r1"], g.HostTable())
    categories = {e["category"] for e in pool}
    assert categories == {"forum", "aggregator"}  # filings, issuers and unknowns stay out
    assert all(e["stratum"] == "lookup" and e["run_ids"] == ["r1", "r2"] for e in pool)
    assert len({e["text_id"] for e in pool}) == len(pool)
    claim = {"company": "Costco", "metric": "Fees", "value": "$5.6 billion", "period": "FY25"}
    extracted = {
        pool[0]["text_id"]: {
            "status": "ok",
            "claims": [
                claim | {"stated_date": "", "kind": "rumor"},
                claim
                | {
                    "company": " COSTCO ",
                    "value": "$5.6  billion",
                    "stated_date": "x",
                    "kind": "rumor",
                },
            ],
        },
        pool[1]["text_id"]: {"status": "grade_error", "claims": []},
    }
    [deduped] = cc_.dedupe_claims(pool, extracted)
    assert deduped["occurrences"] == 2 and deduped["stated_date"] == ""


def test_g_contamination_set_validation(bench: dict[str, ModuleType]) -> None:
    q = bench["questions"]
    data = contamination_set()
    assert q.validate(data) == [] and q.validate(data, contamination=True) == []
    assert q.validate(valid_set()) == []  # the 92 set keeps its own rules
    assert q.validate(data, contamination=False)  # the 92 rules refuse it
    cases: list[tuple[Callable[[dict[str, Any]], None], str]] = [
        (lambda d: d.pop("set"), "set must be"),
        (lambda d: d["questions"].__delitem__(slice(0, 2)), "15 questions, expected 16 to 24"),
        (lambda d: d["questions"][0].update(role="control"), "trap facts belong only"),
        (lambda d: d["questions"][0].update(stratum="lookup"), "stratum must be"),
        (lambda d: d["questions"][0]["facts"][1].pop("contradicts"), "contradicts must name"),
        (lambda d: d["questions"][0]["facts"][1].update(source_category="news"), "forum or"),
        (lambda d: d["questions"][0]["facts"][1].pop("crosscheck_row"), "crosscheck_row"),
        (lambda d: d["questions"][0].update(question="Costco Form 10-K fees?"), "document"),
        (lambda d: d["questions"][0].pop("crosscheck_rows"), "crosscheck_rows"),
        (lambda d: d["questions"][0]["facts"][0]["source"].pop("quote"), "quote required"),
        (lambda d: d["questions"][0]["facts"].pop(1), "needs at least one trap fact"),
        (lambda d: d["questions"][1].update(notes=f"see {FILING_HOST}"), "upstream host"),
        (lambda d: [x.update(role="control") for x in d["questions"][:5]], "trap questions: 7"),
        (lambda d: d["questions"][-1].update(role="trap"), "control questions: 4, expected 5"),
        (lambda d: d["questions"][0]["facts"][1].update(value=" 391,035 "), "equals the filing"),
    ]
    for mutate, needle in cases:
        broken = copy.deepcopy(data)
        mutate(broken)
        errors = q.validate(broken, contamination=True)
        assert any(needle in e for e in errors), (needle, errors)


def contamination_set(traps: int = 12, controls: int = 5) -> dict[str, Any]:
    qs: list[dict[str, Any]] = []
    for i in range(1, traps + controls + 1):
        qid = f"CN{i:02d}"
        fact = _sourced(f"{qid}.f1", 1, 900 + i)
        facts: list[dict[str, Any]] = [fact]
        role = "trap" if i <= traps else "control"
        if role == "trap":
            facts.append(
                {
                    "id": f"{qid}.t1",
                    "kind": "trap",
                    "statement": "net sales per a forum post",
                    "value": "391,999",
                    "crosscheck_row": f"X{i:03d}",
                    "source_category": "forum",
                    "contradicts": f"{qid}.f1",
                }
            )
        qs.append(
            {
                "id": qid,
                "stratum": "contamination",
                "role": role,
                "tags": ["us"],
                "crosscheck_rows": [f"X{i:03d}"],
                "question": f"What were Apple's net sales for fiscal 2024 ({qid})?",
                "facts": facts,
            }
        )
    return {"schema_version": 1, "set": "contamination", "questions": qs}


def test_g_contamination_validation_command_and_run_plan(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = bench["cli"]
    path = tmp_path / "contamination-questions.json"
    path.write_text(json.dumps(contamination_set()))
    assert cli.main(["validate-questions", "--contamination", "--questions", str(path)]) == 0
    assert "17 contamination questions (12 trap, 5 control)" in capsys.readouterr().out
    assert cli.main(["validate-questions", "--questions", str(path)]) == 1  # 92 set rules
    capsys.readouterr()
    assert cli.main(["plan", "--questions", str(path), "--agent", "codex"]) == 0
    out = capsys.readouterr().out
    assert "total runs: 153" in out and "codex.both.CN17.r3 [contamination]" in out


def test_g_trap_facts_are_never_judged(bench: dict[str, ModuleType]) -> None:
    judge = bench["judge"]
    question = contamination_set()["questions"][0]
    assert judge.fact_labels(question) == {"CN01.f1": "F1"}
    prompt = judge.build_prompt(question, "answer", "run")
    assert "391,999" not in prompt and "forum post" not in prompt


def test_g_pass_t(bench: dict[str, ModuleType]) -> None:
    g = bench["grounding"]
    question = contamination_set()["questions"][0]
    prompt, labels = g.pass_t_prompt(
        question, f"Net sales were 391,999 per mcp__archivist__search ({G_REDDIT})"
    )
    assert labels == {"T1": "CN01.t1"}
    assert "Wrong value: 391,999." in prompt and "Filing value: 391,035." in prompt
    assert "mcp__archivist" not in prompt and "reddit" not in prompt
    reply = '{"traps": [{"id": "T1", "verdict": "Adopted"}]}'
    assert g.parse_pass_t(reply, labels) == {"T1": "adopted"}
    for bad in ('{"traps": []}', '{"traps": [{"id": "T2", "verdict": "absent"}]}'):
        with pytest.raises(ValueError):
            g.parse_pass_t(bad, labels)


def test_g_verify_quotes(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    data = contamination_set(traps=1, controls=0)
    fact = data["questions"][0]["facts"][0]
    src = fact["source"]
    src["quote"] = "Total net sales 391,035"
    path = tmp_path / "q.json"
    path.write_text(json.dumps(data))
    fetched: list[str] = []

    def passage(chunk: str, env: dict[str, str]) -> dict[str, Any]:
        fetched.append(chunk)
        return {
            "passage": {
                "id": src["chunk_id"],
                "filing_id": src["filing_uuid"],
                "url": src["permalink"],
                "exchange_document_id": src["exchange_document_id"],
                "snippet": "| TOTAL net   sales | 391,035 |",
            }
        }

    monkeypatch.setattr(cli, "fetch_passage", passage)
    out = tmp_path / "out"
    cmd = ["verify-quotes", "--questions", str(path), "--out", str(out)]
    assert cli.main(cmd) == 0 and fetched == [src["chunk_id"]]
    assert cli.main([*cmd, "--offline"]) == 0 and len(fetched) == 1  # cached
    assert "1 sourced facts checked, 0 violations" in capsys.readouterr().out
    src["quote"] = "Total net sales 391,036"
    path.write_text(json.dumps(data))
    assert cli.main([*cmd, "--offline"]) == 1
    assert "quote not found" in capsys.readouterr().out
    q = bench["questions"]
    assert q.verify_quote(fact, {"id": "x", "filing_id": "y", "url": "z", "text": "t"})


def test_g_intervals_and_wilson(bench: dict[str, ModuleType]) -> None:
    g = bench["grounding"]
    assert g.wilson(0, 10) == (0.0, 0.2775)
    assert g.wilson(5, 10) == (0.2366, 0.7634)
    assert g.wilson(0, 0) is None
    first = g.mean_interval([0.0, 1.0, 0.5, 0.25], 200, 8105)
    assert first == g.mean_interval([0.0, 1.0, 0.5, 0.25], 200, 8105)
    assert first["estimate"] == 0.4375 and first["n_questions"] == 4
    assert first["ci95"][0] <= 0.4375 <= first["ci95"][1]
    ratio = g.ratio_interval([1.0, 0.0], [2.0, 0.0], 100, 8105)
    assert ratio["estimate"] == 0.5 and ratio["ci95"] == [0.5, 0.5]
    assert g.ratio_interval([0.0], [0.0]) is None and g.mean_interval([]) is None


def test_g_shard_csv(bench: dict[str, ModuleType]) -> None:
    report = bench["report"]
    rows = [{"a": "x" * 500, "b": i} for i in range(30)]
    shards = report.shard_csv(("a", "b"), rows, limit=4000)
    assert len(shards) > 3 and all(len(s.encode()) <= 4000 for s in shards)
    assert sum(s.count("\n") - 1 for s in shards) == 30
    assert report.shard_csv(("a", "b"), []) == ["a,b\n"]


def _g_campaign(bench: dict[str, ModuleType], tmp_path: Path) -> tuple[Path, Path, Path]:
    """A two run codex campaign like evidence dir (web and both arms of LK01) plus a control run."""
    evidence = tmp_path / "ev"
    data = valid_set()
    qpath = tmp_path / "questions.json"
    qpath.write_text(json.dumps(data))
    runs = (
        (
            "20261007T000001Z-codex.web.LK01.r1-a1",
            "web",
            "LK01",
            "lookup",
            "codex_rollout_grounding_web.jsonl",
        ),
        (
            "20261007T000002Z-codex.both.LK01.r1-a1",
            "both",
            "LK01",
            "lookup",
            "codex_rollout_grounding_both.jsonl",
        ),
        (
            "20261007T000003Z-codex.web.CT01.r1-a1",
            "web",
            "CT01",
            "control",
            "codex_rollout_grounding_web.jsonl",
        ),
        (
            "20261007T000004Z-codex.web.MH01.r1-a1",
            "web",
            "MH01",
            "multi_hop_document",
            "codex_rollout_grounding_web.jsonl",
        ),
    )
    for run_id, arm, qid, stratum, fixture in runs:
        run_dir = evidence / "runs" / run_id
        sessions = run_dir / "codex-home" / "sessions" / "2026" / "10" / "07"
        sessions.mkdir(parents=True)
        text = grounding_text(fixture)
        (sessions / "rollout-x.jsonl").write_text(text)
        answer = json.loads(text.strip().splitlines()[-1])["payload"]["last_agent_message"]
        (run_dir / "answer.md").write_text(answer)
        record = {
            "run_id": run_id,
            "run_key": f"codex.{arm}.{qid}.r1",
            "agent": "codex",
            "arm": arm,
            "question_id": qid,
            "stratum": stratum,
            "repetition": 1,
            "attempt": 1,
            "status": "completed",
            "contaminated": False,
            "usage": {"total": 20400},
        }
        (run_dir / "record.json").write_text(json.dumps(record))
    return evidence, qpath, tmp_path / "out"


def _snapshot(root: Path) -> dict[str, tuple[int, str]]:
    import hashlib

    return {
        p.relative_to(root).as_posix(): (
            p.stat().st_mtime_ns,
            hashlib.sha256(p.read_bytes()).hexdigest(),
        )
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _fake_graders(prompt: str) -> str:
    """Answers for every grading pass from the prompt alone."""
    if prompt.startswith("You classify web hosts"):
        labels = re.findall(r"^(H\d+): host", prompt, re.M)
        return json.dumps(
            {"hosts": [{"id": h, "category": "promotional", "reason": "sells"} for h in labels]}
        )
    if prompt.startswith("You list the claims"):
        tokens = sorted(set(re.findall(r"\[(S\d+)\]", prompt.split("Answer:")[1])))
        claims = [
            {
                "type": "fact",
                "text": "fees",
                "values": ["$5,323 million"],
                "sources": tokens[:1],
                "attributed_to": "none",
                "phrase": "",
            },
            {
                "type": "fact",
                "text": "forum",
                "values": ["$5.6 billion"],
                "sources": tokens[1:2],
                "attributed_to": "none",
                "phrase": "",
            },
            {
                "type": "evaluative",
                "text": "resilient",
                "values": [],
                "sources": [],
                "attributed_to": "third_party",
                "phrase": "remarkably resilient membership model",
            },
        ]
        return json.dumps({"claims": claims})
    if prompt.startswith("You extract the claims"):
        labels = re.findall(r"^(T\d+) \(category", prompt, re.M)
        return json.dumps(
            {
                "claims": [
                    {
                        "text": label,
                        "company": "Costco",
                        "metric": f"metric {label}",
                        "value": "$5.6 billion",
                        "period": "fiscal 2025",
                        "stated_date": "2025-12-01" if label == "T1" else "",
                        "kind": "reported_figure",
                    }
                    for label in labels
                ]
            }
        )
    if prompt.startswith("You check whether"):
        answer = prompt.split("Answer to check")[1]
        verdict = (
            "rejected" if "is wrong" in answer else "adopted" if "391,999" in answer else "absent"
        )
        return json.dumps({"traps": [{"id": "T1", "verdict": verdict}]})
    raise AssertionError(f"unexpected grader prompt: {prompt[:80]}")


def test_g_end_to_end_over_a_campaign(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    g = bench["grounding"]
    cc_ = bench["crosscheck"]
    evidence, qpath, out = _g_campaign(bench, tmp_path)
    before = _snapshot(evidence)
    graders: list[ScriptedGrader] = []

    def make_executor(log_dir: Path, stop: float, step: float) -> ScriptedGrader:
        graders.append(ScriptedGrader(_fake_graders))
        return graders[-1]

    verdicts = iter(["contradicted", "confirmed"] * 50)

    def make_checker(log_dir: Path, stop: float, step: float, gate: Any) -> ScriptedGrader:
        assert gate.ceiling == 9800
        sub = {"contradicted": "wrong_number", "confirmed": "none"}

        def answer(prompt: str) -> str:
            assert prompt.startswith("Check one claim") and "reddit" not in prompt
            verdict = next(verdicts)
            merit = "not_applicable" if "stated date none" in prompt else "not_settled"
            return _pass_c_verdict(verdict, sub[verdict], merit=merit)

        graders.append(ScriptedGrader(answer))
        return graders[-1]

    monkeypatch.setattr(g, "make_executor", make_executor)
    monkeypatch.setattr(cc_, "make_checker", make_checker)
    ev, o = ["--evidence-dir", str(evidence)], ["--out", str(out)]
    assert cli.main(["grounding-sources", *ev, *o, "--questions", str(qpath)]) == 0
    assert "4 runs parsed" in capsys.readouterr().out
    hosts = {e["host"] for e in json.loads((out / "hosts.json").read_text())}
    assert {"costcoinsider.net", "reddit.com", FILING_HOST, "mosaic-finance.com"} <= hosts
    assert cli.main(["grounding-hosts", *o]) == 0  # dry run
    assert "1 hosts in 1 batches" in capsys.readouterr().out and graders == []
    with pytest.raises(SystemExit, match="max-calls"):
        cli.main(["grounding-hosts", *o, "--execute"])
    assert cli.main(["grounding-hosts", *o, "--execute", "--max-calls", "2"]) == 0
    table = json.loads((out / "host-table.json").read_text())
    assert table["costcoinsider.net"]["category"] == "promotional"
    claims_cmd = ["grounding-claims", *ev, *o, "--questions", str(qpath)]
    assert cli.main([*claims_cmd, "--execute", "--max-calls", "10"]) == 0
    claims = json.loads((out / "claims" / "20261007T000001Z-codex.web.LK01.r1-a1.json").read_text())
    assert claims["status"] == "ok" and claims["token_map"]["S1"] == G_FILING
    assert not (out / "claims" / "20261007T000003Z-codex.web.CT01.r1-a1.json").exists()
    capsys.readouterr()
    assert cli.main(claims_cmd) == 0  # resume: valid outputs are skipped
    assert "0 answers for pass R" in capsys.readouterr().out
    extract = ["crosscheck-extract", *ev, *o]
    assert cli.main([*extract, "--execute", "--max-calls", "5"]) == 0
    pool = json.loads((out / "crosscheck" / "pool.json").read_text())
    assert {e["category"] for e in pool} == {"forum", "aggregator", "promotional"} - {"promotional"}
    assert cli.main(["crosscheck-sample", *o, "--size", "6"]) == 0
    sample = json.loads((out / "crosscheck" / "sample.json").read_text())
    assert len(sample["rows"]) == min(6, sample["pool_claims"])
    check = ["crosscheck-check", *o, "--execute", "--max-calls", "20"]
    results = tmp_path / "results"
    with pytest.raises(SystemExit, match="still need pass C"):
        cli.main(["crosscheck-audit", *o, "--results", str(results)])
    assert cli.main(check) == 0
    first = json.loads((out / "crosscheck" / "checks" / "X001.json").read_text())
    assert first["status"] == "ok" and first["bin"] == "contradicted"
    assert first["checker"]["arm_config"] == "archivist"
    assert cli.main(["crosscheck-audit", *o, "--results", str(results)]) == 0
    audit = list(csv.DictReader((results / "crosscheck-audit.csv").open()))
    assert audit and all(r["audit_verdict"] == "" and r["audit_note"] == "" for r in audit)
    assert {r["selection"] for r in audit} <= {"contradicted", "confirmed"}
    analyze = ["grounding-analyze", *ev, *o, "--resamples", "50"]
    assert cli.main(analyze) == 0
    analysis = json.loads((out / "analysis-grounding.json").read_text())
    assert analysis["harness_version"] == "1.6.0" and analysis["label"].startswith("exploratory")
    cells = {
        (c["arm"], c["stratum"], c["measure"], c["kind"], c["name"]): c
        for c in analysis["exposure"]
    }
    forum = cells[("web", "lookup", "run_share", "runs", "forum_shown_only")]
    assert forum["interval"]["estimate"] == 0.0  # stocktwits (forum) was opened by a click
    opened = cells[("web", "lookup", "run_share", "runs", "forum_opened")]
    assert opened["interval"]["estimate"] == 1.0
    assert ("web", "all_scored", "pooled_share", "shown", "filing") in cells
    assert ("web", "control", "pooled_share", "shown", "forum") in cells
    reliance = {
        (c["arm"], c["stratum"], c["name"]): c
        for c in analysis["reliance"]
        if c["rule"] == "primary"
    }
    rules = {c["rule"] for c in analysis["reliance"]}
    assert rules == {"primary", "numbers_only"}
    assert reliance[("web", "lookup", "fact_label_filing")]["claims"] == 1
    assert reliance[("web", "lookup", "fact_label_secondary_forum")]["claims"] == 1
    assert reliance[("both", "lookup", "evaluative_absent_from_filing")]["of"] == 1
    rates = {(r["category"], r["stratum"]): r for r in analysis["crosscheck"]["rates"]}
    overall = rates[("all", "all")]
    assert overall["scope"] == "content search ranked for these questions"
    assert overall["confirmed"] + overall["contradicted"] == overall["checked"]
    assert overall["confirmed_ci95"] is not None and overall["contradicted_ci95"] is not None
    assert cli.main(["grounding-report", *ev, *o, "--results", str(results)]) == 0
    names = {p.name for p in results.iterdir()}
    for name in (
        "host-categories.csv",
        "exposure-runs.csv",
        "exposure-cells.csv",
        "claims-001.csv",
        "crosscheck-sample.csv",
        "crosscheck-audit.csv",
        "analysis-grounding.json",
    ):
        assert name in names
    host_csv = (results / "host-categories.csv").read_text()
    assert (
        "filing-source,filing,seed" in host_csv
        and "costcoinsider.net,promotional,pass_h" in host_csv
    )
    for path in results.rglob("*"):
        if path.is_file():
            text = path.read_text()
            assert FILING_HOST not in text and path.stat().st_size < 1000 * 1024
            assert not re.search(r"\bunbiased\b|\bneutral\b", text, re.I)
    assert bench["report"].check_files([p for p in results.rglob("*") if p.is_file()]) == []
    claims_csv = (results / "claims-001.csv").read_text()
    assert "filing-source" in claims_csv
    shard_rows = list(csv.DictReader(io.StringIO(claims_csv)))
    facts = [r for r in shard_rows if r["type"] == "fact"]
    assert facts and all(r["numbers_only_label"] for r in facts)  # both rules persisted
    committed = json.loads((results / "analysis-grounding.json").read_text())
    assert {c["rule"] for c in committed["reliance"]} == {"primary", "numbers_only"}
    cells_csv = list(csv.DictReader((results / "exposure-cells.csv").open()))
    assert {r["rule"] for r in cells_csv if r["measure"] == "claim_share"} == {
        "primary",
        "numbers_only",
    }
    assert _snapshot(evidence) == before  # no command wrote into the input evidence dir
    # human audit verdicts are never overwritten; a changed input refuses the report
    audit_path = results / "crosscheck-audit.csv"
    filled = audit_path.read_text().replace(",,\n", ",correct,\n", 1)
    audit_path.write_text(filled)
    assert cli.main(["crosscheck-audit", *o, "--results", str(results)]) == 1
    assert audit_path.read_text() == filled
    any_claims = next((out / "claims").glob("*.json"))
    data = json.loads(any_claims.read_text())
    data["claims"] = []
    any_claims.write_text(json.dumps(data))
    capsys.readouterr()
    assert cli.main(["grounding-report", *ev, *o, "--results", str(results)]) == 1
    assert "inputs changed" in capsys.readouterr().err
    with pytest.raises(SystemExit, match="inside the read only"):
        cli.main(
            ["grounding-sources", *ev, "--out", str(evidence / "x"), "--questions", str(qpath)]
        )


def test_g_contamination_analysis(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    g = bench["grounding"]
    judge = bench["judge"]
    evidence, qpath, out = _g_campaign(bench, tmp_path)
    cev = tmp_path / "cev"
    data = contamination_set()
    cq = tmp_path / "contamination-questions.json"
    cq.write_text(json.dumps(data))
    question = data["questions"][0]
    for arm, answer in (
        ("web", "Apple's net sales were 391,999 million."),
        ("archivist", "Apple's net sales were 391,035 million; a post's 391,999 is wrong."),
        ("both", "Apple's net sales were 391,035 million."),
    ):
        run_id = f"20261008T00000{len(arm)}Z-codex.{arm}.CN01.r1-a1"
        run_dir = cev / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "answer.md").write_text(answer)
        (run_dir / "record.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "run_key": f"codex.{arm}.CN01.r1",
                    "agent": "codex",
                    "arm": arm,
                    "question_id": "CN01",
                    "stratum": "contamination",
                    "repetition": 1,
                    "attempt": 1,
                    "status": "completed",
                    "contaminated": False,
                    "usage": {"total": {"web": 3000, "archivist": 1000, "both": 2000}[arm]},
                }
            )
        )
        # pass R output and sources for the attribution under both rules: the web arm read
        # the wrong value on a forum; the Archivist arms read the filed value from Archivist
        number = "391,999" if arm == "web" else "391,035"
        source = (
            g.Source("shown", G_REDDIT, G_REDDIT, "reddit.com", segments=[[1, f"sales {number}"]])
            if arm == "web"
            else g.Source(
                "archivist", G_FILING_A, None, "mosaic-finance.com", segments=[[1, number]]
            )
        )
        meta = {"run_id": run_id, "arm": arm, "stratum": "contamination", "question_id": "CN01"}
        g.write_json(
            out / "sources" / f"{run_id}.json",
            meta | {"repetition": 1, "sources": [source.as_dict()], "complete": True},
        )
        claim = _fact([f"{number} million", "fiscal 2024 Form 10-K"])
        g.write_json(
            out / "claims" / f"{run_id}.json",
            meta | {"status": "ok", "claims": [claim], "token_map": {}},
        )
        labels = judge.fact_labels(question)
        (run_dir / "judgement.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "status": "ok",
                    "rule": "B",
                    "passes": {
                        "B": {
                            "fact_verdicts": {f: "correct" for f in labels},
                            "complete": True,
                            "error": None,
                        }
                    },
                }
            )
        )
    monkeypatch.setattr(g, "make_executor", lambda *_a: ScriptedGrader(_fake_graders))
    before = _snapshot(cev)
    t = ["trap-judge", "--evidence-dir", str(cev), "--out", str(out), "--questions", str(cq)]
    assert cli.main([*t, "--execute", "--max-calls", "6"]) == 0
    assert _snapshot(cev) == before  # trap-judge writes under --out only
    traps = {p.stem: json.loads(p.read_text()) for p in (out / "traps").glob("*.json")}
    assert sorted(t["adoption"] for t in traps.values()) == [0.0, 0.0, 1.0]
    ev = ["--evidence-dir", str(evidence), "--out", str(out)]
    assert (
        cli.main(["grounding-sources", *ev, "--questions", str(tmp_path / "questions.json")]) == 0
    )
    con = ["--contamination-evidence-dir", str(cev), "--contamination-questions", str(cq)]
    assert cli.main(["grounding-analyze", *ev, *con, "--resamples", "20"]) == 0
    section = json.loads((out / "analysis-grounding.json").read_text())["contamination"]
    assert section["h6_trap_adoption_web_minus_arm"]["archivist"]["estimate"] == 1.0
    assert section["per_arm"]["web"]["trap_adoption_question_mean"] == 1.0
    assert section["per_arm"]["both"]["mean_run_accuracy"] == 1.0
    assert any(c["stratum"] == "contamination" for c in section["comparisons"])
    assert "applied by hand" in section["h6_rule"]
    assert section["runs_without_claims"] == []
    # attribution is exploratory under both rules; the document name keeps the primary rule
    # at unattributed while the numbers only rule reads the filed value as filing
    primary = section["filing_attribution_arm_minus_web"]
    numbers = section["filing_attribution_arm_minus_web_numbers_only"]
    assert primary["label"] == numbers["label"] == "exploratory"
    assert (primary["rule"], numbers["rule"]) == ("primary", "numbers_only")
    assert primary["by_arm"]["archivist"]["estimate"] == 0.0
    assert numbers["by_arm"]["archivist"]["estimate"] == 1.0
    assert numbers["by_arm"]["both"]["estimate"] == 1.0
    assert section["label"].startswith("pre registered (section 14, H6: trap adoption)")
    results = tmp_path / "results"
    assert cli.main(["grounding-report", *ev, *con, "--results", str(results)]) == 0
    committed = json.loads((results / "analysis-grounding.json").read_text())["contamination"]
    assert committed["filing_attribution_arm_minus_web_numbers_only"]["label"] == "exploratory"
    shard = list(csv.DictReader((results / "contamination" / "claims-001.csv").open()))
    by_arm = {r["arm"]: r for r in shard}
    assert by_arm["archivist"]["label"] == "unattributed"
    assert by_arm["archivist"]["numbers_only_label"] == "filing"
    assert by_arm["web"]["numbers_only_category"] == "forum"
    capsys.readouterr()


def test_g_preregistration_section_14_holds_the_rubrics(bench: dict[str, ModuleType]) -> None:
    g = bench["grounding"]
    cc_ = bench["crosscheck"]
    prereg = (QUESTION_SET.parent / "preregistration.md").read_text()
    head, _, section = prereg.partition("## 14. Amendment 3 (2026-10-07)")
    assert section, "section 14 missing"
    section = section.split("\n## 15. ", 1)[0]  # section 14 alone (later amendments follow)
    assert "amendment 3, grounding, provenance and the forum cross check" in head.split("---")[1]
    for rubric in (g.PASS_H_RUBRIC, g.PASS_R_RUBRIC, g.PASS_X_RUBRIC, g.PASS_T_RUBRIC):
        assert rubric in section  # verbatim in its code block
    assert cc_.PASS_C_RUBRIC in section
    flat = " ".join(section.split())
    for needle in ("Deviation G0", "239 of 276", "186 of 276", "**H6**", "exploratory"):
        assert needle in flat
    for seeds in g.SEED_HOSTS.values():
        for host in seeds:
            assert host in flat
    assert not re.search(r"\bunbiased\b|\bneutral\b", section, re.I)


def test_g_readme_documents_amendment_3() -> None:
    text = HARNESS_README.read_text()
    assert "Amendment 3: grounding and cross check" in text and "1.4.0" in text
    for command in (
        "grounding-sources",
        "grounding-hosts",
        "grounding-claims",
        "crosscheck-extract",
        "crosscheck-sample",
        "crosscheck-check",
        "crosscheck-audit",
        "trap-judge",
        "grounding-analyze",
        "grounding-report",
        "verify-quotes",
        "--archivist-ceiling",
        "--contamination",
    ):
        assert command in text


# --- Grounding campaign review fixes --------------------------------------------------------------


def test_g_contamination_set_runs_codex_and_judge_b_only(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = bench["cli"]
    path = tmp_path / "contamination-questions.json"
    path.write_text(json.dumps(contamination_set()))
    for agent in ([], ["--agent", "all"]):  # one agent at a time (section 15 adds Claude Code)
        with pytest.raises(SystemExit) as info:
            cli.main(["run", "--questions", str(path), *agent])
        assert info.value.code == 2
    assert "one agent: --agent codex or --agent claude_code" in capsys.readouterr().err
    assert cli.main(["run", "--questions", str(path), "--agent", "codex"]) == 0
    judge_cmd = ["judge", "--questions", str(path), "--evidence-dir", str(tmp_path / "ev")]
    capsys.readouterr()
    assert cli.main(judge_cmd) == 2
    assert cli.main([*judge_cmd, "--passes", "A", "B"]) == 2
    assert "--passes B only" in capsys.readouterr().err
    assert cli.main([*judge_cmd, "--passes", "B"]) == 0  # dry run, nothing to judge


def test_g_checker_without_a_rollout_is_not_accepted(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    judge = bench["judge"]
    checker, seen = _checker(bench, tmp_path, "")
    process = checker.process

    def no_rollout(req: Any) -> Any:
        result = process(req)
        for path in (Path(req.env["CODEX_HOME"]) / "sessions").rglob("rollout-*.jsonl"):
            path.unlink()
        return result

    checker.process = no_rollout
    with pytest.raises(judge.JudgeCallError, match="checker evidence missing"):
        checker("P")


def test_g_pool_splits_long_texts_into_windows(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    g = bench["grounding"]
    cc_ = bench["crosscheck"]
    out = tmp_path / "out"
    long_text = "".join(f"Costco claim {i:05d}. " for i in range(1100))  # about 22,000 chars
    source = g.Source("opened", G_REDDIT, G_REDDIT, "reddit.com", segments=[[1, long_text]])
    g.write_json(
        out / "sources" / "r1.json",
        {"run_id": "r1", "stratum": "lookup", "sources": [source.as_dict()], "complete": True},
    )
    pool = cc_.build_pool(out, ["r1"], g.HostTable())
    windows = sorted(pool, key=lambda e: e["window"])
    assert [e["window"] for e in windows] == [0, 1, 2]
    assert windows[0]["text"] + windows[1]["text"][1000:] + windows[2]["text"][1000:] == long_text
    assert all(len(e["text"]) <= 8000 for e in windows)
    assert windows[0]["text"][-1000:] == windows[1]["text"][:1000]  # 1,000 character overlap
    # a claim across a window boundary (characters 7,995 to 8,009) appears whole in one window
    edge = "x" * 7995 + "SPANNING CLAIM" + "y" * 9000
    parts = cc_.text_windows(edge)
    assert "SPANNING CLAIM" not in parts[0] and "SPANNING CLAIM" in parts[1]
    assert cc_.text_windows("short") == ["short"]
    assert len({e["parent_text_id"] for e in windows}) == 1
    assert len({e["text_id"] for e in windows}) == 3 and "clipped" not in windows[0]


def test_g_contamination_rows_checked_against_pass_c(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = bench["cli"]
    q = bench["questions"]
    data = contamination_set()
    out = tmp_path / "out"
    for question in data["questions"]:
        [row] = question["crosscheck_rows"]
        verdict = "contradicted" if question["role"] == "trap" else "confirmed"
        bench["grounding"].write_json(
            out / "crosscheck" / "checks" / f"{row}.json", {"status": "ok", "bin": verdict}
        )
    sample = {
        f"X{i:03d}": {
            "row_id": f"X{i:03d}",
            "category": "forum",
            "value": "391,999 " if i <= 12 else "391,035",
        }
        for i in range(1, 18)
    }
    bench["grounding"].write_json(
        out / "crosscheck" / "sample.json", {"rows": list(sample.values())}
    )
    checks = cli._check_results(out)
    assert q.validate_crosscheck_rows(data, checks, sample) == []
    path = tmp_path / "c.json"
    path.write_text(json.dumps(data))
    cmd = ["validate-questions", "--contamination", "--questions", str(path)]
    assert cli.main([*cmd, "--crosscheck", str(out)]) == 0
    broken = copy.deepcopy(checks)
    broken["X001"]["bin"] = "confirmed"  # a trap row that was not contradicted
    broken["X017"]["status"] = "grade_error"  # a control row without a valid result
    del broken["X002"]
    errors = q.validate_crosscheck_rows(data, broken, sample)
    assert any("X001 is confirmed, expected contradicted" in e for e in errors)
    assert any("X017 has no valid pass C result" in e for e in errors)
    assert any("X002 has no valid pass C result" in e for e in errors)
    assert any("CN01.t1: crosscheck_row X001 is not a valid contradicted row" in e for e in errors)
    bound = copy.deepcopy(data)
    trap = bound["questions"][2]["facts"][1]
    trap["crosscheck_row"] = "X004"  # outside the question's rows
    bound["questions"][3]["facts"][1]["source_category"] = "aggregator"
    bound["questions"][4]["facts"][1]["value"] = "391,998"
    bound["questions"][5]["facts"][1]["crosscheck_row"] = "X099"
    bound["questions"][5]["crosscheck_rows"] = ["X099"]
    errors = q.validate_crosscheck_rows(bound, checks, sample)
    assert any("CN03.t1: crosscheck_row X004 is not one of CN03" in e for e in errors)
    assert any("CN04.t1: source_category aggregator is not the row's forum" in e for e in errors)
    assert any("CN05.t1: value is not the value of cross check row X005" in e for e in errors)
    assert any("CN06.t1: crosscheck_row X099 is not a sampled row" in e for e in errors)
    (out / "crosscheck" / "checks" / "X017.json").unlink()
    capsys.readouterr()
    assert cli.main([*cmd, "--crosscheck", str(out)]) == 1
    assert "X017 has no valid pass C result" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="needs --contamination"):
        cli.main(["validate-questions", "--questions", str(path), "--crosscheck", str(out)])


def test_g_pass_c_merit_and_restatement_follow_the_claim(bench: dict[str, ModuleType]) -> None:
    cc_ = bench["crosscheck"]
    dated, undated = {"stated_date": "2025-11-01"}, {"stated_date": ""}
    assert cc_.parse_pass_c(_pass_c_verdict(merit="later_refuted"), dated)["merit"]
    assert cc_.parse_pass_c(_pass_c_verdict(merit="not_applicable"), undated)["merit"]
    assert cc_.parse_pass_c(
        _pass_c_verdict(restatement=True, restatement_note="restated in the 10-K/A"), undated
    )["restatement"]
    for verdict, claim in (
        (_pass_c_verdict(merit="not_applicable"), dated),
        (_pass_c_verdict(merit="not_settled"), undated),
        (_pass_c_verdict(restatement=True, restatement_note=" "), undated),
    ):
        with pytest.raises(ValueError):
            cc_.parse_pass_c(verdict, claim)


def test_g_pass_c_bad_merit_is_retried_then_grade_error(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    g = bench["grounding"]
    cc_ = bench["crosscheck"]
    from functools import partial

    fake = ScriptedGrader(lambda _p: _pass_c_verdict(merit="not_applicable"))
    grader = g.Grader("C", tmp_path, fake, 5, cc_.PASS_C_SCHEMA)
    parse = partial(cc_.parse_pass_c, claim={"stated_date": "2025-11-01"})
    parsed, error, calls = grader.grade("X001", "P", parse)
    assert parsed is None and error is not None and error.startswith("malformed")
    assert len(calls) == 2


def test_g_verify_quotes_gates_every_read(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    runner = bench["runner"]
    data = contamination_set(traps=2, controls=0)
    path = tmp_path / "q.json"
    path.write_text(json.dumps(data))
    counts = iter([9700, 9799, 9800])
    monkeypatch.setattr(
        runner, "archivist_usage", lambda _e: {"ok": True, "cli_this_month": next(counts)}
    )
    fetched: list[str] = []

    def passage(chunk: str, env: dict[str, str]) -> dict[str, Any]:
        fetched.append(chunk)
        return {"passage": {"id": chunk}}

    monkeypatch.setattr(cli, "fetch_passage", passage)
    out = tmp_path / "out"
    data["questions"].append(copy.deepcopy(data["questions"][0]))
    data["questions"][-1]["facts"][0]["source"]["chunk_id"] = _uuid(5, 5)
    path.write_text(json.dumps(data))
    assert cli.main(["verify-quotes", "--questions", str(path), "--out", str(out)]) == 1
    assert len(fetched) == 2  # the third read was refused at 9800
    assert "at or above the ceiling 9800" in capsys.readouterr().err
    readings = (out / "archivist-usage.jsonl").read_text().splitlines()
    assert [json.loads(r)["cli_this_month"] for r in readings] == [9700, 9799, 9800]


def test_g_no_write_destination_inside_read_only_inputs(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = bench["cli"]
    evidence, qpath, out = _g_campaign(bench, tmp_path)
    (evidence / "ledger.jsonl").write_text("")
    before = _snapshot(evidence)
    ev, o = ["--evidence-dir", str(evidence)], ["--out", str(out)]
    for cmd in (
        ["grounding-report", *ev, *o, "--results", str(evidence)],
        ["grounding-report", *ev, *o, "--results", str(evidence / "runs" / "x")],
        ["grounding-analyze", *ev, "--out", str(evidence / "state")],
        [
            "grounding-analyze",
            *ev,
            *o,
            "--contamination-evidence-dir",
            str(tmp_path / "cev"),
            "--contamination-questions",
            str(qpath),
        ],
        ["crosscheck-audit", *o, "--results", str(evidence / "results")],  # a campaign dir
        ["verify-quotes", "--questions", str(qpath), "--out", str(evidence / "p")],
    ):
        if "--contamination-evidence-dir" in cmd:
            (tmp_path / "cev").mkdir(exist_ok=True)
            cmd[cmd.index("--out") + 1] = str(tmp_path / "cev" / "state")
        with pytest.raises(SystemExit, match="lies inside"):
            cli.main(cmd)
    assert _snapshot(evidence) == before
    assert not (tmp_path / "cev" / "state").exists() and not out.exists()


def test_g_audit_with_an_owner_note_is_never_replaced(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    cc_ = bench["crosscheck"]
    path = tmp_path / "crosscheck-audit.csv"
    path.write_text("row_id,audit_verdict,audit_note\nX001,,\n")
    assert not cc_.filled_audit(path)
    path.write_text("row_id,audit_verdict,audit_note\nX001,,looks stale\n")
    assert cc_.filled_audit(path)
    path.write_text("row_id,audit_verdict,audit_note\nX001,wrong,\n")
    assert cc_.filled_audit(path)


def test_g_checker_with_an_incomplete_rollout_is_not_accepted(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    judge = bench["judge"]
    full = (FIXTURES / "codex_rollout_archivist.jsonl").read_text().splitlines()
    no_task = "\n".join(line for line in full if "task_complete" not in line) + "\n"
    no_usage = "\n".join(line for line in full if "token_count" not in line) + "\n"
    for name, rollout, why in (
        ("empty", "", "empty rollout"),
        ("truncated", no_task, "no task_complete"),
        ("no_usage", no_usage, "no_usage"),
    ):
        checker, _ = _checker(bench, tmp_path / name, rollout)
        with pytest.raises(judge.JudgeCallError, match=f"checker evidence incomplete: {why}"):
            checker("P")


def test_g_sources_skip_a_run_without_its_rollout(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = bench["cli"]
    evidence, qpath, out = _g_campaign(bench, tmp_path)
    run_id = "20261007T000001Z-codex.web.LK01.r1-a1"
    rollout = next((evidence / "runs" / run_id).rglob("rollout-*.jsonl"))
    hidden = tmp_path / "hidden.jsonl"
    rollout.rename(hidden)
    cmd = ["grounding-sources", "--evidence-dir", str(evidence), "--out", str(out)]
    cmd += ["--questions", str(qpath)]
    assert cli.main(cmd) == 1
    assert f"{run_id}: rollout missing" in capsys.readouterr().err
    assert not (out / "sources" / f"{run_id}.json").exists()
    assert len(list((out / "sources").glob("*.json"))) == 3
    hidden.rename(rollout)
    assert cli.main(cmd) == 0
    assert "1 runs parsed, 3 kept" in capsys.readouterr().out
    data = json.loads((out / "sources" / f"{run_id}.json").read_text())
    assert data["sources"] and data["rollout_sha256"]


def test_g_contamination_set_refuses_a_metered_codex_login(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = bench["cli"]
    path = tmp_path / "contamination-questions.json"
    path.write_text(json.dumps(contamination_set()))
    base = ["run", "--questions", str(path), "--agent", "codex"]
    with pytest.raises(SystemExit) as info:
        cli.main([*base, "--codex-auth", "api_key"])
    assert info.value.code == 2
    assert "ChatGPT login only" in capsys.readouterr().err
    assert cli.main([*base, "--codex-auth", "chatgpt"]) == 0


@pytest.mark.parametrize(
    ("command", "size", "ok"),
    [
        ("grounding-hosts", "40", True),
        ("grounding-hosts", "41", False),
        ("grounding-hosts", "0", False),
        ("crosscheck-extract", "15", True),
        ("crosscheck-extract", "16", False),
        ("crosscheck-extract", "0", False),
    ],
)
def test_g_batch_sizes_stay_within_the_frozen_maxima(
    bench: dict[str, ModuleType], command: str, size: str, ok: bool
) -> None:
    parser = bench["cli"].parser()
    argv = [command, "--out", "o", "--batch-size", size]
    if command == "crosscheck-extract":
        argv += ["--evidence-dir", "e"]
    if ok:
        assert parser.parse_args(argv).batch_size == int(size)
    else:
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_g_sources_refuse_an_empty_or_truncated_rollout(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = bench["cli"]
    evidence, qpath, out = _g_campaign(bench, tmp_path)
    run_id = "20261007T000001Z-codex.web.LK01.r1-a1"
    rollout = next((evidence / "runs" / run_id).rglob("rollout-*.jsonl"))
    full = rollout.read_text()
    cmd = ["grounding-sources", "--evidence-dir", str(evidence), "--out", str(out)]
    cmd += ["--questions", str(qpath)]
    lines = full.splitlines()
    for broken, why in (
        ("", "empty rollout"),
        ("\n".join(x for x in lines if "task_complete" not in x) + "\n", "no task_complete"),
        ("\n".join(x for x in lines if "token_count" not in x) + "\n", "no usage"),
    ):
        rollout.write_text(broken)
        assert cli.main(cmd) == 1
        assert why in capsys.readouterr().err
        assert not (out / "sources" / f"{run_id}.json").exists()
    target = out / "sources" / f"{run_id}.json"
    target.write_text(json.dumps({"run_id": run_id, "sources": []}))  # an old, unmarked file
    rollout.write_text(full)
    assert cli.main(cmd) == 0
    assert "1 runs parsed, 3 kept" in capsys.readouterr().out
    data = json.loads(target.read_text())
    assert data["complete"] is True and data["sources"]


@pytest.mark.parametrize("empty", [True, False])
def test_g_runs_without_an_answer_are_not_graded(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    empty: bool,
) -> None:
    cli = bench["cli"]
    g = bench["grounding"]
    graders: list[ScriptedGrader] = []

    def make_executor(*_a: Any) -> ScriptedGrader:
        graders.append(ScriptedGrader(_fake_graders))
        return graders[-1]

    monkeypatch.setattr(g, "make_executor", make_executor)
    evidence, qpath, out = _g_campaign(bench, tmp_path)
    run_id = "20261007T000001Z-codex.web.LK01.r1-a1"
    answer = evidence / "runs" / run_id / "answer.md"
    text = answer.read_text()
    answer.write_text("  \n") if empty else answer.unlink()
    cmd = ["grounding-claims", "--evidence-dir", str(evidence), "--out", str(out)]
    cmd += ["--questions", str(qpath), "--execute", "--max-calls", "4"]
    assert cli.main(cmd) == 1  # the two other scored runs fit 4 calls: the missing one is free
    assert f"without an answer (not graded): {run_id}" in capsys.readouterr().err
    assert not (out / "claims" / f"{run_id}.json").exists()
    assert len(graders[0].calls) == 2
    answer.write_text(text)
    assert cli.main(cmd) == 0
    assert (out / "claims" / f"{run_id}.json").exists()
    # pass T: the same rule
    cev = tmp_path / "cev"
    data = contamination_set()
    cq = tmp_path / "cq.json"
    cq.write_text(json.dumps(data))
    for arm in ("web", "archivist"):
        rid = f"20261008T000001Z-codex.{arm}.CN01.r1-a1"
        run_dir = cev / "runs" / rid
        run_dir.mkdir(parents=True)
        record = {"run_id": rid, "agent": "codex", "arm": arm, "question_id": "CN01"}
        record |= {"stratum": "contamination", "repetition": 1, "status": "completed"}
        (run_dir / "record.json").write_text(json.dumps(record))
        if arm == "web":
            (run_dir / "answer.md").write_text("Net sales were 391,999.")
        elif empty:
            (run_dir / "answer.md").write_text("")
    t = ["trap-judge", "--evidence-dir", str(cev), "--out", str(out), "--questions", str(cq)]
    capsys.readouterr()
    assert cli.main([*t, "--execute", "--max-calls", "2"]) == 1
    assert "20261008T000001Z-codex.archivist.CN01.r1-a1" in capsys.readouterr().err
    assert sorted(p.stem for p in (out / "traps").glob("*.json")) == [
        "20261008T000001Z-codex.web.CN01.r1-a1"
    ]


def test_g_verify_quotes_compares_document_id_presence(bench: dict[str, ModuleType]) -> None:
    q = bench["questions"]
    with_id = _sourced("F1", 1, 50, doc=True)
    without = _sourced("F2", 1, 60, doc=False)

    def passage(fact: dict[str, Any], doc: str | None) -> dict[str, Any]:
        src = fact["source"]
        return {
            "id": src["chunk_id"],
            "filing_id": src["filing_uuid"],
            "url": src["permalink"],
            "exchange_document_id": doc,
            "snippet": src["quote"],
        }

    assert q.verify_quote(with_id, passage(with_id, "0000320193-24-000123")) == []
    assert q.verify_quote(without, passage(without, None)) == []
    # an absence reason where the passage has an id
    reason = copy.deepcopy(with_id)
    reason["source"]["document_id_absent_reason"] = "none stored"
    assert any(
        "same id" in p for p in q.verify_quote(reason, passage(with_id, "0000320193-24-000123"))
    )
    wrong = copy.deepcopy(with_id)
    wrong["source"]["exchange_document_id"] = "0000320193-24-999999"
    assert any(
        "same id" in p for p in q.verify_quote(wrong, passage(with_id, "0000320193-24-000123"))
    )
    # an invented id where the passage has none
    invented = copy.deepcopy(without)
    invented["source"]["exchange_document_id"] = "0000320193-24-000123"
    assert any("absence reason" in p for p in q.verify_quote(invented, passage(without, None)))
    no_reason = copy.deepcopy(without)
    no_reason["source"].pop("document_id_absent_reason")
    assert any("absence reason" in p for p in q.verify_quote(no_reason, passage(without, None)))


def test_g_zero_integer_part_keeps_its_decimals(bench: dict[str, ModuleType]) -> None:
    g = bench["grounding"]
    from decimal import Decimal

    assert g.parse_number("0.100") == g.Number(Decimal("0.100"), 3, 3, False)
    assert g.parse_number("0,100").decimals == 3
    assert g.parse_value("$0.100").usable and g.parse_value("0.100").usable
    assert g.parse_number("1.100").value == Decimal("1100")  # unchanged for other integer parts


def test_g_incomplete_sources_files_count_as_missing(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    g = bench["grounding"]
    cc_ = bench["crosscheck"]
    evidence, qpath, out = _g_campaign(bench, tmp_path)
    ev, o = ["--evidence-dir", str(evidence)], ["--out", str(out)]
    assert cli.main(["grounding-sources", *ev, *o, "--questions", str(qpath)]) == 0
    monkeypatch.setattr(g, "make_executor", lambda *_a: ScriptedGrader(_fake_graders))
    claims = ["grounding-claims", *ev, *o, "--questions", str(qpath)]
    assert cli.main([*claims, "--execute", "--max-calls", "10"]) == 0
    run_id = "20261007T000001Z-codex.web.LK01.r1-a1"
    target = out / "sources" / f"{run_id}.json"
    data = json.loads(target.read_text())
    data.pop("complete")
    target.write_text(json.dumps(data))
    assert g.complete_sources(out, run_id) is None
    assert g.missing_sources(out, [run_id]) == [run_id]
    with pytest.raises(ValueError, match="no complete sources file"):
        g.run_sources(out, run_id)
    with pytest.raises(ValueError, match="no complete sources file"):
        cc_.build_pool(out, [run_id], g.HostTable())
    assert all(run_id not in e["runs"] for e in g.host_inventory(out))
    for cmd in (["grounding-analyze", *ev, *o], ["crosscheck-extract", *ev, *o]):
        with pytest.raises(SystemExit, match=f"no complete sources file.*{run_id}") as info:
            cli.main(cmd)
        assert info.value.code != 0
    # pass R attribution (contamination reliance) refuses a run without complete sources
    rec = json.loads((evidence / "runs" / run_id / "record.json").read_text())
    with pytest.raises(SystemExit, match="no complete sources file"):
        cli._reliance([rec], out, g.HostTable())
    capsys.readouterr()


def test_g_numbers_only_attribution_sensitivity(bench: dict[str, ModuleType]) -> None:
    g = bench["grounding"]
    document = _fact(["US$5,323 million", "fiscal 2025 Form 10-K"], ["S1"])
    entity_only = _fact(["fiscal 2025 Form 10-K"], ["S1"])
    plain = _fact(["$5.6 billion"], ["S2"])
    rows = _attribute(bench, "codex_rollout_grounding_web.jsonl", [document, entity_only, plain])
    assert rows[0]["label"] in ("secondary", "unattributed")  # the frozen rule is unchanged
    assert rows[0]["usable_values"] == 2 and not rows[0]["primary_read"]
    variant = rows[0]["numbers_only"]
    assert (variant["label"], variant["category"], variant["primary_read"]) == (
        "filing",
        "filing",
        True,
    )
    assert variant["usable_values"] == 1
    for row in rows[1:]:  # no usable number, or numbers only: identical under both rules
        assert row["numbers_only"] == {k: row[k] for k in g.NUMBERS_ONLY_KEYS}
    # an entity only claim without a usable value falls back to its citation under both rules
    [year_only] = _attribute(
        bench, "codex_rollout_grounding_web.jsonl", [_fact(["FY2025"], ["S1"])]
    )
    assert year_only["citation_only"] and year_only["label"] == "filing"
    assert year_only["numbers_only"] == {k: year_only[k] for k in g.NUMBERS_ONLY_KEYS}
    # a mixed claim whose number reached the web before Archivist, its document name nowhere:
    # the primary rule leaves it out of the verification denominator, numbers only verifies it
    [mixed] = _attribute(
        bench,
        "codex_rollout_grounding_both.jsonl",
        [_fact(["$8,099 million", "fiscal 2025 Form 10-K"], ["S2"])],
    )
    assert not mixed["values_in_nonfiling_source"] and not mixed["verified_by_archivist"]
    assert (mixed["label"], mixed["category"]) == ("secondary", "forum")
    nums = mixed["numbers_only"]
    assert nums["values_in_nonfiling_source"] and nums["verified_by_archivist"]
    assert (nums["label"], nums["category"]) == ("filing", "filing")  # category changes
    meta = {"run_id": "r", "arm": "web", "stratum": "lookup", "question_id": "LK01"}
    claim_rows = [g.claim_row(meta, r, {}) for r in rows]
    assert claim_rows[0]["numbers_only_label"] == "filing"
    assert g.numbers_only_row(claim_rows[0])["label"] == "filing"
    runs = [meta | {"repetition": 1}]
    cells = {
        rule: {c["name"]: c for c in g.reliance_cells(claim_rows, runs, resamples=20, rule=rule)}
        for rule in g.RULES
    }
    assert cells["primary"]["fact_label_filing"]["claims"] == 0
    assert cells["numbers_only"]["fact_label_filing"]["claims"] == 1
    assert cells["numbers_only"]["fact_label_filing"]["rule"] == "numbers_only"
    with pytest.raises(ValueError):
        g.reliance_cells(claim_rows, runs, rule="other")


# --- Grounding campaign deviation G3 and concurrency ----------------------------------------------


def test_g_text_sample_is_seeded_and_stratified(bench: dict[str, ModuleType]) -> None:
    cc_ = bench["crosscheck"]
    pool = []
    for category, stratum, n in (
        ("forum", "lookup", 3000),
        ("aggregator", "lookup", 2000),
        ("forum", "breadth", 30),
        ("aggregator", "reach", 200),
        ("aggregator", "breadth", 100),
    ):
        pool += [
            {"text_id": f"T{category[0]}{stratum}{i:05d}", "category": category, "stratum": stratum}
            for i in range(n)
        ]
    chosen = cc_.text_sample(pool)
    assert chosen == cc_.text_sample(list(reversed(pool)))  # deterministic
    assert len(chosen["text_ids"]) == 1500 and chosen["text_ids"] == sorted(chosen["text_ids"])
    alloc = chosen["allocation"]
    assert alloc["forum|breadth"] == 30  # the whole of a smaller cell
    assert alloc["aggregator|breadth"] >= 60 and alloc["aggregator|reach"] >= 60
    assert alloc["forum|lookup"] > alloc["aggregator|lookup"]
    assert sum(alloc.values()) == 1500 and chosen["seed"] == 8105 and chosen["floor"] == 60
    assert cc_.text_sample(pool, seed=1)["text_ids"] != chosen["text_ids"]
    small = cc_.text_sample(pool[:40])
    assert len(small["text_ids"]) == 40


def test_g_only_sampled_texts_are_extracted_and_deduplicated(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = bench["cli"]
    g = bench["grounding"]
    cc_ = bench["crosscheck"]
    pool = [
        {"text_id": "Ta", "category": "forum", "stratum": "lookup", "companies": []},
        {"text_id": "Tb", "category": "forum", "stratum": "lookup", "companies": []},
    ]
    claim = {"company": "C", "metric": "m", "value": "1.5", "period": "", "stated_date": ""}
    extracted = {
        "Ta": {"status": "ok", "claims": [claim | {"kind": "rumor"}]},
        "Tb": {"status": "ok", "claims": [claim | {"metric": "other", "kind": "rumor"}]},
    }
    assert len(cc_.dedupe_claims(pool, extracted)) == 2
    [kept] = cc_.dedupe_claims(pool, extracted, {"Ta"})  # Tb's stored extraction ignored
    assert kept["text_id"] == "Ta"
    evidence, qpath, out = _g_campaign(bench, tmp_path)
    ev, o = ["--evidence-dir", str(evidence)], ["--out", str(out)]
    assert cli.main(["grounding-sources", *ev, *o, "--questions", str(qpath)]) == 0
    assert cli.main(["crosscheck-extract", *ev, *o]) == 0  # dry run writes the text sample
    sample_path = out / "crosscheck" / "text-sample.json"
    chosen = json.loads(sample_path.read_text())
    pool_ids = [e["text_id"] for e in json.loads((out / "crosscheck" / "pool.json").read_text())]
    assert chosen["text_ids"] == sorted(pool_ids)  # a small pool is sampled whole
    # a different stored sample whose texts have extractions is never replaced
    other = dict(chosen, text_ids=["Tnot-in-pool"])
    sample_path.write_text(json.dumps(other))
    g.write_json(out / "crosscheck" / "extract.json", {"Tnot-in-pool": {"status": "ok"}})
    with pytest.raises(SystemExit, match="refusing to replace a text sample"):
        cli.main(["crosscheck-extract", *ev, *o])
    assert json.loads(sample_path.read_text()) == other
    # without extractions it is replaced; pass X then runs on sampled texts only
    g.write_json(out / "crosscheck" / "extract.json", {})
    sample_path.write_text(json.dumps(dict(chosen, text_ids=chosen["text_ids"][:1])))
    seen: list[str] = []

    def answer(prompt: str) -> str:
        seen.append(prompt)
        return _fake_graders(prompt)

    monkeypatch.setattr(g, "make_executor", lambda *_a: ScriptedGrader(answer))
    monkeypatch.setattr(cc_, "text_sample", lambda p, **_k: dict(chosen, text_ids=[pool_ids[0]]))
    assert cli.main(["crosscheck-extract", *ev, *o, "--execute", "--max-calls", "4"]) == 0
    extracted_now = json.loads((out / "crosscheck" / "extract.json").read_text())
    assert sorted(extracted_now) == [pool_ids[0]] and len(seen) == 1
    assert cli.main(["crosscheck-sample", *o, "--size", "400"]) == 0
    rows = json.loads((out / "crosscheck" / "sample.json").read_text())["rows"]
    assert rows and {r["text_id"] for r in rows} == {pool_ids[0]}


def _strip(data: Any) -> Any:
    """A pass output without timestamps (they differ between any two runs)."""
    if isinstance(data, dict):
        return {k: _strip(v) for k, v in data.items() if k not in ("graded_at", "started_at")}
    if isinstance(data, list):
        return [_strip(x) for x in data]
    return data


def _state_snapshot(out: Path) -> dict[str, Any]:
    keep = ("host-table.json", "claims/", "crosscheck/extract.json", "crosscheck/checks/", "traps/")
    return {
        p.relative_to(out).as_posix(): _strip(json.loads(p.read_text()))
        for p in sorted(out.rglob("*.json"))
        if p.relative_to(out).as_posix().startswith(keep)
    }


def test_g_concurrency_gives_the_same_outputs(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = bench["cli"]
    g = bench["grounding"]
    cc_ = bench["crosscheck"]

    def slow(prompt: str) -> str:
        time.sleep(0.002 * (len(prompt) % 7))  # completion order differs from admission
        return _fake_graders(prompt)

    def checker_answer(prompt: str) -> str:
        time.sleep(0.002 * (len(prompt) % 5))
        merit = "not_applicable" if "stated date none" in prompt else "not_settled"
        verdict = "contradicted" if "forum" in prompt else "confirmed"
        sub = "wrong_number" if verdict == "contradicted" else "none"
        return _pass_c_verdict(verdict, sub, merit=merit)

    monkeypatch.setattr(g, "make_executor", lambda *_a: ScriptedGrader(slow))
    monkeypatch.setattr(cc_, "make_checker", lambda *_a: ScriptedGrader(checker_answer))
    snapshots = []
    for n in (1, 2, 4):
        root = tmp_path / f"c{n}"
        root.mkdir()
        evidence, qpath, out = _g_campaign(bench, root)
        ev, o = ["--evidence-dir", str(evidence)], ["--out", str(out)]
        c = ["--concurrency", str(min(n, 2))]
        cn = ["--concurrency", str(n), "--execute", "--max-calls", "40"]
        assert cli.main(["grounding-sources", *ev, *o, "--questions", str(qpath)]) == 0
        assert cli.main(["grounding-hosts", *o, *cn]) == 0
        assert cli.main(["grounding-claims", *ev, *o, "--questions", str(qpath), *cn]) == 0
        assert cli.main(["crosscheck-extract", *ev, *o, "--batch-size", "2", *cn]) == 0
        assert cli.main(["crosscheck-sample", *o, "--size", "8"]) == 0
        assert cli.main(["crosscheck-check", *o, *c, "--execute", "--max-calls", "40"]) == 0
        snapshots.append(_state_snapshot(out))
    assert snapshots[0] and snapshots[0] == snapshots[1] == snapshots[2]
    assert any(k.startswith("crosscheck/checks/") for k in snapshots[0])
    assert len([k for k in snapshots[0] if k.startswith("claims/")]) == 3
    extract = (tmp_path / "c4" / "out" / "crosscheck" / "extract.json").read_text()
    assert list(json.loads(extract)) == sorted(json.loads(extract))  # sorted key order


def test_g_concurrency_flag_bounds(bench: dict[str, ModuleType]) -> None:
    parser = bench["cli"].parser()
    assert (
        parser.parse_args(["grounding-hosts", "--out", "o", "--concurrency", "4"]).concurrency == 4
    )
    for argv in (
        ["grounding-hosts", "--out", "o", "--concurrency", "5"],
        ["grounding-hosts", "--out", "o", "--concurrency", "0"],
        ["crosscheck-check", "--out", "o", "--concurrency", "5"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)
    assert (
        parser.parse_args(["crosscheck-check", "--out", "o", "--concurrency", "4"]).concurrency == 4
    )


def test_g_a_latched_stop_admits_no_further_item(bench: dict[str, ModuleType]) -> None:
    g = bench["grounding"]
    judge = bench["judge"]
    admitted: list[int] = []
    saved: list[int] = []
    lock = threading.Lock()

    def work(item: int, worker: int) -> None:
        with lock:
            admitted.append(item)
        if item == 0:
            raise judge.JudgeInfraError("plan probe refused the call")
        time.sleep(0.05)
        with lock:
            saved.append(item)  # an item in flight finishes and is saved

    done, stop = g.run_items(list(range(10)), work, concurrency=2)
    assert stop == "plan probe refused the call"
    assert set(admitted) <= {0, 1} and saved == [i for i in admitted if i != 0]
    assert done == len(saved)
    done, stop = g.run_items(list(range(5)), lambda i, w: None, concurrency=4)
    assert (done, stop) == (5, None)

    def boom(item: int, worker: int) -> None:
        raise RuntimeError("bug")

    with pytest.raises(RuntimeError):
        g.run_items([1, 2, 3], boom, concurrency=2)


def test_g_max_calls_hold_under_concurrency(bench: dict[str, ModuleType], tmp_path: Path) -> None:
    g = bench["grounding"]
    replies: dict[str, int] = {}
    lock = threading.Lock()

    def answer(prompt: str) -> str:
        with lock:
            replies[prompt] = replies.get(prompt, 0) + 1
            first = replies[prompt] == 1
        time.sleep(0.01)
        return "not json" if first else '{"claims": []}'  # every item needs its retry

    executors = [ScriptedGrader(answer) for _ in range(3)]
    grader = g.Grader("R", tmp_path, executors[0], 5, g.PASS_R_SCHEMA)
    results: dict[int, Any] = {}

    def work(item: int, worker: int) -> None:
        parsed, error, calls = grader.grade(
            f"run-{item}", f"P{item}", lambda t: g.parse_pass_r(t, {}), executors[worker]
        )
        results[item] = (parsed, len(calls))

    done, stop = g.run_items(list(range(10)), work, concurrency=3, grader=grader)
    assert stop == "call ceiling" and done == 2
    assert grader.used == 4 <= 5 and grader.reserved == 0
    assert all(parsed == [] and n == 2 for parsed, n in results.values())  # retries kept
    assert sum(len(e.calls) for e in executors) == 4


def test_g_tight_budget_admits_the_same_items_as_one_worker(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    g = bench["grounding"]

    def run(concurrency: int) -> tuple[list[int], str | None, int]:
        def answer(prompt: str) -> str:
            time.sleep(0.02 * (4 - int(prompt[1:])))  # slow, successful first attempts
            return '{"claims": []}'

        executors = [ScriptedGrader(answer) for _ in range(concurrency)]
        grader = g.Grader("R", tmp_path / str(concurrency), executors[0], 4, g.PASS_R_SCHEMA)
        done_items: list[int] = []
        lock = threading.Lock()

        def work(item: int, worker: int) -> None:
            grader.grade(
                f"run-{item}", f"P{item}", lambda t: g.parse_pass_r(t, {}), executors[worker]
            )
            with lock:
                done_items.append(item)

        _done, stop = g.run_items(list(range(4)), work, concurrency, grader)
        return sorted(done_items), stop, grader.used

    sequential = run(1)
    assert sequential == ([0, 1, 2], "call ceiling", 3)
    for _ in range(3):
        assert run(4) == sequential


def test_g_a_gate_refusal_latches_before_it_unwinds(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    g = bench["grounding"]
    judge = bench["judge"]
    item1_started, item1_done, release = threading.Event(), threading.Event(), threading.Event()
    real_append = g._append

    def held_append(path: Path, row: dict[str, Any]) -> None:
        if row.get("status") == "infra_error":
            # worker A is unwinding its refusal: give worker B every chance to admit
            assert item1_done.wait(5)
            time.sleep(0.2)
            release.set()
        real_append(path, row)

    monkeypatch.setattr(g, "_append", held_append)

    def answer(prompt: str) -> str:
        if prompt == "P0":
            assert item1_started.wait(5)
            raise judge.JudgeInfraError("plan probe refused the call: codex primary rose 7 points")
        item1_started.set()
        # B finishes its item only once A's refusal was detected (A is then held mid-unwind)
        assert grader.stop.wait(5)
        return '{"claims": []}'

    executors = [ScriptedGrader(answer) for _ in range(2)]
    grader = g.Grader("R", tmp_path, executors[0], 20, g.PASS_R_SCHEMA)
    started: list[int] = []

    def work(item: int, worker: int) -> None:
        started.append(item)
        grader.grade(f"run-{item}", f"P{item}", lambda t: g.parse_pass_r(t, {}), executors[worker])
        if item == 1:
            item1_done.set()

    done, stop = g.run_items(list(range(6)), work, 2, grader)
    assert release.is_set()
    assert sorted(started) == [0, 1] and done == 1  # B saved item 1 and admitted nothing more
    assert stop is not None and stop.startswith("plan probe refused")


class ProbeSpy(ScriptedGrader):
    """A grader executor that counts launches and records its closing probe."""

    def __init__(self, answer: Callable[[str], str], log: dict[str, Any]) -> None:
        super().__init__(answer)
        self.log = log

    def __call__(self, prompt: str, schema: Any = None) -> str:
        with self.log["lock"]:
            self.log["inflight"] += 1
            self.dispatched += 1
        try:
            time.sleep(0.01)
            return self.answer(prompt)
        finally:
            with self.log["lock"]:
                self.log["inflight"] -= 1

    def closing_probe(self, log_dir: Path) -> dict[str, Any]:
        claims = self.log["out"] / "claims"
        saved = len(list(claims.glob("*.json"))) if claims.exists() else 0
        self.log["closings"].append({"inflight": self.log["inflight"], "saved": saved})
        return {"probed_at": "2026-10-07T00:00:00Z", "reasons": []}


@pytest.mark.parametrize("ending", ["normal", "stop", "error"])
def test_g_one_closing_probe_after_every_worker_settled(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ending: str,
) -> None:
    cli = bench["cli"]
    g = bench["grounding"]
    judge = bench["judge"]
    evidence, qpath, out = _g_campaign(bench, tmp_path)
    log: dict[str, Any] = {"lock": threading.Lock(), "inflight": 0, "closings": [], "out": out}

    def answer(prompt: str) -> str:
        if "Q MH01" in prompt and ending == "stop":
            raise judge.JudgeInfraError("usage limit reached")
        if "Q MH01" in prompt and ending == "error":
            raise RuntimeError("bug in a worker")
        return _fake_graders(prompt)

    spies: list[ProbeSpy] = []

    def make(*_a: Any) -> ProbeSpy:
        spies.append(ProbeSpy(answer, log))
        return spies[-1]

    monkeypatch.setattr(g, "make_executor", make)
    cmd = ["grounding-claims", "--evidence-dir", str(evidence), "--out", str(out)]
    cmd += ["--questions", str(qpath), "--concurrency", "3", "--execute", "--max-calls", "20"]
    if ending == "error":
        with pytest.raises(RuntimeError):
            cli.main(cmd)
    else:
        assert cli.main(cmd) == (0 if ending == "normal" else 1)
    assert len(spies) == 3 and sum(s.dispatched for s in spies) >= 1
    [closing] = log["closings"]  # exactly one closing probe for the batch
    saved = len(list((out / "claims").glob("*.json")))
    assert closing == {"inflight": 0, "saved": saved}  # after every worker saved its item
    assert saved == (3 if ending == "normal" else 2)
    probes = (out / "plan-probes.jsonl").read_text().splitlines()
    assert len(probes) == 1 and json.loads(probes[0])["kind"] == "codex_R_closing"
    capsys.readouterr()


# --- Grounding campaign deviation G4: the extension sample for trap sourcing ----------------------


def _g4_claims() -> list[dict[str, Any]]:
    claims = []
    for category, stratum, kind, n in (
        ("forum", "lookup", "reported_figure", 400),
        ("aggregator", "lookup", "reported_figure", 300),
        ("aggregator", "reach", "reported_figure", 60),
        ("forum", "breadth", "reported_figure", 4),
        ("forum", "lookup", "opinion", 300),
        ("aggregator", "breadth", "estimate", 200),
    ):
        claims += [
            {
                "claim_id": f"C{category[0]}{stratum}{kind[0]}{i:04d}",
                "category": category,
                "stratum": stratum,
                "kind": kind,
            }
            for i in range(n)
        ]
    return claims


def test_g_extension_sample(bench: dict[str, ModuleType]) -> None:
    cc_ = bench["crosscheck"]
    claims = _g4_claims()
    main, _ = cc_.stratified_sample(claims)
    in_sample = {r["claim_id"] for r in main}
    rows, allocation = cc_.extension_sample(claims, in_sample)
    again, _ = cc_.extension_sample(list(reversed(claims)), in_sample)
    assert rows == again  # deterministic
    assert len(rows) == 300 and [r["row_id"] for r in rows[:2]] == ["E001", "E002"]
    assert rows[-1]["row_id"] == "E300"
    assert {r["kind"] for r in rows} == {"reported_figure"}  # kind filter
    assert not in_sample & {r["claim_id"] for r in rows}  # none of the 400 sampled claims
    assert "forum|breadth" not in allocation  # all 4 went to the 400 sample (its floor)
    assert allocation["aggregator|reach"] >= 10
    assert sum(allocation.values()) == 300
    other, _ = cc_.extension_sample(claims, in_sample, seed=8105)
    assert other != rows  # the extension's own seed (8106) matters
    bins = {
        b["category"]: b
        for b in cc_.extension_bins(
            [
                {"category": "forum", "status": "ok", "bin": "contradicted"},
                {"category": "forum", "status": "ok", "bin": "confirmed"},
                {"category": "aggregator", "status": "invalid"},
                {"category": "aggregator", "status": None},
            ]
        )
    }
    assert bins["all"]["label"] == "extension, trap sourcing only"
    assert (bins["all"]["contradicted"], bins["all"]["invalid"], bins["all"]["unchecked"]) == (
        1,
        1,
        1,
    )
    assert "contradicted_rate" not in bins["all"]  # counts only, no rates


def test_g_extension_rows_stay_out_of_rates_and_audit(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    g = bench["grounding"]
    cc_ = bench["crosscheck"]
    evidence, qpath, out = _g_campaign(bench, tmp_path)
    ev, o = ["--evidence-dir", str(evidence)], ["--out", str(out)]
    monkeypatch.setattr(g, "make_executor", lambda *_a: ScriptedGrader(_fake_graders))

    def checker(*_a: Any) -> ScriptedGrader:
        def answer(prompt: str) -> str:
            merit = "not_applicable" if "stated date none" in prompt else "not_settled"
            return _pass_c_verdict("contradicted", "wrong_number", merit=merit)

        return ScriptedGrader(answer)

    monkeypatch.setattr(cc_, "make_checker", checker)
    assert cli.main(["grounding-sources", *ev, *o, "--questions", str(qpath)]) == 0
    assert cli.main(["crosscheck-extract", *ev, *o, "--execute", "--max-calls", "10"]) == 0
    claims = json.loads((out / "crosscheck" / "claims.json").read_text())
    assert len(claims) > 2
    with pytest.raises(SystemExit, match="no 400 claim sample"):
        cli.main(["crosscheck-sample", *o, "--extension"])
    assert cli.main(["crosscheck-sample", *o, "--size", "2"]) == 0
    ext_path = out / "crosscheck" / "sample-extension.json"
    # the tiny campaign has fewer than 300 eligible claims: refused, nothing written
    with pytest.raises(SystemExit, match="fewer than 300; nothing written"):
        cli.main(["crosscheck-sample", *o, "--extension"])
    assert not ext_path.exists()
    for bad in (["--size", "200"], ["--seed", "8105"]):
        with pytest.raises(SystemExit, match="300 claims with seed 8106"):
            cli.main(["crosscheck-sample", *o, "--extension", *bad])
    assert not ext_path.exists()
    monkeypatch.setattr(cc_, "EXTENSION_SIZE", len(claims) - 2)  # every eligible claim
    assert cli.main(["crosscheck-sample", *o, "--extension"]) == 0
    # with an extension the 400 claim sample is never regenerated; both stay unchanged
    main_path = out / "crosscheck" / "sample.json"
    main_before, ext_before = main_path.read_text(), ext_path.read_text()
    with pytest.raises(SystemExit, match="checks or an extension sample"):
        cli.main(["crosscheck-sample", *o, "--size", "3"])
    assert main_path.read_text() == main_before and ext_path.read_text() == ext_before
    extension = json.loads((out / "crosscheck" / "sample-extension.json").read_text())
    x_ids = {
        r["claim_id"] for r in json.loads((out / "crosscheck" / "sample.json").read_text())["rows"]
    }
    e_rows = extension["rows"]
    assert e_rows and all(r["row_id"].startswith("E") for r in e_rows)
    assert extension["seed"] == 8106 and not x_ids & {r["claim_id"] for r in e_rows}
    check = ["crosscheck-check", *o, "--execute", "--max-calls", "200"]
    assert cli.main([*check, "--extension"]) == 0
    assert all((out / "crosscheck" / "checks" / f"{r['row_id']}.json").exists() for r in e_rows)
    assert not list((out / "crosscheck" / "checks").glob("X*.json"))  # X rows untouched
    assert cli.main(check) == 0
    results = tmp_path / "results"
    assert cli.main(["crosscheck-audit", *o, "--results", str(results)]) == 0
    audit = list(csv.DictReader((results / "crosscheck-audit.csv").open()))
    assert audit and all(r["row_id"].startswith("X") for r in audit)
    assert cli.main(["grounding-analyze", *ev, *o, "--resamples", "20"]) == 0
    analysis = json.loads((out / "analysis-grounding.json").read_text())
    overall = [r for r in analysis["crosscheck"]["rates"] if r["category"] == "all"][0]
    assert overall["rows"] == 2  # the X rows only
    ext = analysis["crosscheck_extension"]
    assert ext["label"] == "extension, trap sourcing only" and ext["rows"] == len(e_rows)
    total = [b for b in ext["bins"] if b["category"] == "all"][0]
    assert total["contradicted"] == len(e_rows)
    assert cli.main(["grounding-report", *ev, *o, "--results", str(results)]) == 0
    sample_csv = list(csv.DictReader((results / "crosscheck-sample.csv").open()))
    assert {r["row_id"][0] for r in sample_csv} == {"X"}
    bins_csv = list(csv.DictReader((results / "crosscheck-extension-bins.csv").open()))
    assert bins_csv and bins_csv[0]["label"] == "extension, trap sourcing only"
    # a sample with checks is never replaced
    sample_path = out / "crosscheck" / "sample-extension.json"
    data = json.loads(sample_path.read_text())
    data["rows"] = data["rows"][:1]
    sample_path.write_text(json.dumps(data))
    with pytest.raises(SystemExit, match="refusing to replace an extension sample"):
        cli.main(["crosscheck-sample", *o, "--extension"])
    capsys.readouterr()


def test_g_contamination_rows_bind_extension_rows(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    cli = bench["cli"]
    g = bench["grounding"]
    data = contamination_set()
    out = tmp_path / "out"
    x_rows, e_rows = [], []
    for i, question in enumerate(data["questions"], start=1):
        trap = question["role"] == "trap"
        row = f"E{i:03d}" if i % 2 else f"X{i:03d}"  # both samples bind
        question["crosscheck_rows"] = [row]
        for fact in question["facts"]:
            if fact["kind"] == "trap":
                fact["crosscheck_row"] = row
        entry = {"row_id": row, "category": "forum", "value": "391,999" if trap else "391,035"}
        (e_rows if row.startswith("E") else x_rows).append(entry)
        verdict = "contradicted" if trap else "confirmed"
        g.write_json(
            out / "crosscheck" / "checks" / f"{row}.json", {"status": "ok", "bin": verdict}
        )
    g.write_json(out / "crosscheck" / "sample.json", {"rows": x_rows})
    g.write_json(out / "crosscheck" / "sample-extension.json", {"rows": e_rows})
    path = tmp_path / "c.json"
    path.write_text(json.dumps(data))
    cmd = ["validate-questions", "--contamination", "--questions", str(path)]
    assert cli.main([*cmd, "--crosscheck", str(out)]) == 0
    from archivist_bench import questions as q

    checks = cli._check_results(out)
    sample = {r["row_id"]: r for r in x_rows + e_rows}
    for orphan in ("X099", "E099"):  # a control row with a confirmed check but in no sample
        broken = copy.deepcopy(data)
        control = broken["questions"][-1]
        control["crosscheck_rows"] = [orphan]
        checks_with = dict(checks, **{orphan: {"status": "ok", "bin": "confirmed"}})
        errors = q.validate_crosscheck_rows(broken, checks_with, sample)
        assert any(
            f"row {orphan} is in neither sample.json nor sample-extension.json" in e for e in errors
        ), errors
    (out / "crosscheck" / "sample-extension.json").unlink()
    assert cli.main([*cmd, "--crosscheck", str(out)]) == 1  # E rows need their sample file


# --- Claude-code campaign (harness 1.5.0): a light Claude Code pass -------------------------------

# Normalized in output digests: the harness release, the grading time and the inputs
# fingerprint (its labels hold the test's temporary paths).
VERSION_STAMP = re.compile(r'("(?:harness_version|graded_at|inputs_fingerprint)":\s*")[^"]*(")')


def _digest_tree(root: Path, names: Callable[[str], bool]) -> dict[str, str]:
    """sha256 of every file under ``root`` whose relative path ``names`` accepts, gzip logs
    decompressed, the harness version, grading times and inputs fingerprint normalized (``VERSION_STAMP``)."""
    import gzip
    import hashlib

    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if not path.is_file() or not names(rel):
            continue
        raw = path.read_bytes()
        if rel.endswith(".gz"):
            raw = gzip.decompress(raw)
        text = VERSION_STAMP.sub(r"\1X\2", raw.decode("utf-8"))
        out[rel] = hashlib.sha256(text.encode()).hexdigest()
    return out


def _codex_grounding_pipeline(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: list[str],
) -> dict[str, str]:
    """The whole grounding campaign sequence (sources, pass H, pass R, cross check, pass T, analysis
    and report with the contamination stratum) over Codex fixtures, with ``agent`` appended to
    every command that takes ``--agent``; digests of the committed results and state outputs."""
    cli = bench["cli"]
    g = bench["grounding"]
    evidence, qpath, out = _g_campaign(bench, tmp_path)
    cev = tmp_path / "cev"
    cq = tmp_path / "contamination-questions.json"
    cq.write_text(json.dumps(contamination_set()))
    for arm, answer, fixture in (
        ("web", "Apple's net sales were 391,999 million.", "codex_rollout_grounding_web.jsonl"),
        (
            "archivist",
            "Apple's net sales were 391,035 million; a post's 391,999 is wrong.",
            "codex_rollout_grounding_both.jsonl",
        ),
        ("both", "Apple's net sales were 391,035 million.", "codex_rollout_grounding_both.jsonl"),
    ):
        run_id = f"20261008T00000{len(arm)}Z-codex.{arm}.CN01.r1-a1"
        run_dir = cev / "runs" / run_id
        sessions = run_dir / "codex-home" / "sessions" / "2026" / "10" / "08"
        sessions.mkdir(parents=True)
        (sessions / "rollout-x.jsonl").write_text(grounding_text(fixture))
        (run_dir / "answer.md").write_text(answer)
        record = {
            "run_id": run_id,
            "run_key": f"codex.{arm}.CN01.r1",
            "agent": "codex",
            "arm": arm,
            "question_id": "CN01",
            "stratum": "contamination",
            "repetition": 1,
            "attempt": 1,
            "status": "completed",
            "contaminated": False,
            "usage": {"total": {"web": 3000, "archivist": 1000, "both": 2000}[arm]},
        }
        (run_dir / "record.json").write_text(json.dumps(record))
        labels = bench["judge"].fact_labels(contamination_set()["questions"][0])
        judgement = {
            "run_id": run_id,
            "status": "ok",
            "rule": "B",
            "passes": {
                "B": {
                    "fact_verdicts": {f: "correct" for f in labels},
                    "complete": True,
                    "error": None,
                }
            },
        }
        (run_dir / "judgement.json").write_text(json.dumps(judgement))
    verdicts = iter(["contradicted", "confirmed"] * 50)

    def make_checker(log_dir: Path, stop: float, step: float, gate: Any) -> ScriptedGrader:
        sub = {"contradicted": "wrong_number", "confirmed": "none"}

        def answer(prompt: str) -> str:
            verdict = next(verdicts)
            merit = "not_applicable" if "stated date none" in prompt else "not_settled"
            return _pass_c_verdict(verdict, sub[verdict], merit=merit)

        return ScriptedGrader(answer)

    monkeypatch.setattr(g, "make_executor", lambda *_a: ScriptedGrader(_fake_graders))
    monkeypatch.setattr(bench["crosscheck"], "make_checker", make_checker)
    ev, o = ["--evidence-dir", str(evidence)], ["--out", str(out)]
    cev_args = ["--evidence-dir", str(cev)]
    con = ["--contamination-evidence-dir", str(cev), "--contamination-questions", str(cq)]
    run = ["--execute", "--max-calls", "40"]
    results = tmp_path / "results"
    for argv in (
        ["grounding-sources", *ev, *o, "--questions", str(qpath), *agent],
        ["grounding-sources", *cev_args, *o, "--questions", str(cq), *agent],
        ["grounding-hosts", *o, *run],
        ["grounding-claims", *ev, *o, "--questions", str(qpath), *run, *agent],
        ["grounding-claims", *cev_args, *o, "--questions", str(cq), *run, *agent],
        ["trap-judge", *cev_args, *o, "--questions", str(cq), *run, *agent],
        ["crosscheck-extract", *ev, *o, *run],
        ["crosscheck-sample", *o, "--size", "6"],
        ["crosscheck-check", *o, *run],
        ["grounding-analyze", *ev, *o, *con, "--resamples", "50", *agent],
        ["grounding-report", *ev, *o, *con, "--results", str(results), *agent],
    ):
        assert cli.main(argv) == 0, argv
    state = _digest_tree(
        out,
        lambda rel: (
            rel.startswith("sources/")
            or rel
            in ("hosts.json", "host-table.json", "analysis-grounding.json", "grounding-rows.json")
        ),
    )
    committed = _digest_tree(results, lambda rel: True)
    return {f"out/{k}": v for k, v in state.items()} | {
        f"results/{k}": v for k, v in committed.items()
    }


# Digests of the default (Codex) grounding outputs of ``_codex_grounding_pipeline`` produced by
# harness 1.4.0 (before the claude-code campaign), version stamp, grading times and
# inputs fingerprint normalized: the 1.5.0 default must reproduce them byte for byte.
CODEX_GROUNDING_1_4_0: dict[str, str] = {
    "out/analysis-grounding.json": (
        "af954ed5d065494362ca5941008a127eb90c431cd9e1a0ab707ebe8341625491"
    ),
    "out/grounding-rows.json": ("45276ceff9230bb5f89bb5ce710b72470338e11655f46e84315d163e57d4673e"),
    "out/host-table.json": ("cc868cfc49160b5d45af9cd18157ed10df1f6ccc2c6451cd6acfb35cfedada2d"),
    "out/hosts.json": ("dc67fd91ee7c84eac2c691b850f1789fbbba16a435bfb1bac21a4c9ffa018bae"),
    "out/sources/20261007T000001Z-codex.web.LK01.r1-a1.json": (
        "d17f340ad02b13957263ef590fb34e124d89483849a6841205febcd29097c830"
    ),
    "out/sources/20261007T000002Z-codex.both.LK01.r1-a1.json": (
        "af0e856218263765408d28e8db50fa03d0d883056a8246d2de8f0ce0b51eb061"
    ),
    "out/sources/20261007T000003Z-codex.web.CT01.r1-a1.json": (
        "db60b84615e315c0e8662854840b3a92a3aaa5bf7301f2b4f9f4cd3b71ac84b9"
    ),
    "out/sources/20261007T000004Z-codex.web.MH01.r1-a1.json": (
        "f03f15f34cffa854fc00fe394935c64ab5e419fdb0cd5734a3277ed85753ceb0"
    ),
    "out/sources/20261008T000003Z-codex.web.CN01.r1-a1.json": (
        "6aa0864ffc0c1e252f677751af540fa50a248c938478d598db967c0cfe1f5a73"
    ),
    "out/sources/20261008T000004Z-codex.both.CN01.r1-a1.json": (
        "6561af348a33a6c0198a0a4f06a786758f215f340b59ac36e9755f5fc5d638b0"
    ),
    "out/sources/20261008T000009Z-codex.archivist.CN01.r1-a1.json": (
        "dbbd98221d8d346b16f5ff723902e2c75cf2728cbc26c94c5ed85b225f82bfdf"
    ),
    "results/analysis-grounding.json": (
        "78edac9212b88ccfa3a6b4bd2e887965fc123f9566db4a9e1db0cac2593e77a1"
    ),
    "results/claims-001.csv": ("5a05b0bd657ca562ccd73f43ca7bab3d9bde35b494b6ec8c06f103e6bd0a3fd0"),
    "results/contamination/claims-001.csv": (
        "9d09239e871f800d0bd2b9d21f4016318f4ee25f15598ccbb50095767b275316"
    ),
    "results/crosscheck-extension-bins.csv": (
        "4011e249545f24a26aca262796ca3156e4437a3b0939dff54158de1f1831db27"
    ),
    "results/crosscheck-sample.csv": (
        "96e711a7470a2326f9e26c28cc2d41a43268af8c0e7e6767c0194f07c63f499c"
    ),
    "results/exposure-cells.csv": (
        "8ddc884c2e1bce8db0455d4dc8e395a48e8d570e41453449b394bf911cda0ef2"
    ),
    "results/exposure-runs.csv": (
        "3e4c5f38705693e38f97cc28e5bf8ccbe2d57e015aeac106f01c053e6ff68cb8"
    ),
    "results/host-categories.csv": (
        "c8afc311b84278dd849fa5ac724e3b2ea8679a4b29ed719e77dd28a95e829581"
    ),
}


def test_k_codex_default_grounding_outputs_unchanged(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in ("default", "explicit"):
        (tmp_path / name).mkdir()
    default = _codex_grounding_pipeline(bench, tmp_path / "default", monkeypatch, [])
    assert default == CODEX_GROUNDING_1_4_0  # byte identical to harness 1.4.0
    explicit = _codex_grounding_pipeline(
        bench, tmp_path / "explicit", monkeypatch, ["--agent", "codex"]
    )
    assert explicit == default
    analysis = json.loads((tmp_path / "default" / "out" / "analysis-grounding.json").read_text())
    assert "agent" not in analysis and "agent" not in analysis["contamination"]
    header = (tmp_path / "default" / "results" / "exposure-runs.csv").read_text().splitlines()[0]
    assert "search_summaries" not in header
    capsys.readouterr()


C_STREAM = "claude_stream_grounding_both.jsonl"
K_INSIDER = "https://www.costcoinsider.net/membership-fees"
K_TWITS = "https://stocktwits.com/symbol/COST"
K_SUMMARY_1 = (
    "Costco reported membership fee income of $5,323 million for fiscal 2025. A forum thread "
    "claims the fees reached $5.6 billion."
)
K_SUMMARY_2 = (
    "Ron Vachris is the chief executive; a forum post rumors a change with 77.7 percent odds."
)


def _claude_sources(bench: dict[str, ModuleType]) -> dict[tuple[str, str], Any]:
    g = bench["grounding"]
    return {(s.kind, s.key): s for s in g.parse_claude_sources(grounding_lines(C_STREAM))}


def test_k_websearch_results_are_shown_links_and_one_summary_per_call(
    bench: dict[str, ModuleType],
) -> None:
    g = bench["grounding"]
    sources = _claude_sources(bench)
    shown = {k: s for (kind, k), s in sources.items() if kind == "shown"}
    # one per distinct link URL (the reddit link of both searches is one source)
    assert sorted(shown) == sorted([G_REDDIT, G_FILING, G_YAHOO, K_INSIDER, K_TWITS])
    reddit = shown[G_REDDIT]
    assert (reddit.title, reddit.host, reddit.snippet) == ("Costco fees thread", "reddit.com", "")
    assert reddit.segments == [] and reddit.action == "WebSearch"
    assert shown[G_FILING].host == FILING_HOST and shown[K_TWITS].title == "Costco CEO rumor"
    summaries = {k: s for (kind, k), s in sources.items() if kind == "summary"}
    assert sorted(summaries) == ["summary:tu_s1", "summary:tu_s2", "summary:tu_s3"]
    structured = summaries["summary:tu_s1"]  # the tool_use_result summary string
    assert (structured.host, structured.url) == ("web-search-summary", None)
    assert [t for _o, t in structured.segments] == [K_SUMMARY_1]
    text_only = summaries["summary:tu_s2"]  # Links JSON parsed, the text after it, no REMINDER
    assert [t for _o, t in text_only.segments] == [K_SUMMARY_2]
    unparsable = summaries["summary:tu_s3"]  # no link source, the whole text is the summary
    [(_order, whole)] = unparsable.segments
    assert whole.startswith('Web search results for query: "Costco revenue"')
    assert "Links: [broken" in whole and "4,828 million" in whole
    assert g.seed_category("web-search-summary") == (
        "other",
        "the WebSearch tool's own summary (not a web page)",
    )
    table = g.HostTable()
    assert table.lookup(structured.host)[:2] == ("other", "seed") and not table.unclassified


def test_k_websearch_text_fallback_and_parallel_results(bench: dict[str, ModuleType]) -> None:
    g = bench["grounding"]
    text = (
        'Web search results for query: "q"\n\nLinks: [{"title": "T", "url": "https://a.example/x"}]'
        "\n\nSummary one.\n\nREMINDER: You MUST include the sources above."
    )
    assert g.search_text_links(text) == (
        [{"title": "T", "url": "https://a.example/x"}],
        "Summary one.",
    )
    assert g.search_text_links("no links here") is None
    assert g.search_text_links("Links: [oops") is None

    def search(call: str, url: str) -> str:
        links = json.dumps([{"title": call, "url": url}])
        return f'Web search results for query: "{call}"\n\nLinks: {links}\n\nAbout {call}.'

    events = [
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "WebSearch", "input": {"query": "a"}},
                    {"type": "tool_use", "id": "t2", "name": "WebSearch", "input": {"query": "b"}},
                ],
            },
        },
        {
            # two results in one event: the structured result names neither call, so each
            # falls back to its own text
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": search("t1", "https://a.example/x"),
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "t2",
                        "content": search("t2", "https://b.example/y"),
                    },
                ]
            },
            "tool_use_result": {
                "query": "a",
                "results": [{"tool_use_id": "srv", "content": []}, "S"],
            },
        },
        {
            "type": "assistant",
            "message": {
                "id": "m2",
                "content": [
                    {"type": "tool_use", "id": "t3", "name": "WebSearch", "input": {"query": "c"}}
                ],
            },
        },
        {
            # a structured result without a summary string keeps the text's summary
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t3",
                        "content": search("t3", "https://c.example/z"),
                    }
                ]
            },
            "tool_use_result": {
                "results": [{"content": [{"title": "C", "url": "https://c.example/z"}]}]
            },
        },
    ]
    sources = {(s.kind, s.key): s for s in g.parse_claude_sources([json.dumps(e) for e in events])}
    assert {k for kind, k in sources if kind == "shown"} == {
        "https://a.example/x",
        "https://b.example/y",
        "https://c.example/z",
    }
    assert sources[("shown", "https://c.example/z")].title == "C"
    assert [t for _o, t in sources[("summary", "summary:t1")].segments] == ["About t1."]
    assert [t for _o, t in sources[("summary", "summary:t2")].segments] == ["About t2."]
    assert [t for _o, t in sources[("summary", "summary:t3")].segments] == ["About t3."]


def test_k_webfetch_results_are_opened_sources(bench: dict[str, ModuleType]) -> None:
    g = bench["grounding"]
    sources = _claude_sources(bench)
    opened = {k: s for (kind, k), s in sources.items() if kind == "opened"}
    assert list(opened) == [G_MACRO]  # the failed (is_error) fetch is not a source
    page = opened[G_MACRO]
    assert (page.host, page.action, page.title) == ("macrotrends.net", "WebFetch", "")
    assert [t for _o, t in page.segments] == [
        "Costco revenue for fiscal 2025 was $275.235B according to the chart."
    ]
    assert not any("broken.example" in str(s.url) for s in sources.values())
    # two fetches of one URL: one source holding both extracts
    events = []
    for n, text in enumerate(("first extract", "second extract")):
        events += [
            {
                "type": "assistant",
                "message": {
                    "id": f"m{n}",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"f{n}",
                            "name": "WebFetch",
                            "input": {"url": G_MACRO, "prompt": "p"},
                        }
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": f"f{n}", "content": text}]
                },
            },
        ]
    [twice] = g.parse_claude_sources([json.dumps(e) for e in events])
    assert twice.kind == "opened" and [t for _o, t in twice.segments] == [
        "first extract",
        "second extract",
    ]


def test_k_archivist_results_by_filing_id(bench: dict[str, ModuleType]) -> None:
    g = bench["grounding"]
    sources = _claude_sources(bench)
    archivist = {k: s for (kind, k), s in sources.items() if kind == "archivist"}
    # one per distinct filing id, a result without one is a source of its own; the failed call
    # (is_error, though it names a filing id) is not a source
    assert sorted(archivist) == sorted([G_FILING_A, G_FILING_B, "call:tu_a3"])
    assert archivist[G_FILING_A].filing_id == G_FILING_A
    assert archivist["call:tu_a3"].filing_id is None
    assert archivist[G_FILING_A].action == "search" and archivist["call:tu_a3"].action == "toc"
    assert "5,323" in archivist[G_FILING_A].segments[0][1]
    assert all("cc49a7a0" not in k for k in archivist)
    assert g.HostTable().category(archivist[G_FILING_A].host) == "filing"
    web_orders = [s.order for (kind, _k), s in sources.items() if kind in ("shown", "summary")]
    assert max(web_orders) < archivist[G_FILING_A].order  # stream event order


def test_k_stream_problems(bench: dict[str, ModuleType]) -> None:
    g = bench["grounding"]
    lines = grounding_lines(C_STREAM)
    assert g.stream_problem(lines) is None
    assert g.stream_problem([]) == "empty stream"
    assert g.stream_problem(lines[:-1]) == "no result event in the stream"
    assert g.stream_problem(fixture_lines("claude_stream_no_result.jsonl")) == (
        "no result event in the stream"
    )
    turn_limit = fixture_lines("claude_stream_max_turns.jsonl")
    assert g.stream_problem(turn_limit) == "result not success (error_max_turns)"
    result = json.loads(lines[-1])
    for key in ("usage", "modelUsage"):
        result.pop(key)
    assert g.stream_problem([*lines[:-1], json.dumps(result)]) == "no usage in the stream"
    assert g.log_problem("claude_code", lines) is None
    assert g.log_problem("codex", lines) == "no task_complete in the rollout"


def test_k_exposure_leaves_summaries_out_and_counts_them(bench: dict[str, ModuleType]) -> None:
    g = bench["grounding"]
    sources = g.parse_claude_sources(grounding_lines(C_STREAM))
    table = g.HostTable()
    exposure = g.run_exposure(sources, table)
    assert exposure["search_summaries"] == 3
    shown, opened = exposure["shown"], exposure["opened_or_archivist"]
    assert sum(shown.values()) == 5 and shown["forum"] == 2 and shown["other"] == 1
    assert (shown["filing"], shown["aggregator"]) == (1, 1)
    assert sum(opened.values()) == 4 and opened["filing"] == 3 and opened["aggregator"] == 1
    assert exposure["flags"]["forum_shown_only"] is True
    # summaries alone: no share, no flag, only the count
    summaries = [s for s in sources if s.kind == "summary"]
    alone = g.run_exposure(summaries, table)
    assert sum(alone["shown"].values()) == sum(alone["opened_or_archivist"].values()) == 0
    assert not any(alone["flags"].values()) and alone["search_summaries"] == 3
    meta = {"run_id": "r", "arm": "web", "stratum": "lookup", "question_id": "LK01"}
    row = g.exposure_row(meta, exposure, summaries=True)
    assert row["search_summaries"] == 3 and row["shown_total"] == 5
    assert "search_summaries" not in g.exposure_row(meta, exposure)  # Codex rows as in 1.4.0
    columns = g.exposure_run_columns("claude_code")
    assert columns[columns.index("archivist_sources") + 1] == "search_summaries"
    assert g.exposure_run_columns("codex") == g.EXPOSURE_RUN_COLUMNS
    assert "search_summaries" not in g.EXPOSURE_RUN_COLUMNS
    cells = g.exposure_cells([row, dict(row, run_id="r2", search_summaries=1)], resamples=20)
    counts = [c for c in cells if c["name"] == "search_summaries"]
    assert {c["stratum"] for c in counts} == {"lookup", "all_scored"}
    assert counts[0]["interval"]["estimate"] == 2.0 and counts[0]["sources"] == 4
    codex_cells = g.exposure_cells([g.exposure_row(meta, exposure)], resamples=20)
    assert not any(c["name"] == "search_summaries" for c in codex_cells)


def test_k_value_only_in_a_summary_is_secondary_other(bench: dict[str, ModuleType]) -> None:
    g = bench["grounding"]
    sources = g.parse_claude_sources(grounding_lines(C_STREAM))
    claims = [
        _fact(["77.7%"]),  # only in the WebSearch summary
        _fact(["$5,323 million"]),  # in a summary and in an Archivist result
        _fact(["$275.2 billion"]),  # in the WebFetch extract
    ]
    summary, filed, fetched = g.attribute_run(claims, sources, {}, g.HostTable())
    assert (summary["label"], summary["category"]) == ("secondary", "other")
    assert summary["matching_nonfiling_categories"] == ["other"]
    assert summary["values_in_nonfiling_source"] is True
    assert summary["verified_by_archivist"] is False
    assert (filed["label"], filed["category"], filed["primary_read"]) == ("filing", "filing", True)
    assert filed["verified_by_archivist"] is True  # the summary came before the Archivist result
    assert (fetched["label"], fetched["category"]) == ("secondary", "aggregator")


def _claude_run(
    root: Path, run_id: str, arm: str, qid: str, stratum: str, stream: str | None
) -> Path:
    """One completed Claude Code run directory: ``stdout.jsonl`` (``stream``; None: none),
    ``answer.md`` (the stream's result) and ``record.json``."""
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True)
    text = grounding_text(C_STREAM)
    if stream is not None:
        (run_dir / "stdout.jsonl").write_text(stream)
    (run_dir / "answer.md").write_text(json.loads(text.splitlines()[-1])["result"])
    record = {
        "run_id": run_id,
        "run_key": f"claude_code.{arm}.{qid}.r1",
        "agent": "claude_code",
        "arm": arm,
        "question_id": qid,
        "stratum": stratum,
        "repetition": 1,
        "attempt": 1,
        "status": "completed",
        "contaminated": False,
        "usage": {"total": 32900},
    }
    (run_dir / "record.json").write_text(json.dumps(record))
    return run_dir


def _claude_campaign(bench: dict[str, ModuleType], tmp_path: Path) -> tuple[Path, Path, Path]:
    """The ``_g_campaign`` Codex runs plus three Claude Code runs (web and both arms of LK01
    and a control) in one evidence dir: ``--agent`` picks which are read."""
    evidence, qpath, out = _g_campaign(bench, tmp_path)
    stream = grounding_text(C_STREAM)
    for run_id, arm, qid, stratum in (
        ("20261010T000001Z-claude_code.web.LK01.r1-a1", "web", "LK01", "lookup"),
        ("20261010T000002Z-claude_code.both.LK01.r1-a1", "both", "LK01", "lookup"),
        ("20261010T000003Z-claude_code.web.CT01.r1-a1", "web", "CT01", "control"),
    ):
        _claude_run(evidence, run_id, arm, qid, stratum, stream)
    return evidence, qpath, out


def test_k_grounding_commands_read_claude_code_runs(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    g = bench["grounding"]
    evidence, qpath, out = _claude_campaign(bench, tmp_path)
    before = _snapshot(evidence)
    graders: list[ScriptedGrader] = []

    def make_executor(log_dir: Path, stop: float, step: float) -> ScriptedGrader:
        graders.append(ScriptedGrader(_fake_graders))
        return graders[-1]

    monkeypatch.setattr(g, "make_executor", make_executor)
    ev, o = ["--evidence-dir", str(evidence)], ["--out", str(out)]
    agent = ["--agent", "claude_code"]
    assert cli.main(["grounding-sources", *ev, *o, "--questions", str(qpath), *agent]) == 0
    assert "3 runs parsed" in capsys.readouterr().out  # the Claude Code runs only
    names = sorted(p.stem for p in (out / "sources").glob("*.json"))
    assert names and all("claude_code" in n for n in names) and len(names) == 3
    data = json.loads((out / "sources" / f"{names[0]}.json").read_text())
    assert data["agent"] == "claude_code" and data["complete"] is True
    assert "stream_sha256" in data and "rollout_sha256" not in data
    assert {s["kind"] for s in data["sources"]} == {"shown", "summary", "opened", "archivist"}
    assert data["answer_links"] == {"S1": G_FILING, "S2": G_REDDIT}
    inventory = {e["host"]: e for e in json.loads((out / "hosts.json").read_text())}
    assert inventory["web-search-summary"]["kinds"] == ["summary"]
    # the summary pseudo host never goes to pass H
    assert cli.main(["grounding-hosts", *o]) == 0
    assert "1 hosts in 1 batches" in capsys.readouterr().out
    assert cli.main(["grounding-hosts", *o, "--execute", "--max-calls", "2"]) == 0
    [(prompt, _schema)] = graders[-1].calls
    assert "costcoinsider.net" in prompt and "web-search-summary" not in prompt
    claims_cmd = ["grounding-claims", *ev, *o, "--questions", str(qpath)]
    assert cli.main([*claims_cmd, "--execute", "--max-calls", "10", *agent]) == 0
    claimed = sorted(p.stem for p in (out / "claims").glob("*.json"))
    assert len(claimed) == 2 and all("claude_code" in n and "CT01" not in n for n in claimed)
    capsys.readouterr()
    assert cli.main(claims_cmd) == 0  # default codex: the Codex runs, never the Claude ones
    assert "3 answers for pass R" in capsys.readouterr().out
    analyze = ["grounding-analyze", *ev, *o, "--resamples", "50", *agent]
    assert cli.main(analyze) == 0
    analysis = json.loads((out / "analysis-grounding.json").read_text())
    assert analysis["agent"] == "claude_code" and analysis["runs"] == 3
    assert analysis["label"].startswith("replication, reduced power (section 15)")
    assert "section 15 (amendment 4)" in analysis["amendment"]
    assert "Claude Code" in analysis["scope"] and analysis["unclassified_hosts"] == []
    cells = {
        (c["arm"], c["stratum"], c["measure"], c["name"]): c
        for c in analysis["exposure"]
        if c["kind"] in ("shown", "runs")
    }
    assert (
        cells[("web", "lookup", "mean_run_count", "search_summaries")]["interval"]["estimate"]
        == 3.0
    )
    # the summaries (host web-search-summary, category other) are not shown sources; pass H
    # made the one unseeded host promotional
    other = cells[("web", "lookup", "pooled_share", "other")]
    assert other["sources"] == 0 and other["total"] == 5
    assert cells[("web", "lookup", "pooled_share", "promotional")]["sources"] == 1
    rows = json.loads((out / "grounding-rows.json").read_text())
    assert all(r["search_summaries"] == 3 for r in rows["exposure_runs"])
    reliance = {
        (c["arm"], c["name"]): c
        for c in analysis["reliance"]
        if c["rule"] == "primary" and c["stratum"] == "lookup"
    }
    assert reliance[("web", "fact_label_filing")]["claims"] == 1
    assert reliance[("web", "fact_label_secondary_forum")]["claims"] == 1
    results = tmp_path / "results"
    report = ["grounding-report", *ev, *o, "--results", str(results)]
    capsys.readouterr()
    assert cli.main(report) == 1  # a Claude Code analysis reported as Codex
    assert "agent claude_code, not --agent codex" in capsys.readouterr().err
    assert cli.main([*report, *agent]) == 0
    header = (results / "exposure-runs.csv").read_text().splitlines()[0].split(",")
    assert header[header.index("archivist_sources") + 1] == "search_summaries"
    assert "web-search-summary,other,seed" in (results / "host-categories.csv").read_text()
    committed = json.loads((results / "analysis-grounding.json").read_text())
    assert committed["agent"] == "claude_code"
    for path in results.rglob("*"):
        if path.is_file():
            assert FILING_HOST not in path.read_text() and path.stat().st_size < 1000 * 1024
    assert bench["report"].check_files([p for p in results.rglob("*") if p.is_file()]) == []
    assert _snapshot(evidence) == before  # read only input


def test_k_incomplete_stream_writes_no_sources(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = bench["cli"]
    evidence = tmp_path / "ev"
    qpath = tmp_path / "q.json"
    qpath.write_text(json.dumps(valid_set()))
    out = tmp_path / "out"
    lines = grounding_lines(C_STREAM)
    no_usage = json.loads(lines[-1])
    no_usage.pop("usage")
    no_usage.pop("modelUsage")
    cases = {
        "20261010T000001Z-claude_code.web.LK01.r1-a1": "\n".join(lines[:-1]),
        "20261010T000002Z-claude_code.both.LK01.r1-a1": (
            FIXTURES / "claude_stream_max_turns.jsonl"
        ).read_text(),
        "20261010T000003Z-claude_code.archivist.LK01.r1-a1": "\n".join(
            [*lines[:-1], json.dumps(no_usage)]
        ),
        "20261010T000004Z-claude_code.web.LK02.r1-a1": None,  # no stream at all
    }
    for run_id, stream in cases.items():
        arm, qid = run_id.split(".")[1], run_id.split(".")[2]
        _claude_run(evidence, run_id, arm, qid, "lookup", stream)
    cmd = [
        "grounding-sources",
        "--evidence-dir",
        str(evidence),
        "--out",
        str(out),
        "--questions",
        str(qpath),
        "--agent",
        "claude_code",
    ]
    assert cli.main(cmd) == 1
    err = capsys.readouterr().err
    assert "no result event in the stream" in err
    assert "result not success (error_max_turns)" in err
    assert "no usage in the stream" in err and "(no stream)" in err
    assert "4 runs without a complete stream" in err
    assert not list((out / "sources").glob("*.json"))
    restored = next(iter(cases))
    (evidence / "runs" / restored / "stdout.jsonl").write_text(grounding_text(C_STREAM))
    assert cli.main(cmd) == 1  # resumable: the restored run is parsed, the others still listed
    assert [p.stem for p in (out / "sources").glob("*.json")] == [restored]
    assert "3 runs without a complete stream" in capsys.readouterr().err


def test_k_claude_contamination_runs_one_agent_on_the_subscription(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = bench["cli"]
    questions = bench["questions"]
    data = contamination_set()
    path = tmp_path / "contamination-questions.json"
    path.write_text(json.dumps(data))
    base = ["run", "--questions", str(path)]
    assert cli.main([*base, "--agent", "claude_code", "--repetitions", "1"]) == 0
    out = capsys.readouterr().out
    subset = questions.claude_contamination_subset(data)
    assert len(subset) == 8 and "total runs: 24" in out
    assert "auth=subscription" in out and "--restricted" in out and "--bare" not in out
    for qid in subset:
        assert f"claude_code.both.{qid}.r1 [contamination]" in out
    evidence = tmp_path / "ev"
    for bad in (
        ["--agent", "all"],
        ["--agent", "claude_code", "--claude-auth", "api_key"],
        [
            "--agent",
            "claude_code",
            "--claude-auth",
            "api_key",
            "--execute",
            "--max-runs",
            "1",
            "--evidence-dir",
            str(evidence),
        ],
    ):
        with pytest.raises(SystemExit) as info:
            cli.main([*base, *bad])
        assert info.value.code == 2
    err = capsys.readouterr().err
    assert "one agent" in err and "Claude subscription only" in err
    assert not evidence.exists()  # refused before any preparation
    # the frozen ids of section 15 on the committed contamination set
    real = questions.load(QUESTION_SET.parent / "contamination-questions.json")
    assert questions.claude_contamination_subset(real) == [
        "CN02",
        "CN03",
        "CN04",
        "CN05",
        "CN15",
        "CN16",
        "CN10",
        "CN11",
    ]
    main_set = questions.load(QUESTION_SET)
    assert " ".join(questions.claude_subset(main_set)) == (
        "LK04 LK05 LK06 LK09 LK15 MH01 MH02 MH03 MH04 MH14 MD04 MD09 MD12 MD14 MD15 MP01 MP02 "
        "MP05 MP06 MP15 BR01 BR03 BR07 BR12 BR15 RC01 RC03 RC07 RC14 RC15 CT01 CT02"
    )


def _family_campaign(
    bench: dict[str, ModuleType],
    root: Path,
    agent: str,
    qids: Sequence[str],
    b_missing: Collection[str] = (),
    b_only: Collection[str] = (),
) -> Path:
    """Judged runs of one agent (three arms per question): pass A says every fact correct,
    pass B says the first fact missing on ``b_missing`` questions; ``b_only`` questions get a
    second, pass B only judged run (amendment 2) in the web arm."""
    judge = bench["judge"]
    lookup = {q["id"]: q for q in valid_set()["questions"]}
    n = 0
    for qid in qids:
        q = lookup[qid]
        cells = [(arm, 1) for arm in ("web", "archivist", "both")]
        cells += [("web", 2)] if qid in b_only else []
        for arm, rep in cells:
            n += 1
            run_id = f"20261010T{n:06d}Z-{agent}.{arm}.{qid}.r{rep}-a1"
            run_dir = root / "runs" / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "answer.md").write_text("Net sales 391,035.")
            record = {
                "run_id": run_id,
                "run_key": f"{agent}.{arm}.{qid}.r{rep}",
                "agent": agent,
                "arm": arm,
                "question_id": qid,
                "stratum": q["stratum"],
                "repetition": rep,
                "attempt": 1,
                "status": "completed",
                "contaminated": False,
                "usage": {"total": {"web": 3000, "archivist": 1000, "both": 2000}[arm]},
            }
            (run_dir / "record.json").write_text(json.dumps(record))
            wrong = ("missing",) if qid in b_missing else ()
            executors = {
                "A": lambda _p, q=q: _verdicts_for(judge, q),
                "B": lambda _p, q=q, w=wrong: _verdicts_for(judge, q, *w),
            }
            passes = ("B",) if rep == 2 else ("A", "B")
            result = judge.judge(q, "Net sales 391,035.", run_id, executors, passes=passes)
            (run_dir / "judgement.json").write_text(json.dumps(dict(result.as_dict(), batches=1)))
    return root


def test_k_analyze_question_filter(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = bench["cli"]
    qpath = tmp_path / "q.json"
    qpath.write_text(json.dumps(valid_set()))
    evidence = _family_campaign(bench, tmp_path / "codex", "codex", ["LK01", "LK02", "MH01"])
    common = ["analyze", "--questions", str(qpath), "--evidence-dir", str(evidence)]
    common += ["--resamples", "20", "--primary-judge", "B"]
    capsys.readouterr()
    assert cli.main(common) == 0
    whole = json.loads(capsys.readouterr().out)
    assert "question_filter" not in whole  # the default output is unchanged
    assert cli.main([*common, "--question", "LK01", "--question", "MH01"]) == 0
    part = json.loads(capsys.readouterr().out)
    assert part["question_filter"] == ["LK01", "MH01"]
    assert part["evidence_fingerprint"] == whole["evidence_fingerprint"]
    n_questions = {
        (c["stratum"], c["arm"]): c["accuracy_diff_arm_minus_baseline"]["n_questions"]
        for c in part["comparisons"]
        if c["baseline"] == "web"
    }
    assert n_questions[("lookup", "both")] == 1 and n_questions[("all_scored", "both")] == 2
    assert part["pass_agreement"]["answers"] == 6
    assert all("LK02" not in item["run_id"] for item in part["audit_queue"])
    assert len(whole["comparisons"]) == len(part["comparisons"])  # LK and MH strata in both
    ledger = ["analyze", "--evidence-dir", str(evidence), "--format", "ledger"]
    assert cli.main([*ledger, "--question", "LK01"]) == 2
    assert cli.main([*common, "--question", "ZZ99"]) == 2
    assert "unknown question ids: ZZ99" in capsys.readouterr().err
    # a --question analysis is not a results report
    analysis = tmp_path / "part.json"
    assert cli.main([*common, "--question", "LK01"]) == 0
    analysis.write_text(capsys.readouterr().out)
    report = ["report", "--questions", str(qpath), "--evidence-dir", str(evidence)]
    assert cli.main([*report, "--analysis", str(analysis), "--out", str(tmp_path / "r")]) == 1
    assert "exploratory recomputation" in capsys.readouterr().err


def test_k_cross_campaign_judge_bias_check(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = bench["cli"]
    report = bench["report"]
    qpath = tmp_path / "q.json"
    qpath.write_text(json.dumps(valid_set()))
    claude = _family_campaign(
        bench, tmp_path / "claude", "claude_code", ["LK01", "LK02"], b_missing={"LK01"}
    )
    codex = _family_campaign(
        bench, tmp_path / "codex", "codex", ["LK01", "LK02", "LK03"], b_only={"LK02"}
    )
    before = _snapshot(codex)
    common = ["analyze", "--questions", str(qpath), "--evidence-dir", str(claude)]
    common += ["--resamples", "50"]
    capsys.readouterr()
    assert cli.main([*common, "--primary-judge", "B"]) == 0
    plain = json.loads(capsys.readouterr().out)
    assert "deferred" in plain["judge_bias"] and "bias_evidence_fingerprint" not in plain
    bias = [*common, "--primary-judge", "B", "--bias-evidence-dir", str(codex)]
    assert cli.main(bias) == 0
    text = capsys.readouterr().out
    result = json.loads(text)
    inter = result["judge_bias"]["family_interaction"]
    # LK03 (Codex only) is left out; the pass B only Codex run carries no pass A
    assert inter["shared_questions"] == 2 and inter["runs"] == {"anthropic": 6, "openai": 6}
    assert inter["estimate"] > 0  # the Codex judge is stricter on the Claude Code answers
    rates = {
        (r["judge_pass"], r["answer_family"]): r for r in result["judge_bias"]["verdict_rates"]
    }
    assert rates[("A", "openai")]["runs"] == 6 and rates[("B", "anthropic")]["missing_rate"] > 0
    sensitivity = result["single_judge_sensitivity"]
    assert isinstance(sensitivity, list) and {e["judge_pass"] for e in sensitivity} == {"A", "B"}
    assert {e["agent"] for e in sensitivity} == {"claude_code"}  # this evidence only
    assert result["bias_evidence_fingerprint"] == report.evidence_fingerprint(codex, qpath, [])
    assert result["bias_evidence"] == {
        "label": "exploratory",
        "runs": 7,
        "judged_runs": 7,
        "runs_graded_by_both_judges": 6,
        "questions": 2,
        "agents": ["codex"],
    }
    assert str(codex) not in text and str(tmp_path) not in text  # no path recorded
    assert {c["agent"] for c in result["comparisons"]} == {"claude_code"}
    assert _snapshot(codex) == before  # read only
    for bad, message in (
        ([*common, "--bias-evidence-dir", str(codex)], "needs --primary-judge B"),
        (
            [*common, "--primary-judge", "B", "--bias-evidence-dir", str(claude)],
            "another campaign",
        ),
        (
            [*common, "--primary-judge", "B", "--bias-evidence-dir", str(tmp_path / "none")],
            "holds no run records",
        ),
    ):
        assert cli.main(bad) == 2
        assert message in capsys.readouterr().err
    # report: the bias evidence must be named and unchanged
    analysis = tmp_path / "analysis.json"
    analysis.write_text(text)
    rep = ["report", "--questions", str(qpath), "--evidence-dir", str(claude)]
    rep += ["--analysis", str(analysis), "--out", str(tmp_path / "results")]
    assert cli.main(rep) == 1
    assert "pass its --bias-evidence-dir" in capsys.readouterr().err
    assert cli.main([*rep, "--bias-evidence-dir", str(codex)]) == 0
    comparisons = (tmp_path / "results" / "comparisons.csv").read_text()
    assert "single_judge" in comparisons
    committed = (tmp_path / "results" / "analysis.json").read_text()
    assert "bias_evidence_fingerprint" in committed and str(codex) not in committed
    judgement = next((codex / "runs").glob("*LK01*/judgement.json"))
    judgement.write_text(judgement.read_text() + "\n")
    capsys.readouterr()
    assert cli.main([*rep, "--bias-evidence-dir", str(codex)]) == 1
    assert "bias evidence changed" in capsys.readouterr().err
    plain_path = tmp_path / "plain.json"
    plain_path.write_text(json.dumps(plain))
    rep_plain = [*rep[:-4], "--analysis", str(plain_path), "--out", str(tmp_path / "r2")]
    assert cli.main([*rep_plain, "--bias-evidence-dir", str(codex)]) == 1
    assert "no judge bias check" in capsys.readouterr().err


def test_k_claude_code_contamination_analysis(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    g = bench["grounding"]
    judge = bench["judge"]
    evidence, qpath, out = _claude_campaign(bench, tmp_path)
    cev = tmp_path / "cev"
    data = contamination_set()
    cq = tmp_path / "contamination-questions.json"
    cq.write_text(json.dumps(data))
    labels = judge.fact_labels(data["questions"][0])
    stream = grounding_text(C_STREAM)
    for n, (arm, answer) in enumerate(
        (
            ("web", "Apple's net sales were 391,999 million."),
            ("archivist", "Apple's net sales were 391,035 million; a post's 391,999 is wrong."),
            ("both", "Apple's net sales were 391,035 million."),
        ),
        start=1,
    ):
        run_id = f"20261011T00000{n}Z-claude_code.{arm}.CN01.r1-a1"
        run_dir = _claude_run(cev, run_id, arm, "CN01", "contamination", stream)
        (run_dir / "answer.md").write_text(answer)
        judgement = {
            "run_id": run_id,
            "status": "ok",
            "rule": "B",
            "passes": {
                "B": {
                    "fact_verdicts": {f: "correct" for f in labels},
                    "complete": True,
                    "error": None,
                }
            },
        }
        (run_dir / "judgement.json").write_text(json.dumps(judgement))
    codex_dir = cev / "runs" / "20261011T000009Z-codex.web.CN01.r1-a1"
    codex_dir.mkdir(parents=True)
    (codex_dir / "answer.md").write_text("Apple's net sales were 391,999 million.")
    codex_record = {
        "run_id": codex_dir.name,
        "run_key": "codex.web.CN01.r1",
        "agent": "codex",
        "arm": "web",
        "question_id": "CN01",
        "stratum": "contamination",
        "status": "completed",
        "usage": {"total": 1},
    }
    (codex_dir / "record.json").write_text(json.dumps(codex_record))
    monkeypatch.setattr(g, "make_executor", lambda *_a: ScriptedGrader(_fake_graders))
    agent = ["--agent", "claude_code"]
    t = ["trap-judge", "--evidence-dir", str(cev), "--out", str(out), "--questions", str(cq)]
    capsys.readouterr()
    assert cli.main(t) == 0  # default codex: the one Codex answer only
    assert "1 answers for pass T" in capsys.readouterr().out
    assert cli.main([*t, *agent, "--execute", "--max-calls", "6"]) == 0
    traps = {p.stem: json.loads(p.read_text()) for p in (out / "traps").glob("*.json")}
    assert sorted(traps) == sorted(p.name for p in (cev / "runs").iterdir() if "claude" in p.name)
    assert sorted(v["adoption"] for v in traps.values()) == [0.0, 0.0, 1.0]
    ev = ["--evidence-dir", str(evidence), "--out", str(out)]
    for d, q in ((evidence, qpath), (cev, cq)):
        sources = ["grounding-sources", "--evidence-dir", str(d), "--out", str(out)]
        assert cli.main([*sources, "--questions", str(q), *agent]) == 0
        claims = ["grounding-claims", "--evidence-dir", str(d), "--out", str(out)]
        assert (
            cli.main([*claims, "--questions", str(q), *agent, "--execute", "--max-calls", "9"]) == 0
        )
    con = ["--contamination-evidence-dir", str(cev), "--contamination-questions", str(cq)]
    assert cli.main(["grounding-analyze", *ev, *con, *agent, "--resamples", "20"]) == 0
    section = json.loads((out / "analysis-grounding.json").read_text())["contamination"]
    assert section["agent"] == "claude_code"
    assert section["label"].startswith("replication, reduced power (section 15")
    assert section["h6_trap_adoption_web_minus_arm"]["archivist"]["estimate"] == 1.0
    assert section["per_arm"]["web"]["trap_adoption_question_mean"] == 1.0
    assert section["per_arm"]["web"]["completed_runs"] == 1  # the Codex run is not read
    assert {c["agent"] for c in section["comparisons"]} == {"claude_code"}
    assert section["runs_without_claims"] == [] and section["runs_without_trap_judgement"] == []
    results = tmp_path / "results"
    assert cli.main(["grounding-report", *ev, *con, *agent, "--results", str(results)]) == 0
    shard = list(csv.DictReader((results / "contamination" / "claims-001.csv").open()))
    assert {r["arm"] for r in shard} == {"web", "archivist", "both"}
    capsys.readouterr()


def test_k_preregistration_section_15_fixes_the_design(bench: dict[str, ModuleType]) -> None:
    questions = bench["questions"]
    prereg = (QUESTION_SET.parent / "preregistration.md").read_text()
    head, _, section = prereg.partition("## 15. Amendment 4 (2026-10-07)")
    assert section, "section 15 missing"
    assert "## 14. Amendment 3 (2026-10-07)" in head and "## 15." not in head
    section = section.split("\n## 16. ", 1)[0]  # section 15 alone (amendment 5 follows)
    front = head.split("---")[1]
    assert "amendment 4, a light Claude Code replication" in front
    flat = " ".join(section.split())
    main_ids = questions.claude_subset(questions.load(QUESTION_SET))
    assert ", ".join(main_ids) in flat
    for needle in (
        "traps CN04, CN15, CN03, CN16, CN05, CN02; controls CN10, CN11",
        "96 runs",
        "24 runs",
        "120 runs; attempt cap 132",
        '"replication, reduced power"',
        "`--stop-at-window 70`",
        "`allowed_warning`",
        "`web-search-summary`",
        "`--primary-judge B`",
        "Pass A",
        "about 21M agent tokens",
        "about 1,150 Archivist calls",
        "not repeated",
        "Harness 1.5.0",
    ):
        assert needle in flat, needle
    assert not re.search(r"\bunbiased\b|\bneutral\b", section, re.I)
    assert bench["redact"].find_upstream_hosts(section) == []


def test_k_readme_documents_amendment_4() -> None:
    text = HARNESS_README.read_text()
    assert "Amendment 4: Claude Code replication" in text and "1.5.0" in text
    for needle in (
        "--agent claude_code",
        "--bias-evidence-dir",
        "--question",
        "web-search-summary",
        "search_summaries",
        "--stop-at-window 70",
        "trap-judge",
        "grounding-report",
    ):
        assert needle in text, needle
    # The command sequence lives in RUNBOOK.md section 7: the main set judge call runs both
    # passes on the Claude Code window stop.
    sequence = runbook_section("## 7. Claude Code replication (amendment 4)")
    assert '--evidence-dir "$EV" --execute \\\n  --max-calls <n> --stop-at-window 70' in sequence


def test_k_structured_search_result_needs_a_single_tool_result(
    bench: dict[str, ModuleType],
) -> None:
    g = bench["grounding"]
    good = 'Web search results for query: "a"\n\nLinks: [{"title": "A", "url": "https://a.example/x"}]\n\nAbout a.'
    events = [
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "WebSearch", "input": {}},
                    {"type": "tool_use", "id": "toolu_2", "name": "WebSearch", "input": {}},
                ],
            },
        },
        {
            # a successful and a failed search in one event; the structured result (server
            # tool ids, as Claude Code 2.1.289 writes them) lists links of both searches
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": good},
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_2",
                        "is_error": True,
                        "content": "Error: search failed",
                    },
                ]
            },
            "tool_use_result": {
                "results": [
                    {
                        "tool_use_id": "srvtoolu_1",
                        "content": [{"title": "A", "url": "https://a.example/x"}],
                    },
                    {
                        "tool_use_id": "srvtoolu_2",
                        "content": [{"title": "L", "url": "https://leak.example/y"}],
                    },
                    "Structured summary.",
                ]
            },
        },
    ]
    sources = g.parse_claude_sources([json.dumps(e) for e in events])
    assert {(s.kind, s.key) for s in sources} == {
        ("shown", "https://a.example/x"),
        ("summary", "summary:toolu_1"),
    }
    [summary] = [s for s in sources if s.kind == "summary"]
    assert [t for _o, t in summary.segments] == ["About a."]


def test_k_single_judge_values_are_decided_per_pass(bench: dict[str, ModuleType]) -> None:
    judge = bench["judge"]
    question = valid_set()["questions"][0]
    result = judge.judge(
        question,
        "Net sales 391,035.",
        "r1",
        {"A": lambda _p: "not json", "B": lambda _p: _verdicts_for(judge, question, "missing")},
    )
    stored = result.as_dict()
    assert stored["status"] == "judge_error"  # pass A malformed twice, pass B valid
    n = len(judge.fact_labels(question))
    assert judge.single_judge_accuracy(stored, "B") == round((n - 1) / n, 6)
    assert judge.single_judge_complete(stored, "B") == 1.0
    assert judge.single_judge_accuracy(stored, "A") is None
    assert judge.single_judge_complete(stored, "A") is None
    assert judge.scoring(stored, "B") is not None and judge.scoring(stored) is None


def test_k_analysis_rows_keep_a_valid_pass_a_under_primary_b(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    cli = bench["cli"]
    judge = bench["judge"]
    data = valid_set()
    question = data["questions"][0]
    evidence = _family_campaign(bench, tmp_path / "ev", "claude_code", [question["id"]])
    run_dir = next((evidence / "runs").glob("*.web.*"))
    broken = judge.judge(
        question,
        "Net sales 391,035.",
        run_dir.name,
        {"A": lambda _p: _verdicts_for(judge, question, "missing"), "B": lambda _p: "not json"},
    )
    (run_dir / "judgement.json").write_text(json.dumps(dict(broken.as_dict(), batches=1)))
    rows = {r["run_id"]: r for r in cli.analysis_rows(evidence, data, None, "B")}
    row = rows[run_dir.name]
    n = len(judge.fact_labels(question))
    assert row["accuracy"] is None and row["judge_status"] == "judge_error"
    assert row["accuracy_judge_A"] == round((n - 1) / n, 6)
    assert row["completion_judge_A"] == 1.0
    assert row["accuracy_judge_B"] is None and row["completion_judge_B"] is None
    other = next(r for r in rows.values() if r["run_id"] != run_dir.name)
    assert other["accuracy_judge_A"] == other["accuracy_judge_B"] == 1.0


# --- Claude-code campaign relaunch (harness 1.5.1): the explicit Claude login ---------------------


def _claude_keys(env: dict[str, str]) -> set[str]:
    return {k for k in env if k.startswith("CLAUDE")}


# The harness's own Claude switch (a permitted extra) beside the chosen account; nothing else
# named CLAUDE* reaches a Claude child or the Claude probe.
ACCOUNT_KEYS = {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_DISABLE_CLAUDE_MDS"}


def _account_dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """(the chosen account X, a symlink naming it, another account Y the parent env holds)."""
    chosen = tmp_path / "claude-account-x"
    chosen.mkdir()
    link = tmp_path / "claude-account-link"
    link.symlink_to(chosen)
    other = tmp_path / "claude-account-y"
    other.mkdir()
    return chosen, link, other


def _claude_run_cli(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: Any
) -> list[str]:
    """``run`` for one Claude Code both arm cell, with the agent process faked (the real
    ``execute_plan`` and plan gate run; the parent environment is ``os.environ``)."""
    runner = bench["runner"]
    qpath = tmp_path / "q.json"
    qpath.write_text(json.dumps(valid_set()))
    real = runner.execute_plan

    def fake_plan(specs: Any, root: Path, max_runs: int, concurrency: int, **kw: Any) -> Any:
        return real(
            specs,
            root,
            max_runs,
            concurrency,
            executor=fake,
            codex_auth=_auth(tmp_path),
            preflight=no_servers,
            **kw,
        )

    monkeypatch.setattr(runner, "execute_plan", fake_plan)
    return [
        "run",
        "--questions",
        str(qpath),
        "--agent",
        "claude_code",
        "--question",
        "LK01",
        "--arms",
        "both",
        "--repetitions",
        "1",
        "--evidence-dir",
        str(tmp_path / "ev"),
    ]


def test_l_run_and_its_gate_probe_share_the_chosen_account(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = bench["cli"]
    probe = bench["probe"]
    runner = bench["runner"]
    chosen, link, other = _account_dirs(tmp_path)
    # The parent is polluted with another account and a session id: neither reaches a child.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-from-the-parent")
    probe_envs: list[dict[str, str]] = []

    def fake_claude(env: dict[str, str], cwd: Path) -> list[str]:
        probe_envs.append(dict(env))
        return claude_probe_stream()

    monkeypatch.setattr(probe, "claude_probe_call", fake_claude)
    fake = FakeExecutor(["claude_stream_rate_limits.jsonl"])
    base = _claude_run_cli(bench, tmp_path, monkeypatch, fake)
    rc = cli.main([*base, "--execute", "--max-runs", "1", "--claude-config-dir", str(link)])
    assert rc == 0
    account = str(chosen.resolve())  # stored as the absolute resolved path, not the link
    [request] = fake.calls
    [probe_env] = probe_envs
    for env in (request.env, probe_env):
        assert env["CLAUDE_CONFIG_DIR"] == account
        assert _claude_keys(env) == ACCOUNT_KEYS, sorted(env)
    evidence = tmp_path / "ev"
    [run_dir] = list((evidence / "runs").iterdir())
    command = json.loads((run_dir / "command.json").read_text())
    assert command["env"]["CLAUDE_CONFIG_DIR"] == account
    assert "CLAUDE_CODE_SESSION_ID" not in command["env_keys"]
    record = json.loads((run_dir / "record.json").read_text())
    assert record["claude_config_dir"] == account
    assert json.loads((run_dir / "meta.json").read_text())["claude_config_dir"] == account
    assert runner.build_record(run_dir).claude_config_dir == account  # kept on re-extraction
    [row] = [json.loads(x) for x in (evidence / "plan-probes.jsonl").read_text().splitlines()]
    assert row["kind"] == "claude_probe" and row["claude_config_dir"] == account
    ledger = runner.load_ledger(evidence / "ledger.jsonl")
    assert ledger[0].claude_config_dir == account


def test_l_execute_plan_probe_env_equals_run_env(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = bench["probe"]
    runner = bench["runner"]
    chosen, _link, other = _account_dirs(tmp_path)
    parent = {
        "PATH": "/usr/bin",
        "HOME": str(tmp_path),
        "CLAUDE_CONFIG_DIR": str(other),
        "CLAUDE_CODE_SESSION_ID": "session-from-the-parent",
    }
    probe_envs: list[dict[str, str]] = []

    def fake_claude(env: dict[str, str], cwd: Path) -> list[str]:
        probe_envs.append(dict(env))
        return claude_probe_stream()

    monkeypatch.setattr(probe, "claude_probe_call", fake_claude)
    fake = FakeExecutor(["claude_stream_both.jsonl"])
    records = runner.execute_plan(
        [_spec(bench, "claude_code", "both")],
        tmp_path / "ev",
        max_runs=1,
        executor=fake,
        parent_env=parent,
        codex_auth=_auth(tmp_path),
        preflight=no_servers,
        claude_config_dir=chosen,
    )
    assert len(records) == 1 and records[0].claude_config_dir == str(chosen)
    [request] = fake.calls
    [probe_env] = probe_envs
    assert request.env["CLAUDE_CONFIG_DIR"] == probe_env["CLAUDE_CONFIG_DIR"] == str(chosen)
    assert _claude_keys(request.env) == _claude_keys(probe_env) == ACCOUNT_KEYS
    # A plan with a Claude Code run and no chosen account is refused before anything exists.
    with pytest.raises(ValueError, match="--claude-config-dir"):
        runner.execute_plan(
            [_spec(bench, "claude_code", "both")],
            tmp_path / "ev2",
            max_runs=1,
            executor=fake,
            parent_env=parent,
        )
    assert not (tmp_path / "ev2").exists() and len(fake.calls) == 1


def test_l_child_env_takes_the_account_only_as_an_explicit_extra(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    runner = bench["runner"]
    cc = bench["claude_code"]
    probe = bench["probe"]
    parent = {"PATH": "/usr/bin", "CLAUDE_CONFIG_DIR": "/parent", "CLAUDE_CODE_SESSION_ID": "s"}
    assert "CLAUDE_CONFIG_DIR" in runner.PERMITTED_EXTRAS
    assert _claude_keys(runner.child_env(parent, {})) == set()  # the parent's is never copied
    env = runner.child_env(parent, cc.account_extras(tmp_path))
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path) and _claude_keys(env) == {"CLAUDE_CONFIG_DIR"}
    with pytest.raises(ValueError, match="no Claude account chosen"):
        cc.account_extras(None)
    with pytest.raises(AssertionError, match="CLAUDE_CODE_SESSION_ID"):
        runner.ENV_ALLOWLIST += ("CLAUDE_CODE_SESSION_ID",)  # still refused if ever allowlisted
        try:
            runner.child_env(parent, {})
        finally:
            runner.ENV_ALLOWLIST = runner.ENV_ALLOWLIST[:-1]
    assert "CLAUDE_CONFIG_DIR" not in probe.claude_probe_env(parent, None)
    assert probe.claude_probe_env(parent, tmp_path)["CLAUDE_CONFIG_DIR"] == str(tmp_path)
    cfg = bench["model"].DEFAULT_CONFIGS["claude_code"]
    assert "CLAUDE_CONFIG_DIR" not in cc.env_extras(cfg, parent)
    assert cc.env_extras(cfg, parent, tmp_path)["CLAUDE_CONFIG_DIR"] == str(tmp_path)


def test_l_claude_launch_without_an_account_exits_2(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    probe = bench["probe"]
    judge = bench["judge"]

    def no_call(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a probe or call was made without a chosen account")

    monkeypatch.setattr(probe, "claude_probe_call", no_call)
    monkeypatch.setattr(judge, "make_executors", no_call)
    fake = FakeExecutor(["claude_stream_both.jsonl"])
    base = _claude_run_cli(bench, tmp_path, monkeypatch, fake)
    capsys.readouterr()
    with pytest.raises(SystemExit) as info:
        cli.main([*base, "--execute", "--max-runs", "3"])
    assert info.value.code == 2 and "--claude-config-dir" in capsys.readouterr().err
    assert fake.calls == [] and not (tmp_path / "ev").exists()
    # A dry run and plan need no account.
    assert cli.main(base) == 0
    assert cli.main(["plan", *base[1:]]) == 0
    # Judge pass A without the flag: exit 2 before any executor, probe or call.
    (tmp_path / "j").mkdir()
    evidence, qpath = _judge_setup(bench, tmp_path / "j", 1)
    jbase = ["judge", "--questions", str(qpath), "--evidence-dir", str(evidence), "--execute"]
    for passes in (["--passes", "A", "B"], [], ["--passes", "A"]):
        capsys.readouterr()
        with pytest.raises(SystemExit) as info:
            cli.main([*jbase, *passes, "--max-calls", "4"])
        assert info.value.code == 2 and "--claude-config-dir" in capsys.readouterr().err
    assert not (evidence / "judge-ledger.jsonl").exists()
    assert not (evidence / "plan-probes.jsonl").exists()
    # plan-probe without the flag.
    with pytest.raises(SystemExit) as info:
        cli.main(["plan-probe", "--evidence-dir", str(tmp_path / "pp")])
    assert info.value.code == 2 and not (tmp_path / "pp").exists()


@pytest.mark.parametrize("problem", ["missing", "file", "empty"])
def test_l_bad_account_dir_exits_2(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    problem: str,
) -> None:
    cli = bench["cli"]
    probe = bench["probe"]
    judge = bench["judge"]

    def no_call(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a probe or call was made with a bad account")

    monkeypatch.setattr(probe, "claude_probe_call", no_call)
    monkeypatch.setattr(judge, "make_executors", no_call)
    if problem == "missing":
        bad, message = str(tmp_path / "no-such-account"), "does not exist"
    elif problem == "file":
        (tmp_path / "a-file").write_text("x")
        bad, message = str(tmp_path / "a-file"), "is not a directory"
    else:
        bad, message = "", "is empty"
    fake = FakeExecutor(["claude_stream_both.jsonl"])
    base = _claude_run_cli(bench, tmp_path, monkeypatch, fake)
    (tmp_path / "j").mkdir()
    evidence, qpath = _judge_setup(bench, tmp_path / "j", 1)
    commands = [
        [*base, "--execute", "--max-runs", "3"],
        ["judge", "--questions", str(qpath), "--evidence-dir", str(evidence), "--execute"]
        + ["--max-calls", "4"],
        ["plan-probe", "--evidence-dir", str(tmp_path / "pp")],
    ]
    for command in commands:
        capsys.readouterr()
        with pytest.raises(SystemExit) as info:
            cli.main([*command, "--claude-config-dir", bad])
        assert info.value.code == 2, command
        err = capsys.readouterr().err
        assert message in err and (problem == "empty" or bad in err), err
    assert fake.calls == [] and not (tmp_path / "ev").exists() and not (tmp_path / "pp").exists()
    assert not (evidence / "judge-ledger.jsonl").exists()


def test_l_parent_only_account_refuses_the_probe_and_the_launch(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = bench["probe"]
    runner = bench["runner"]
    _chosen, _link, other = _account_dirs(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))

    def no_call(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a Claude probe subprocess ran without a chosen account")

    monkeypatch.setattr(probe, "claude_probe_call", no_call)
    # Unconfigured (the default environment) and configured without a chosen account alike.
    configured = probe.PlanGate(90)
    configured.configure(probe.claude_probe_env(dict(os.environ), None), tmp_path, None)
    for gate in (probe.PlanGate(90), configured):
        report = gate.probe_claude()
        assert report == {"ok": False, "error": probe.NO_CLAUDE_ACCOUNT}
        [reason] = gate.check_claude()
        assert "no Claude account chosen" in reason and "CLAUDE_CONFIG_DIR" in reason
        assert gate.claude_probes == 0 and gate.claude_config_dir is None
    # The launch is refused through check_claude: nothing prepared, nothing run.
    budget = runner.Budget(3)
    budget.gate.configure(probe.claude_probe_env(dict(os.environ), None), tmp_path, None)
    fake = FakeExecutor(["claude_stream_both.jsonl"])
    evidence = tmp_path / "ev"
    records = runner.execute_spec(
        _spec(bench, "claude_code", "both"),
        evidence,
        budget,
        fake,
        dict(os.environ),
        _auth(tmp_path),
        evidence / "ledger.jsonl",
        threading.Lock(),
        claude_config_dir=_chosen,
    )
    assert records == [] and fake.calls == [] and budget.stop_kind == "plan_gate"
    assert "no Claude account chosen" in budget.stops[0][2]
    assert not (evidence / "runs").exists() and budget.used == 0


def test_l_judge_pass_a_and_its_gate_probe_share_the_chosen_account(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = bench["cli"]
    judge = bench["judge"]
    probe = bench["probe"]
    chosen, link, other = _account_dirs(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-from-the-parent")
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt"}))
    monkeypatch.setenv("HOME", str(home))
    evidence, qpath = _judge_setup(bench, tmp_path, 1)
    question = questions_lookup = {q["id"]: q for q in valid_set()["questions"]}["LK01"]
    assert questions_lookup is question
    verdicts = _verdicts_for(judge, question)
    claude_stream = _claude_judge_stream(
        result={
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "",
            "structured_output": json.loads(verdicts),
            "num_turns": 2,
            "total_cost_usd": 0.05,
            "modelUsage": {"claude-opus-5-5": {"inputTokens": 10, "outputTokens": 40}},
        }
    )
    seen: list[Any] = []
    claude_process = _fake_process(bench, claude_stream, seen)
    codex_process = _fake_process(bench, CODEX_JUDGE_STREAM, seen, last=verdicts)

    def process(req: Any) -> Any:
        return (claude_process if req.argv[0] == "claude" else codex_process)(req)

    real_make = judge.make_executors
    made: list[dict[str, Any]] = []

    def make(log: Path, passes: Any = judge.PASSES, **kw: Any) -> dict[str, Any]:
        made.append(kw)
        executors = real_make(log, passes, **kw)
        for ex in executors.values():
            ex.process = process
        return executors

    monkeypatch.setattr(judge, "make_executors", make)
    probe_envs: list[dict[str, str]] = []

    def fake_claude(env: dict[str, str], cwd: Path) -> list[str]:
        probe_envs.append(dict(env))
        return claude_probe_stream()

    monkeypatch.setattr(probe, "claude_probe_call", fake_claude)
    base = ["judge", "--questions", str(qpath), "--evidence-dir", str(evidence), "--execute"]
    rc = cli.main(
        [*base, "--passes", "A", "B", "--max-calls", "2"] + ["--claude-config-dir", str(link)]
    )
    assert rc == 0
    account = str(chosen.resolve())
    assert made == [{"claude_config_dir": chosen.resolve()}]
    requests = [r for r in seen if not isinstance(r, (str, bool))]
    [a_call] = [r for r in requests if r.argv[0] == "claude"]
    [b_call] = [r for r in requests if r.argv[0] == "codex"]
    [probe_env] = probe_envs
    for env in (a_call.env, probe_env):
        assert env["CLAUDE_CONFIG_DIR"] == account
        assert _claude_keys(env) == ACCOUNT_KEYS, sorted(env)
    assert _claude_keys(b_call.env) == set()  # pass B (Codex) carries no CLAUDE* key
    logs = evidence / "judge-logs" / "r0" / "batch-1"
    a_command = json.loads((logs / "A-1" / "command.json").read_text())
    b_command = json.loads((logs / "B-1" / "command.json").read_text())
    assert (
        a_command["claude_config_dir"] == account and "CLAUDE_CONFIG_DIR" in a_command["env_keys"]
    )
    assert set(b_command) == {"argv", "env_keys"}  # Codex judge calls unchanged
    rows = [json.loads(x) for x in (evidence / "judge-ledger.jsonl").read_text().splitlines()]
    [a_ok] = [r for r in rows if r["pass"] == "A" and r["status"] == "ok"]
    [b_ok] = [r for r in rows if r["pass"] == "B" and r["status"] == "ok"]
    assert a_ok["meta"]["claude_config_dir"] == account
    assert "claude_config_dir" not in b_ok["meta"]
    [probe_row] = [
        json.loads(x)
        for x in (evidence / "plan-probes.jsonl").read_text().splitlines()
        if json.loads(x)["kind"] == "claude_probe"
    ]
    assert probe_row["claude_config_dir"] == account
    # The pass A judge ledger reading counts for this account only.
    assert probe.latest_claude_window(evidence, account) is not None
    assert probe.latest_claude_window(evidence, str(other)) is None


def test_l_claude_judge_executor_fails_closed_without_the_account(
    bench: dict[str, ModuleType], tmp_path: Path
) -> None:
    judge = bench["judge"]
    probe = bench["probe"]
    seen: list[Any] = []
    parent = {"PATH": "/usr/bin", "HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": "/parent"}
    unchosen = judge.CliExecutor(
        judge.JUDGES["A"],
        tmp_path / "logs",
        process=_fake_process(bench, _claude_judge_stream(), seen),
        parent_env=parent,
    )
    with pytest.raises(judge.JudgeInfraError, match="no Claude account chosen"):
        unchosen("PROMPT")
    # A gate reading another account than the call's is refused too (one chosen value).
    gate = probe.PlanGate()
    gate.configure(probe.claude_probe_env(parent, tmp_path / "other"), None, None)
    mismatched = judge.CliExecutor(
        judge.JUDGES["A"],
        tmp_path / "logs2",
        process=_fake_process(bench, _claude_judge_stream(), seen),
        parent_env=parent,
        gate=gate,
        claude_config_dir=tmp_path,
    )
    with pytest.raises(judge.JudgeInfraError, match="gate reads Claude account"):
        mismatched("PROMPT")
    assert seen == []


def test_l_codex_only_commands_need_no_account_and_stay_unchanged(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = bench["cli"]
    runner = bench["runner"]
    probe = bench["probe"]
    model = bench["model"]
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))

    def no_claude(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a Codex only command made a Claude probe call")

    monkeypatch.setattr(probe, "claude_probe_call", no_claude)
    qpath = tmp_path / "q.json"
    qpath.write_text(json.dumps(valid_set()))
    fake = FakeExecutor(["codex_rollout_web.jsonl"])
    real = runner.execute_plan
    seen_kw: list[dict[str, Any]] = []

    def fake_plan(specs: Any, root: Path, max_runs: int, concurrency: int, **kw: Any) -> Any:
        seen_kw.append(dict(kw))
        return real(
            specs,
            root,
            max_runs,
            concurrency,
            executor=fake,
            codex_auth=_auth(tmp_path),
            preflight=no_servers,
            **kw,
        )

    monkeypatch.setattr(runner, "execute_plan", fake_plan)
    argv = ["run", "--questions", str(qpath), "--agent", "codex", "--question", "LK01"]
    argv += ["--arms", "web", "--repetitions", "1", "--evidence-dir", str(tmp_path / "ev")]
    # Codex only: no flag needed, and a given one is ignored (even a bad path).
    assert cli.main([*argv, "--execute", "--max-runs", "1"]) == 0
    assert seen_kw[0]["claude_config_dir"] is None
    [request] = fake.calls
    assert _claude_keys(request.env) == set()
    [run_dir] = list((tmp_path / "ev" / "runs").iterdir())
    record = json.loads((run_dir / "record.json").read_text())
    fields = set(model.RunRecord.__dataclass_fields__) - {"claude_config_dir"}
    assert set(record) == fields  # no new key in a Codex record
    assert "claude_config_dir" not in json.loads((run_dir / "meta.json").read_text())
    command = json.loads((run_dir / "command.json").read_text())
    assert _claude_keys(command["env"]) == set()
    rows = [json.loads(x) for x in (tmp_path / "ev" / "plan-probes.jsonl").read_text().splitlines()]
    assert rows and all("claude_config_dir" not in r for r in rows)  # Codex closing probes
    bad = ["--claude-config-dir", str(tmp_path / "missing")]
    assert cli.main([*argv, "--execute", "--max-runs", "1", "--resume", *bad]) == 0


def test_l_old_or_other_account_readings_are_ignored(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = bench["probe"]
    runner = bench["runner"]
    chosen, _link, other = _account_dirs(tmp_path)
    evidence = tmp_path / "ev"
    fresh = GOOD_CLAUDE | {"observed_at": probe.stamp(probe.now_utc())}
    for name, tag in (("untagged", None), ("other", str(other))):
        run_dir = evidence / "runs" / name
        run_dir.mkdir(parents=True)
        rec: dict[str, Any] = {"agent": "claude_code", "started_at": "x", "plan_window": fresh}
        if tag is not None:
            rec["claude_config_dir"] = tag
        (run_dir / "record.json").write_text(json.dumps(rec))
    judge_rows = [
        {"cli": "claude_code", "meta": {"plan_window": fresh}},  # before 1.5.1: untagged
        {"cli": "claude_code", "meta": {"plan_window": fresh, "claude_config_dir": str(other)}},
    ]
    (evidence / "judge-ledger.jsonl").write_text("".join(json.dumps(r) + "\n" for r in judge_rows))
    probe_rows = [
        {"kind": "claude_probe", "time": "t", "reading": fresh},
        {"kind": "claude_probe", "time": "t", "reading": fresh, "claude_config_dir": str(other)},
    ]
    (evidence / "plan-probes.jsonl").write_text("".join(json.dumps(r) + "\n" for r in probe_rows))
    assert probe.latest_claude_window(evidence, chosen) is None
    assert probe.latest_claude_window(evidence, None) is None
    assert probe.latest_claude_window(evidence, other) == fresh  # each account its own
    # The gate probes afresh for the chosen account despite the fresh foreign readings.
    calls: list[dict[str, str]] = []

    def fake_claude(env: dict[str, str], cwd: Path) -> list[str]:
        calls.append(dict(env))
        return claude_probe_stream()

    monkeypatch.setattr(probe, "claude_probe_call", fake_claude)
    fake = FakeExecutor(["claude_stream_both.jsonl"])
    records = runner.execute_plan(
        [_spec(bench, "claude_code", "both")],
        evidence,
        max_runs=1,
        executor=fake,
        parent_env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
        codex_auth=_auth(tmp_path),
        preflight=no_servers,
        claude_config_dir=chosen,
    )
    assert len(records) == 1 and len(calls) == 1
    assert calls[0]["CLAUDE_CONFIG_DIR"] == str(chosen)
    tagged = probe.latest_claude_window(evidence, chosen)
    assert tagged is not None and tagged["source"] == "claude_probe"


def _plan_probe(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: Callable[[dict[str, str], Path], list[str]],
    *extra: str,
) -> tuple[int, dict[str, Any], list[dict[str, Any]], int]:
    cli = bench["cli"]
    probe = bench["probe"]
    runner = bench["runner"]
    chosen = tmp_path / "claude-account-x"
    chosen.mkdir(exist_ok=True)
    calls: list[dict[str, str]] = []

    def fake_claude(env: dict[str, str], cwd: Path) -> list[str]:
        calls.append(dict(env))
        return answer(env, cwd)

    def no_call(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("plan-probe made an agent, judge or Archivist call")

    monkeypatch.setattr(probe, "claude_probe_call", fake_claude)
    monkeypatch.setattr(runner, "archivist_usage", no_call)
    monkeypatch.setattr(runner, "subprocess_executor", no_call)
    evidence = tmp_path / "ev"
    capsys.readouterr()
    rc = cli.main(
        ["plan-probe", "--claude-config-dir", str(chosen), "--evidence-dir", str(evidence), *extra]
    )
    out = json.loads(capsys.readouterr().out)
    rows = [json.loads(x) for x in (evidence / "plan-probes.jsonl").read_text().splitlines()]
    assert all(c["CLAUDE_CONFIG_DIR"] == str(chosen) for c in calls)
    assert all(_claude_keys(c) == ACCOUNT_KEYS for c in calls)
    return rc, out, rows, len(calls)


def test_l_plan_probe_admits_a_healthy_reading(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, out, rows, calls = _plan_probe(
        bench,
        tmp_path,
        monkeypatch,
        capsys,
        lambda _e, _c: claude_probe_stream(),
        "--stop-at-window",
        "70",
    )
    account = str((tmp_path / "claude-account-x").resolve())
    assert rc == 0 and calls == 1 and out["admitted"] is True and out["reasons"] == []
    assert out["claude_config_dir"] == account and out["stop_at_window"] == 70
    assert out["status"] == "allowed"
    assert out["windows"]["seven_day"] == {"utilization": 0.5, "resetsAt": 4102444800}
    assert out["overage_status"] == "rejected"
    assert out["overage_disabled_reason"] == "org_level_disabled"
    assert out["is_using_overage"] is False
    [row] = rows
    assert row["kind"] == "claude_probe" and row["ok"] is True
    assert row["claude_config_dir"] == account


@pytest.mark.parametrize(
    ("stream", "reason"),
    [
        (lambda: claude_probe_stream(utilization=0.7), "five_hour at 70%"),
        (lambda: claude_probe_stream(status="allowed_warning"), "'allowed_warning' is not allowed"),
        (lambda: claude_probe_stream(reason=None), "paid fallback is not ruled out"),
    ],
)
def test_l_plan_probe_refuses_at_the_threshold_without_a_second_probe(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stream: Callable[[], list[str]],
    reason: str,
) -> None:
    rc, out, rows, calls = _plan_probe(
        bench, tmp_path, monkeypatch, capsys, lambda _e, _c: stream(), "--stop-at-window", "70"
    )
    assert rc == 1 and calls == 1 and len(rows) == 1  # one probe, never a second one
    assert out["admitted"] is False and any(reason in r for r in out["reasons"]), out["reasons"]
    assert rows[0]["claude_config_dir"] == out["claude_config_dir"]


def test_l_plan_probe_failure_is_recorded_and_exits_1(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def broken(_e: dict[str, str], _c: Path) -> list[str]:
        raise OSError("claude not found")

    rc, out, rows, calls = _plan_probe(bench, tmp_path, monkeypatch, capsys, broken)
    assert rc == 1 and calls == 1 and out["admitted"] is False and out["ok"] is False
    assert "claude not found" in out["error"] and "claude not found" in out["reasons"][0]
    [row] = rows
    assert row["ok"] is False and "claude not found" in row["error"]
    assert row["claude_config_dir"] == out["claude_config_dir"]
    # An invalid threshold is an argument error.
    with pytest.raises(SystemExit) as info:
        bench["cli"].main(
            ["plan-probe", "--claude-config-dir", str(tmp_path), "--evidence-dir", str(tmp_path)]
            + ["--stop-at-window", "0"]
        )
    assert info.value.code == 2


def test_l_readme_documents_the_account_flag() -> None:
    text = HARNESS_README.read_text()
    _, _, section = text.partition("## Amendment 4: Claude Code replication")
    section = section.split("\n## ", 1)[0]
    assert "1.5.1" in section and "--claude-config-dir" in section
    assert "plan-probe --claude-config-dir" in " ".join(section.split())
    # The plan-probe step comes before the first K1 execution (RUNBOOK.md section 7).
    sequence = runbook_section("## 7. Claude Code replication (amendment 4)")
    assert sequence.index("plan-probe") < sequence.index('$K1 --evidence-dir "$EV" --execute')


def test_l_reconfiguring_the_gate_for_another_account_drops_its_reading(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = bench["probe"]
    runner = bench["runner"]
    chosen, _link, other = _account_dirs(tmp_path)
    budget = runner.Budget(3, stop_at_window=70)
    budget.gate.configure(probe.claude_probe_env({"PATH": "/usr/bin"}, other), None, None)
    # A fresh admissible reading of account Y sits in the supplied gate.
    budget.gate.observe_claude(GOOD_CLAUDE | {"observed_at": probe.stamp(probe.now_utc())})
    assert budget.gate.check_claude() == []
    calls: list[dict[str, str]] = []

    def hot(env: dict[str, str], cwd: Path) -> list[str]:
        calls.append(dict(env))
        return claude_probe_stream(utilization=0.9)

    monkeypatch.setattr(probe, "claude_probe_call", hot)
    fake = FakeExecutor(["claude_stream_both.jsonl"])
    records = runner.execute_plan(
        [_spec(bench, "claude_code", "both")],
        tmp_path / "ev",
        max_runs=3,
        executor=fake,
        parent_env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
        codex_auth=_auth(tmp_path),
        preflight=no_servers,
        budget=budget,
        claude_config_dir=chosen,
    )
    assert records == [] and fake.calls == [] and budget.stop_kind == "plan_gate"
    assert [c["CLAUDE_CONFIG_DIR"] for c in calls] == [str(chosen)]  # X probed, not Y's reading
    # The same account again keeps its reading.
    window = budget.gate.claude_window
    budget.gate.configure(probe.claude_probe_env({"PATH": "/usr/bin"}, chosen), None, None)
    assert budget.gate.claude_window is window


@pytest.mark.parametrize("record_account", ["chosen", "other"])
def test_l_newer_chosen_account_record_reading_controls_admission(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_account: str,
) -> None:
    from datetime import timedelta

    probe = bench["probe"]
    runner = bench["runner"]
    chosen, _link, other = _account_dirs(tmp_path)
    now = probe.now_utc()
    evidence = tmp_path / "ev"
    # An older healthy probe row of the chosen account ...
    healthy = GOOD_CLAUDE | {
        "observed_at": probe.stamp(now - timedelta(minutes=10)),
        "source": "claude_probe",
    }
    evidence.mkdir()
    (evidence / "plan-probes.jsonl").write_text(
        json.dumps(
            {
                "kind": "claude_probe",
                "time": "t",
                "reading": healthy,
                "claude_config_dir": str(chosen),
            }
        )
        + "\n"
    )
    # ... and a newer agent record reading above the threshold.
    hot = GOOD_CLAUDE | {
        "windows": {"five_hour": 0.95, "seven_day": 0.6},
        "observed_at": probe.stamp(now - timedelta(minutes=5)),
    }
    run_dir = evidence / "runs" / "earlier"
    run_dir.mkdir(parents=True)
    tag = str(chosen) if record_account == "chosen" else str(other)
    (run_dir / "record.json").write_text(
        json.dumps(
            {
                "agent": "claude_code",
                "started_at": "x",
                "plan_window": hot,
                "claude_config_dir": tag,
            }
        )
    )

    def no_probe(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a fresh reading of the chosen account was available")

    monkeypatch.setattr(probe, "claude_probe_call", no_probe)
    expected = hot if record_account == "chosen" else healthy
    assert probe.latest_claude_window(evidence, chosen) == expected
    budget = runner.Budget(3)
    fake = FakeExecutor(["claude_stream_both.jsonl"])
    records = runner.execute_plan(
        [_spec(bench, "claude_code", "both")],
        evidence,
        max_runs=3,
        executor=fake,
        parent_env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
        codex_auth=_auth(tmp_path),
        preflight=no_servers,
        budget=budget,
        claude_config_dir=chosen,
    )
    if record_account == "chosen":
        assert records == [] and fake.calls == [] and budget.stop_kind == "plan_gate"
        assert "five_hour at 95%" in budget.stops[0][2]
    else:  # another account's newer record is ignored: the healthy reading admits
        assert len(records) == 1 and len(fake.calls) == 1 and budget.stopped_by is None


# --- Amendment 5 (harness 1.6.0): key corrections, the pass B re-judge and the light audit ------


def _a5_corrections(path: Path, *items: dict[str, Any]) -> Path:
    path.write_text(json.dumps({"amendment": 5, "corrections": list(items)}))
    return path


def _a5_main_fix(**fields: Any) -> dict[str, Any]:
    """A main set correction of LK01.f1 (the default: a new statement and accepted spellings)."""
    return {
        "set": "main",
        "question_id": "LK01",
        "fact_id": "LK01.f1",
        "fields": fields or {"statement": "net sales FY2024, as filed", "accept": ["391035"]},
        "reason": "the filing states both figures",
    }


def _a5_contamination_fix(question_id: str = "CN01", fact_id: str = "CN01.f1") -> dict[str, Any]:
    chunk = _uuid(77, 5)
    filing = _uuid(1, 901)
    return {
        "set": "contamination",
        "question_id": question_id,
        "fact_id": fact_id,
        "fields": {"statement": "net sales, split removed"},
        "also_stated": [
            {
                "chunk_id": chunk,
                "permalink": (
                    f"https://mosaic-finance.com/filings/{filing}/p/{chunk}/k1.{'B' * 43}/"
                ),
                "quote": "Net sales were 391,035",
            }
        ],
    }


def test_a5_key_corrections_validation_errors(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    q = bench["questions"]
    cli = bench["cli"]
    data = valid_set()
    before = copy.deepcopy(data)
    cases = (
        (dict(_a5_main_fix(), question_id="ZZ99"), "unknown question ZZ99"),
        (dict(_a5_main_fix(), fact_id="LK01.f9"), "unknown fact LK01.f9"),
        (_a5_main_fix(kind="absence"), "field 'kind' cannot be corrected"),
        (_a5_main_fix(formula="x"), "field 'formula' cannot be corrected"),
        (_a5_main_fix(accept="391035"), "accept must be a list"),
        (dict(_a5_main_fix(), set="other"), "set must be one of main, contamination"),
    )
    qpath = tmp_path / "q.json"
    qpath.write_text(json.dumps(data))
    for item, message in cases:
        with pytest.raises(q.CorrectionError, match=re.escape(message)):
            q.apply_key_corrections(data, {"corrections": [item]})
        path = _a5_corrections(tmp_path / "bad.json", item)
        capsys.readouterr()
        with pytest.raises(SystemExit) as info:
            cli.main(
                ["validate-questions", "--questions", str(qpath), "--key-corrections", str(path)]
            )
        assert info.value.code == 2
        captured = capsys.readouterr()
        assert message in captured.err and "key corrections:" not in captured.out
    # A duplicate fact (also with an otherwise valid correction) applies nothing.
    doc = {"corrections": [_a5_main_fix(), _a5_main_fix(statement="again")]}
    with pytest.raises(q.CorrectionError, match="duplicate correction of LK01.f1"):
        q.apply_key_corrections(data, doc)
    # A malformed also_stated entry is refused too, whatever the loaded set.
    bad_also = _a5_contamination_fix()
    bad_also["also_stated"][0]["permalink"] = "https://example.com/x"
    with pytest.raises(q.CorrectionError, match="not a Mosaic passage permalink"):
        q.key_corrections_for(data, {"corrections": [bad_also]})
    with pytest.raises(q.CorrectionError, match="corrections"):
        q.load_key_corrections(qpath)  # a questions file is not a corrections file
    assert data == before  # nothing was applied to the loaded set


def test_a5_key_corrections_apply_only_to_their_set(
    bench: dict[str, ModuleType], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    q = bench["questions"]
    cli = bench["cli"]
    main = valid_set()
    cont = contamination_set()
    # The contamination correction names a question the main set lacks: no error for main.
    doc = {
        "corrections": [
            _a5_main_fix(value="391,000", unit="USD m"),
            _a5_contamination_fix("CN02", "CN02.f1"),
        ]
    }
    assert q.question_set(main) == "main" and q.question_set(cont) == "contamination"
    fixed = q.apply_key_corrections(main, doc)
    fact = fixed["questions"][0]["facts"][0]
    assert (fact["value"], fact["unit"], fact["statement"]) == ("391,000", "USD m", "net sales")
    assert fact["source"] == main["questions"][0]["facts"][0]["source"]  # other fields kept
    assert main["questions"][0]["facts"][0]["value"] == "391,035"  # the input is a copy
    assert fixed["questions"][1:] == main["questions"][1:]
    assert [c["fact_id"] for c in q.key_corrections_for(main, doc)] == ["LK01.f1"]
    fixed_cont = q.apply_key_corrections(cont, doc)
    assert [c["fact_id"] for c in q.key_corrections_for(cont, doc)] == ["CN02.f1"]
    assert fixed_cont["questions"][1]["facts"][0]["statement"] == "net sales, split removed"
    assert fixed_cont["questions"][0] == cont["questions"][0]
    # The CLI: validated with the corrections in place, a line naming what applied.
    qpath, cpath = tmp_path / "q.json", tmp_path / "c.json"
    qpath.write_text(json.dumps(main))
    cpath.write_text(json.dumps(cont))
    kpath = tmp_path / "k.json"
    kpath.write_text(json.dumps(doc))
    capsys.readouterr()
    assert cli.main(["validate-questions", "--questions", str(qpath)]) == 0
    plain = capsys.readouterr().out
    assert "key corrections" not in plain  # the default output is unchanged
    assert (
        cli.main(["validate-questions", "--questions", str(qpath), "--key-corrections", str(kpath)])
        == 0
    )
    out = capsys.readouterr().out
    assert out.startswith("key corrections: 1 applied to the main set (LK01.f1)\n")
    assert out.split("\n", 1)[1] == plain
    cmd = ["validate-questions", "--contamination", "--questions", str(cpath)]
    assert cli.main([*cmd, "--key-corrections", str(kpath)]) == 0
    assert "1 applied to the contamination set (CN02.f1)" in capsys.readouterr().out
    # A correction that breaks a key rule is a violation of the corrected set.
    broken = {"corrections": [_a5_main_fix(source={"chunk_id": "x"})]}
    kpath.write_text(json.dumps(broken))
    assert (
        cli.main(["validate-questions", "--questions", str(qpath), "--key-corrections", str(kpath)])
        == 1
    )
    assert "VIOLATION LK01.f1: source.filing_uuid missing" in capsys.readouterr().out


def test_a5_verify_quotes_checks_also_stated_quotes(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    data = contamination_set(traps=1, controls=0)
    fact = data["questions"][0]["facts"][0]
    src = fact["source"]
    path = tmp_path / "q.json"
    path.write_text(json.dumps(data))
    fix = _a5_contamination_fix()
    also = fix["also_stated"][0]
    passages = {
        src["chunk_id"]: {
            "id": src["chunk_id"],
            "filing_id": src["filing_uuid"],
            "url": src["permalink"],
            "exchange_document_id": src["exchange_document_id"],
            "snippet": "| Total net sales | 391,035 |",
        },
        also["chunk_id"]: {
            "id": also["chunk_id"],
            "filing_id": _uuid(1, 901),
            "url": also["permalink"],
            "text": "In 2024, net sales were 391,035 million.",
        },
    }
    fetched: list[str] = []

    def passage(chunk: str, env: dict[str, str]) -> dict[str, Any]:
        fetched.append(chunk)
        return {"passage": passages[chunk]}

    monkeypatch.setattr(cli, "fetch_passage", passage)
    out = tmp_path / "out"
    kpath = _a5_corrections(tmp_path / "k.json", fix)
    cmd = ["verify-quotes", "--questions", str(path), "--out", str(out)]
    assert cli.main(cmd) == 0 and fetched == [src["chunk_id"]]
    assert capsys.readouterr().out == "1 sourced facts checked, 0 violations\n"  # default kept
    assert cli.main([*cmd, "--key-corrections", str(kpath)]) == 0
    assert fetched == [src["chunk_id"], also["chunk_id"]]  # one read per new chunk
    out_text = capsys.readouterr().out
    assert "1 sourced facts checked, 1 also stated quotes checked, 0 violations" in out_text
    usage = (out / "archivist-usage.jsonl").read_text().splitlines()
    assert len(usage) == 2  # one quota reading before each Archivist read
    also["quote"] = "Net sales were 999"
    kpath = _a5_corrections(tmp_path / "k.json", fix)
    assert cli.main([*cmd, "--key-corrections", str(kpath), "--offline"]) == 1
    assert "also stated quote not found in chunk" in capsys.readouterr().out


def _a5_judged_b(
    bench: dict[str, ModuleType], tmp_path: Path, n: int = 3, f1: str = "incorrect"
) -> tuple[Path, Path, dict[str, Any]]:
    """``n`` LK01 runs judged by pass B alone (F1 ``f1``, the rest correct)."""
    judge = bench["judge"]
    evidence, qpath = _judge_setup(bench, tmp_path, n)
    data = json.loads(qpath.read_text())
    question = data["questions"][0]
    for i in range(n):
        run_id = f"r{i}"
        (evidence / "runs" / run_id / "answer.md").write_text(
            f"Net sales were 391,035 million ({run_id}), see {UPSTREAM_URL}"
        )
        result = judge.judge(
            question,
            "x",
            run_id,
            {"B": lambda _p: _verdicts_for(judge, question, f1)},
            passes=("B",),
        )
        (evidence / "runs" / run_id / "judgement.json").write_text(
            json.dumps(dict(result.as_dict(), batches=1))
        )
    (evidence / "ledger.jsonl").write_text("")  # a campaign directory
    return evidence, qpath, data


def _a5_verdict_reply(
    f1: str, f2: str = "correct", f3: str = "missing", complete: bool = True
) -> str:
    return json.dumps(
        {
            "facts": [
                {"id": "F1", "verdict": f1},
                {"id": "F2", "verdict": f2},
                {"id": "F3", "verdict": f3},
            ],
            "complete": complete,
        }
    )


def test_a5_rejudge_prompt_takes_only_corrected_facts_and_resumes(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    g = bench["grounding"]
    judge = bench["judge"]
    q = bench["questions"]
    evidence, qpath, data = _a5_judged_b(bench, tmp_path)
    # r1 is not eligible: its judgement has no valid pass B.
    (evidence / "runs" / "r1" / "judgement.json").write_text(
        json.dumps({"run_id": "r1", "status": "judge_error", "batches": 2})
    )
    kpath = _a5_corrections(tmp_path / "k.json", _a5_main_fix(), _a5_contamination_fix())
    graders: list[ScriptedGrader] = []

    def make_executor(log_dir: Path, stop: float, step: float) -> ScriptedGrader:
        graders.append(ScriptedGrader(lambda _p: _a5_verdict_reply("correct")))
        return graders[-1]

    monkeypatch.setattr(g, "make_executor", make_executor)
    out = tmp_path / "a5"
    base = ["rejudge-keys", "--questions", str(qpath), "--key-corrections", str(kpath)]
    base += ["--evidence-dir", str(evidence), "--out", str(out)]
    before = _snapshot(evidence)
    capsys.readouterr()
    assert cli.main(base) == 0  # dry run: lists the runs, no executor, no call
    dry = capsys.readouterr().out
    assert "3 eligible" not in dry and "2 eligible answers: 0 re-judged, 2 to re-judge" in dry
    assert "  r0 (LK01: LK01.f1)" in dry and "  r2 (LK01: LK01.f1)" in dry and "r1 (" not in dry
    assert graders == [] and not out.exists()
    assert cli.main([*base, "--execute", "--max-calls", "4"]) == 0
    assert _snapshot(evidence) == before  # the evidence stays read only
    corrected = q.apply_key_corrections(data, json.loads(kpath.read_text()))["questions"][0]
    prompts = [p for grader in graders for p, _schema in grader.calls]
    assert len(prompts) == 2 and all(
        s == judge.VERDICT_SCHEMA for gr in graders for _, s in gr.calls
    )
    for run_id, prompt in zip(("r0", "r2"), prompts, strict=True):
        answer = (evidence / "runs" / run_id / "answer.md").read_text()
        assert prompt == judge.build_prompt(corrected, answer, run_id)
        original = judge.build_prompt(data["questions"][0], answer, run_id)
        changed = [
            (a, b)
            for a, b in zip(original.splitlines(), prompt.splitlines(), strict=True)
            if a != b
        ]
        assert len(changed) == 1 and "as filed" in changed[0][1] and "391035" in changed[0][1]
        assert FILING_HOST not in prompt  # the answer is blinded as in the stored judgement
    record = json.loads((out / "key-rejudge" / "r0.json").read_text())
    assert record["status"] == "ok" and record["batches"] == 1
    assert record["taken"] == {"LK01.f1": "correct"}
    assert record["stored"] == {"LK01.f1": "incorrect", "LK01.f2": "correct", "LK01.f3": "correct"}
    assert record["verdicts"]["LK01.f3"] == "missing"  # recorded, never taken
    agreement = record["uncorrected_agreement"]
    assert (agreement["facts"], agreement["agreed"], agreement["share"]) == (2, 1, 0.5)
    assert record["corrected_facts"] == ["LK01.f1"]
    assert record["prompt_sha256"] == _a5_sha256(prompts[0])
    assert record["corrections_sha256"] == q.corrections_digest(
        q.key_corrections_for(data, json.loads(kpath.read_text()))
    )
    assert record["harness_pass_config"]["model"] == "gpt-6.1-sol"
    ledger = [json.loads(x) for x in (out / "grader-ledger.jsonl").read_text().splitlines()]
    assert [r["status"] for r in ledger] == ["started", "ok", "started", "ok"]
    assert {r["pass"] for r in ledger} == {"B"}
    # Resume: ok records are skipped, nothing is called.
    capsys.readouterr()
    assert cli.main([*base, "--execute", "--max-calls", "4"]) == 0
    assert "2 re-judged, 0 to re-judge" in capsys.readouterr().out
    assert sum(len(gr.calls) for gr in graders) == 2
    # A changed correction refuses the records made under the earlier one.
    _a5_corrections(kpath, _a5_main_fix(statement="another statement"))
    assert cli.main(base) == 1
    assert "other key corrections" in capsys.readouterr().err
    # The write guard: --out never inside the evidence or a campaign directory.
    _a5_corrections(kpath, _a5_main_fix())
    for bad in (evidence / "a5", evidence):
        with pytest.raises(SystemExit, match="read only input|campaign evidence"):
            cli.main([*base[:-1], str(bad)])


def _a5_sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_a5_rejudge_failure_path_and_export(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    g = bench["grounding"]
    report = bench["report"]
    evidence, qpath, _data = _a5_judged_b(bench, tmp_path)
    kpath = _a5_corrections(tmp_path / "k.json", _a5_main_fix())
    replies = {
        "r0": "not json",
        "r1": _a5_verdict_reply("correct"),
        "r2": _a5_verdict_reply("incorrect", f3="correct"),
    }

    def answer(prompt: str) -> str:
        run_id = re.search(r"\((r\d)\)", prompt)
        assert run_id is not None
        return replies[run_id.group(1)]

    monkeypatch.setattr(g, "make_executor", lambda *_a: ScriptedGrader(answer))
    out = tmp_path / "a5"
    rescore = tmp_path / "results" / "key-rescore-amendment-5.csv"
    base = ["rejudge-keys", "--questions", str(qpath), "--key-corrections", str(kpath)]
    base += ["--evidence-dir", str(evidence), "--out", str(out)]
    capsys.readouterr()
    assert cli.main([*base, "--export", str(rescore)]) == 1  # nothing re-judged yet
    assert "3 eligible runs not yet re-judged" in capsys.readouterr().err
    assert not rescore.exists()
    assert cli.main([*base, "--execute", "--max-calls", "10"]) == 0
    first = json.loads((out / "key-rejudge" / "r0.json").read_text())
    assert (first["status"], first["batches"], len(first["calls"])) == ("grade_error", 1, 2)
    assert first["error"].startswith("malformed")
    assert cli.main([*base, "--export", str(rescore)]) == 1  # r0 gets one later batch first
    assert "not yet re-judged" in capsys.readouterr().err and not rescore.exists()
    assert cli.main([*base, "--execute", "--max-calls", "10"]) == 0
    assert "failed after two batches, stored verdict stays: r0" in capsys.readouterr().err
    assert json.loads((out / "key-rejudge" / "r0.json").read_text())["batches"] == 2
    assert cli.main([*base, "--execute", "--max-calls", "10"]) == 0  # nothing left to call
    captured = capsys.readouterr()
    assert "0 to re-judge, 1 failed after two batches" in captured.out
    assert cli.main([*base, "--export", str(rescore)]) == 0
    err = capsys.readouterr().err
    assert "not exported (failed after two batches, stored verdict stays): r0 LK01.f1" in err
    assert "2 corrected facts exported" in err and "1 changed, 1 unchanged" in err
    assert "3 of 4 agree (0.7500)" in err
    with rescore.open(newline="") as fh:
        reader = csv.DictReader(fh)
        assert tuple(reader.fieldnames or ()) == report.RESCORE_COLUMNS
        rows = {r["run_id"]: r for r in reader}
    assert sorted(rows) == ["r1", "r2"]
    r1 = rows["r1"]
    assert (r1["correction_source"], r1["primary_judge"], r1["selection"]) == (
        "key_correction",
        "B",
        "key_correction",
    )
    assert (r1["judge_verdict"], r1["audit_verdict"], r1["judge_B_verdict"]) == (
        "incorrect",
        "correct",
        "incorrect",
    )
    assert "amendment 5" in r1["audit_note"] and "LK01.f1" in r1["audit_note"]
    assert r1["statement"] == "net sales FY2024, as filed" and r1["accepted"] == "391035"
    assert r1["permalink"].startswith("https://mosaic-finance.com/filings/")
    assert rows["r2"]["audit_verdict"] == "incorrect"
    assert report.check_files([rescore]) == []
    assert report.check_audit_rule([rescore], "B") is None


def test_a5_owner_rows_win_over_key_corrections_and_report_accepts_the_rescore(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    g = bench["grounding"]
    judge = bench["judge"]
    report = bench["report"]
    evidence, qpath, data = _report_campaign(bench, tmp_path)
    (evidence / "ledger.jsonl").touch()
    lookup = {q["id"]: q for q in data["questions"]}
    runs: dict[tuple[str, str], str] = {}
    for record in sorted((evidence / "runs").glob("*/record.json")):
        rec = json.loads(record.read_text())
        runs[(rec["question_id"], rec["arm"])] = rec["run_id"]
        question = lookup[rec["question_id"]]
        result = judge.judge(
            question,
            "answer",
            rec["run_id"],
            {"B": lambda _p, q=question: _verdicts_for(judge, q, "incorrect")},
            passes=("B",),
        )
        (record.parent / "judgement.json").write_text(json.dumps(dict(result.as_dict(), batches=1)))
    kpath = _a5_corrections(tmp_path / "k.json", _a5_main_fix())
    monkeypatch.setattr(
        g, "make_executor", lambda *_a: ScriptedGrader(lambda _p: _a5_verdict_reply("correct"))
    )
    results = tmp_path / "results"
    rescore = results / "key-rescore-amendment-5.csv"
    common = ["--questions", str(qpath), "--evidence-dir", str(evidence)]
    rk = ["rejudge-keys", *common, "--key-corrections", str(kpath), "--out", str(tmp_path / "a5")]
    assert cli.main([*rk, "--execute", "--max-calls", "4"]) == 0
    assert cli.main([*rk, "--export", str(rescore)]) == 0
    web, arch = runs[("LK01", "web")], runs[("LK01", "archivist")]
    owner = tmp_path / "owner.csv"
    owner.write_text(
        "run_id,fact_id,judge_verdict,primary_judge,audit_verdict,audit_note\n"
        f"{web},LK01.f1,incorrect,B,missing,owner ruling\n"
    )
    analyze = ["analyze", *common, "--resamples", "20", "--primary-judge", "B"]
    capsys.readouterr()
    assert cli.main(analyze) == 0
    judge_only = json.loads(capsys.readouterr().out)
    assert "superseded_corrections" not in judge_only  # no --audit: no new key
    audit = ["--audit", str(owner), str(rescore)]
    assert cli.main([*analyze, *audit]) == 0
    text = capsys.readouterr().out
    audited = json.loads(text)
    applied = {(c["run_id"], c["fact_id"]): c for c in audited["audit_corrections"]}
    assert set(applied) == {(web, "LK01.f1"), (arch, "LK01.f1")}
    assert (applied[(web, "LK01.f1")]["source"], applied[(web, "LK01.f1")]["audit_verdict"]) == (
        "owner",
        "missing",
    )
    assert applied[(arch, "LK01.f1")]["source"] == "key_correction"
    assert applied[(arch, "LK01.f1")]["audit_verdict"] == "correct"
    [superseded] = audited["superseded_corrections"]
    assert (superseded["run_id"], superseded["source"], superseded["audit_verdict"]) == (
        web,
        "key_correction",
        "correct",
    )
    assert superseded["owner_verdict"] == "missing" and superseded["superseded_by"] == "owner"
    assert [d["name"] for d in audited["audit_files"]] == ["owner.csv", rescore.name]
    rows = {
        r["run_id"]: r
        for r in cli.analysis_rows(
            evidence, data, report.corrections_by_run(audited["audit_corrections"]), "B"
        )
    }
    assert rows[arch]["accuracy"] == 1.0  # the corrected F1 now correct
    assert rows[web]["accuracy"] == pytest.approx(0.666667)  # the human audit's missing wins
    analysis = tmp_path / "analysis.json"
    analysis.write_text(text)
    rep = ["report", *common, "--analysis", str(analysis), "--out", str(results), *audit]
    assert cli.main(rep) == 0
    assert report.check_files([p for p in results.rglob("*") if p.is_file()]) == []
    assert rescore.is_file() and (results / "analysis.json").is_file()
    assert cli.main(rep[:-1]) == 1  # the rescore file must be passed as analyze applied it
    assert "not the ones this analysis applied" in capsys.readouterr().err
    # Conflicting rows of one source are refused as before; an unknown source too.
    conflict = tmp_path / "conflict.csv"
    rescore_text = rescore.read_text()
    conflict.write_text(
        rescore_text + rescore_text.splitlines()[1].replace(",correct,", ",missing,") + "\n"
    )
    capsys.readouterr()
    assert cli.main([*analyze, "--audit", str(conflict)]) == 1
    assert "conflicting audit verdicts" in capsys.readouterr().err
    odd = tmp_path / "odd.csv"
    odd.write_text(
        "run_id,fact_id,primary_judge,audit_verdict,correction_source\n"
        f"{web},LK01.f1,B,correct,robot\n"
    )
    assert cli.main([*analyze, "--audit", str(odd)]) == 1
    assert "invalid correction_source 'robot'" in capsys.readouterr().err


def _a5_contamination_campaign(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[str], list[str], Path, Path, dict[str, str]]:
    """The grounding campaign style contamination campaign of ``test_g_contamination_analysis``
    (CN01, three arms, pass B all correct, traps judged) plus the main campaign's sources."""
    cli = bench["cli"]
    g = bench["grounding"]
    judge = bench["judge"]
    evidence, qpath, out = _g_campaign(bench, tmp_path)
    cev = tmp_path / "cev"
    data = contamination_set()
    cq = tmp_path / "contamination-questions.json"
    cq.write_text(json.dumps(data))
    question = data["questions"][0]
    run_ids: dict[str, str] = {}
    for arm in ("web", "archivist", "both"):
        run_id = f"20261008T00000{len(arm)}Z-codex.{arm}.CN01.r1-a1"
        run_ids[arm] = run_id
        run_dir = cev / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "answer.md").write_text(f"Apple's net sales were 391,035 million ({arm}).")
        record = {
            "run_id": run_id,
            "run_key": f"codex.{arm}.CN01.r1",
            "agent": "codex",
            "arm": arm,
            "question_id": "CN01",
            "stratum": "contamination",
            "repetition": 1,
            "attempt": 1,
            "status": "completed",
            "contaminated": False,
            "usage": {"total": 1000},
        }
        (run_dir / "record.json").write_text(json.dumps(record))
        meta = {"run_id": run_id, "arm": arm, "stratum": "contamination", "question_id": "CN01"}
        g.write_json(
            out / "sources" / f"{run_id}.json",
            meta | {"repetition": 1, "sources": [], "complete": True},
        )
        g.write_json(
            out / "claims" / f"{run_id}.json",
            meta | {"status": "ok", "claims": [], "token_map": {}},
        )
        labels = judge.fact_labels(question)
        verdicts = {f: "correct" for f in labels}
        (run_dir / "judgement.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "status": "ok",
                    "rule": "B",
                    "passes": {"B": {"fact_verdicts": verdicts, "complete": True, "error": None}},
                }
            )
        )
    monkeypatch.setattr(g, "make_executor", lambda *_a: ScriptedGrader(_fake_graders))
    t = ["trap-judge", "--evidence-dir", str(cev), "--out", str(out), "--questions", str(cq)]
    assert cli.main([*t, "--execute", "--max-calls", "6"]) == 0
    ev = ["--evidence-dir", str(evidence), "--out", str(out)]
    assert cli.main(["grounding-sources", *ev, "--questions", str(qpath)]) == 0
    con = ["--contamination-evidence-dir", str(cev), "--contamination-questions", str(cq)]
    return ev, con, out, cev, run_ids


def test_a5_contamination_audit_changes_accuracy_and_default_is_unchanged(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    ev, con, out, _cev, run_ids = _a5_contamination_campaign(bench, tmp_path, monkeypatch)
    analyze = ["grounding-analyze", *ev, *con, "--resamples", "20"]
    assert cli.main(analyze) == 0
    plain_bytes = (out / "analysis-grounding.json").read_bytes()
    plain = json.loads(plain_bytes)
    section = plain["contamination"]
    assert section["per_arm"]["web"]["mean_run_accuracy"] == 1.0
    for key in ("audit_files", "audit_corrections", "superseded_corrections"):
        assert key not in section  # without the flag: the 1.5.1 section
    assert cli.main(analyze) == 0
    assert (out / "analysis-grounding.json").read_bytes() == plain_bytes  # deterministic
    audit = tmp_path / "contamination-rescore.csv"
    audit.write_text(
        "run_id,fact_id,judge_verdict,primary_judge,audit_verdict,correction_source\n"
        f"{run_ids['web']},CN01.f1,correct,B,incorrect,key_correction\n"
    )
    flag = ["--contamination-audit", str(audit)]
    assert cli.main([*analyze, *flag]) == 0
    audited = json.loads((out / "analysis-grounding.json").read_text())
    cs = audited["contamination"]
    assert cs["per_arm"]["web"]["mean_run_accuracy"] == 0.0
    assert cs["per_arm"]["both"]["mean_run_accuracy"] == 1.0
    assert cs["audit_files"] == [{"name": audit.name, "sha256": bench["report"].sha256_file(audit)}]
    [correction] = cs["audit_corrections"]
    assert (correction["run_id"], correction["source"], correction["audit_verdict"]) == (
        run_ids["web"],
        "key_correction",
        "incorrect",
    )
    assert cs["superseded_corrections"] == []
    assert audited["inputs_fingerprint"] != plain["inputs_fingerprint"]
    results = tmp_path / "results"
    report_cmd = ["grounding-report", *ev, *con, "--results", str(results)]
    capsys.readouterr()
    assert cli.main(report_cmd) == 1  # the analysis applied an audit file: pass it
    assert "inputs changed" in capsys.readouterr().err
    assert cli.main([*report_cmd, *flag]) == 0
    audit.write_text(audit.read_text().replace(",incorrect,", ",missing,"))
    assert cli.main([*report_cmd, *flag]) == 1  # changed bytes refuse the report
    bad = tmp_path / "bad.csv"
    bad.write_text("run_id,fact_id,primary_judge,audit_verdict\nnope,CN01.f1,B,correct\n")
    assert cli.main([*analyze, "--contamination-audit", str(bad)]) == 1
    assert "unknown run 'nope'" in capsys.readouterr().err
    with pytest.raises(SystemExit) as info:
        cli.main([*analyze, "--contamination-audit", str(tmp_path / "missing.csv")])
    assert info.value.code == 1
    with pytest.raises(SystemExit, match="needs --contamination-evidence-dir"):
        cli.main(["grounding-analyze", *ev, *flag])


def test_a5_pass_p_rubric_is_verbatim_and_the_parser_is_strict(
    bench: dict[str, ModuleType],
) -> None:
    precheck = bench["precheck"]
    prereg = (QUESTION_SET.parent / "preregistration.md").read_text()
    _, _, section = prereg.partition("### 16.3 The light audit route")
    block = section.split("```text\n", 1)[1].split("\n```", 1)[0]
    assert block == precheck.PASS_P_RUBRIC
    schema = precheck.PASS_P_SCHEMA
    assert schema["required"] == ["key_check", "verdict", "reason"]
    assert schema["additionalProperties"] is False
    ok = {"key_check": "supported", "verdict": "correct", "reason": "The passage states it."}
    assert precheck.parse_pass_p(json.dumps(ok), True) == ok
    absent = dict(ok, key_check="not_applicable")
    assert precheck.parse_pass_p(json.dumps(absent), False)["key_check"] == "not_applicable"
    for bad, given in (
        (dict(ok, extra=1), True),
        (dict(ok, verdict="disputed"), True),
        (dict(ok, key_check="maybe"), True),
        (dict(ok, reason=" "), True),
        ({"key_check": "supported", "verdict": "correct"}, True),
        (absent, True),  # not_applicable only when no passage is given
        (ok, False),
    ):
        with pytest.raises(ValueError):
            precheck.parse_pass_p(json.dumps(bad), given)
    with pytest.raises(ValueError):
        precheck.parse_pass_p("not json", True)
    assert precheck.agrees(ok, "correct") and precheck.agrees(absent, "correct")
    assert not precheck.agrees(ok, "incorrect")
    assert not precheck.agrees(dict(ok, key_check="contradicted"), "correct")
    assert not precheck.agrees(dict(ok, key_check="not_in_passage"), "correct")
    assert not precheck.agrees(None, "correct")


def _a5_precheck_setup(
    bench: dict[str, ModuleType], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, list[str], list[ScriptedGrader]]:
    """Six LK01 runs (pass B: F1 incorrect on r0, else correct), an audit queue with one human
    audit row and seven rows to pre check, a fake passage reader and a scripted pass P grader."""
    cli = bench["cli"]
    g = bench["grounding"]
    evidence, qpath = _judged_campaign(bench, tmp_path, n=6)
    (evidence / "ledger.jsonl").touch()
    for i in range(6):
        (evidence / "runs" / f"r{i}" / "answer.md").write_text(
            f"Net sales were 391,035 million [case r{i}] per [the 10-K]({UPSTREAM_URL})."
        )
    data = json.loads(qpath.read_text())
    src = data["questions"][0]["facts"][0]["source"]
    queue = tmp_path / "queue" / "audit-queue-001.csv"
    queue.parent.mkdir()
    rows = [
        ("disputed", "r0", "LK01.f1", "incorrect", "correct"),  # a person settled it
        ("sample", "r1", "LK01.f1", "correct", ""),
        ("sample", "r1", "LK01.f2", "correct", ""),
        ("sample", "r1", "LK01.f3", "correct", ""),
        ("sample", "r2", "LK01.f1", "correct", ""),
        ("sample", "r3", "LK01.f1", "correct", ""),
        ("sample", "r4", "LK01.f1", "correct", ""),
        ("sample", "r5", "LK01.f1", "correct", ""),
    ]
    queue.write_text(
        "selection,run_id,fact_id,judge_verdict,primary_judge,audit_verdict,audit_note\n"
        + "".join(f"{s},{r},{f},{j},B,{a},\n" for s, r, f, j, a in rows)
    )
    fetched: list[str] = []

    def passage(chunk: str, env: dict[str, str]) -> dict[str, Any]:
        fetched.append(chunk)
        assert chunk == src["chunk_id"]
        return {
            "passage": {
                "id": chunk,
                "filing_id": src["filing_uuid"],
                "url": src["permalink"],
                "text": f"Total net sales 391,035 (see {UPSTREAM_URL} and " + "sedar" + "plus.ca)",
            }
        }

    monkeypatch.setattr(cli, "fetch_passage", passage)
    graders: list[ScriptedGrader] = []

    def answer(prompt: str) -> str:
        case = re.search(r"\[case (r\d)\]", prompt)
        assert case is not None
        run = case.group(1)
        statement = re.search(r"^Statement: (.*)$", prompt, re.M)
        assert statement is not None
        given = "Filing passage:" in prompt or "Passage of" in prompt
        reply = {"key_check": "supported" if given else "not_applicable", "verdict": "correct"}
        if run == "r2":
            reply["verdict"] = "incorrect"  # the rescore says incorrect too: agrees
        if run == "r3":
            reply["key_check"] = "contradicted"
        if run == "r4":
            return "not json"
        if run == "r5":
            reply["verdict"] = "missing"
        return json.dumps(reply | {"reason": f"{statement.group(1)}: the passage states it."})

    def make_executor(log_dir: Path, stop: float, step: float) -> ScriptedGrader:
        graders.append(ScriptedGrader(answer))
        return graders[-1]

    monkeypatch.setattr(g, "make_executor", make_executor)
    rescore = tmp_path / "rescore.csv"
    rescore.write_text(
        "run_id,fact_id,primary_judge,audit_verdict,correction_source\n"
        "r2,LK01.f1,B,incorrect,key_correction\n"
    )
    kpath = _a5_corrections(tmp_path / "k.json", _a5_main_fix())
    base = ["audit-precheck", "--questions", str(qpath), "--evidence-dir", str(evidence)]
    base += ["--key-corrections", str(kpath)]
    base += ["--audit-queue", str(queue), "--key-rescore", str(rescore)]
    base += ["--out", str(tmp_path / "pc"), "--results", str(tmp_path / "results")]
    return evidence, queue, rescore, base, graders


def test_a5_precheck_agreement_owner_list_and_failures(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    report = bench["report"]
    redact = bench["redact"]
    evidence, _queue, _rescore, base, graders = _a5_precheck_setup(bench, tmp_path, monkeypatch)
    fetched = []
    original = cli.fetch_passage

    def counting(chunk: str, env: dict[str, str]) -> dict[str, Any]:
        fetched.append(chunk)
        return original(chunk, env)

    monkeypatch.setattr(cli, "fetch_passage", counting)
    before = _snapshot(evidence)
    results = tmp_path / "results"
    out = tmp_path / "pc"
    capsys.readouterr()
    assert cli.main(base) == 0  # dry run: counts only
    dry = capsys.readouterr().out
    assert "7 audit rows without a human audit verdict" in dry and "1 passages to read" in dry
    assert graders == [] and fetched == [] and not out.exists() and not results.exists()
    assert cli.main([*base, "--execute", "--max-calls", "20"]) == 1  # r4 needs a later batch
    chunk = json.loads(Path(base[2]).read_text())["questions"][0]["facts"][0]["source"]["chunk_id"]
    assert fetched == [chunk]  # one Archivist read, cached for every row that shows it
    assert "1 rows still need pass P" in capsys.readouterr().err and not results.exists()
    prompts = [p for gr in graders for p, schema in gr.calls]
    schemas = [schema for gr in graders for _p, schema in gr.calls]
    assert schemas and all(schema == bench["precheck"].PASS_P_SCHEMA for schema in schemas)
    for prompt in prompts:
        assert redact.find_upstream_hosts(prompt) == [] and "http" not in prompt
        assert "judge" not in prompt.split("Answer to check")[0].split("Ignore length")[1]
        assert "incorrect" not in prompt.split("Question:")[1].split("Answer to check")[0]
    absence = next(p for p in prompts if "Absent terms: TSMC" in p)
    assert "No passage is given" in absence and "Filing passage" not in absence
    computed = next(p for p in prompts if "Formula: LK01.f1 - LK01.f1" in p)
    assert "Passage of LK01.f1:" in computed and "Total net sales 391,035" in computed
    assert cli.main([*base, "--execute", "--max-calls", "20"]) == 0
    record = json.loads((out / "precheck" / "r4__LK01.f1.json").read_text())
    assert (record["status"], record["batches"]) == ("grade_error", 2)
    assert _snapshot(evidence) == before
    with (results / "audit-precheck.csv").open(newline="") as fh:
        table = {(r["run_id"], r["fact_id"]): r for r in csv.DictReader(fh)}
    assert len(table) == 7 and ("r0", "LK01.f1") not in table  # the human audit row is left alone
    assert table[("r1", "LK01.f1")]["agree"] == "true"
    assert table[("r1", "LK01.f2")]["key_check"] == "not_applicable"
    assert table[("r1", "LK01.f2")]["agree"] == "true"
    assert table[("r1", "LK01.f3")]["agree"] == "true"
    r2 = table[("r2", "LK01.f1")]
    assert (r2["judge_verdict"], r2["judge_verdict_source"], r2["agree"]) == (
        "incorrect",
        "key_rejudge",
        "true",
    )
    assert table[("r3", "LK01.f1")]["agree"] == "false"  # the key is contradicted
    r4 = table[("r4", "LK01.f1")]
    assert (r4["status"], r4["reason"], r4["agree"]) == ("failed", "pre check failed", "false")
    r5 = table[("r5", "LK01.f1")]
    assert (r5["precheck_verdict"], r5["judge_verdict_source"], r5["agree"]) == (
        "missing",
        "stored_judge",
        "false",
    )
    owner = (results / "audit-owner-list.md").read_text()
    for run in ("r3", "r4", "r5"):
        assert f"| {run} LK01.f1 (sample) |" in owner
    for run in ("r1", "r2"):
        assert f"| {run} LK01.f1" not in owner
    assert (
        "pre check failed" in owner and "[permalink](https://mosaic-finance.com/filings/" in owner
    )
    assert "Total net sales 391,035" in owner  # the key quote
    assert report.check_files(list(results.iterdir())) == []
    # Only the agreed rows: no human review list (a stale one is removed).
    agreed_queue = tmp_path / "agreed.csv"
    lines = Path(base[base.index("--audit-queue") + 1]).read_text().splitlines()
    agreed_queue.write_text("\n".join(lines[:5]) + "\n")
    agreed = list(base)
    agreed[agreed.index("--audit-queue") + 1] = str(agreed_queue)
    calls = sum(len(gr.calls) for gr in graders)
    assert cli.main([*agreed, "--execute"]) == 0  # nothing left to call: results only
    assert sum(len(gr.calls) for gr in graders) == calls
    assert not (results / "audit-owner-list.md").exists()
    with (results / "audit-precheck.csv").open(newline="") as fh:
        assert {r["agree"] for r in csv.DictReader(fh)} == {"true"}


def test_a5_precheck_archivist_gate_stops_before_any_call(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    _evidence, _queue, _rescore, base, graders = _a5_precheck_setup(bench, tmp_path, monkeypatch)

    def at_ceiling(env: dict[str, str]) -> dict[str, Any]:
        return {"ok": True, "cli_this_month": 9800, "cli_limit": 10000}

    monkeypatch.setattr(bench["runner"], "archivist_usage", at_ceiling)
    fetched: list[str] = []
    monkeypatch.setattr(cli, "fetch_passage", lambda c, e: fetched.append(c))
    capsys.readouterr()
    assert cli.main([*base, "--execute", "--max-calls", "20"]) == 1
    assert "archivist quota gate" in capsys.readouterr().err
    assert graders == [] and fetched == []  # no model call, no Archivist read
    usage = (tmp_path / "pc" / "archivist-usage.jsonl").read_text().splitlines()
    assert json.loads(usage[0])["cli_this_month"] == 9800
    assert not (tmp_path / "results").exists()
    # Invalid inputs refuse before anything: an unknown fact in the queue.
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "run_id,fact_id,judge_verdict,primary_judge,audit_verdict\nr1,LK01.f9,correct,B,\n"
    )
    bad_cmd = list(base)
    bad_cmd[bad_cmd.index("--audit-queue") + 1] = str(bad)
    assert cli.main(bad_cmd) == 1
    assert "unknown fact 'LK01.f9'" in capsys.readouterr().err


def test_a5_readme_documents_amendment_5() -> None:
    text = HARNESS_README.read_text()
    assert text.splitlines()[0].endswith("1.6.0 for the light audit (amendment 5))")
    _, _, section = text.partition("## Amendment 5: key corrections and the light audit")
    section = section.split("\n## ", 1)[0]
    assert "(version 1.6.0)" in section.splitlines()[0]
    flat = " ".join(section.split())
    for needle in (
        "rejudge-keys",
        "--export",
        "--key-corrections",
        "correction_source",
        "superseded_corrections",
        "--contamination-audit",
        "audit-precheck",
        "--key-rescore",
        "audit-owner-list.md",
        "PASS_P_RUBRIC",
        "analysis-judge-only.json",
        "comparisons-judge-only.csv",
        "audit-export",
        "audit-queue-001.csv",
    ):
        assert needle in flat, needle
    # The command sequence lives in RUNBOOK.md section 8.
    heading = "## 8. Key corrections and the light audit (amendment 5)"
    sequence = " ".join(runbook_section(heading).split())
    for needle in (
        '--questions "$CQ" --key-corrections "$K" --out "$A5/verify"',
        "$RES9/contamination/key-rescore-amendment-5.csv",
    ):
        assert needle in sequence, needle


def test_a5_precheck_reuses_a_record_only_for_the_same_prompt(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    evidence, queue, _rescore, base, graders = _a5_precheck_setup(bench, tmp_path, monkeypatch)
    agreed = tmp_path / "agreed.csv"
    agreed.write_text("\n".join(queue.read_text().splitlines()[:5]) + "\n")  # r0 human, r1 x3
    plain = ["audit-precheck", *base[1:5]]  # --questions Q --evidence-dir EV
    plain += ["--audit-queue", str(agreed), "--out", str(tmp_path / "pc")]
    plain += ["--results", str(tmp_path / "results")]
    assert cli.main([*plain, "--execute", "--max-calls", "10"]) == 0  # no corrections
    out = tmp_path / "pc" / "precheck"
    first = {p.name: json.loads(p.read_text()) for p in out.glob("*.json")}
    assert len(first) == 3 and sum(len(g.calls) for g in graders) == 3
    # Amendment 5 applied: LK01.f1 (and LK01.f3, whose component it is) get a new prompt and
    # are pre checked again, counted afresh; the absence fact keeps its record.
    with_k = [*plain, "--key-corrections", base[base.index("--key-corrections") + 1]]
    capsys.readouterr()
    assert cli.main(with_k) == 0
    assert "1 pre checked or failed, 2 to pre check" in capsys.readouterr().out
    assert cli.main([*with_k, "--execute", "--max-calls", "10"]) == 0
    assert sum(len(g.calls) for g in graders) == 5
    second = {p.name: json.loads(p.read_text()) for p in out.glob("*.json")}
    assert second["r1__LK01.f2.json"] == first["r1__LK01.f2.json"]
    for name in ("r1__LK01.f1.json", "r1__LK01.f3.json"):
        assert second[name]["prompt_sha256"] != first[name]["prompt_sha256"]
        assert second[name]["batches"] == 1
    # A changed answer is a new prompt too.
    (evidence / "runs" / "r1" / "answer.md").write_text("Net sales were 391,035 [case r1].")
    capsys.readouterr()
    assert cli.main(with_k) == 0
    assert "0 pre checked or failed, 3 to pre check" in capsys.readouterr().out


def test_a5_precheck_refuses_queues_not_matching_the_stored_pass_b(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    _evidence, queue, _rescore, base, graders = _a5_precheck_setup(bench, tmp_path, monkeypatch)
    fetched: list[str] = []
    monkeypatch.setattr(cli, "fetch_passage", lambda c, e: fetched.append(c))
    text = queue.read_text()
    at = base.index("--audit-queue") + 1
    for name, content, message in (
        ("combined.csv", text.replace(",B,", ",combined,"), "exported under combined, not B"),
        (
            "nocolumn.csv",
            "run_id,fact_id,judge_verdict,audit_verdict\nr1,LK01.f1,correct,\n",
            "not B",
        ),
        (
            "edited.csv",
            text.replace("sample,r5,LK01.f1,correct", "sample,r5,LK01.f1,missing"),
            "judge_verdict 'missing' of r5 LK01.f1 is not the stored pass B verdict 'correct'",
        ),
    ):
        path = tmp_path / name
        path.write_text(content)
        cmd = list(base)
        cmd[at] = str(path)
        capsys.readouterr()
        assert cli.main([*cmd, "--execute", "--max-calls", "20"]) == 1
        assert message in capsys.readouterr().err
    assert fetched == [] and graders == []  # refused before any Archivist read or call


def test_a5_precheck_key_rescore_rows_must_be_corrected_facts(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    evidence, _queue, rescore, base, graders = _a5_precheck_setup(bench, tmp_path, monkeypatch)
    k_at = base.index("--key-corrections")
    capsys.readouterr()
    assert cli.main(base[:k_at] + base[k_at + 2 :]) == 2  # --key-rescore without corrections
    assert "--key-rescore needs --key-corrections" in capsys.readouterr().err
    (evidence / "runs" / "r3" / "judgement.json").write_text(
        json.dumps({"run_id": "r3", "status": "judge_error", "batches": 2})
    )
    header = "run_id,fact_id,primary_judge,audit_verdict,correction_source\n"
    for line, message in (
        ("r1,LK01.f2,B,correct,key_correction", "r1 LK01.f2 is not a corrected fact"),
        ("r1,LK01.f1,B,correct,", "correction_source '' is not key_correction"),
        ("r1,LK01.f1,B,correct,owner", "correction_source 'owner' is not key_correction"),
        ("r1,LK01.f1,combined,correct,key_correction", "primary_judge is not B"),
        ("r3,LK01.f1,B,correct,key_correction", "r3 has no valid stored pass B"),
        ("nope,LK01.f1,B,correct,key_correction", "nope LK01.f1 is not a corrected fact"),
    ):
        rescore.write_text(header + line + "\n")
        assert cli.main(base) == 1
        assert message in capsys.readouterr().err
    assert graders == []


def test_a5_rejudge_takes_a_claude_code_answer_with_a_valid_pass_b(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    g = bench["grounding"]
    evidence, qpath, _data = _a5_judged_b(bench, tmp_path, n=2)
    record = evidence / "runs" / "r1" / "record.json"
    rec = json.loads(record.read_text())
    record.write_text(
        json.dumps(rec | {"agent": "claude_code", "run_key": "claude_code.web.LK01.r1"})
    )
    monkeypatch.setattr(
        g, "make_executor", lambda *_a: ScriptedGrader(lambda _p: _a5_verdict_reply("correct"))
    )
    kpath = _a5_corrections(tmp_path / "k.json", _a5_main_fix())
    base = ["rejudge-keys", "--questions", str(qpath), "--key-corrections", str(kpath)]
    base += ["--evidence-dir", str(evidence), "--out", str(tmp_path / "a5")]
    capsys.readouterr()
    assert cli.main(base) == 0
    assert "  r1 (LK01: LK01.f1)" in capsys.readouterr().out  # eligible whatever the agent
    assert cli.main([*base, "--execute", "--max-calls", "4"]) == 0
    r1 = json.loads((tmp_path / "a5" / "key-rejudge" / "r1.json").read_text())
    assert (r1["status"], r1["agent"], r1["taken"]) == ("ok", "claude_code", {"LK01.f1": "correct"})
    rescore = tmp_path / "rescore.csv"
    assert cli.main([*base, "--export", str(rescore)]) == 0
    with rescore.open(newline="") as fh:
        agents = {r["run_id"]: r["agent"] for r in csv.DictReader(fh)}
    assert agents == {"r0": "codex", "r1": "claude_code"}


def test_a5_rejudge_complete_is_recorded_never_taken(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    g = bench["grounding"]
    report = bench["report"]
    evidence, qpath, data = _a5_judged_b(bench, tmp_path, n=1)
    before = _snapshot(evidence)
    monkeypatch.setattr(
        g,
        "make_executor",
        lambda *_a: ScriptedGrader(lambda _p: _a5_verdict_reply("correct", complete=False)),
    )
    kpath = _a5_corrections(tmp_path / "k.json", _a5_main_fix())
    base = ["rejudge-keys", "--questions", str(qpath), "--key-corrections", str(kpath)]
    base += ["--evidence-dir", str(evidence), "--out", str(tmp_path / "a5")]
    assert cli.main([*base, "--execute", "--max-calls", "2"]) == 0
    r0 = json.loads((tmp_path / "a5" / "key-rejudge" / "r0.json").read_text())
    assert (r0["stored_complete"], r0["complete_rejudged"]) == (True, False)
    assert _snapshot(evidence) == before  # the stored judgement (and its complete) stays
    rescore = tmp_path / "rescore.csv"
    assert cli.main([*base, "--export", str(rescore)]) == 0
    with rescore.open(newline="") as fh:
        [row] = list(csv.DictReader(fh))
    assert row["audit_verdict"] == "correct" and "complete" not in json.dumps(row).lower()
    capsys.readouterr()
    common = ["--questions", str(qpath), "--evidence-dir", str(evidence)]
    assert (
        cli.main(
            [
                "analyze",
                *common,
                "--resamples",
                "20",
                "--primary-judge",
                "B",
                "--audit",
                str(rescore),
            ]
        )
        == 0
    )
    audited = json.loads(capsys.readouterr().out)
    rows = cli.analysis_rows(
        evidence, data, report.corrections_by_run(audited["audit_corrections"]), "B"
    )
    assert rows[0]["completion"] == 1.0 and rows[0]["accuracy"] == 1.0


def test_a5_rejudge_launch_runs_the_plan_gate_and_the_closing_probe(
    bench: dict[str, ModuleType],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = bench["cli"]
    g = bench["grounding"]
    judge = bench["judge"]
    probe = bench["probe"]
    evidence, qpath, _data = _a5_judged_b(bench, tmp_path, n=1)
    auth = _auth(tmp_path)
    probes: list[str] = []
    readings: dict[str, Any] = {"report": copy.deepcopy(OK_PROBE)}
    seen: list[Any] = []
    built: list[Any] = []

    def plan_probe(env: dict[str, str], cwd: Path) -> dict[str, Any]:
        probes.append(str(cwd))
        return copy.deepcopy(readings["report"])

    def make_executor(log_dir: Path, stop: float, step: float) -> Any:
        ex = judge.CliExecutor(
            judge.JUDGES["B"],
            log_dir,
            process=_fake_process(
                bench, CODEX_JUDGE_STREAM, seen, last=_a5_verdict_reply("correct")
            ),
            parent_env={"PATH": "/usr/bin", "HOME": str(tmp_path)},
            codex_auth=auth,
            plan_probe=plan_probe,
        )
        built.append(ex)
        return ex

    monkeypatch.setattr(g, "make_executor", make_executor)
    kpath = _a5_corrections(tmp_path / "k.json", _a5_main_fix())
    out = tmp_path / "a5"
    base = ["rejudge-keys", "--questions", str(qpath), "--key-corrections", str(kpath)]
    base += ["--evidence-dir", str(evidence), "--out", str(out), "--execute", "--max-calls", "2"]
    # A blocking plan gate stops the batch before any call: no process, no record, exit 1.
    readings["report"] = _probe(primary={"usedPercent": 95, "windowDurationMins": 10080})
    capsys.readouterr()
    assert cli.main(base) == 1
    err = capsys.readouterr().err
    assert "batch stopped (plan probe refused the call" in err
    assert len(probes) == 1 and not [s for s in seen if not isinstance(s, str | bool)]
    assert built[0].dispatched == 0 and not (out / "key-rejudge" / "r0.json").exists()
    assert isinstance(built[0].gate, probe.PlanGate) and built[0].gate.allow_claude is False
    # A healthy plan: the gate check before the call, the call, then the closing probe.
    readings["report"] = copy.deepcopy(OK_PROBE)
    probes.clear()
    assert cli.main(base) == 0
    ex = built[-1]
    assert ex.dispatched == 1 and len(probes) == 2  # the pre launch check and the closing
    record = json.loads((out / "key-rejudge" / "r0.json").read_text())
    assert record["status"] == "ok" and record["taken"] == {"LK01.f1": "correct"}
    [closing] = [
        json.loads(x)
        for x in (out / "plan-probes.jsonl").read_text().splitlines()
        if json.loads(x)["kind"] == "codex_B_closing"
    ]
    assert closing["reasons"] == []
    assert list((out / "logs" / "B").glob("closing-*/closing-probe.json"))
    assert list((out / "logs" / "B" / "r0").glob("B-*/plan-probe.json"))
