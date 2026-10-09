"""Paired bootstrap over questions (pre registered: 10,000 resamples, seed 8105, percentile 95%).

Unit of analysis is the question. Per question and arm the repetitions are averaged first; the
bootstrap then resamples question indices with replacement, the same indices for both arms, so
the pairing by question is kept.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from statistics import fmean
from typing import Any

from .model import BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED


@dataclass(frozen=True)
class Interval:
    estimate: float
    low: float
    high: float
    n_questions: int
    resamples: int
    seed: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "estimate": round(self.estimate, 4),
            "ci95": [round(self.low, 4), round(self.high, 4)],
            "n_questions": self.n_questions,
            "resamples": self.resamples,
            "seed": self.seed,
        }


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear interpolation percentile (same as numpy's default) on a sorted sequence."""
    if not sorted_values:
        raise ValueError("empty sample")
    pos = (len(sorted_values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def paired_bootstrap(
    pairs: Sequence[tuple[float, float]],
    statistic: Callable[[Sequence[tuple[float, float]]], float],
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> Interval:
    if not pairs:
        raise ValueError("no paired questions")
    rng = random.Random(seed)
    n = len(pairs)
    values = []
    for _ in range(resamples):
        sample = [pairs[rng.randrange(n)] for _ in range(n)]
        values.append(statistic(sample))
    values.sort()
    return Interval(
        estimate=statistic(pairs),
        low=percentile(values, 0.025),
        high=percentile(values, 0.975),
        n_questions=n,
        resamples=resamples,
        seed=seed,
    )


def token_ratio(sample: Sequence[tuple[float, float]]) -> float:
    """Sum of web tokens over sum of the other arm's tokens (above 1: the arm used fewer)."""
    denominator = sum(b for _, b in sample)
    return sum(a for a, _ in sample) / denominator if denominator else float("inf")


def mean_difference(sample: Sequence[tuple[float, float]]) -> float:
    """Mean of (arm minus web); for accuracy, above 0 means the arm scored higher."""
    return fmean(b - a for a, b in sample)


def question_means(
    rows: Sequence[dict[str, Any]], value_key: str
) -> dict[tuple[str, str, str], dict[str, float]]:
    """Average repetitions: ``{(agent, stratum, arm): {question_id: mean value}}``."""
    acc: dict[tuple[str, str, str, str], list[float]] = defaultdict(list)
    for row in rows:
        value = row.get(value_key)
        if value is None:
            continue
        acc[(row["agent"], row["stratum"], row["arm"], row["question_id"])].append(float(value))
    out: dict[tuple[str, str, str], dict[str, float]] = defaultdict(dict)
    for (agent, stratum, arm, qid), values in acc.items():
        out[(agent, stratum, arm)][qid] = fmean(values)
    return out


def paired(
    means: dict[tuple[str, str, str], dict[str, float]],
    agent: str,
    stratum: str,
    arm: str,
    baseline: str = "web",
) -> list[tuple[float, float]]:
    base = means.get((agent, stratum, baseline), {})
    other = means.get((agent, stratum, arm), {})
    return [(base[q], other[q]) for q in sorted(set(base) & set(other))]


# (arm, baseline) pairs. H2 needs both against web and against archivist (the best single arm
# is whichever scores higher), so both comparisons are always reported.
COMPARISONS: tuple[tuple[str, str], ...] = (
    ("archivist", "web"),
    ("both", "web"),
    ("both", "archivist"),
)


def dropped(
    means: dict[tuple[str, str, str], dict[str, float]],
    seen: set[str],
    agent: str,
    stratum: str,
    arm: str,
    baseline: str,
) -> list[str]:
    """Every question of the agent and stratum (any row, None values included) that is not in
    the paired comparison."""
    base = set(means.get((agent, stratum, baseline), {}))
    other = set(means.get((agent, stratum, arm), {}))
    return sorted(seen - (base & other))


def analyze(
    rows: Sequence[dict[str, Any]],
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
    token_key: str = "total_tokens",
) -> list[dict[str, Any]]:
    """Per agent and stratum and each (arm, baseline) pair: token ratio (sum of baseline over
    sum of arm), accuracy difference (arm minus baseline) and completion difference (arm minus
    baseline, H5), each with its paired bootstrap interval and the questions dropped because
    only one arm has a value.

    ``rows``: ``agent, stratum, arm, question_id`` plus ``total_tokens``, ``accuracy`` and
    ``completion`` (``None`` values are skipped).
    """
    metrics = {
        "token_ratio_baseline_over_arm": (question_means(rows, token_key), token_ratio),
        "accuracy_diff_arm_minus_baseline": (question_means(rows, "accuracy"), mean_difference),
        "completion_diff_arm_minus_baseline": (
            question_means(rows, "completion"),
            mean_difference,
        ),
    }
    keys = sorted({(r["agent"], r["stratum"]) for r in rows})
    seen: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in rows:
        seen[(r["agent"], r["stratum"])].add(str(r["question_id"]))
    results: list[dict[str, Any]] = []
    for agent, stratum in keys:
        for arm, baseline in COMPARISONS:
            entry: dict[str, Any] = {
                "agent": agent,
                "stratum": stratum,
                "arm": arm,
                "baseline": baseline,
                "dropped_questions": {},
            }
            for name, (means, statistic) in metrics.items():
                pairs = paired(means, agent, stratum, arm, baseline)
                if pairs:
                    entry[name] = paired_bootstrap(pairs, statistic, resamples, seed).as_dict()
                gone = dropped(means, seen[(agent, stratum)], agent, stratum, arm, baseline)
                if gone:
                    entry["dropped_questions"][name] = gone
            if len(entry) > 5 or entry["dropped_questions"]:
                results.append(entry)
    return results


def completion_rates(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """H5: per agent, stratum and arm, the share of runs that completed, with event counts."""
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["agent"], row["stratum"], row["arm"])].append(row)
    out = []
    for (agent, stratum, arm), items in sorted(groups.items()):
        completed = [float(r["completion"]) for r in items if r.get("completion") is not None]
        out.append(
            {
                "agent": agent,
                "stratum": stratum,
                "arm": arm,
                "runs": len(items),
                "runs_without_valid_judgement": sum(
                    1 for r in items if r.get("completion") is None
                ),
                "completion_rate": round(fmean(completed), 4) if completed else None,
                "timeouts": sum(1 for r in items if r.get("timeout")),
                "turn_limits": sum(1 for r in items if r.get("turn_limit")),
                "compactions": sum(int(r.get("compactions") or 0) for r in items),
                "truncations": sum(int(r.get("truncations") or 0) for r in items),
                "runs_with_truncation": sum(1 for r in items if r.get("truncations")),
            }
        )
    return out
