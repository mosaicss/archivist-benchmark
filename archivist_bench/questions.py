"""Question set loading and validation (``validate-questions``)."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .model import BOOTSTRAP_SEED, CLAUDE_SUBSET_PER_STRATUM
from .redact import UPSTREAM_HOST_PATTERN

STRATA: tuple[str, ...] = (
    "lookup",
    "multi_hop_document",
    "multi_document",
    "multi_period",
    "breadth",
    "reach",
)
CONTROL_STRATUM = "control"
PER_STRATUM = 15
CONTROL_COUNT = 2
MIN_NON_US = 18
REACH_MIN_FILINGS = 20
REACH_MAX_FILINGS = 40
FACT_KINDS = frozenset({"sourced", "absence", "computed", "control"})

# Contamination stratum (pre registration section 14, amendment 3; harness 1.4.0).
CONTAMINATION_STRATUM = "contamination"
CONTAMINATION_SET = "contamination"
CONTAMINATION_MIN, CONTAMINATION_MAX = 16, 24
CONTAMINATION_MIN_TRAPS, CONTAMINATION_MAX_TRAPS = 12, 19
CONTAMINATION_CONTROLS = 5
CONTAMINATION_ROLES = ("trap", "control")
CONTAMINATION_FACT_KINDS = frozenset({"sourced", "trap"})
TRAP_SOURCE_CATEGORIES = ("forum", "aggregator")
# "Phrased as users ask: name the company and the period, not the document or form."
DOCUMENT_WORDS = re.compile(
    r"\b(?:form|10-k|10-q|8-k|20-f|40-f|6-k|annual report|quarterly report|filed|filing|"
    r"filings|accession|prospectus|proxy statement)\b",
    re.IGNORECASE,
)

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PERMALINK_RE = re.compile(
    r"^https://mosaic-finance\.com/filings/(?P<filing>[0-9a-f-]{36})/p/(?P<chunk>[0-9a-f-]{36})/"
    r"[a-z0-9]{1,16}\.[A-Za-z0-9_-]{43}/$"
)


def load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        raise ValueError(f"{path}: expected an object with a 'questions' list")
    return data


def _check_source(fid: str, fact: dict[str, Any], errors: list[str]) -> str | None:
    src = fact.get("source")
    if not isinstance(src, dict):
        errors.append(f"{fid}: sourced fact without source")
        return None
    filing = str(src.get("filing_uuid", ""))
    chunk = str(src.get("chunk_id", ""))
    if not UUID_RE.match(filing):
        errors.append(f"{fid}: source.filing_uuid missing or not a UUID")
    if not UUID_RE.match(chunk):
        errors.append(f"{fid}: source.chunk_id missing or not a UUID")
    match = PERMALINK_RE.match(str(src.get("permalink", "")))
    if not match:
        errors.append(f"{fid}: source.permalink is not a Mosaic passage permalink")
    elif match.group("filing") != filing or match.group("chunk") != chunk:
        errors.append(f"{fid}: permalink does not match filing_uuid and chunk_id")
    if not src.get("exchange_document_id") and not src.get("document_id_absent_reason"):
        errors.append(f"{fid}: exchange_document_id or document_id_absent_reason required")
    if not str(src.get("quote", "")).strip():
        errors.append(f"{fid}: source.quote required")
    return filing if UUID_RE.match(filing) else None


def is_contamination(data: dict[str, Any]) -> bool:
    """A contamination stratum set (``"set": "contamination"``, section 14)."""
    return data.get("set") == CONTAMINATION_SET


def validate(data: dict[str, Any], contamination: bool | None = None) -> list[str]:
    """Return every violation; an empty list means the set is acceptable. ``contamination``
    picks the mode (None: the set's own ``set`` field): the 92 question set of section 3, or
    the contamination stratum of section 14."""
    if contamination is None:
        contamination = is_contamination(data)
    if contamination:
        return validate_contamination(data)
    errors: list[str] = []
    questions: list[dict[str, Any]] = data["questions"]
    blob = json.dumps(data, ensure_ascii=False)
    for host in sorted({m.group(0).lower() for m in UPSTREAM_HOST_PATTERN.finditer(blob)}):
        errors.append(f"upstream host text present anywhere in the set: {host}")

    ids = Counter(str(q.get("id")) for q in questions)
    for qid, count in sorted(ids.items()):
        if count > 1:
            errors.append(f"{qid}: duplicate question id ({count} times)")

    strata = Counter(str(q.get("stratum")) for q in questions)
    for stratum in STRATA:
        if strata.get(stratum, 0) != PER_STRATUM:
            errors.append(
                f"stratum {stratum}: {strata.get(stratum, 0)} questions, expected {PER_STRATUM}"
            )
    if strata.get(CONTROL_STRATUM, 0) != CONTROL_COUNT:
        errors.append(
            f"controls: {strata.get(CONTROL_STRATUM, 0)} questions, expected {CONTROL_COUNT}"
        )
    unknown = sorted(set(strata) - set(STRATA) - {CONTROL_STRATUM})
    for stratum in unknown:
        errors.append(f"unknown stratum {stratum!r}")

    non_us = sum(1 for q in questions if "non_us" in (q.get("tags") or []))
    if non_us < MIN_NON_US:
        errors.append(f"non_us tagged questions: {non_us}, expected at least {MIN_NON_US}")

    for q in questions:
        qid = str(q.get("id"))
        if not str(q.get("question", "")).strip():
            errors.append(f"{qid}: empty question text")
        facts = q.get("facts")
        if not isinstance(facts, list) or not facts:
            errors.append(f"{qid}: no facts")
            continue
        fact_ids = [str(f.get("id")) for f in facts]
        for fid, count in Counter(fact_ids).items():
            if count > 1:
                errors.append(f"{fid}: duplicate fact id")
        filings: set[str] = set()
        is_control = q.get("stratum") == CONTROL_STRATUM
        for fact in facts:
            fid = str(fact.get("id"))
            kind = fact.get("kind")
            if kind not in FACT_KINDS:
                errors.append(f"{fid}: unknown fact kind {kind!r}")
                continue
            if not str(fact.get("statement", "")).strip() or "value" not in fact:
                errors.append(f"{fid}: statement and value required")
            if kind == "control":
                if not is_control:
                    errors.append(f"{fid}: control facts belong only to control questions")
                continue
            if is_control:
                errors.append(f"{fid}: control questions carry only control facts")
            if kind == "sourced":
                filing = _check_source(fid, fact, errors)
                if filing:
                    filings.add(filing)
            elif kind == "absence":
                src = fact.get("source") or {}
                filing = str(src.get("filing_uuid", ""))
                if not UUID_RE.match(filing):
                    errors.append(f"{fid}: absence fact without source.filing_uuid")
                else:
                    filings.add(filing)
                if not str(fact.get("absence_reason", "")).strip():
                    errors.append(f"{fid}: absence fact without absence_reason")
                if not fact.get("absent_terms"):
                    errors.append(f"{fid}: absence fact without absent_terms")
            elif kind == "computed":
                deps = fact.get("computed_from") or []
                if not fact.get("formula") or not deps:
                    errors.append(f"{fid}: computed fact needs formula and computed_from")
                for dep in deps:
                    if dep not in fact_ids:
                        errors.append(f"{fid}: computed_from {dep} is not a fact of {qid}")
        if q.get("stratum") == "reach" and not (
            REACH_MIN_FILINGS <= len(filings) <= REACH_MAX_FILINGS
        ):
            errors.append(
                f"{qid}: reach question cites {len(filings)} filings, expected "
                f"{REACH_MIN_FILINGS} to {REACH_MAX_FILINGS}"
            )
    return errors


def scored_questions(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [q for q in data["questions"] if q.get("stratum") != CONTROL_STRATUM]


def claude_subset(data: dict[str, Any]) -> list[str]:
    """The fixed Claude Code control subset: 5 per stratum by seeded hash, plus every control.

    Deterministic and order independent: within each stratum the five ids with the smallest
    ``sha256("8105:<id>")`` are chosen.
    """
    chosen: list[str] = []
    for stratum in STRATA:
        ids = sorted(str(q["id"]) for q in data["questions"] if q.get("stratum") == stratum)
        ranked = sorted(
            ids, key=lambda i: hashlib.sha256(f"{BOOTSTRAP_SEED}:{i}".encode()).hexdigest()
        )
        chosen.extend(sorted(ranked[:CLAUDE_SUBSET_PER_STRATUM]))
    chosen.extend(
        sorted(str(q["id"]) for q in data["questions"] if q.get("stratum") == CONTROL_STRATUM)
    )
    return chosen


# Amendment 4 (pre registration section 15): the Claude Code contamination subset, within each
# role the ids with the smallest ``sha256("8105:<id>")``.
CLAUDE_CONTAMINATION_SUBSET = {"trap": 6, "control": 2}


def claude_contamination_subset(data: dict[str, Any]) -> list[str]:
    """The fixed Claude Code contamination subset (section 15): the six trap and two control
    questions with the smallest ``sha256("8105:<id>")`` within their role, traps first, each
    role in id order. Deterministic and order independent."""
    chosen: list[str] = []
    for role, count in CLAUDE_CONTAMINATION_SUBSET.items():
        ids = [str(q["id"]) for q in data["questions"] if q.get("role") == role]
        ranked = sorted(
            ids, key=lambda i: hashlib.sha256(f"{BOOTSTRAP_SEED}:{i}".encode()).hexdigest()
        )
        chosen.extend(sorted(ranked[:count]))
    return chosen


def by_id(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(q["id"]): q for q in data["questions"]}


def validate_contamination(data: dict[str, Any]) -> list[str]:
    """The contamination stratum rules (pre registration section 14): ``"set":
    "contamination"``; 16 to 24 questions, all of stratum ``contamination`` with a ``role``
    (``trap`` or ``control``), 12 to 19 trap questions and exactly 5 controls; unique ids; each
    question names its cross check rows (``crosscheck_rows``), has no document or form words
    and at least one ``sourced`` fact with every per fact check of section 3 (filing UUID, chunk
    id, matching Mosaic permalink, exchange document id or absence reason, quote); trap facts
    (only in trap questions, at least one each) carry the wrong value, ``crosscheck_row``,
    ``source_category`` (forum or aggregator) and ``contradicts`` (a sourced fact of the
    question) with a value different from that fact's; no upstream filing host anywhere."""
    errors: list[str] = []
    questions: list[dict[str, Any]] = data["questions"]
    if data.get("set") != CONTAMINATION_SET:
        errors.append(f'set must be "{CONTAMINATION_SET}"')
    blob = json.dumps(data, ensure_ascii=False)
    for host in sorted({m.group(0).lower() for m in UPSTREAM_HOST_PATTERN.finditer(blob)}):
        errors.append(f"upstream host text present anywhere in the set: {host}")
    if not CONTAMINATION_MIN <= len(questions) <= CONTAMINATION_MAX:
        errors.append(
            f"{len(questions)} questions, expected {CONTAMINATION_MIN} to {CONTAMINATION_MAX}"
        )
    ids = Counter(str(q.get("id")) for q in questions)
    for qid, count in sorted(ids.items()):
        if count > 1:
            errors.append(f"{qid}: duplicate question id ({count} times)")
    roles = Counter(str(q.get("role")) for q in questions)
    if not CONTAMINATION_MIN_TRAPS <= roles.get("trap", 0) <= CONTAMINATION_MAX_TRAPS:
        errors.append(
            f"trap questions: {roles.get('trap', 0)}, expected {CONTAMINATION_MIN_TRAPS} to "
            f"{CONTAMINATION_MAX_TRAPS}"
        )
    if roles.get("control", 0) != CONTAMINATION_CONTROLS:
        errors.append(
            f"control questions: {roles.get('control', 0)}, expected {CONTAMINATION_CONTROLS}"
        )
    for q in questions:
        qid = str(q.get("id"))
        role = q.get("role")
        if q.get("stratum") != CONTAMINATION_STRATUM:
            errors.append(f"{qid}: stratum must be {CONTAMINATION_STRATUM}")
        if role not in CONTAMINATION_ROLES:
            errors.append(f"{qid}: role must be trap or control, not {role!r}")
        text = str(q.get("question", ""))
        if not text.strip():
            errors.append(f"{qid}: empty question text")
        for word in sorted({m.group(0).lower() for m in DOCUMENT_WORDS.finditer(text)}):
            errors.append(f"{qid}: question names the document or form ({word!r})")
        rows = q.get("crosscheck_rows")
        if not isinstance(rows, list) or not rows or not all(str(r).strip() for r in rows):
            errors.append(f"{qid}: crosscheck_rows (the cross check rows it comes from) required")
        facts = q.get("facts")
        if not isinstance(facts, list) or not facts:
            errors.append(f"{qid}: no facts")
            continue
        fact_ids = [str(f.get("id")) for f in facts]
        for fid, count in Counter(fact_ids).items():
            if count > 1:
                errors.append(f"{fid}: duplicate fact id")
        sourced = {str(f.get("id")) for f in facts if f.get("kind") == "sourced"}
        if not sourced:
            errors.append(f"{qid}: at least one sourced fact required")
        traps = 0
        for fact in facts:
            fid = str(fact.get("id"))
            kind = fact.get("kind")
            if kind not in CONTAMINATION_FACT_KINDS:
                errors.append(f"{fid}: fact kind {kind!r} is not sourced or trap")
                continue
            if not str(fact.get("statement", "")).strip() or "value" not in fact:
                errors.append(f"{fid}: statement and value required")
            if kind == "sourced":
                _check_source(fid, fact, errors)
                continue
            traps += 1
            if role != "trap":
                errors.append(f"{fid}: trap facts belong only to trap questions")
            if not str(fact.get("value", "")).strip():
                errors.append(f"{fid}: trap fact needs the wrong web value")
            if not str(fact.get("crosscheck_row", "")).strip():
                errors.append(f"{fid}: trap fact needs its crosscheck_row")
            if fact.get("source_category") not in TRAP_SOURCE_CATEGORIES:
                errors.append(f"{fid}: trap source_category must be forum or aggregator")
            if str(fact.get("contradicts")) not in sourced:
                errors.append(f"{fid}: contradicts must name a sourced fact of {qid}")
            else:
                filed = next(f for f in facts if str(f.get("id")) == str(fact["contradicts"]))
                if _bare(fact.get("value")) == _bare(filed.get("value")):
                    errors.append(f"{fid}: the trap value equals the filing value it contradicts")
            if fact.get("source"):
                errors.append(f"{fid}: a trap fact has no filing source (never scored)")
        if role == "trap" and not traps:
            errors.append(f"{qid}: a trap question needs at least one trap fact")
    return errors


def _bare(value: Any) -> str:
    """A value folded (``grounding.fold``) with every whitespace removed."""
    from .grounding import fold

    return "".join(fold(str(value or "")).split())


def validate_crosscheck_rows(
    data: dict[str, Any],
    checks: dict[str, dict[str, Any]],
    sample: dict[str, dict[str, Any]],
) -> list[str]:
    """Every ``crosscheck_rows`` id of a contamination set must be a row of either sample
    (``sample``: the 400 claim sample and the G4 extension) and a pass C result with status ok: ``contradicted`` for a trap question, ``confirmed`` for a control.
    Each trap fact is bound to its row: its ``crosscheck_row`` is one of the question's rows, a
    sampled row whose pass C result is ok and ``contradicted``, with the same source category
    and the same value (folded, whitespace removed) as the sampled claim."""
    errors: list[str] = []
    for q in data["questions"]:
        qid = str(q.get("id"))
        rows = [str(r) for r in q.get("crosscheck_rows") or []]
        wanted = "contradicted" if q.get("role") == "trap" else "confirmed"
        for row in rows:
            if row not in sample:
                errors.append(
                    f"{qid}: cross check row {row} is in neither sample.json nor "
                    "sample-extension.json"
                )
            result = checks.get(row)
            if result is None or result.get("status") != "ok":
                errors.append(f"{qid}: cross check row {row} has no valid pass C result")
            elif result.get("bin") != wanted:
                errors.append(
                    f"{qid}: cross check row {row} is {result.get('bin')}, expected {wanted}"
                )
        for fact in q.get("facts") or []:
            if fact.get("kind") != "trap":
                continue
            fid, row = str(fact.get("id")), str(fact.get("crosscheck_row"))
            claim, result = sample.get(row), checks.get(row) or {}
            if row not in rows:
                errors.append(f"{fid}: crosscheck_row {row} is not one of {qid}'s crosscheck_rows")
            if claim is None:
                errors.append(f"{fid}: crosscheck_row {row} is not a sampled row")
                continue
            if result.get("status") != "ok" or result.get("bin") != "contradicted":
                errors.append(f"{fid}: crosscheck_row {row} is not a valid contradicted row")
            if fact.get("source_category") != claim.get("category"):
                errors.append(
                    f"{fid}: source_category {fact.get('source_category')} is not the row's "
                    f"{claim.get('category')}"
                )
            if _bare(fact.get("value")) != _bare(claim.get("value")):
                errors.append(f"{fid}: value is not the value of cross check row {row}")
    return errors


# Section 3 quote normalization: non breaking spaces and repeated whitespace collapsed, curly
# quotes and markdown table characters normalized, case ignored.
_QUOTE_CHARS = str.maketrans(
    {
        "\u00a0": " ",
        "\u202f": " ",
        "\u2009": " ",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "|": " ",
    }
)


def normalize_quote(text: str) -> str:
    return " ".join(text.translate(_QUOTE_CHARS).split()).lower()


def verify_quote(fact: dict[str, Any], passage: dict[str, Any]) -> list[str]:
    """Problems of one sourced fact against its stored passage (``archivist read passage
    <chunk> --window 0``): the quote must occur in the passage text after the section 3
    normalization, the passage's filing and permalink must match the fact's source, and the
    document id must agree: a passage with an exchange document id needs the same id on the fact
    (no absence reason), a passage without one needs an absence reason and no id."""
    src = fact.get("source") or {}
    fid = str(fact.get("id"))
    problems: list[str] = []
    text = str(passage.get("text") or passage.get("content") or passage.get("snippet") or "")
    if not text:
        problems.append(f"{fid}: passage {src.get('chunk_id')} has no text")
    elif normalize_quote(str(src.get("quote", ""))) not in normalize_quote(text):
        problems.append(f"{fid}: quote not found in the stored chunk text")
    if str(passage.get("id")) != str(src.get("chunk_id")):
        problems.append(f"{fid}: passage id {passage.get('id')} is not chunk {src.get('chunk_id')}")
    if str(passage.get("filing_id")) != str(src.get("filing_uuid")):
        problems.append(f"{fid}: passage filing {passage.get('filing_id')} is not the fact's")
    if passage.get("url") != src.get("permalink"):
        problems.append(f"{fid}: permalink differs from the passage url")
    stored = passage.get("exchange_document_id")
    if stored:
        if src.get("exchange_document_id") != stored or src.get("document_id_absent_reason"):
            problems.append(
                f"{fid}: the passage carries exchange document id {stored}; the fact must carry "
                "the same id and no absence reason"
            )
    elif src.get("exchange_document_id") or not src.get("document_id_absent_reason"):
        problems.append(
            f"{fid}: the passage carries no exchange document id; the fact must give an absence "
            "reason and no id"
        )
    return problems


# --- Amendment 5 (pre registration section 16, harness 1.6.0): key corrections -----------------

MAIN_SET = "main"
CORRECTION_SETS = (MAIN_SET, CONTAMINATION_SET)
# The fields a key correction may replace; every other field and fact stays as frozen.
CORRECTION_FIELDS = ("statement", "value", "unit", "accept", "source")


class CorrectionError(ValueError):
    """A key corrections file that cannot be applied; nothing is applied."""


def question_set(data: dict[str, Any]) -> str:
    """The set a loaded question file is: ``contamination`` (``"set": "contamination"``) or
    ``main`` (no ``set`` field, or any other value)."""
    return CONTAMINATION_SET if is_contamination(data) else MAIN_SET


def load_key_corrections(path: Path) -> dict[str, Any]:
    """A key corrections file (``{"corrections": [...]}``); CorrectionError when unreadable."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorrectionError(f"{path}: not a readable JSON file ({exc})") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("corrections"), list):
        raise CorrectionError(f"{path}: expected an object with a 'corrections' list")
    return doc


def _check_also_stated(label: str, entries: Any, errors: list[str]) -> None:
    if entries is None:
        return
    if not isinstance(entries, list):
        errors.append(f"{label}: also_stated must be a list")
        return
    for n, entry in enumerate(entries, start=1):
        where = f"{label}: also_stated {n}"
        if not isinstance(entry, dict):
            errors.append(f"{where} is not an object")
            continue
        chunk = str(entry.get("chunk_id", ""))
        if not UUID_RE.match(chunk):
            errors.append(f"{where}: chunk_id missing or not a UUID")
        match = PERMALINK_RE.match(str(entry.get("permalink", "")))
        if not match:
            errors.append(f"{where}: permalink is not a Mosaic passage permalink")
        elif match.group("chunk") != chunk:
            errors.append(f"{where}: permalink does not match chunk_id")
        if not str(entry.get("quote", "")).strip():
            errors.append(f"{where}: quote required")


def _check_fields(label: str, fields: Any, errors: list[str]) -> None:
    if not isinstance(fields, dict) or not fields:
        errors.append(f"{label}: fields must be a non empty object")
        return
    for name in sorted(set(fields) - set(CORRECTION_FIELDS)):
        errors.append(
            f"{label}: field {name!r} cannot be corrected (allowed: {', '.join(CORRECTION_FIELDS)})"
        )
    if "statement" in fields and not (
        isinstance(fields["statement"], str) and fields["statement"].strip()
    ):
        errors.append(f"{label}: statement must be a non empty string")
    if "value" in fields and not isinstance(fields["value"], str):
        errors.append(f"{label}: value must be a string")
    if "unit" in fields and not isinstance(fields["unit"], str | None):
        errors.append(f"{label}: unit must be a string")
    accept = fields.get("accept")
    if "accept" in fields and not (
        isinstance(accept, list) and all(isinstance(a, str) and a.strip() for a in accept)
    ):
        errors.append(f"{label}: accept must be a list of non empty strings")
    if "source" in fields and not isinstance(fields["source"], dict):
        errors.append(f"{label}: source must be an object")


def key_corrections_for(data: dict[str, Any], doc: dict[str, Any]) -> list[dict[str, Any]]:
    """The corrections of ``doc`` that apply to the loaded question file ``data`` (their
    ``set`` is the file's set, ``question_set``), in file order, after validating every
    correction: a known set, a non empty ``fields`` object naming only ``CORRECTION_FIELDS``,
    no fact corrected twice, well formed ``also_stated`` entries, and, for the applicable
    ones, a known question and a fact of that question. Raises CorrectionError naming every
    problem, so a bad file applies nothing."""
    errors: list[str] = []
    wanted = question_set(data)
    lookup = by_id(data)
    seen: Counter[tuple[str, str]] = Counter()
    chosen: list[dict[str, Any]] = []
    corrections = doc.get("corrections")
    if not isinstance(corrections, list):
        raise CorrectionError("expected an object with a 'corrections' list")
    for n, item in enumerate(corrections, start=1):
        if not isinstance(item, dict):
            errors.append(f"correction {n} is not an object")
            continue
        set_name = item.get("set")
        qid, fid = str(item.get("question_id", "")), str(item.get("fact_id", ""))
        label = f"correction {n} ({fid or '?'})"
        if set_name not in CORRECTION_SETS:
            errors.append(f"{label}: set must be one of {', '.join(CORRECTION_SETS)}")
            continue
        if not qid or not fid:
            errors.append(f"{label}: question_id and fact_id required")
            continue
        seen[(str(set_name), fid)] += 1
        if seen[(str(set_name), fid)] == 2:
            errors.append(f"{label}: duplicate correction of {fid} in set {set_name}")
        _check_fields(label, item.get("fields"), errors)
        _check_also_stated(label, item.get("also_stated"), errors)
        if set_name != wanted:
            continue
        question = lookup.get(qid)
        if question is None:
            errors.append(f"{label}: unknown question {qid} in the {wanted} set")
            continue
        if fid not in {str(f.get("id")) for f in question.get("facts") or []}:
            errors.append(f"{label}: unknown fact {fid} of {qid}")
            continue
        chosen.append(item)
    if errors:
        raise CorrectionError("; ".join(errors))
    return chosen


def apply_key_corrections(data: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    """A deep copy of ``data`` with the named fields of every applicable correction
    (``key_corrections_for``) replaced; every other field and fact is unchanged and ``data``
    itself is never modified. Raises CorrectionError (nothing applied)."""
    chosen = key_corrections_for(data, doc)
    out = copy.deepcopy(data)
    lookup = by_id(out)
    for item in chosen:
        question = lookup[str(item["question_id"])]
        fact = next(f for f in question["facts"] if str(f.get("id")) == str(item["fact_id"]))
        for name, value in item["fields"].items():
            fact[name] = copy.deepcopy(value)
    return out


def corrections_digest(chosen: Sequence[dict[str, Any]]) -> str:
    """sha256 over the applied corrections' set, question, fact and fields (canonical JSON):
    a re-judge is bound to it; editing a correction's reason or ruling does not change it."""
    body = [
        {k: item.get(k) for k in ("set", "question_id", "fact_id", "fields")} for item in chosen
    ]
    text = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def verify_also_stated(fid: str, entry: dict[str, Any], passage: dict[str, Any]) -> list[str]:
    """Problems of one ``also_stated`` quote of a key correction against its stored passage:
    the quote must occur in the passage text (section 3 normalization) and the passage must be
    the entry's chunk at its permalink."""
    chunk = str(entry.get("chunk_id"))
    problems: list[str] = []
    text = str(passage.get("text") or passage.get("content") or passage.get("snippet") or "")
    if not text:
        problems.append(f"{fid}: also stated passage {chunk} has no text")
    elif normalize_quote(str(entry.get("quote", ""))) not in normalize_quote(text):
        problems.append(f"{fid}: also stated quote not found in chunk {chunk}")
    if str(passage.get("id")) != chunk:
        problems.append(f"{fid}: passage id {passage.get('id')} is not chunk {chunk}")
    if passage.get("url") != entry.get("permalink"):
        problems.append(f"{fid}: also stated permalink differs from the passage url")
    match = PERMALINK_RE.match(str(entry.get("permalink", "")))
    if match and str(passage.get("filing_id")) != match.group("filing"):
        problems.append(f"{fid}: also stated passage filing is not the permalink's filing")
    return problems
