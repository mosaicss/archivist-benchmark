"""Prices, per run cost and the codex campaign cost estimate.

Prices are USD per million tokens unless noted, read 2026-10-06:
OpenAI developers.openai.com/api/docs/pricing; Anthropic platform.claude.com pricing. The judges
run on the same plans as the agents (pre registration amendment 1), so no judge price is metered.
Plan allowance runs (ChatGPT login, Claude subscription) are not billed per token: their cost is
an API list price equivalent, labelled as such.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PRICES_READ = "2026-10-06"


@dataclass(frozen=True)
class TokenPrice:
    input: float
    cached_input: float
    cache_write: float
    output: float
    long_context_threshold: int | None = None
    long_context_multiplier: float = 1.0
    source: str = ""


PRICES: dict[str, TokenPrice] = {
    "gpt-6.1-sol": TokenPrice(
        input=2.00,
        cached_input=0.10,
        cache_write=2.50,
        output=10.00,
        long_context_threshold=272_000,
        long_context_multiplier=2.0,
        source="developers.openai.com/api/docs/pricing",
    ),
    "claude-opus-5-5": TokenPrice(
        input=4.00,
        cached_input=0.20,
        cache_write=5.00,
        output=20.00,
        source="platform.claude.com pricing (5 minute cache write)",
    ),
    "claude-sonnet-5-5": TokenPrice(
        input=2.00,
        cached_input=0.20,
        cache_write=2.50,
        output=10.00,
        source="platform.claude.com pricing (5 minute cache write)",
    ),
}
WEB_SEARCH_USD_PER_CALL = 10.00 / 1000  # OpenAI and Anthropic, both 10 USD per 1,000 calls


def price_for(model: str) -> TokenPrice | None:
    if model in PRICES:
        return PRICES[model]
    for name, price in PRICES.items():
        if model.startswith(name):
            return price
    return None


def _call_cost(price: TokenPrice, call: dict[str, Any]) -> float:
    """One model call priced from its own usage (Codex ``token_usage_record`` keys); the long
    context rate applies to the whole call only when its input exceeds the threshold."""
    inp = int(call.get("input_tokens") or 0)
    cached = int(call.get("cached_input_tokens") or 0)
    write = int(call.get("cache_write_input_tokens") or 0)
    out = int(call.get("output_tokens") or 0)
    cost = (
        (inp - cached - write) * price.input
        + cached * price.cached_input
        + write * price.cache_write
        + out * price.output
    ) / 1_000_000
    if price.long_context_threshold and inp > price.long_context_threshold:
        cost *= price.long_context_multiplier
    return cost


def token_cost(
    model: str,
    usage: dict[str, Any],
    per_call_usage: list[dict[str, Any]] | None = None,
) -> float | None:
    """API list price of one run's tokens. With per call usage each call is priced on its own
    (long context surcharge per call); otherwise the aggregate at the standard rate.
    ``uncached_input`` includes cache writes."""
    price = price_for(model)
    if price is None:
        return None
    if per_call_usage:
        return round(sum(_call_cost(price, call) for call in per_call_usage), 6)
    write = int(usage.get("cache_write_input") or 0)
    fresh = int(usage.get("uncached_input") or 0) - write
    cached = int(usage.get("cached_input") or 0)
    out = int(usage.get("output") or 0)
    cost = (
        fresh * price.input
        + cached * price.cached_input
        + write * price.cache_write
        + out * price.output
    ) / 1_000_000
    return round(cost, 6)


def web_search_calls(agent: str, web_actions: dict[str, int]) -> int:
    """Billable web searches: Codex ``search`` actions; Claude server searches (a WebSearch call
    can spend several), else its WebSearch calls."""
    if agent == "claude_code" and "server_searches" in web_actions:
        return int(web_actions["server_searches"])
    return int(web_actions.get("search", 0))


def run_cost(
    agent: str,
    model: str,
    usage: dict[str, Any] | None,
    web_actions: dict[str, int],
    per_call_usage: list[dict[str, Any]] | None,
    client_cost_usd: float | None,
    auth_mode: str,
) -> tuple[float | None, str]:
    """Cost of one run and its basis."""
    if usage is None:
        return None, "no usage"
    searches = web_search_calls(agent, web_actions)
    if agent == "claude_code" and client_cost_usd is not None:
        basis = "Claude Code total_cost_usd (client estimate" + (
            ", metered Console billing)" if auth_mode == "api_key" else "; plan allowance)"
        )
        return round(float(client_cost_usd), 6), basis
    tokens = token_cost(model, usage, per_call_usage)
    if tokens is None:
        return None, f"no price for {model}"
    total = tokens + searches * WEB_SEARCH_USD_PER_CALL
    if agent == "codex" and auth_mode == "chatgpt":
        basis = f"API list price equivalent ({PRICES_READ}); ChatGPT plan allowance, not billed"
    else:
        basis = f"API list price ({PRICES_READ})"
    return round(total, 6), basis


@dataclass(frozen=True)
class Profile:
    """Mean per run token profile of one agent and arm (from measured runs)."""

    uncached_input: float
    cached_input: float
    cache_write: float
    output: float
    web_searches: float = 0.0
    archivist_calls: float = 0.0

    def cost(self, price: TokenPrice) -> float:
        return (
            (self.uncached_input - self.cache_write) * price.input
            + self.cached_input * price.cached_input
            + self.cache_write * price.cache_write
            + self.output * price.output
        ) / 1_000_000 + self.web_searches * WEB_SEARCH_USD_PER_CALL


ARCHIVIST_ADMIN_TOOLS = frozenset({"auth_status", "auth_whoami", "doctor", "usage", "version"})
# Pilot questions mapped to the closest pre registered stratum (pilot/README.md).
PILOT_STRATA: dict[str, str] = {
    "apple-fy25-headline": "lookup",
    "nvda-china-export-controls": "multi_hop_document",
    "msft-ai-risk-diff-fy24-fy25": "multi_period",
    "aselsan-kap-contracts-apr-sep-2026": "multi_document",
    "couche-tard-4q": "multi_period",
    "couche-tard-8q": "multi_period",
    "couche-tard-12q": "multi_period",
    "tr-grocers-fy25": "breadth",
    "semis-tsmc-12-companies": "breadth",
}
PILOT_ARMS = {"web": "web", "mosaic": "archivist"}
REACH_SCALE = 2.0  # reach (20 to 40 filings) priced as twice the multi period profile


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def profile_of(rows: list[dict[str, Any]]) -> Profile:
    return Profile(
        uncached_input=_mean([float(r["uncached_input"]) for r in rows]),
        cached_input=_mean([float(r["cached_input"]) for r in rows]),
        cache_write=_mean([float(r.get("cache_write", 0)) for r in rows]),
        output=_mean([float(r["output"]) for r in rows]),
        web_searches=_mean([float(r.get("web_searches", 0)) for r in rows]),
        archivist_calls=_mean([float(r.get("archivist_calls", 0)) for r in rows]),
    )


def scale(p: Profile, k: float) -> Profile:
    return Profile(
        p.uncached_input * k,
        p.cached_input * k,
        p.cache_write * k,
        p.output * k,
        p.web_searches * k,
        p.archivist_calls * k,
    )


def upper(a: Profile, b: Profile) -> Profile:
    return Profile(
        max(a.uncached_input, b.uncached_input),
        max(a.cached_input, b.cached_input),
        max(a.cache_write, b.cache_write),
        max(a.output, b.output),
        max(a.web_searches, b.web_searches),
        max(a.archivist_calls, b.archivist_calls),
    )


def total_tokens(p: Profile) -> float:
    return p.uncached_input + p.cached_input + p.output


def ledger_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Completed run records reduced to profile rows."""
    rows = []
    for r in records:
        usage = r.get("usage") or {}
        if r.get("status") != "completed" or not usage:
            continue
        archivist = sum(
            n
            for tool, n in (r.get("tool_calls") or {}).items()
            if tool.startswith(("mcp:archivist.", "mcp__archivist__"))
            and tool.rsplit(".", 1)[-1].rsplit("__", 1)[-1] not in ARCHIVIST_ADMIN_TOOLS
        )
        rows.append(
            {
                "agent": r["agent"],
                "arm": r["arm"],
                "stratum": r["stratum"],
                "question_id": r["question_id"],
                "uncached_input": usage.get("uncached_input", 0),
                "cached_input": usage.get("cached_input", 0),
                "cache_write": usage.get("cache_write_input", 0),
                "output": usage.get("output", 0),
                "total": usage.get("total", 0),
                "web_searches": web_search_calls(r["agent"], r.get("web_actions") or {}),
                "archivist_calls": archivist,
            }
        )
    return rows


def pilot_rows(pilot: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for r in pilot:
        qid = str(r["question_id"]).removesuffix("-rerun")
        arm = PILOT_ARMS.get(str(r["arm"]))
        if arm is None or qid not in PILOT_STRATA:
            continue
        tools = r.get("tool_calls_observed") or {}
        rows.append(
            {
                "agent": "codex",
                "arm": arm,
                "stratum": PILOT_STRATA[qid],
                "question_id": qid,
                "uncached_input": r["uncached_input_tokens"],
                "cached_input": r["cached_input_tokens"],
                "cache_write": 0,
                "output": r["output_tokens"],
                "total": r["total_tokens"],
                "web_searches": tools.get("web_search_query", 0),
                "archivist_calls": sum(n for t, n in tools.items() if not t.startswith("web_")),
            }
        )
    return rows


def build_estimate(
    question_counts: dict[str, dict[str, int]],
    smoke: list[dict[str, Any]],
    pilot: list[dict[str, Any]],
    models: dict[str, str],
    repetitions: int = 3,
) -> dict[str, Any]:
    """Cost and Archivist quota of the codex campaign matrix.

    ``question_counts`` is ``{agent: {stratum: questions}}``. Codex profiles come from the
    measured smoke where it covers the stratum and arm, else from the pilot, else from the
    stated proxies; the both arm without a measurement takes the larger of web and Archivist
    field by field (an upper bound). Claude Code profiles are the Codex profiles, scaled by the
    Claude to Codex total token ratio only in a stratum and arm where both ran the same question.
    """
    measured = [r for r in smoke if r["agent"] == "codex"]
    sources = {"smoke": measured, "pilot": pilot}
    strata = sorted({s for counts in question_counts.values() for s in counts})

    def codex_profile(stratum: str, arm: str) -> tuple[Profile, str]:
        for label, rows in sources.items():
            hit = [r for r in rows if r["stratum"] == stratum and r["arm"] == arm]
            if hit:
                return profile_of(hit), f"{label} (n={len(hit)})"
        if arm == "both":
            web, web_basis = codex_profile(stratum, "web")
            arc, arc_basis = codex_profile(stratum, "archivist")
            return upper(web, arc), f"max(web: {web_basis}; archivist: {arc_basis})"
        if stratum == "reach":
            base, basis = codex_profile("multi_period", arm)
            return scale(base, REACH_SCALE), f"{REACH_SCALE}x multi_period [{basis}]"
        if stratum == "control":
            base, basis = codex_profile("lookup", arm)
            return base, f"lookup proxy [{basis}]"
        if stratum == "multi_period":
            raise ValueError(
                f"no measured or pilot profile for stratum multi_period, arm {arm} (needed as "
                "the proxy for unmeasured strata); pass --pilot or a ledger that covers it"
            )
        base, basis = codex_profile("multi_period", arm)
        return base, f"multi_period proxy [{basis}]"

    # Claude to Codex total token ratio, measured only where both agents ran the same question
    # and arm. It is applied to that stratum alone: a control's fixed overhead does not predict
    # a research question, so other strata assume Claude Code uses as many tokens as Codex.
    claude_ratio: dict[tuple[str, str], float] = {}
    for c in (r for r in smoke if r["agent"] == "claude_code"):
        match = [
            r
            for r in measured
            if r["question_id"] == c["question_id"] and r["arm"] == c["arm"] and r["total"]
        ]
        if match:
            claude_ratio[(c["stratum"], c["arm"])] = c["total"] / _mean(
                [float(r["total"]) for r in match]
            )

    out: dict[str, Any] = {
        "agents": {},
        "claude_ratio": {f"{k[0]}/{k[1]}": round(v, 4) for k, v in claude_ratio.items()},
    }
    for agent, counts in question_counts.items():
        price = price_for(models[agent])
        if price is None:
            raise ValueError(f"no price for {models[agent]}")
        lines = []
        usd = 0.0
        quota = 0.0
        runs = 0
        for stratum in strata:
            n = counts.get(stratum, 0)
            if not n:
                continue
            for arm in ("web", "archivist", "both"):
                prof, basis = codex_profile(stratum, arm)
                if agent == "claude_code":
                    if (stratum, arm) in claude_ratio:
                        k = claude_ratio[(stratum, arm)]
                        prof = scale(prof, k)
                        basis = f"{basis} x measured Claude ratio {k:.2f}"
                    else:
                        basis = f"{basis}; Claude assumed equal to Codex"
                arm_runs = n * repetitions
                cost = prof.cost(price) * arm_runs
                calls = prof.archivist_calls * arm_runs if arm != "web" else 0.0
                usd += cost
                quota += calls
                runs += arm_runs
                lines.append(
                    {
                        "stratum": stratum,
                        "arm": arm,
                        "runs": arm_runs,
                        "tokens_per_run": round(total_tokens(prof)),
                        "usd": round(cost, 2),
                        "archivist_calls": round(calls),
                        "basis": basis,
                    }
                )
        out["agents"][agent] = {
            "model": models[agent],
            "runs": runs,
            "usd_api_equivalent": round(usd, 2),
            "archivist_calls": round(quota),
            "lines": lines,
        }
    return out
