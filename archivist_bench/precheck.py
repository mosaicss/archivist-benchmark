"""Pass P, the light audit pre check (pre registration section 16.3, amendment 5; harness
1.6.0).

One Codex call per audit queue row without a human audit verdict, blind to the judge verdict: the
question, the key fact under the corrected key (statement, expected value and unit, accepted
spellings), its filing evidence (the passage, links blinded; for a computed fact its formula and
each component fact with its passage; for an absence fact its absent terms and the full text
search record, no passage) and the blinded answer. The reply gives ``key_check``, ``verdict``
and ``reason``. A row agrees when the pass P verdict equals the effective judge verdict and the
key check is ``supported`` or ``not_applicable``; every other row goes to the human review
list.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .grounding import STRING, _loads, _str, _strict_object, safe_prompt
from .judge import VERDICTS
from .redact import blind

KEY_CHECKS = ("supported", "contradicted", "not_in_passage", "not_applicable")
AGREEING_KEY_CHECKS = ("supported", "not_applicable")
NO_PASSAGE = "not_applicable"
FAILED_REASON = "pre check failed"

# Verbatim from pre registration section 16.3 (a test holds them equal).
PASS_P_RUBRIC = """You check one grading decision of a research benchmark. A key fact was taken from a company filing, and a research answer was graded against it.
First compare the key fact with the filing evidence given: "key_check" is "supported" when the evidence states the key's value for the same period, unit and entity; "contradicted" when the evidence states a different value, period, unit or entity for it; "not_in_passage" when the evidence does not contain it; "not_applicable" when no passage is given (an absence fact checked by a full text search).
Then grade the answer on this one fact: "correct" when the answer states it with the same value, period and unit (equivalent formatting, rounding to the precision shown in the key, and the listed accepted spellings count as the same); "incorrect" when the answer gives a different value, period, unit or entity for it; "missing" when the answer does not state it. For a fact that says a filing does not mention something, "correct" means the answer says it is not mentioned (or omits it from a list of companies that mention it).
Ignore length, style, tone, other facts and how sources are cited. Links and tool names are hidden.
Give a "reason" of one or two sentences naming the evidence.
Return only JSON: {"key_check": "supported|contradicted|not_in_passage|not_applicable", "verdict": "correct|incorrect|missing", "reason": "<text>"}."""

PASS_P_SCHEMA: dict[str, Any] = _strict_object(
    {
        "key_check": {"type": "string", "enum": list(KEY_CHECKS)},
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "reason": STRING,
    }
)


def passage_text(passage: Mapping[str, Any]) -> str:
    """The text of a stored passage (``archivist read passage`` JSON, ``passage`` object)."""
    return str(passage.get("text") or passage.get("content") or passage.get("snippet") or "")


def _components(fact: Mapping[str, Any], facts: Mapping[str, Mapping[str, Any]]) -> list[Any]:
    return [facts[str(dep)] for dep in fact.get("computed_from") or [] if str(dep) in facts]


def _chunk(fact: Mapping[str, Any]) -> str | None:
    if fact.get("kind") != "sourced":
        return None
    chunk = (fact.get("source") or {}).get("chunk_id")
    return str(chunk) if chunk else None


def needed_chunks(fact: Mapping[str, Any], facts: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """The passages a row's prompt shows: a sourced fact's own chunk; a computed fact's
    component chunks; none for an absence fact."""
    if fact.get("kind") == "computed":
        found = [_chunk(dep) for dep in _components(fact, facts)]
    else:
        found = [_chunk(fact)]
    return sorted({c for c in found if c})


def _value_line(fact: Mapping[str, Any]) -> str:
    unit = f" [{fact['unit']}]" if fact.get("unit") else ""
    return f"Expected value: {fact.get('value')}{unit}"


def _passage_block(chunk: str, passages: Mapping[str, Mapping[str, Any]]) -> str:
    passage = passages.get(chunk)
    if passage is None:
        raise ValueError(f"passage {chunk} is not cached")
    return f"<<<\n{blind(passage_text(passage))}\n>>>"


def evidence_text(
    fact: Mapping[str, Any],
    facts: Mapping[str, Mapping[str, Any]],
    passages: Mapping[str, Mapping[str, Any]],
) -> tuple[str, bool]:
    """(the filing evidence block, whether a passage is given)."""
    kind = fact.get("kind")
    if kind == "absence":
        terms = "; ".join(map(str, fact.get("absent_terms") or []))
        return (
            f"Absent terms: {terms}\nFull text search record: "
            f"{fact.get('absence_reason') or ''}\nNo passage is given (an absence fact checked "
            "by a full text search).",
            False,
        )
    if kind == "computed":
        lines = [f"Formula: {fact.get('formula')}", "Component facts:"]
        given = False
        for dep in _components(fact, facts):
            lines.append(f"{dep.get('id')}: {dep.get('statement')} {_value_line(dep)}")
            chunk = _chunk(dep)
            if chunk:
                lines.append(f"Passage of {dep.get('id')}:\n{_passage_block(chunk, passages)}")
                given = True
        return "\n".join(lines), given
    chunk = _chunk(fact)
    if not chunk:
        return "No passage is given.", False
    return f"Filing passage:\n{_passage_block(chunk, passages)}", True


def pass_p_prompt(
    question: Mapping[str, Any],
    fact: Mapping[str, Any],
    passages: Mapping[str, Mapping[str, Any]],
    answer: str,
) -> tuple[str, bool]:
    """The pass P prompt (``PASS_P_RUBRIC`` then the inputs it names) and whether a passage is
    given. Passage text and answer are blinded (``redact.blind``); the whole prompt passes
    ``grounding.safe_prompt``, so no upstream filing URL or host reaches it."""
    facts = {str(f.get("id")): f for f in question.get("facts") or []}
    accept = "; ".join(map(str, fact.get("accept") or [])) or "none"
    evidence, given = evidence_text(fact, facts, passages)
    prompt = (
        f"{PASS_P_RUBRIC}\n\nQuestion:\n{question.get('question')}\n"
        f"\nKey fact:\nStatement: {fact.get('statement')}\n{_value_line(fact)}\n"
        f"Accepted spellings: {accept}\n"
        f"\nFiling evidence:\n{evidence}\n"
        f"\nAnswer to check:\n<<<\n{blind(answer)}\n>>>\n"
    )
    return safe_prompt(prompt), given


def parse_pass_p(text: str, passage_given: bool) -> dict[str, str]:
    """A strict pass P reply: exactly ``key_check``, ``verdict`` and ``reason`` (a non empty
    string); ``not_applicable`` exactly when no passage was given. Raises ValueError."""
    data = _loads(text)
    if not isinstance(data, dict) or set(data) != {"key_check", "verdict", "reason"}:
        raise ValueError("reply is not an object with key_check, verdict and reason only")
    key_check = _str(data["key_check"], "key_check").strip().lower()
    verdict = _str(data["verdict"], "verdict").strip().lower()
    reason = _str(data["reason"], "reason").strip()
    if key_check not in KEY_CHECKS:
        raise ValueError(f"bad key_check {key_check!r}")
    if verdict not in VERDICTS:
        raise ValueError(f"bad verdict {verdict!r}")
    if not reason:
        raise ValueError("reason is empty")
    if (key_check == NO_PASSAGE) == passage_given:
        raise ValueError(
            f"key_check {key_check!r} with {'a' if passage_given else 'no'} passage given"
        )
    return {"key_check": key_check, "verdict": verdict, "reason": reason}


def agrees(result: Mapping[str, Any] | None, effective: str) -> bool:
    """The section 16.3 agreement rule: a pass P result equal to the effective judge verdict
    with a ``supported`` or ``not_applicable`` key check. A failed pre check never agrees."""
    if not result:
        return False
    return result.get("verdict") == effective and result.get("key_check") in AGREEING_KEY_CHECKS


def row_key(run_id: str, fact_id: str) -> str:
    """The file stem of a row's record under ``<out>/precheck``."""
    return f"{run_id}__{fact_id}"


def evidence_cells(
    fact: Mapping[str, Any], facts: Mapping[str, Mapping[str, Any]]
) -> list[tuple[str | None, str]]:
    """(Mosaic permalink or None, text) pairs naming a row's filing evidence for the human
    review list: the fact's permalink and quote; a computed fact's formula and each component's;
    an absence fact's absent terms and search record."""
    kind = fact.get("kind")
    if kind == "absence":
        terms = "; ".join(map(str, fact.get("absent_terms") or []))
        return [(None, f"absent terms: {terms}. {fact.get('absence_reason') or ''}".strip())]
    if kind == "computed":
        out: list[tuple[str | None, str]] = [(None, f"formula {fact.get('formula')}")]
        for dep in _components(fact, facts):
            src = dep.get("source") or {}
            out.append((src.get("permalink"), f"{dep.get('id')}: {src.get('quote') or ''}"))
        return out
    src = fact.get("source") or {}
    return [(src.get("permalink"), str(src.get("quote") or ""))]
