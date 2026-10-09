"""Blinded cross family LLM judge (pre registered rubric in preregistration.md, amendment 1).

Two judges grade every answer from one identical prompt (the rubric, the question, the answer key
with its facts in a seeded shuffled order, then the blinded answer): pass A is Claude Code
(``claude -p`` on the operator's Claude subscription, ``--restricted``, no tools, no API key) and
pass B is Codex (``codex exec`` on the operator's ChatGPT login, read only, no shell, ephemeral).
Both return a JSON verdict per fact (``correct``, ``incorrect`` or ``missing``) plus ``complete``.
Where the judges disagree the fact is ``disputed``, scores 0.5 and is queued for a human audit.
A malformed verdict is retried once, then the pass is a ``judge_error``. A plan usage limit or
rate limit is a ``JudgeInfraError`` that stops the batch without counting against the answer.

Amendment 2 (pre registration section 13, harness 1.3.0): ``judge --passes B`` runs pass B
alone and merges it into an existing judgement, keeping any pass A verdicts untouched; ``analyze
--primary-judge B`` scores every fact by the pass B verdict (``scoring``), and a fact is disputed
when an existing pass A verdict differs from pass B (``audit_queue``, ``pass_agreement``).
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import tempfile
from collections.abc import Callable, Collection, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import fmean
from typing import Any

from . import claude_code, codex, probe
from .cost import _call_cost, price_for
from .model import BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED
from .redact import blind
from .runner import (
    ProcessExecutor,
    ProcessRequest,
    child_env,
    subprocess_executor,
)
from .stats import percentile

VERDICTS = ("correct", "incorrect", "missing")
SCORES = {"correct": 1.0, "incorrect": 0.0, "missing": 0.0, "disputed": 0.5}
PASSES = ("A", "B")
# Scoring rules of ``analyze --primary-judge``: ``combined`` is amendment 1 (agreement scores the
# verdict, disagreement is disputed and scores 0.5); ``B`` is amendment 2 (the pass B verdict
# scores every fact).
PRIMARY_JUDGES = ("combined", "B")
DEFAULT_PRIMARY_JUDGE = "combined"
DEFERRED_WITH_CLAUDE_ARM = (
    "deferred with the Claude Code arm (pre registration section 13, amendment 2): the family "
    "interaction and the single judge recomputations need both answering families and both "
    "judges on every answer; pass_agreement reports the descriptive pass A and pass B agreement"
)
# Model family of each answering agent, for the judge bias check.
AGENT_FAMILY = {"claude_code": "anthropic", "codex": "openai"}

RUBRIC = """You grade one research answer against an answer key taken from company filings.
Rules:
- Judge each key fact on its own: "correct" when the answer states it with the same value,
  period and unit (equivalent formatting, rounding to the precision shown in the key, and the
  listed accepted spellings count as the same); "incorrect" when the answer gives a different
  value, period, unit or entity for it; "missing" when the answer does not state it.
- For a fact that says a filing does not mention something, "correct" means the answer says it
  is not mentioned (or omits it from a list of companies that mention it).
- Ignore length, style, tone, formatting, extra correct detail and how sources are cited. Do not
  reward or penalize an answer for being long or short. Links and tool names are hidden.
- "complete" is true when the answer addresses every part the question asks for, false when it
  stops early, refuses, says it ran out of time or covers only part of the requested items.
Return only JSON: {"facts": [{"id": "<fact label>", "verdict": "correct|incorrect|missing"}],
"complete": true|false}. Include every fact label exactly once."""

# Structured output schema for both CLIs (Claude Code ``--json-schema``, Codex
# ``--output-schema``); OpenAI strict mode needs every property required and no extras.
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["facts", "complete"],
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "verdict"],
                "properties": {
                    "id": {"type": "string"},
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                },
            },
        },
        "complete": {"type": "boolean"},
    },
}


@dataclass(frozen=True)
class JudgeConfig:
    """One judge: fixed CLI, model and effort. Neither CLI exposes a sampling temperature, so
    effort is the fixed, recorded setting (temperature stays the CLI default)."""

    pass_name: str
    family: str
    cli: str
    model: str
    effort: str
    auth_mode: str
    billing_mode: str
    timeout_s: int = 600

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


JUDGES: dict[str, JudgeConfig] = {
    "A": JudgeConfig(
        pass_name="A",
        family="anthropic",
        cli="claude_code",
        model="claude-opus-5-5",
        effort="medium",
        auth_mode="subscription",
        billing_mode="claude_plan_allowance",
    ),
    "B": JudgeConfig(
        pass_name="B",
        family="openai",
        cli="codex",
        model="gpt-6.1-sol",
        effort="medium",
        auth_mode="chatgpt",
        billing_mode="chatgpt_plan_allowance",
    ),
}


@dataclass
class Reply:
    """One judge call: the verdict text plus what the call cost in tokens and plan window."""

    text: str
    usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class JudgeCallError(Exception):
    """A judge call that produced no verdict (nonzero exit, timeout, isolation breach)."""


class JudgeInfraError(JudgeCallError):
    """A plan usage limit or rate limit: stop the batch, do not count it against the answer."""


class JudgeLoginError(JudgeInfraError):
    """The operator's login is not the plan login the judge must use: nothing about the answer,
    so it stops the batch like a plan limit."""


@dataclass
class PassResult:
    verdicts: dict[str, str] = field(default_factory=dict)
    complete: bool | None = None
    error: str | None = None
    attempts: int = 0
    raw: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Judgement:
    run_id: str
    fact_scores: dict[str, float]
    fact_verdicts: dict[str, str]
    disputed: list[str]
    accuracy: float | None
    complete: float | None
    status: str  # "ok" or "judge_error"
    passes: dict[str, PassResult]
    label_of: dict[str, str] = field(default_factory=dict)
    judges: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Passes of an earlier judgement kept untouched (``judge --passes B`` keeps pass A), as
    # stored in judgement.json.
    kept: dict[str, dict[str, Any]] = field(default_factory=dict)
    requested: list[str] = field(default_factory=lambda: list(PASSES))
    # The rule of the top level fields: ``combined`` with both passes, else the one valid pass.
    rule: str | None = None

    def as_dict(self) -> dict[str, Any]:
        by_label = {label: fact for fact, label in self.label_of.items()}
        ran = {
            name: {
                "judge": self.judges.get(name),
                "verdicts": p.verdicts,
                "fact_verdicts": {by_label.get(k, k): v for k, v in p.verdicts.items()},
                "complete": p.complete,
                "error": p.error,
                "attempts": p.attempts,
                "calls": p.calls,
            }
            for name, p in self.passes.items()
        }
        merged = {**self.kept, **ran}
        return {
            "run_id": self.run_id,
            "status": self.status,
            "rule": self.rule,
            "accuracy": self.accuracy,
            "complete": self.complete,
            "fact_verdicts": self.fact_verdicts,
            "fact_scores": self.fact_scores,
            "disputed": self.disputed,
            "passes_requested": list(self.requested),
            "passes_run": [name for name in PASSES if name in self.passes],
            "passes_kept": [name for name in PASSES if name in self.kept],
            "passes": {name: merged[name] for name in PASSES if name in merged},
        }


def _key_lines(facts: list[dict[str, Any]], labels: dict[str, str]) -> str:
    lines = []
    for fact in facts:
        accept = fact.get("accept") or []
        extra = f" (also accepted: {'; '.join(map(str, accept))})" if accept else ""
        unit = f" [{fact['unit']}]" if fact.get("unit") else ""
        lines.append(
            f"{labels[fact['id']]}: {fact['statement']} Expected value: {fact['value']}{unit}"
            f"{extra}"
        )
    return "\n".join(lines)


def judged_facts(question: dict[str, Any]) -> list[dict[str, Any]]:
    """The facts a judge grades: every fact but a ``trap`` (a wrong web value of the
    contamination stratum, pre registration section 14, never scored for accuracy)."""
    return [fact for fact in question["facts"] if fact.get("kind") != "trap"]


def fact_labels(question: dict[str, Any]) -> dict[str, str]:
    """Neutral labels F1..Fn in key order, identical for both judges (trap facts left out)."""
    return {fact["id"]: f"F{i}" for i, fact in enumerate(judged_facts(question), 1)}


def build_prompt(
    question: dict[str, Any], answer: str, run_id: str, seed: int = BOOTSTRAP_SEED
) -> str:
    """The one prompt both judges get: rubric, question, key (facts shuffled, seeded by
    ``<seed>:<run id>:key``), then the blinded answer."""
    labels = fact_labels(question)
    shuffled = judged_facts(question)
    random.Random(f"{seed}:{run_id}:key").shuffle(shuffled)
    return (
        f"{RUBRIC}\n\nQuestion:\n{question['question']}\n"
        f"\nAnswer key:\n{_key_lines(shuffled, labels)}\n\n"
        f"Answer to grade:\n<<<\n{blind(answer)}\n>>>\n"
    )


def parse_verdict(text: str, labels: set[str]) -> tuple[dict[str, str], bool]:
    """Parse one verdict; raise ValueError when malformed or incomplete."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned[cleaned.find("{") :]
    data = json.loads(cleaned)
    if not isinstance(data, dict) or not isinstance(data.get("facts"), list):
        raise ValueError("verdict is not an object with a facts list")
    verdicts: dict[str, str] = {}
    for item in data["facts"]:
        if not isinstance(item, dict):
            raise ValueError(f"fact verdict is not an object: {item!r}")
        label = str(item.get("id"))
        verdict = str(item.get("verdict", "")).lower()
        if label not in labels or verdict not in VERDICTS:
            raise ValueError(f"bad fact verdict {item!r}")
        if label in verdicts:
            raise ValueError(f"duplicate fact label {label}")
        verdicts[label] = verdict
    if set(verdicts) != labels:
        raise ValueError("verdict does not cover every fact exactly")
    complete = data.get("complete")
    if not isinstance(complete, bool):
        raise ValueError("complete must be a boolean")
    return verdicts, complete


Executor = Callable[[str], str | Reply]


def run_pass(prompt: str, labels: set[str], execute: Executor) -> PassResult:
    result = PassResult()
    for _ in range(2):  # one retry on a malformed verdict or a failed call
        result.attempts += 1
        try:
            reply = execute(prompt)
            if isinstance(reply, str):
                reply = Reply(reply)
            result.raw.append(reply.text)
            result.calls.append(
                {"usage": reply.usage, "cost_usd": reply.cost_usd, "meta": reply.meta}
            )
            result.verdicts, result.complete = parse_verdict(reply.text, labels)
            result.error = None
            return result
        except JudgeInfraError:
            raise
        except (ValueError, KeyError, IndexError) as exc:
            result.error = f"malformed: {exc}"
        except JudgeCallError as exc:
            result.error = f"call: {exc}"
    return result


def judge(
    question: dict[str, Any],
    answer: str,
    run_id: str,
    executors: dict[str, Executor],
    seed: int = BOOTSTRAP_SEED,
    judges: dict[str, JudgeConfig] | None = None,
    passes: Sequence[str] = PASSES,
    keep: dict[str, dict[str, Any]] | None = None,
) -> Judgement:
    """Grade one answer with the requested passes (default A and B) on the same prompt. ``keep``
    holds passes of an earlier judgement (as stored) that stay untouched and score beside the
    new ones (``judge --passes B`` keeps pass A). Raises JudgeInfraError."""
    requested = [name for name in PASSES if name in passes]
    if not requested:
        raise ValueError("no judge pass requested")
    label_of = fact_labels(question)
    labels = set(label_of.values())
    prompt = build_prompt(question, answer, run_id, seed)
    ran = {name: run_pass(prompt, labels, executors[name]) for name in requested}
    kept = {k: v for k, v in (keep or {}).items() if k in PASSES and k not in ran}
    configs = {k: v.as_dict() for k, v in (judges or JUDGES).items()}
    extra: dict[str, Any] = {"kept": kept, "requested": requested}
    if any(p.error for p in ran.values()):
        return Judgement(
            run_id, {}, {}, [], None, None, "judge_error", ran, label_of, configs, **extra
        )
    maps = {
        name: {fact: p.verdicts[label] for fact, label in label_of.items()}
        for name, p in ran.items()
    }
    completes = {name: bool(p.complete) for name, p in ran.items()}
    for name, stored in kept.items():
        verdicts = _valid_pass(stored)
        if verdicts is not None and set(verdicts) == set(label_of):
            maps[name], completes[name] = verdicts, bool(stored["complete"])
    scores: dict[str, float] = {}
    fact_verdicts: dict[str, str] = {}
    disputed: list[str] = []
    if len(maps) == len(PASSES):
        rule = "combined"
        for fact_id in label_of:
            a, b = maps["A"][fact_id], maps["B"][fact_id]
            if a == b:
                fact_verdicts[fact_id] = a
            else:
                fact_verdicts[fact_id] = "disputed"
                disputed.append(fact_id)
            scores[fact_id] = SCORES[fact_verdicts[fact_id]]
        complete_a, complete_b = completes["A"], completes["B"]
        complete = (
            1.0 if complete_a and complete_b else 0.0 if not (complete_a or complete_b) else 0.5
        )
    else:
        # One valid pass (amendment 2): its verdict scores every fact, nothing is disputed.
        [rule] = list(maps)
        fact_verdicts = dict(maps[rule])
        scores = {fact_id: SCORES[v] for fact_id, v in fact_verdicts.items()}
        complete = 1.0 if completes[rule] else 0.0
    accuracy = round(sum(scores.values()) / len(scores), 6) if scores else None
    return Judgement(
        run_id,
        scores,
        fact_verdicts,
        disputed,
        accuracy,
        complete,
        "ok",
        ran,
        label_of,
        configs,
        rule=rule,
        **extra,
    )


def _valid_pass(stored: Any) -> dict[str, str] | None:
    """Fact id to verdict of one stored pass when it is valid: no error, a verdict for at least
    one fact, every verdict known and ``complete`` a boolean; None otherwise."""
    if not isinstance(stored, dict) or stored.get("error"):
        return None
    verdicts = stored.get("fact_verdicts")
    if not isinstance(verdicts, dict) or not verdicts:
        return None
    if any(v not in VERDICTS for v in verdicts.values()):
        return None
    if not isinstance(stored.get("complete"), bool):
        return None
    return {str(k): str(v) for k, v in verdicts.items()}


def pass_verdicts(judgement: dict[str, Any] | None, pass_name: str) -> dict[str, str] | None:
    """The valid verdicts of one pass of a stored judgement (fact id to verdict), else None."""
    if not judgement:
        return None
    return _valid_pass((judgement.get("passes") or {}).get(pass_name))


@dataclass(frozen=True)
class Scored:
    """One judgement under a scoring rule: fact verdicts and scores, run accuracy, completion."""

    verdicts: dict[str, str]
    scores: dict[str, float]
    accuracy: float | None
    complete: float | None


def scoring(
    judgement: dict[str, Any] | None, primary: str = DEFAULT_PRIMARY_JUDGE
) -> Scored | None:
    """A judgement under the ``analyze --primary-judge`` rule; None when it has no valid
    judgement under that rule.

    ``combined`` (amendment 1): the stored top level verdicts, scores, accuracy and completion of
    an ``ok`` judgement graded by both judges (a judgement scored by one pass alone is not valid
    under it). ``B`` (amendment 2): the pass B verdicts, correct 1 and otherwise 0, run accuracy
    their mean, completion pass B's ``complete``; the judgement's status does not matter."""
    if primary not in PRIMARY_JUDGES:
        raise ValueError(f"unknown primary judge {primary!r}")
    if not judgement:
        return None
    if primary == "B":
        verdicts = pass_verdicts(judgement, "B")
        if verdicts is None:
            return None
        scores = {fact_id: SCORES[v] for fact_id, v in verdicts.items()}
        complete = judgement["passes"]["B"]["complete"]
        return Scored(
            verdicts,
            scores,
            round(sum(scores.values()) / len(scores), 6),
            1.0 if complete else 0.0,
        )
    if judgement.get("status") != "ok" or (judgement.get("rule") or "combined") != "combined":
        return None
    complete_value = judgement.get("complete")
    return Scored(
        dict(judgement.get("fact_verdicts") or {}),
        dict(judgement.get("fact_scores") or {}),
        judgement.get("accuracy"),
        float(complete_value) if complete_value is not None else None,
    )


AUDIT_SAMPLE_SHARE = 0.05


def audit_queue(
    judgements: list[dict[str, Any]],
    seed: int = BOOTSTRAP_SEED,
    primary: str = DEFAULT_PRIMARY_JUDGE,
) -> list[dict[str, str]]:
    """Facts for the human audit.

    ``combined``: every disputed fact plus a seeded 5% of agreed facts (judgements scored by one
    pass alone are left out). ``B``: every fact whose existing pass A verdict differs from its
    pass B verdict, plus a seeded 5% of every other fact with a valid pass B verdict; each item
    carries the pass B verdict and its ``selection``."""
    if primary not in PRIMARY_JUDGES:
        raise ValueError(f"unknown primary judge {primary!r}")
    disputed: list[dict[str, str]] = []
    agreed: list[dict[str, str]] = []
    for item in sorted(judgements, key=lambda j: str(j.get("run_id"))):
        if primary == "B":
            b = pass_verdicts(item, "B")
            if b is None:
                continue
            a = pass_verdicts(item, "A") or {}
            for fact_id, verdict in sorted(b.items()):
                row = {"run_id": str(item["run_id"]), "fact_id": fact_id, "verdict": verdict}
                if fact_id in a and a[fact_id] != verdict:
                    disputed.append(row | {"selection": "disputed"})
                else:
                    agreed.append(row | {"selection": "sample"})
            continue
        if (item.get("rule") or "combined") != "combined":
            continue
        for fact_id, verdict in sorted((item.get("fact_verdicts") or {}).items()):
            row = {"run_id": str(item["run_id"]), "fact_id": fact_id, "verdict": verdict}
            (disputed if verdict == "disputed" else agreed).append(row)
    k = round(len(agreed) * AUDIT_SAMPLE_SHARE)
    sample = random.Random(seed).sample(agreed, k) if k else []
    return disputed + sorted(sample, key=lambda r: (r["run_id"], r["fact_id"]))


def pass_agreement(
    judgements: Sequence[dict[str, Any]], run_ids: Collection[str] | None = None
) -> dict[str, Any]:
    """Exploratory (amendment 2): how often pass A and pass B agree on the answers graded by
    both (valid verdicts from each), per fact and on ``complete``, with the verdict cross table
    (rows pass A, columns pass B). ``run_ids`` limits it to the analyzed runs."""
    cross = {a: dict.fromkeys(VERDICTS, 0) for a in VERDICTS}
    answers = facts = agreed = complete_agreed = 0
    for item in sorted(judgements, key=lambda j: str(j.get("run_id"))):
        if run_ids is not None and str(item.get("run_id")) not in run_ids:
            continue
        a, b = pass_verdicts(item, "A"), pass_verdicts(item, "B")
        if a is None or b is None:
            continue
        shared = sorted(set(a) & set(b))
        if not shared:
            continue
        answers += 1
        for fact_id in shared:
            facts += 1
            cross[a[fact_id]][b[fact_id]] += 1
            agreed += a[fact_id] == b[fact_id]
        passes = item["passes"]
        complete_agreed += passes["A"]["complete"] == passes["B"]["complete"]
    return {
        "label": "exploratory",
        "description": (
            "descriptive agreement of pass A (Claude Code) and pass B (Codex) on the answers "
            "both graded; not a judge bias check (amendment 2)"
        ),
        "answers": answers,
        "facts": facts,
        "agreed_facts": agreed,
        "agreement_share": round(agreed / facts, 4) if facts else None,
        "complete_agreement_share": round(complete_agreed / answers, 4) if answers else None,
        "cross_table_rows_A_columns_B": cross,
    }


def control_score(question: dict[str, Any], answer: str) -> float:
    """Controls are scored by exact containment of the expected value, no judge call."""
    facts = question["facts"]
    hits = sum(1 for f in facts if str(f["value"]).lower() in answer.lower())
    return hits / len(facts) if facts else 0.0


def single_judge_accuracy(judgement: dict[str, Any], pass_name: str) -> float | None:
    """Run accuracy from one judge alone (sensitivity analysis): share of facts it marks
    ``correct``. Decided per pass (``pass_verdicts``): a valid pass counts even when the other
    pass failed and the judgement's top level status is ``judge_error``."""
    verdicts = pass_verdicts(judgement, pass_name)
    if not verdicts:
        return None
    return round(sum(1 for v in verdicts.values() if v == "correct") / len(verdicts), 6)


def single_judge_complete(judgement: dict[str, Any], pass_name: str) -> float | None:
    """Run completion from one judge alone (sensitivity analysis), decided per pass like
    ``single_judge_accuracy``."""
    if pass_verdicts(judgement, pass_name) is None:
        return None
    return float(judgement["passes"][pass_name]["complete"])


# --- judge bias check (exploratory, pre registered in amendment 1) ------------------------------


def bias_report(
    judgements: Sequence[dict[str, Any]],
    runs: dict[str, dict[str, Any]],
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Verdict rates per judge family and answering agent family, agreement per answering
    family, and the family interaction with a question cluster bootstrap interval.

    ``runs`` maps a run id to its ``agent`` and ``question_id``. The family interaction =
    (Claude judge correct share on Claude Code answers minus on Codex answers) minus (Codex
    judge correct share on Claude Code answers minus on Codex answers), over the questions both
    agents answered only. It measures differential judge agreement by answering family: self
    preference would raise it, but so can judges with different strictness meeting answers of
    different quality, so a flag calls for investigation (the human audit), not a conclusion.
    """
    ok = [
        j
        for j in judgements
        if j.get("status") == "ok"
        and str(j.get("run_id")) in runs
        and all(((j.get("passes") or {}).get(p) or {}).get("verdicts") for p in PASSES)
    ]

    def family_of(j: dict[str, Any]) -> str:
        return AGENT_FAMILY.get(str(runs[str(j["run_id"])].get("agent")), "unknown")

    rates: list[dict[str, Any]] = []
    agreement: list[dict[str, Any]] = []
    for answer_family in sorted({family_of(j) for j in ok}):
        group = [j for j in ok if family_of(j) == answer_family]
        for pass_name in PASSES:
            counts = dict.fromkeys(VERDICTS, 0)
            completes = []
            for j in group:
                p = j["passes"][pass_name]
                for v in p["verdicts"].values():
                    counts[v] += 1
                completes.append(1.0 if p.get("complete") else 0.0)
            n = sum(counts.values())
            rates.append(
                {
                    "judge_pass": pass_name,
                    "judge_family": JUDGES[pass_name].family,
                    "answer_family": answer_family,
                    "runs": len(group),
                    "facts": n,
                    **{f"{v}_rate": round(counts[v] / n, 4) if n else None for v in VERDICTS},
                    "complete_rate": round(fmean(completes), 4) if completes else None,
                }
            )
        facts = sum(len(j["fact_verdicts"]) for j in group)
        agreed = sum(1 for j in group for v in j["fact_verdicts"].values() if v != "disputed")
        agreement.append(
            {
                "answer_family": answer_family,
                "runs": len(group),
                "facts": facts,
                "agreement_rate": round(agreed / facts, 4) if facts else None,
            }
        )
    clusters: dict[str, dict[str, list[tuple[float, float]]]] = {"anthropic": {}, "openai": {}}
    for j in ok:
        family = family_of(j)
        a, b = single_judge_accuracy(j, "A"), single_judge_accuracy(j, "B")
        if family in clusters and a is not None and b is not None:
            qid = str(runs[str(j["run_id"])].get("question_id"))
            clusters[family].setdefault(qid, []).append((a, b))
    return {
        "verdict_rates": rates,
        "agreement": agreement,
        "family_interaction": _family_interaction(clusters, resamples, seed),
    }


Cluster = list[tuple[float, float]]


def _family_interaction(
    clusters: dict[str, dict[str, Cluster]], resamples: int, seed: int
) -> dict[str, Any] | None:
    shared = sorted(set(clusters["anthropic"]) & set(clusters["openai"]))
    if not shared:
        return None
    anth = [clusters["anthropic"][q] for q in shared]
    oai = [clusters["openai"][q] for q in shared]

    def stat(a_clusters: Sequence[Cluster], o_clusters: Sequence[Cluster]) -> float:
        a_runs = [x for c in a_clusters for x in c]
        o_runs = [x for c in o_clusters for x in c]
        claude_gap = fmean(a for a, _ in a_runs) - fmean(a for a, _ in o_runs)
        codex_gap = fmean(b for _, b in a_runs) - fmean(b for _, b in o_runs)
        return claude_gap - codex_gap

    # Resample shared questions (the same indices for both families): arms and repetitions of
    # one question share its facts and grading errors, so the question is the unit.
    rng = random.Random(seed)
    n = len(shared)
    values = []
    for _ in range(resamples):
        idx = [rng.randrange(n) for _ in range(n)]
        values.append(stat([anth[i] for i in idx], [oai[i] for i in idx]))
    values.sort()
    low, high = percentile(values, 0.025), percentile(values, 0.975)
    return {
        "estimate": round(stat(anth, oai), 4),
        "ci95": [round(low, 4), round(high, 4)],
        "shared_questions": n,
        "runs": {"anthropic": sum(map(len, anth)), "openai": sum(map(len, oai))},
        "resamples": resamples,
        "seed": seed,
        "flagged": low > 0 or high < 0,
    }


# --- CLI executors --------------------------------------------------------------------------------

CLAUDE_JUDGE_TOOLS = frozenset({"StructuredOutput"})  # added by --json-schema; nothing else
CLAUDE_INFRA_STATUS = frozenset({429, 500, 502, 503, 529})
CODEX_ALLOWED_ITEMS = frozenset({"agent_message", "reasoning", "todo_list", "error"})
CODEX_LIMIT_TEXT = re.compile(r"usage limit|rate limit|too many requests", re.IGNORECASE)


def claude_command(cfg: JudgeConfig, schema: dict[str, Any] | None = None) -> list[str]:
    """``claude -p`` argv for a judge call: subscription (``--restricted``, never ``--bare``),
    no built in tools, no MCP, structured output (``schema``, default the verdict schema). The
    prompt goes on stdin."""
    if cfg.auth_mode != "subscription":
        raise ValueError("the Claude judge runs on the subscription only (no API key)")
    return [
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
        "4",
        "--tools",
        "",
        "--strict-mcp-config",
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--restricted",
        "--json-schema",
        json.dumps(schema or VERDICT_SCHEMA, separators=(",", ":")),
    ]


def parse_claude_judge(lines: Sequence[str]) -> Reply:
    """Read one judge call's stream-json: isolation checks on ``system/init``, the plan window
    from ``rate_limit_event``, the verdict from ``result.structured_output``."""
    init: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    windows: dict[str, Any] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "system" and obj.get("subtype") == "init":
            init = obj
        elif obj.get("type") == "rate_limit_event":
            info = obj.get("rate_limit_info") or {}
            windows = claude_code.plan_window(info)
            if info.get("status") == "rejected":
                raise JudgeInfraError(f"Claude plan limit: {info.get('rateLimitType')}")
            if info.get("isUsingOverage") is True:
                # Plan allowance only: overage is metered, so it stops the batch like a limit.
                raise JudgeInfraError("Claude overage in use (metered_fallback_guard)")
        elif obj.get("type") == "result":
            result = obj
    if init is None:
        raise JudgeCallError("no system/init event")
    extra_tools = sorted(set(init.get("tools") or []) - CLAUDE_JUDGE_TOOLS)
    if extra_tools or init.get("mcp_servers"):
        raise JudgeCallError(f"judge isolation: tools {extra_tools}, mcp {init.get('mcp_servers')}")
    if init.get("apiKeySource") not in (None, "none"):
        raise JudgeCallError(f"judge used an API key ({init.get('apiKeySource')}), not the plan")
    if result is None:
        raise JudgeCallError("no result event")
    text_result = str(result.get("result") or "")
    structured = result.get("structured_output")
    # 2.1.289 can flag a ``success`` result as ``is_error`` when the connection drops after the
    # structured output arrived (fixed in 2.1.290): a delivered verdict still counts.
    delivered = isinstance(structured, dict) and result.get("subtype") == "success"
    failed = result.get("is_error") or result.get("subtype") != "success"
    if failed and not delivered:
        status = result.get("api_error_status")
        detail = text_result + " " + " ".join(map(str, result.get("errors") or []))
        if status in CLAUDE_INFRA_STATUS or claude_code.INFRA_TEXT.search(detail):
            raise JudgeInfraError(f"Claude infra {status or ''}: {detail.strip()[:200]}")
        raise JudgeCallError(f"Claude result error {result.get('subtype')}: {detail.strip()[:200]}")
    usage = claude_code._sum_model_usage(result.get("modelUsage") or {})
    raw_usage = result.get("usage")
    known = isinstance(raw_usage, dict) and any(k in raw_usage for k in claude_code.USAGE_KEYS)
    if usage is None and known and isinstance(raw_usage, dict):
        usage = claude_code._usage_from_result(raw_usage)
    text = json.dumps(structured) if isinstance(structured, dict) else text_result
    return Reply(
        text=text,
        usage=usage.as_dict() if usage else {},
        cost_usd=result.get("total_cost_usd"),
        meta={
            "model": init.get("model"),
            "cli_version": init.get("claude_code_version"),
            "api_key_source": init.get("apiKeySource"),
            "init_tools": init.get("tools"),
            "num_turns": result.get("num_turns"),
            "structured": isinstance(structured, dict),
            "plan_window": windows,
            "usage_unavailable": usage is None,
            "cost_basis": "claude_code_client_estimate",
        },
    )


def render_codex_judge_config(cfg: JudgeConfig, cwd: Path) -> str:
    """Judge ``config.toml``: no web search, no MCP, no shell, every extension feature off."""
    lines = [
        "# Rendered by archivist_bench judge (amendment 1). Do not edit.",
        f"model = {codex._toml_str(cfg.model)}",
        f"model_reasoning_effort = {codex._toml_str(cfg.effort)}",
        'approval_policy = "never"',
        'sandbox_mode = "read-only"',
        'web_search = "disabled"',
        # Refuse any non ChatGPT credential. Codex logs out on a mismatch, so the cached login
        # is checked first (``check_chatgpt_login``) and a mismatch never reaches Codex.
        'forced_login_method = "chatgpt"',
        "",
        f"[projects.{codex._toml_str(str(cwd))}]",
        'trust_level = "trusted"',
        "",
        "[features]",
    ]
    lines += [f"{name} = false" for name in codex.DISABLED_FEATURES]
    return "\n".join(lines) + "\n"


def codex_command(cfg: JudgeConfig, cwd: Path, last: Path, schema: Path) -> list[str]:
    """``codex exec`` argv for a judge call; the prompt comes on stdin (``-``)."""
    if cfg.auth_mode != "chatgpt":
        raise ValueError("the Codex judge runs on the ChatGPT login only (no API key)")
    return [
        "codex",
        "exec",
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        "-s",
        "read-only",
        "-C",
        str(cwd),
        "-o",
        str(last),
        "--output-schema",
        str(schema),
        "-m",
        cfg.model,
        "-c",
        f"model_reasoning_effort={codex._toml_str(cfg.effort)}",
        "-c",
        'approval_policy="never"',
        "-c",
        'web_search="disabled"',
        "-",
    ]


def parse_codex_judge(lines: Sequence[str], last_message: str) -> Reply:
    """Read one judge call's ``--json`` events: usage from ``turn.completed``; any item other
    than a message or reasoning (a command, file change, MCP or web call) breaks isolation."""
    usage: dict[str, Any] | None = None
    errors: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = obj.get("type")
        if kind in ("item.started", "item.completed"):
            item_type = (obj.get("item") or {}).get("type")
            if item_type not in CODEX_ALLOWED_ITEMS:
                raise JudgeCallError(f"judge isolation: Codex item {item_type}")
        elif kind == "turn.completed":
            usage = obj.get("usage") or {}
        elif kind in ("turn.failed", "error"):
            err = obj.get("error") or obj
            errors.append(str(err.get("message") if isinstance(err, dict) else err))
    if errors and usage is None:
        joined = "; ".join(errors)[:300]
        if any(code in joined for code in codex.INFRA_ERROR_CODES) or CODEX_LIMIT_TEXT.search(
            joined
        ):
            raise JudgeInfraError(f"Codex infra: {joined}")
        raise JudgeCallError(f"Codex turn failed: {joined}")
    if usage is None:
        raise JudgeCallError("no turn.completed usage")
    price = price_for(JUDGES["B"].model)
    cost = round(_call_cost(price, usage), 6) if price else None
    return Reply(
        text=last_message,
        usage=usage,
        cost_usd=cost,
        meta={"cost_basis": "api_list_price_equivalent", "usage_unavailable": not usage},
    )


def check_chatgpt_login(auth: Path) -> None:
    """Fail closed unless the cached Codex login is a ChatGPT login without an API key. Reads
    only ``auth_mode`` and whether an API key value is present; no value is logged."""
    if not auth.exists():
        raise JudgeLoginError(f"Codex login not found at {auth}")
    try:
        data = json.loads(auth.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JudgeLoginError(f"Codex login unreadable: {type(exc).__name__}") from exc
    if not isinstance(data, dict) or data.get("auth_mode") != "chatgpt":
        mode = data.get("auth_mode") if isinstance(data, dict) else None
        raise JudgeLoginError(f"Codex login is not a ChatGPT login (auth_mode {mode!r})")
    if data.get("OPENAI_API_KEY"):
        raise JudgeLoginError("Codex login carries an API key; the judge runs on the plan only")


def _read(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def make_executors(
    log_dir: Path,
    passes: Sequence[str] = PASSES,
    claude_config_dir: Path | None = None,
) -> dict[str, Executor]:
    """The plan judges of the requested passes (pass A Claude Code, pass B Codex) as live CLI
    executors. The Codex login is checked here, before any judge call, so a wrong login stops
    the command at once. Without pass A (``judge --passes B``) no Claude executor exists and the
    Codex executor's gate refuses any Claude call. Pass A runs on ``claude_config_dir``, the
    chosen Claude account (harness 1.5.1); without one every pass A call is refused."""
    executors: dict[str, Executor] = {}
    if "B" in passes:
        gate = None if "A" in passes else probe.PlanGate(allow_claude=False)
        codex_judge = CliExecutor(JUDGES["B"], log_dir, gate=gate)
        check_chatgpt_login(codex_judge.codex_auth)
        executors["B"] = codex_judge
    if "A" in passes:
        executors["A"] = CliExecutor(JUDGES["A"], log_dir, claude_config_dir=claude_config_dir)
    return {name: executors[name] for name in PASSES if name in executors}


class CliExecutor:
    """Run one judge CLI per call in a fresh, empty working directory with an allowlisted
    environment. Each call's argv, stdout and stderr are kept under ``log_dir`` (outside git);
    the caller points ``log_dir`` at each answer's batch through ``begin``. The structured
    output schema is the verdict schema unless the executor (``schema``) or the call
    (``__call__(prompt, schema=...)``, harness 1.4.0 grading passes) gives another."""

    def __init__(
        self,
        cfg: JudgeConfig,
        log_dir: Path,
        process: ProcessExecutor = subprocess_executor,
        parent_env: dict[str, str] | None = None,
        codex_auth: Path | None = None,
        plan_probe: Callable[[dict[str, str], Path], dict[str, Any]] | None = None,
        gate: probe.PlanGate | None = None,
        schema: dict[str, Any] | None = None,
        claude_config_dir: Path | None = None,
    ) -> None:
        self.cfg = cfg
        # The Claude account of a Claude Code judge (``--claude-config-dir``, harness 1.5.1);
        # unused by the Codex judge.
        self.claude_config_dir = claude_config_dir if cfg.cli == "claude_code" else None
        self.schema = schema or VERDICT_SCHEMA
        self.log_dir = log_dir
        self.process = process
        self.parent_env = dict(os.environ) if parent_env is None else parent_env
        self.codex_auth = codex_auth or Path.home() / ".codex" / "auth.json"
        self.count = 0
        self.plan_probe = plan_probe
        # The pre launch plan gate (``judge --stop-at-window``); cmd_judge shares one per batch.
        self.gate = gate or probe.PlanGate()
        if self.gate.env is None:
            self.gate.configure(
                probe.claude_probe_env(self.parent_env, self.claude_config_dir), None, None
            )
        self.dispatched = 0  # Codex calls launched, for the closing probe

    def closing_probe(self, log_dir: Path) -> dict[str, Any]:
        """One more Codex probe after the judge batch's last call (``closing-probe.json`` in
        ``log_dir``): a credit drop, a hot window or a window step is returned as reasons."""
        log_dir.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix="closing-", dir=log_dir))
        try:
            home, cwd = scratch / "codex-home", scratch / "cwd"
            home.mkdir()
            cwd.mkdir()
            (home / "config.toml").write_text(
                render_codex_judge_config(self.cfg, cwd), encoding="utf-8"
            )
            if self.codex_auth.exists():
                (home / "auth.json").symlink_to(self.codex_auth)
            env = child_env(self.parent_env, {"CODEX_HOME": str(home)})
            report = (self.plan_probe or probe.app_server_probe)(env, cwd)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        closing = {
            "probed_at": probe.stamp(probe.now_utc()),
            "probe": report,
            "reasons": self.gate.check_codex(report),
        }
        (log_dir / "closing-probe.json").write_text(json.dumps(closing, indent=2), "utf-8")
        return closing

    def begin(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self.count = 0

    def __call__(self, prompt: str, schema: dict[str, Any] | None = None) -> Reply:
        # A fresh directory per call, never reused: a resumed batch after a plan limit writes
        # beside the earlier attempt instead of over it.
        schema = schema or self.schema
        self.count += 1
        while (self.log_dir / f"{self.cfg.pass_name}-{self.count}").exists():
            self.count += 1
        call_dir = self.log_dir / f"{self.cfg.pass_name}-{self.count}"
        call_dir.mkdir(parents=True)
        # Scratch home and cwd live beside the call log, not under /tmp (Codex refuses to set up
        # its helper binaries under /tmp and warns); removed after the call.
        scratch = Path(tempfile.mkdtemp(prefix="scratch-", dir=call_dir))
        try:
            cwd = scratch / "cwd"
            cwd.mkdir()
            if self.cfg.cli == "claude_code":
                account = self.claude_config_dir
                if account is None:
                    # Fail closed (harness 1.5.1): never a call on the default ~/.claude login.
                    raise JudgeInfraError(
                        "no Claude account chosen for pass A: pass --claude-config-dir"
                    )
                if self.gate.claude_config_dir != str(account):
                    # The gate must read the account the call uses (one chosen value).
                    raise JudgeInfraError(
                        "plan gate refused the call: the gate reads Claude account "
                        f"{self.gate.claude_config_dir!r}, the call uses {str(account)!r}"
                    )
                refused = self.gate.check_claude()
                if refused:
                    raise JudgeInfraError("plan gate refused the call: " + "; ".join(refused))
                argv = claude_command(self.cfg, schema)
                extras = {
                    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
                    "DISABLE_AUTOUPDATER": "1",
                    **claude_code.account_extras(account),
                }
                last = None
            elif self.cfg.cli == "codex":
                home = scratch / "codex-home"
                home.mkdir()
                (home / "config.toml").write_text(
                    render_codex_judge_config(self.cfg, cwd), encoding="utf-8"
                )
                check_chatgpt_login(self.codex_auth)
                (home / "auth.json").symlink_to(self.codex_auth)
                schema_path = scratch / "schema.json"
                schema_path.write_text(json.dumps(schema), encoding="utf-8")
                last = call_dir / "last-message.json"
                argv = codex_command(self.cfg, cwd, last, schema_path)
                extras = {"CODEX_HOME": str(home)}
            else:
                raise ValueError(f"unknown judge CLI {self.cfg.cli!r}")
            env = child_env(self.parent_env, extras)
            if self.cfg.cli == "codex":
                probe_fn = self.plan_probe or probe.app_server_probe
                plan = probe_fn(env, cwd)
                (call_dir / "plan-probe.json").write_text(json.dumps(plan, indent=2), "utf-8")
                refused = self.gate.check_codex(plan)
                if refused:
                    raise JudgeInfraError("plan probe refused the call: " + "; ".join(refused))
            command: dict[str, Any] = {"argv": argv, "env_keys": sorted(env)}
            if self.claude_config_dir is not None:
                command["claude_config_dir"] = str(self.claude_config_dir)
            (call_dir / "command.json").write_text(json.dumps(command, indent=2), encoding="utf-8")
            if self.cfg.cli == "codex":
                self.dispatched += 1
            stdout, stderr = call_dir / "stdout.jsonl", call_dir / "stderr.log"
            done = self.process(
                ProcessRequest(argv, env, cwd, prompt, stdout, stderr, self.cfg.timeout_s)
            )
            if done.timed_out:
                raise JudgeCallError(f"timeout after {self.cfg.timeout_s} s")
            lines = _read(stdout)
            if self.cfg.cli == "claude_code":
                reply = parse_claude_judge(lines)
                window = reply.meta.get("plan_window")
                if window:
                    window["observed_at"] = probe.stamp(probe.now_utc())
                self.gate.observe_claude(window or None)
                # The judge ledger meta names the account the reading belongs to.
                reply.meta["claude_config_dir"] = str(self.claude_config_dir)
            else:
                text = last.read_text(encoding="utf-8") if last and last.exists() else ""
                reply = parse_codex_judge(lines, text)
            if done.returncode not in (0, None) and not reply.text:
                raise JudgeCallError(f"exit {done.returncode}")
            reply.meta.update(
                {"wall_s": done.wall_s, "returncode": done.returncode, "log": str(call_dir)}
            )
            return reply
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
