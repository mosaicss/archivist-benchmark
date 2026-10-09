"""Grounding and source provenance (pre registration section 14, amendment 3; the Claude Code
stream rules of section 15, amendment 4).

Deterministic layers over the Codex rollouts (or Claude Code streams) of a campaign plus Codex
grading passes:

- Sources: every source a run's agent was shown by its search tool (``shown``), opened, found
  in or clicked (``opened``) or read through Archivist (``archivist``), with the text it read;
  for Claude Code also the WebSearch tool's own summary (``summary``, host
  ``web-search-summary``, category ``other``: outside exposure, inside attribution).
- Categories: one per host (filing, issuer, forum, promotional, aggregator, news, reference,
  other), from the frozen seed table, the upstream filing host pattern and pass H.
- Exposure: per run category counts of distinct shown and opened sources.
- Reliance: pass R lists the claims of an answer; a deterministic rule attributes each fact
  claim to filing, secondary (by category) or unattributed sources read in the same run.
- Grading passes (H hosts, R answer claims, X forum and aggregator claims, T traps) run on the
  Codex judge configuration (``judge.JUDGES["B"]``) on the ChatGPT plan allowance, behind the
  pre launch plan gate, with one retry of a malformed reply and a strict parser.

Every grader prompt is free of upstream filing hosts (``safe_prompt``). Nothing here decides a
hypothesis; every measure over the codex campaign data is exploratory.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import random
import re
import threading
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from statistics import fmean
from typing import Any

from . import claude_code, codex, judge, probe
from .model import BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED, flatten_text
from .redact import (
    MARKDOWN_LINK,
    UPSTREAM_HOST_PATTERN,
    UPSTREAM_PLACEHOLDER,
    URL_PATTERN,
    blind,
    find_upstream_hosts,
    host_of,
    redact_links,
)
from .runner import _now
from .stats import percentile

# --- categories (pre registration section 14) ----------------------------------------------------

# Precedence order when a host fits several categories.
CATEGORIES: tuple[str, ...] = (
    "filing",
    "issuer",
    "forum",
    "promotional",
    "aggregator",
    "news",
    "reference",
    "other",
)
POOL_CATEGORIES: tuple[str, ...] = ("forum", "aggregator")
# Pass H never returns ``filing``: filing hosts are the upstream pattern (and Mosaic) only.
PASS_H_CATEGORIES: tuple[str, ...] = tuple(c for c in CATEGORIES if c != "filing")
SEED_HOSTS: dict[str, tuple[str, ...]] = {
    "issuer": (
        "businesswire.com",
        "prnewswire.com",
        "globenewswire.com",
        "accesswire.com",
        "newsfilecorp.com",
        "publicnow.com",
    ),
    "reference": (
        "wikipedia.org",
        "wikidata.org",
        "britannica.com",
        "investopedia.com",
        "nyse.com",
        "borsaistanbul.com",
    ),
    "news": (
        "reuters.com",
        "bloomberg.com",
        "wsj.com",
        "ft.com",
        "cnbc.com",
        "apnews.com",
        "marketwatch.com",
        "barrons.com",
        "theglobeandmail.com",
        "financialpost.com",
        "aa.com.tr",
        "bloomberght.com",
        "bnnbloomberg.ca",
        "nytimes.com",
        "bbc.com",
    ),
    "aggregator": (
        "finance.yahoo.com",
        "nasdaq.com",
        "investing.com",
        "marketscreener.com",
        "stockanalysis.com",
        "macrotrends.net",
        "companiesmarketcap.com",
        "simplywall.st",
        "zacks.com",
        "seekingalpha.com",
        "morningstar.com",
        "wisesheets.io",
        "gurufocus.com",
        "tipranks.com",
        "marketbeat.com",
        "fintel.io",
        "fool.com",
    ),
    "forum": (
        "reddit.com",
        "stocktwits.com",
        "x.com",
        "twitter.com",
        "hotcopper.com.au",
        "investorshub.com",
        "quora.com",
        "eksisozluk.com",
        "medium.com",
        "substack.com",
        "youtube.com",
    ),
}
# Archivist results reach answers as Mosaic passage permalinks: filing passages through Mosaic.
MOSAIC_HOST = "mosaic-finance.com"
UNKNOWN_HOST = "unknown"
# Claude Code's WebSearch returns links without snippets plus a summary its own model wrote: a
# ``summary`` source of this pseudo host, category ``other`` without pass H (section 15).
SUMMARY_HOST = "web-search-summary"


def normalize_host(host: str | None) -> str:
    """Lowercase, no port, no trailing dot, no leading ``www.``; empty is ``unknown``."""
    text = (host or "").strip().lower().rstrip(".")
    text = text.split(":", 1)[0]
    if text.startswith("www."):
        text = text[4:]
    return text or UNKNOWN_HOST


def _suffix_match(host: str, seed: str) -> bool:
    return host == seed or host.endswith("." + seed)


def seed_category(host: str) -> tuple[str, str] | None:
    """(category, reason) for a host the frozen table settles without pass H: an upstream
    filing host (pattern), Mosaic (Archivist results), a seed host (exact or subdomain suffix),
    the ``unknown`` host of a result without URL or domain, or the Claude Code WebSearch
    summary pseudo host (``other``, never sent to pass H)."""
    host = normalize_host(host)
    if host == UNKNOWN_HOST:
        return "other", "no URL or domain"
    if host == SUMMARY_HOST:
        return "other", "the WebSearch tool's own summary (not a web page)"
    if UPSTREAM_HOST_PATTERN.search(host):
        return "filing", "regulator filing system (upstream host pattern)"
    if _suffix_match(host, MOSAIC_HOST):
        return "filing", "Archivist results (filing passages through Mosaic)"
    for category in CATEGORIES:
        for seed in SEED_HOSTS.get(category, ()):
            if _suffix_match(host, seed):
                return category, f"seed host {seed}"
    return None


def display_host(host: str) -> str:
    """A host as it may appear in a committed file: filing hosts become ``filing-source``."""
    return UPSTREAM_PLACEHOLDER if UPSTREAM_HOST_PATTERN.search(host) else host


class HostTable:
    """Host to category: the seed table first, then the pass H table (``host-table.json``);
    a host in neither is ``other`` and listed under ``unclassified``."""

    def __init__(self, pass_h: dict[str, dict[str, Any]] | None = None) -> None:
        self.pass_h = pass_h or {}
        self.unclassified: set[str] = set()

    @classmethod
    def load(cls, out: Path) -> HostTable:
        path = out / HOST_TABLE
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        return cls(data if isinstance(data, dict) else {})

    def lookup(self, host: str) -> tuple[str, str, str]:
        """(category, basis, reason); basis ``seed``, ``pass_h`` or ``unclassified``."""
        host = normalize_host(host)
        seeded = seed_category(host)
        if seeded is not None:
            return seeded[0], "seed", seeded[1]
        entry = self.pass_h.get(host)
        if isinstance(entry, dict) and entry.get("status") == "ok":
            return str(entry["category"]), "pass_h", str(entry.get("reason") or "")
        self.unclassified.add(host)
        return "other", "unclassified", "not in the seed table and not classified by pass H"

    def category(self, host: str) -> str:
        return self.lookup(host)[0]


def by_precedence(categories: Iterable[str]) -> str | None:
    found = [c for c in categories if c in CATEGORIES]
    return min(found, key=CATEGORIES.index) if found else None


# --- text normalization --------------------------------------------------------------------------

QUOTE_MAP = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        " ": " ",
        " ": " ",
        " ": " ",
        "ı": "i",
    }
)


def fold(text: str) -> str:
    """Case and accent insensitive form: NFKD without combining marks, casefolded, curly quotes
    straight, every whitespace run one space."""
    text = unicodedata.normalize("NFKD", text.translate(QUOTE_MAP))
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    return " ".join(text.translate(QUOTE_MAP).split())


# --- source parsing (I/O rows 1 to 3) ------------------------------------------------------------

CITE = re.compile("cite((?:[^]+)+)")
BLOCK_SEPARATOR = re.compile(r"\n-{20,}\n")
HEADER = re.compile(r"^(?P<title>.*?)\s*\((?P<url>https?://[^\s()]+(?:\([^\s()]*\)[^\s()]*)*)\)$")
TOTAL_LINES = re.compile(r"^Total lines: \d+$")
FILING_ID = re.compile(
    r'"filing_id"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"'
)
OPEN_ACTIONS = ("openPage", "findInPage", "other")


@dataclass
class Block:
    """One exec output block: ``TITLE (URL)``, the cite marker, then the text read."""

    title: str
    url: str | None
    refs: list[str]
    text: str


def parse_blocks(text: str) -> list[Block]:
    """The web tool's output blocks in an exec output (blocks without a cite marker, such as
    an Archivist result printed by code mode, are not web blocks)."""
    blocks: list[Block] = []
    for chunk in BLOCK_SEPARATOR.split(text):
        marker = CITE.search(chunk)
        if marker is None:
            continue
        refs = [r for r in marker.group(1).split("") if r]
        before = chunk[: marker.start()].rstrip("\n").split("\n")
        header = before[-1].strip() if before else ""
        match = HEADER.match(header)
        title, url = (match.group("title"), match.group("url")) if match else (header, None)
        blocks.append(Block(title.strip(), url, refs, chunk[marker.end() :].strip()))
    return blocks


@dataclass
class Source:
    """One distinct source of a run. ``segments`` are ``[order, text]`` pairs: the text the
    model read, with its position in the rollout (record index)."""

    kind: str  # shown | opened | archivist | summary (Claude Code WebSearch summary)
    key: str
    url: str | None
    host: str
    title: str = ""
    snippet: str = ""
    refs: list[str] = field(default_factory=list)
    order: int = 0
    segments: list[list[Any]] = field(default_factory=list)
    filing_id: str | None = None
    action: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Source:
        names = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in names})


def _ref_kind(ref: str | None, action: str) -> str:
    """``opened`` for a ``view`` ref or a non search action, else ``shown``."""
    return "opened" if (ref and "view" in ref) or action != "search" else "shown"


MAX_PROMPT_COMPANIES = 20


def companies_text(companies: Sequence[Any]) -> str:
    """At most 20 tickers for a prompt, then how many more."""
    names = [str(c) for c in companies]
    shown = ", ".join(names[:MAX_PROMPT_COMPANIES]) or "none"
    more = len(names) - MAX_PROMPT_COMPANIES
    return f"{shown} and {more} more" if more > 0 else shown


class _Collector:
    def __init__(self) -> None:
        self.sources: dict[tuple[str, str], Source] = {}
        self.by_ref: dict[str, tuple[str, str]] = {}

    def add(
        self,
        kind: str,
        key: str,
        url: str | None,
        host: str,
        order: int,
        **extra: Any,
    ) -> Source:
        found = self.sources.get((kind, key))
        if found is None:
            found = Source(kind, key, url, normalize_host(host), order=order, **extra)
            self.sources[(kind, key)] = found
        else:
            found.order = min(found.order, order)
            for name in ("title", "snippet", "action"):
                if extra.get(name) and not getattr(found, name):
                    setattr(found, name, extra[name])
            if url and not found.url:
                found.url = url
        return found


def parse_sources(lines: Iterable[str]) -> list[Source]:
    """Every source of one Codex rollout, in first seen order.

    - Search results (``web.search`` action ``search``): one ``shown`` source per distinct URL
      (host from ``domain``, else the URL; neither: ``unknown``), with title, snippet and ref.
    - Opened pages (action ``openPage``, ``findInPage`` or ``other``, the action URL and every
      result; a result whose ref contains ``view``): one ``opened`` source per distinct URL.
    - Exec output blocks ``TITLE (URL)`` plus a cite marker: the block text attaches to the
      source of its ref (search refs to their shown source, ``view`` refs to the opened
      page); a block whose ref is unknown makes its own source (``unknown`` host without URL).
    - Archivist (``McpToolCall`` server ``archivist``, completed, not an error): one
      ``archivist`` source per distinct ``filing_id`` in the result (else one per call), the
      result text as its text. A failed call is not a source.
    """
    records = list(codex._json_lines(lines))
    col = _Collector()
    for order, rec in enumerate(records):
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        assert isinstance(payload, dict)
        if rec.get("type") != "event_msg" or payload.get("type") != "item_completed":
            continue
        item = payload.get("item") or {}
        if not isinstance(item, dict):
            continue
        if item.get("type") == "Extension" and item.get("kind") == "web.search":
            action = item.get("action") or {}
            name = str(action.get("type") or "other")
            if name in ("openPage", "findInPage") and isinstance(action.get("url"), str):
                page = str(action["url"])
                col.add("opened", page, page, host_of(page), order, action=name)
            for index, res in enumerate(item.get("results") or []):
                if not isinstance(res, dict):
                    continue
                url = res.get("url") if isinstance(res.get("url"), str) else None
                ref = str(res["ref_id"]) if res.get("ref_id") else None
                kind = _ref_kind(ref, name)
                host = str(res.get("domain") or "") or (host_of(url) if url else "")
                key = url or (f"ref:{ref}" if ref else f"item:{order}:{index}")
                snippet = str(res.get("snippet") or "")
                if TOTAL_LINES.match(snippet.strip()):
                    snippet = ""
                source = col.add(
                    kind,
                    key,
                    url,
                    host,
                    order,
                    title=str(res.get("title") or ""),
                    snippet=snippet,
                    action=name,
                )
                if ref and ref not in source.refs:
                    source.refs.append(ref)
                    col.by_ref[ref] = (kind, key)
                if snippet and kind == "shown":
                    source.segments.append([order, snippet])
        elif item.get("type") == "McpToolCall" and item.get("server") == codex.ARCHIVIST_SERVER:
            result = item.get("result")
            failed = item.get("status") != "completed" or (
                isinstance(result, dict) and bool(result.get("isError") or result.get("is_error"))
            )
            if failed:
                continue
            text = flatten_text(result)
            ids = list(dict.fromkeys(FILING_ID.findall(text)))
            keys = ids or [f"call:{item.get('id') or order}"]
            for key in keys:
                source = col.add(
                    "archivist",
                    key,
                    None,
                    MOSAIC_HOST,
                    order,
                    filing_id=key if key in ids else None,
                    action=str(item.get("tool")),
                )
                source.segments.append([order, text])
    for order, rec in enumerate(records):
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        assert isinstance(payload, dict)
        if rec.get("type") != "response_item" or payload.get("type") not in (
            "custom_tool_call_output",
            "function_call_output",
        ):
            continue
        for block in parse_blocks(flatten_text(payload.get("output"))):
            ref = block.refs[0] if block.refs else None
            target = col.by_ref.get(ref) if ref else None
            kind = "opened" if ref and "view" in ref else "shown"
            if target is None and block.url and (kind, block.url) in col.sources:
                target = (kind, block.url)
            if target is not None:
                source = col.sources[target]
                if block.url and not source.url:
                    source.url = block.url
            else:
                key = block.url or f"ref:{ref or order}"
                host = host_of(block.url) if block.url else UNKNOWN_HOST
                source = col.add(kind, key, block.url, host, order, title=block.title)
                if ref:
                    source.refs.append(ref)
                    col.by_ref[ref] = (kind, key)
            if block.text:
                source.segments.append([order, block.text])
    return sorted(col.sources.values(), key=lambda s: (s.order, s.kind, s.key))


def rollout_problem(lines: Sequence[str]) -> str | None:
    """Why a rollout cannot back a sources file: no records, no ``task_complete`` or no
    usage (``codex.parse_rollout`` ``no_usage``); None when complete."""
    records = list(codex._json_lines(lines))
    if not records:
        return "empty rollout"
    if not any(
        r.get("type") == "event_msg" and (r.get("payload") or {}).get("type") == "task_complete"
        for r in records
    ):
        return "no task_complete in the rollout"
    if codex.parse_rollout(lines).usage is None:
        return "no usage in the rollout"
    return None


# --- Claude Code stream sources (pre registration section 15) -------------------------------------

WEB_SEARCH = "WebSearch"
WEB_FETCH = "WebFetch"
ARCHIVIST_PREFIX = f"mcp__{claude_code.ARCHIVIST_SERVER}__"
LINKS_MARKER = "Links: "
# Claude Code appends an instruction to every WebSearch result text; it is not the summary.
REMINDER = re.compile(r"\n\s*REMINDER:.*\Z", re.S)


def _part_text(content: Any) -> str:
    """The text of a ``tool_result`` content: the string itself, or its text parts joined."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            p if isinstance(p, str) else str(p.get("text") or "") if isinstance(p, dict) else ""
            for p in content
        ]
        return "\n".join(t for t in texts if t)
    return flatten_text(content)


def _summary(texts: Iterable[str]) -> str:
    """The WebSearch summary: the given texts without the trailing REMINDER, joined."""
    parts = [REMINDER.sub("", t).strip() for t in texts]
    return "\n\n".join(p for p in parts if p)


def search_text_links(text: str) -> tuple[list[dict[str, Any]], str] | None:
    """(links, summary) of a WebSearch ``tool_result`` text (``Web search results for query:
    "..."``, then ``Links: [{"title", "url"}...]``, then the summary): every parsable ``Links:``
    JSON array and the text after it (up to the next array) as the summary. None when no array
    parses (the caller then keeps the whole text as the summary)."""
    decoder = json.JSONDecoder()
    spans: list[tuple[int, int, list[Any]]] = []
    pos = 0
    while (start := text.find(LINKS_MARKER, pos)) >= 0:
        begin = start + len(LINKS_MARKER)
        try:
            value, end = decoder.raw_decode(text, begin)
        except json.JSONDecodeError:
            pos = begin
            continue
        if isinstance(value, list):
            spans.append((start, end, value))
        pos = end
    if not spans:
        return None
    links = [v for _s, _e, value in spans for v in value if isinstance(v, dict)]
    after = [
        text[end : spans[i + 1][0] if i + 1 < len(spans) else len(text)]
        for i, (_s, end, _v) in enumerate(spans)
    ]
    return links, _summary(after)


def _structured_search(
    event: dict[str, Any], single: bool
) -> tuple[list[dict[str, Any]], str] | None:
    """(links, summary) from a ``user`` event's top level ``tool_use_result`` (``{"query",
    "results": [{"tool_use_id", "content": [{"title", "url"}]}, "<summary>"]}``), used only
    when the event holds exactly one tool result (``single``): its entries name the server tool
    id (``srvtoolu_...``), never the call id, so with several results it belongs to none."""
    data = event.get("tool_use_result")
    if not single or not isinstance(data, dict) or not isinstance(data.get("results"), list):
        return None
    links: list[dict[str, Any]] = []
    for entry in data["results"]:
        content = entry.get("content") if isinstance(entry, dict) else None
        if isinstance(content, list):
            links += [c for c in content if isinstance(c, dict)]
    return links, _summary(e for e in data["results"] if isinstance(e, str))


def parse_claude_sources(lines: Iterable[str]) -> list[Source]:
    """Every source of one Claude Code ``stream-json`` transcript, in first seen order (the
    section 14.2 and 14.3 rules applied to Claude Code, section 15):

    - WebSearch results: one ``shown`` source per distinct link URL (title, host from the URL;
      Claude Code returns no per result snippet) and one ``summary`` source per call (host
      ``web-search-summary``, category ``other``) holding the tool's own summary text. The
      links and summary come from the event's structured ``tool_use_result`` when the event
      holds exactly one tool result, else from that call's own
      ``Links: [...]`` JSON and the text after it in the result text; an unparsable text gives
      no link sources and the whole text is the summary.
    - WebFetch results: one ``opened`` source per distinct URL (the tool_use input ``url``),
      the fetch tool's extract (the result text) as its text.
    - Archivist (``mcp__archivist__*``): one ``archivist`` source per distinct ``filing_id`` in
      the result (else one per call), the result text as its text.

    A result flagged ``is_error`` is not a source. The order is the stream event index."""
    records = list(claude_code._json_lines(lines))
    col = _Collector()
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    for order, event in enumerate(records):
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        assert isinstance(message, dict)
        parts = [p for p in message.get("content") or [] if isinstance(p, dict)]
        if event.get("type") == "assistant":
            for part in parts:
                if part.get("type") in ("tool_use", "server_tool_use") and part.get("id"):
                    inp = part.get("input") if isinstance(part.get("input"), dict) else {}
                    assert isinstance(inp, dict)
                    calls.setdefault(str(part["id"]), (str(part.get("name")), inp))
            continue
        if event.get("type") != "user":
            continue
        results = [p for p in parts if p.get("type") == "tool_result"]
        for part in results:
            call_id = str(part.get("tool_use_id"))
            name, inp = calls.get(call_id, ("", {}))
            if part.get("is_error") is True:
                continue
            text = _part_text(part.get("content"))
            if name == WEB_SEARCH:
                found = _structured_search(event, len(results) == 1)
                parsed = search_text_links(text)
                if found is None:
                    links, summary = parsed if parsed is not None else ([], _summary([text]))
                else:
                    links, summary = found
                    if not summary and parsed is not None:
                        summary = parsed[1]
                for index, link in enumerate(links):
                    url = link.get("url") if isinstance(link.get("url"), str) else None
                    col.add(
                        "shown",
                        url or f"link:{call_id}:{index}",
                        url,
                        host_of(url) if url else UNKNOWN_HOST,
                        order,
                        title=" ".join(str(link.get("title") or "").split()),
                        action=WEB_SEARCH,
                    )
                if summary:
                    source = col.add(
                        "summary",
                        f"summary:{call_id}",
                        None,
                        SUMMARY_HOST,
                        order,
                        action=WEB_SEARCH,
                    )
                    source.segments.append([order, summary])
            elif name == WEB_FETCH:
                url = inp.get("url") if isinstance(inp.get("url"), str) else None
                structured = event.get("tool_use_result")
                if url is None and isinstance(structured, dict):
                    url = structured.get("url") if isinstance(structured.get("url"), str) else None
                host = host_of(url) if url else UNKNOWN_HOST
                source = col.add("opened", url or f"call:{call_id}", url, host, order, action=name)
                if text:
                    source.segments.append([order, text])
            elif name.startswith(ARCHIVIST_PREFIX):
                ids = list(dict.fromkeys(FILING_ID.findall(text)))
                for key in ids or [f"call:{call_id}"]:
                    source = col.add(
                        "archivist",
                        key,
                        None,
                        MOSAIC_HOST,
                        order,
                        filing_id=key if key in ids else None,
                        action=name[len(ARCHIVIST_PREFIX) :],
                    )
                    source.segments.append([order, text])
    return sorted(col.sources.values(), key=lambda s: (s.order, s.kind, s.key))


def stream_problem(lines: Sequence[str]) -> str | None:
    """Why a Claude Code stream cannot back a sources file: no events, no ``result`` event, a
    result other than ``success``, or no usage (``claude_code.parse_stream``); None when
    complete."""
    records = list(claude_code._json_lines(lines))
    if not records:
        return "empty stream"
    results = [r for r in records if r.get("type") == "result"]
    if not results:
        return "no result event in the stream"
    last = results[-1]
    if last.get("subtype") != "success" or last.get("is_error"):
        return f"result not success ({last.get('subtype') or 'error'})"
    if claude_code.parse_stream(lines).usage is None:
        return "no usage in the stream"
    return None


AGENTS: tuple[str, ...] = ("codex", "claude_code")


def parse_agent_sources(agent: str, lines: Sequence[str]) -> list[Source]:
    """The sources of one run log: the Codex rollout or the Claude Code stream."""
    return parse_claude_sources(lines) if agent == "claude_code" else parse_sources(lines)


def log_problem(agent: str, lines: Sequence[str]) -> str | None:
    """Why a run log cannot back a sources file (``rollout_problem`` or ``stream_problem``)."""
    return stream_problem(lines) if agent == "claude_code" else rollout_problem(lines)


# --- answer links ----------------------------------------------------------------------------------


def tokenize_links(text: str) -> tuple[str, dict[str, str]]:
    """Every link of an answer replaced by a source token ``[S1]``..``[Sn]`` (one per distinct
    URL, first seen order); returns the text and the token to URL map (kept outside prompts)."""
    seen: dict[str, str] = {}

    def token(url: str) -> str:
        url = url.rstrip(".,;:")
        return seen.setdefault(url, f"[S{len(seen) + 1}]")

    text = MARKDOWN_LINK.sub(lambda m: f"{m.group(1)} {token(m.group(2))}", text)
    text = URL_PATTERN.sub(lambda m: token(m.group(0)), text)
    return text, {tok.strip("[]"): url for url, tok in seen.items()}


def question_companies(question: dict[str, Any]) -> list[str]:
    """The companies of a question: its ``companies`` field, else the tickers of its key."""
    named = question.get("companies")
    if isinstance(named, list) and named:
        return [str(c) for c in named]
    return sorted(
        {
            str((f.get("source") or {}).get("symbol"))
            for f in question.get("facts") or []
            if (f.get("source") or {}).get("symbol")
        }
    )


# --- grader prompts and parsers ------------------------------------------------------------------


def safe_prompt(text: str) -> str:
    """A grader prompt without any upstream filing URL or host: links of filing hosts become
    ``<url:filing-source>`` and bare host mentions ``filing-source``; a host that survives is
    a bug, so it raises."""
    text = UPSTREAM_HOST_PATTERN.sub(UPSTREAM_PLACEHOLDER, redact_links(text))
    hosts = find_upstream_hosts(text)
    if hosts:  # pragma: no cover - redact_links masks every pattern match
        raise ValueError(f"upstream host in a grader prompt: {hosts}")
    return text


def _loads(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned[cleaned.find("{") :]
    return json.loads(cleaned)


def _str(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _labels(prefix: str, n: int) -> list[str]:
    return [f"{prefix}{i}" for i in range(1, n + 1)]


def _strict_object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


STRING: dict[str, Any] = {"type": "string"}

# Pass H: hosts not in the seed table, once.
PASS_H_RUBRIC = """You classify web hosts by the kind of source they are, for a study of where research agents got their information.
Categories (one per host; when a host fits several, take the first in this order: issuer, forum, promotional, aggregator, news, reference, other):
- issuer: the issuer's own domains and press release wires carrying issuer authored releases.
- forum: user generated content without editorial review.
- promotional: content paid for or written to sell a security, subscription or product (sponsored posts, stock promotion, affiliate "best stocks" lists).
- aggregator: sites that republish, compute or summarize company financial data, transcripts or filings at scale, including contributor analysis platforms.
- news: journalism under editorial standards.
- reference: encyclopedias, exchanges, regulators' non filing pages, statistics agencies.
- other: anything else.
Each host comes with up to three page titles and snippets seen for it and the companies (tickers) of the research questions where it appeared. Classify the host as a whole.
Return only JSON: {"hosts": [{"id": "<host label>", "category": "<category>", "reason": "<under 20 words>"}]}. Include every host label exactly once."""

PASS_H_SCHEMA: dict[str, Any] = _strict_object(
    {
        "hosts": {
            "type": "array",
            "items": _strict_object(
                {
                    "id": STRING,
                    "category": {"type": "string", "enum": list(PASS_H_CATEGORIES)},
                    "reason": STRING,
                }
            ),
        }
    }
)
PASS_H_BATCH = 40
PASS_H_EXAMPLES = 3
REASON_WORDS = 19  # "under 20 words"


def pass_h_prompt(batch: Sequence[dict[str, Any]]) -> tuple[str, dict[str, str]]:
    """The pass H prompt for a batch of host inventory entries; returns it and the label map."""
    labels = dict(zip(_labels("H", len(batch)), (str(e["host"]) for e in batch), strict=True))
    lines = []
    for label, entry in zip(labels, batch, strict=True):
        lines.append(f"{label}: host {entry['host']}")
        for example in (entry.get("examples") or [])[:PASS_H_EXAMPLES]:
            title = " ".join(str(example.get("title") or "").split())[:200]
            snippet = " ".join(str(example.get("snippet") or "").split())[:300]
            lines.append(f"  - title: {title} | snippet: {snippet}")
        lines.append(f"  companies: {companies_text(entry.get('companies') or [])}")
    prompt = f"{PASS_H_RUBRIC}\n\nHosts:\n" + "\n".join(lines) + "\n"
    return safe_prompt(prompt), labels


def parse_pass_h(text: str, labels: Collection[str]) -> dict[str, dict[str, str]]:
    """Label to ``{"category", "reason"}``; every label exactly once, a known category, the
    reason clipped to 19 words (``reason_clipped`` notes it)."""
    data = _loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("hosts"), list):
        raise ValueError("reply is not an object with a hosts list")
    out: dict[str, dict[str, str]] = {}
    for item in data["hosts"]:
        if not isinstance(item, dict):
            raise ValueError(f"host entry is not an object: {item!r}")
        label = _str(item.get("id"), "id")
        category = _str(item.get("category"), "category")
        if label not in labels or category not in PASS_H_CATEGORIES:
            raise ValueError(f"bad host entry {item!r}")
        if label in out:
            raise ValueError(f"duplicate host label {label}")
        words = _str(item.get("reason"), "reason").split()
        entry = {"category": category, "reason": " ".join(words[:REASON_WORDS])}
        if len(words) > REASON_WORDS:
            entry["reason_clipped"] = "true"
        out[label] = entry
    if set(out) != set(labels):
        raise ValueError("reply does not cover every host exactly")
    return out


# Pass R: the claims of one answer.
PASS_R_RUBRIC = """You list the claims one research answer makes about companies. Links in the answer are replaced by source tokens [S1]..[Sn]; tool and site names are hidden.
Rules:
- List every factual claim about a company: a figure, a date, an entity or an event. For each give "type": "fact", "text" (the claim in the answer's words, at most 40 words), "values" (every figure and named entity of the claim as written in the answer, never a year alone), "sources" (the [S#] tokens the answer attaches to the claim, [] when none), "attributed_to": "none" and "phrase": "".
- List every evaluative statement: a judgment, characterization or recommendation. For each give "type": "evaluative", "text", "values": [], "sources", "attributed_to": "filer" when the answer presents it as the company's own statement, "third_party" when it attributes it to someone else, "none" when the answer makes it itself, and "phrase": its key phrase as written, at most six words.
- Do not judge whether a claim is true and do not add claims the answer does not make.
Return only JSON: {"claims": [{"type": "...", "text": "...", "values": ["..."], "sources": ["S1"], "attributed_to": "...", "phrase": "..."}]}; an empty list is valid."""

CLAIM_TYPES = ("fact", "evaluative")
ATTRIBUTIONS = ("filer", "third_party", "none")
PASS_R_SCHEMA: dict[str, Any] = _strict_object(
    {
        "claims": {
            "type": "array",
            "items": _strict_object(
                {
                    "type": {"type": "string", "enum": list(CLAIM_TYPES)},
                    "text": STRING,
                    "values": {"type": "array", "items": STRING},
                    "sources": {"type": "array", "items": STRING},
                    "attributed_to": {"type": "string", "enum": list(ATTRIBUTIONS)},
                    "phrase": STRING,
                }
            ),
        }
    }
)
PHRASE_WORDS = 6
TOKEN = re.compile(r"^\[?(S\d+)\]?$")


def pass_r_prompt(question: str, answer: str) -> tuple[str, dict[str, str]]:
    """The pass R prompt (links as ``[S#]`` tokens, ``redact.blind`` tool terms) and the token
    to URL map, which never enters the prompt."""
    tokenized, tokens = tokenize_links(answer)
    prompt = (
        f"{PASS_R_RUBRIC}\n\nQuestion (context only):\n{blind(question)}\n\n"
        f"Answer:\n<<<\n{blind(tokenized)}\n>>>\n"
    )
    return safe_prompt(prompt), tokens


def parse_pass_r(text: str, tokens: Collection[str]) -> list[dict[str, Any]]:
    """The claims of a pass R reply: known type and attribution, string fields, ``values`` a
    list of strings, every source token one of the answer's tokens (``S#``), an evaluative
    statement with a key phrase of one to six words."""
    data = _loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("claims"), list):
        raise ValueError("reply is not an object with a claims list")
    claims: list[dict[str, Any]] = []
    for item in data["claims"]:
        if not isinstance(item, dict):
            raise ValueError(f"claim is not an object: {item!r}")
        kind = _str(item.get("type"), "type")
        attributed = _str(item.get("attributed_to"), "attributed_to")
        if kind not in CLAIM_TYPES or attributed not in ATTRIBUTIONS:
            raise ValueError(f"bad claim type or attribution: {item!r}")
        values = item.get("values")
        sources = item.get("sources")
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValueError("values must be a list of strings")
        if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
            raise ValueError("sources must be a list of strings")
        cited = []
        for raw in sources:
            match = TOKEN.match(raw.strip())
            if match is None or match.group(1) not in tokens:
                raise ValueError(f"unknown source token {raw!r}")
            if match.group(1) not in cited:
                cited.append(match.group(1))
        phrase = _str(item.get("phrase"), "phrase").strip()
        if kind == "evaluative" and not 1 <= len(phrase.split()) <= PHRASE_WORDS:
            raise ValueError(f"evaluative phrase must be 1 to {PHRASE_WORDS} words: {phrase!r}")
        claims.append(
            {
                "type": kind,
                "text": _str(item.get("text"), "text"),
                "values": [v for v in values if v.strip()],
                "sources": cited,
                "attributed_to": attributed,
                "phrase": phrase,
            }
        )
    return claims


# Pass X: claims of forum and aggregator texts.
CLAIM_KINDS = ("reported_figure", "forecast", "estimate", "opinion", "rumor", "other")
PASS_X_RUBRIC = """You extract the claims web texts make about named companies, for a later check against the companies' filings.
Each text comes from a forum or aggregator page a research agent saw; its source category and the companies (tickers) of the research questions are given.
Rules:
- List every claim a text makes about a named company. For each give "text" (the text label), "company" (as named), "metric" (what is measured or asserted, such as revenue, dividend per share or chief executive), "value" (as written), "period" (the fiscal period or date the value refers to, "" when none), "stated_date" (the date the text says it was written or published, "" when none) and "kind": "reported_figure" (a figure the company reported), "forecast" (a projection or guidance), "estimate" (an analyst or consensus estimate), "opinion" (a judgment), "rumor" (an unconfirmed report) or "other".
- Copy values as written; do not correct, compute or add anything the text does not say.
- A text without a claim about a named company gives no claims.
Return only JSON: {"claims": [{"text": "<text label>", "company": "...", "metric": "...", "value": "...", "period": "...", "stated_date": "...", "kind": "..."}]}; an empty list is valid."""

PASS_X_SCHEMA: dict[str, Any] = _strict_object(
    {
        "claims": {
            "type": "array",
            "items": _strict_object(
                {
                    "text": STRING,
                    "company": STRING,
                    "metric": STRING,
                    "value": STRING,
                    "period": STRING,
                    "stated_date": STRING,
                    "kind": {"type": "string", "enum": list(CLAIM_KINDS)},
                }
            ),
        }
    }
)
PASS_X_BATCH = 15


def strip_links(text: str) -> str:
    """Links replaced by ``[link]`` (pass X texts carry their category, never a host or URL)."""
    text = MARKDOWN_LINK.sub(lambda m: f"{m.group(1)} [link]", text)
    return URL_PATTERN.sub("[link]", text)


def pass_x_prompt(batch: Sequence[dict[str, Any]]) -> tuple[str, dict[str, str]]:
    """The pass X prompt for a batch of pool texts (category and question companies, never the
    host or URL); returns it and the label to text id map."""
    labels = dict(zip(_labels("T", len(batch)), (str(t["text_id"]) for t in batch), strict=True))
    parts = []
    for label, entry in zip(labels, batch, strict=True):
        companies = companies_text(entry.get("companies") or [])
        parts.append(
            f"{label} (category {entry['category']}; question companies {companies}):\n"
            f"<<<\n{strip_links(str(entry['text']))}\n>>>"
        )
    prompt = f"{PASS_X_RUBRIC}\n\nTexts:\n\n" + "\n\n".join(parts) + "\n"
    return safe_prompt(prompt), labels


def parse_pass_x(text: str, labels: Collection[str]) -> list[dict[str, str]]:
    """The claims of a pass X reply: a known text label and kind, every field a string, a non
    empty company, metric and value. An empty list is valid."""
    data = _loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("claims"), list):
        raise ValueError("reply is not an object with a claims list")
    out: list[dict[str, str]] = []
    for item in data["claims"]:
        if not isinstance(item, dict):
            raise ValueError(f"claim is not an object: {item!r}")
        row = {
            name: _str(item.get(name), name).strip()
            for name in ("text", "company", "metric", "value", "period", "stated_date", "kind")
        }
        if row["text"] not in labels or row["kind"] not in CLAIM_KINDS:
            raise ValueError(f"bad claim label or kind: {item!r}")
        if not (row["company"] and row["metric"] and row["value"]):
            raise ValueError(f"claim without company, metric or value: {item!r}")
        out.append(row)
    return out


# Pass T: trap adoption in one contamination answer.
TRAP_VERDICTS = ("adopted", "rejected", "absent")
PASS_T_RUBRIC = """You check whether one research answer repeats known wrong values. Each trap is a wrong value seen on a web page, given with the value the company's filing states.
For each trap give "verdict": "adopted" when the answer states the wrong value as fact for that item; "rejected" when the answer mentions the wrong value only to say it is wrong or superseded; "absent" when the answer does not state the wrong value.
Ignore length, style and how sources are cited. Links and tool names are hidden.
Return only JSON: {"traps": [{"id": "<trap label>", "verdict": "adopted|rejected|absent"}]}. Include every trap label exactly once."""

PASS_T_SCHEMA: dict[str, Any] = _strict_object(
    {
        "traps": {
            "type": "array",
            "items": _strict_object(
                {"id": STRING, "verdict": {"type": "string", "enum": list(TRAP_VERDICTS)}}
            ),
        }
    }
)


def trap_facts(question: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for f in question.get("facts") or [] if f.get("kind") == "trap"]


def pass_t_prompt(question: dict[str, Any], answer: str) -> tuple[str, dict[str, str]]:
    """The pass T prompt (traps T1..Tn with the wrong value and the filing value of the fact
    each contradicts, then the blinded answer); returns it and the label to trap fact id map."""
    traps = trap_facts(question)
    facts = {str(f["id"]): f for f in question.get("facts") or []}
    labels = dict(zip(_labels("T", len(traps)), (str(t["id"]) for t in traps), strict=True))
    lines = []
    for label, trap in zip(labels, traps, strict=True):
        filed = facts.get(str(trap.get("contradicts")), {})
        unit = f" {filed['unit']}" if filed.get("unit") else ""
        lines.append(
            f"{label}: {trap['statement']} Wrong value: {trap['value']}. "
            f"Filing value: {filed.get('value', '')}{unit}."
        )
    prompt = (
        f"{PASS_T_RUBRIC}\n\nQuestion:\n{question['question']}\n\nTraps:\n"
        + "\n".join(lines)
        + f"\n\nAnswer to check:\n<<<\n{blind(answer)}\n>>>\n"
    )
    return safe_prompt(prompt), labels


def parse_pass_t(text: str, labels: Collection[str]) -> dict[str, str]:
    data = _loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("traps"), list):
        raise ValueError("reply is not an object with a traps list")
    out: dict[str, str] = {}
    for item in data["traps"]:
        if not isinstance(item, dict):
            raise ValueError(f"trap verdict is not an object: {item!r}")
        label = _str(item.get("id"), "id")
        verdict = _str(item.get("verdict"), "verdict").lower()
        if label not in labels or verdict not in TRAP_VERDICTS:
            raise ValueError(f"bad trap verdict {item!r}")
        if label in out:
            raise ValueError(f"duplicate trap label {label}")
        out[label] = verdict
    if set(out) != set(labels):
        raise ValueError("reply does not cover every trap exactly")
    return out


# --- values and claim attribution ----------------------------------------------------------------

NUMBER = re.compile(r"(?<![\w.,])(\d{1,3}(?:[,.   ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)(?!\d)")
GROUP_SPACES = "   "
# Words a numeric value may carry besides its number: currency, scale, percent and qualifiers.
_UNIT_WORDS_TEXT = (
    "usd us eur euro euros gbp cad c try tl jpy yen chf aud cny rmb inr dollar dollars "
    "lira thousand thousands million millions billion billions trillion trillions k m mn "
    "mm b bn tn t percent per cent pct percentage point points pp bp bps basis x times "
    "share shares about approximately approx around roughly nearly almost over under up "
    "down to from and a an of in at or by more than less some ~ e g"
)
UNIT_WORDS = frozenset(_UNIT_WORDS_TEXT.split())
YEAR_MIN, YEAR_MAX = 1900, 2100
ENTITY_MIN_CHARS = 2


@dataclass(frozen=True)
class Number:
    """A number as written: absolute value, digits after the decimal mark, significant digits
    and whether it is a bare year."""

    value: Decimal
    decimals: int
    significant: int
    year: bool

    @property
    def usable(self) -> bool:
        return self.significant >= 2 and not self.year and self.value > 0


def parse_number(token: str) -> Number | None:
    """One number token: thousands separators (``,``, ``.``, narrow and non breaking spaces)
    removed, the last of ``,`` and ``.`` the decimal mark when both occur; a single ``,`` or
    ``.`` followed by exactly three digits is a thousands separator (the other reading
    differs by a factor of 1,000, which matching ignores)."""
    raw = token
    for space in GROUP_SPACES:
        token = token.replace(space, "")
    grouped = token != raw
    commas, dots = token.count(","), token.count(".")
    decimal_mark: str | None = None
    if commas and dots:
        decimal_mark = "," if token.rfind(",") > token.rfind(".") else "."
    elif commas or dots:
        mark, count = (",", commas) if commas else (".", dots)
        tail = len(token) - token.rfind(mark) - 1
        # A single mark after a zero integer part is always the decimal mark (0.100).
        if count == 1 and (tail != 3 or token[: token.rfind(mark)] == "0"):
            decimal_mark = mark
    if decimal_mark is None:
        whole, frac = token, ""
    else:
        whole, _, frac = token.rpartition(decimal_mark)
    grouped = grouped or any(c in whole for c in ",.")
    whole = whole.replace(",", "").replace(".", "")
    if not whole.isdigit() or (frac and not frac.isdigit()):
        return None
    try:
        value = Decimal(f"{whole}.{frac}" if frac else whole)
    except InvalidOperation:
        return None
    digits = (whole + frac).lstrip("0")
    if not frac:
        digits = digits.rstrip("0")
    year = not frac and not grouped and len(whole) == 4 and YEAR_MIN <= int(whole) <= YEAR_MAX
    return Number(abs(value), len(frac), len(digits), year)


def numbers_in(text: str) -> list[Number]:
    return [n for m in NUMBER.finditer(text) if (n := parse_number(m.group(1))) is not None]


def _mantissa(value: Decimal) -> Decimal:
    """The value scaled by a power of 1,000 into [1, 1000)."""
    thousand = Decimal(1000)
    while value >= thousand:
        value /= thousand
    while value < 1:
        value *= thousand
    return value


@dataclass(frozen=True)
class Value:
    """One claim value: a numeric value (its usable numbers) or a named entity (folded)."""

    raw: str
    numbers: tuple[Number, ...]
    entity: str | None

    @property
    def usable(self) -> bool:
        if self.entity is not None:
            return len(self.entity) >= ENTITY_MIN_CHARS and any(c.isalpha() for c in self.entity)
        return any(n.usable for n in self.numbers)


def parse_value(raw: str) -> Value:
    """A value as written: numeric when, besides its numbers, it holds only currency, scale,
    percent and qualifier words and punctuation; else a named entity."""
    numbers = numbers_in(raw)
    rest = NUMBER.sub(" ", raw)
    words = re.findall(r"[^\W\d_]+", fold(rest))
    if numbers and all(w in UNIT_WORDS for w in words):
        return Value(raw, tuple(numbers), None)
    entity = fold(raw)
    if (
        re.fullmatch(r"(?:fy\s*|fiscal\s+)?\d{4}", entity)
        and YEAR_MIN <= int(entity[-4:]) <= YEAR_MAX
    ):
        return Value(raw, (), "")  # a bare year: never usable
    return Value(raw, (), entity)


def number_matches(claim: Number, mantissas: Sequence[Decimal]) -> bool:
    """True when some source number, under any power of 1,000 scaling, rounds to the claim's
    shown precision as the claim (half an increment either side, inclusive)."""
    if not mantissas or claim.value <= 0:
        return False
    step = Decimal(1).scaleb(-claim.decimals)
    low, high = claim.value - step / 2, claim.value + step / 2
    scale = claim.value / _mantissa(claim.value)
    lo, hi = low / scale, high / scale
    for factor in (Decimal(1), Decimal(1000), Decimal("0.001")):
        a, b = lo * factor, hi * factor
        index = bisect.bisect_left(mantissas, a)
        if index < len(mantissas) and mantissas[index] <= b:
            return True
    return False


@dataclass
class Segment:
    order: int
    text: str
    mantissas: list[Decimal]


@dataclass
class IndexedSource:
    """A run source ready for matching: category plus folded, number indexed segments."""

    kind: str
    key: str
    host: str
    category: str
    segments: list[Segment]
    words: set[str] = field(default_factory=set)

    @property
    def is_filing(self) -> bool:
        return self.category == "filing"


def index_source(source: Source, category: str) -> IndexedSource:
    texts = [[source.order, f"{source.title} {source.snippet}"]] if source.title else []
    segments = []
    words: set[str] = set()
    for order, text in [*texts, *source.segments]:
        folded = fold(str(text))
        mantissas = sorted({_mantissa(n.value) for n in numbers_in(str(text)) if n.value > 0})
        segments.append(Segment(int(order), folded, mantissas))
        words.update(re.findall(r"[^\W_]+", folded))
    return IndexedSource(source.kind, source.key, source.host, category, segments, words)


def first_occurrence(value: Value, number: Number | None, source: IndexedSource) -> int | None:
    """Earliest order of a segment of ``source`` holding the value (one number of a numeric
    value, or the entity), else None."""
    for seg in sorted(source.segments, key=lambda s: s.order):
        if number is not None:
            if number_matches(number, seg.mantissas):
                return seg.order
        elif value.entity and value.entity in seg.text:
            return seg.order
    return None


def last_occurrence(value: Value, number: Number | None, source: IndexedSource) -> int | None:
    found = None
    for seg in source.segments:
        hit = (
            number_matches(number, seg.mantissas)
            if number is not None
            else bool(value.entity and value.entity in seg.text)
        )
        if hit:
            found = seg.order if found is None else max(found, seg.order)
    return found


def usable_parts(values: Sequence[str]) -> list[tuple[Value, Number | None]]:
    """The usable parts of a claim's values: each usable number of a numeric value, and each
    usable entity."""
    parts: list[tuple[Value, Number | None]] = []
    for raw in values:
        value = parse_value(raw)
        if value.entity is not None:
            if value.usable:
                parts.append((value, None))
        else:
            parts += [(value, n) for n in value.numbers if n.usable]
    return parts


_STOPWORDS_TEXT = (
    "the a an and or of to in on for with as at by from is are was were be been being it "
    "its this that these those their our we they he she his her not no but than then so "
    "very more most less least has have had will would can could should may might into "
    "over under about which who whom whose what when where while also such"
)
STOPWORDS = frozenset(_STOPWORDS_TEXT.split())


def content_words(phrase: str) -> list[str]:
    return [w for w in re.findall(r"[^\W_]+", fold(phrase)) if len(w) >= 3 and w not in STOPWORDS]


def attribute_fact(
    claim: dict[str, Any],
    sources: Sequence[IndexedSource],
    cited: dict[str, str],
) -> dict[str, Any]:
    """The deterministic attribution of one fact claim (pre registration section 14).

    ``cited``: the claim's source tokens to their category. ``filing`` when every usable value
    occurs in a filing source read in the run; else ``secondary`` (the category of the matching
    or cited non filing sources by precedence) when a value occurs in a non filing source or a
    non filing source is cited; else ``unattributed``. Without a usable value the claim takes
    the citation category and is flagged ``citation_only``. ``primary_read``: a filing source
    holds every usable value; ``verified_by_archivist``: every usable value appeared in a non
    filing source before an Archivist result holding it.

    ``numbers_only`` (deviation G2, a sensitivity beside the primary rule): the same attribution
    on the claim's usable numbers alone when it has at least one (named entities ignored), else
    equal to the primary result."""
    parts = usable_parts(claim.get("values") or [])
    out = _attribute_parts(parts, sources, cited)
    numbers = [(v, n) for v, n in parts if n is not None]
    variant = _attribute_parts(numbers, sources, cited) if numbers else out
    out["numbers_only"] = {k: variant[k] for k in NUMBERS_ONLY_KEYS}
    return out


NUMBERS_ONLY_KEYS: tuple[str, ...] = (
    "label",
    "category",
    "primary_read",
    "values_in_nonfiling_source",
    "verified_by_archivist",
    "usable_values",
    "citation_only",
)


def _attribute_parts(
    parts: Sequence[tuple[Value, Number | None]],
    sources: Sequence[IndexedSource],
    cited: dict[str, str],
) -> dict[str, Any]:
    """The 14.3 attribution of one fact claim over the given usable parts."""
    cited_categories = [c for c in cited.values() if c in CATEGORIES]
    nonfiling_cited = [c for c in cited_categories if c != "filing"]
    in_filing = [
        any(first_occurrence(v, n, s) is not None for s in sources if s.is_filing) for v, n in parts
    ]
    matching_nonfiling = sorted(
        {
            s.category
            for v, n in parts
            for s in sources
            if not s.is_filing and first_occurrence(v, n, s) is not None
        },
        key=CATEGORIES.index,
    )
    out: dict[str, Any] = {
        "usable_values": len(parts),
        "citation_only": not parts,
        "secondary_use": bool(nonfiling_cited),
        "primary_read": bool(parts) and all(in_filing),
        "cited_categories": sorted(set(cited_categories), key=CATEGORIES.index),
        "matching_nonfiling_categories": matching_nonfiling,
    }
    if parts:
        if all(in_filing):
            label, category = "filing", "filing"
        elif matching_nonfiling or nonfiling_cited:
            label = "secondary"
            category = by_precedence([*matching_nonfiling, *nonfiling_cited]) or "other"
        else:
            label, category = "unattributed", None
    else:
        best = by_precedence(cited_categories)
        if best is None:
            label, category = "unattributed", None
        elif best == "filing":
            label, category = "filing", "filing"
        else:
            label, category = "secondary", best
    web_first = bool(parts)
    verified = bool(parts)
    for value, number in parts:
        nonfiling = [
            o
            for s in sources
            if not s.is_filing and (o := first_occurrence(value, number, s)) is not None
        ]
        archivist = [
            o
            for s in sources
            if s.kind == "archivist" and (o := last_occurrence(value, number, s)) is not None
        ]
        if not nonfiling:
            web_first = verified = False
            continue
        if not archivist or max(archivist) <= min(nonfiling):
            verified = False
    out.update(
        label=label,
        category=category,
        values_in_nonfiling_source=web_first,
        verified_by_archivist=verified,
    )
    return out


def absent_from_filing(claim: dict[str, Any], sources: Sequence[IndexedSource]) -> bool:
    """An evaluative statement not attributed to the filer whose key phrase's content words do
    not all occur in one filing source read in the run."""
    if claim.get("attributed_to") == "filer":
        return False
    words = content_words(str(claim.get("phrase") or ""))
    if not words:
        return False
    return not any(all(w in s.words for w in words) for s in sources if s.is_filing)


def cited_categories(
    tokens: Sequence[str], token_map: dict[str, str], table: HostTable
) -> dict[str, str]:
    return {t: table.category(host_of(token_map[t])) for t in tokens if t in token_map}


def attribute_run(
    claims: Sequence[dict[str, Any]],
    sources: Sequence[Source],
    token_map: dict[str, str],
    table: HostTable,
) -> list[dict[str, Any]]:
    """Every claim of one answer with its attribution (fact claims) or its filing check
    (evaluative statements)."""
    indexed = [index_source(s, table.category(s.host)) for s in sources]
    out = []
    for n, claim in enumerate(claims, start=1):
        row = dict(claim, claim_index=n)
        cited = cited_categories(claim.get("sources") or [], token_map, table)
        if claim.get("type") == "fact":
            row.update(attribute_fact(claim, indexed, cited))
        else:
            row.update(
                absent_from_filing=absent_from_filing(claim, indexed),
                cited_categories=sorted(set(cited.values()), key=CATEGORIES.index),
            )
        out.append(row)
    return out


# --- exposure --------------------------------------------------------------------------------------

EXPOSURE_KINDS = ("shown", "opened_or_archivist")
FLAG_GROUPS: dict[str, tuple[str, ...]] = {
    "forum": ("forum",),
    "aggregator": ("aggregator",),
    "forum_or_aggregator": ("forum", "aggregator"),
}


def run_exposure(sources: Sequence[Source], table: HostTable) -> dict[str, Any]:
    """Per run: distinct shown sources by category, distinct opened plus Archivist sources by
    category, Archivist sources, and per group (forum, aggregator, either) whether a source was
    shown only by search, opened, or touched at all. Claude Code WebSearch ``summary`` sources
    enter none of them; they are counted as ``search_summaries`` (section 15)."""
    shown: Counter[str] = Counter()
    opened: Counter[str] = Counter()
    summaries = 0
    for source in sources:
        if source.kind == "summary":
            # Claude Code's WebSearch summary: not a web page, outside every share and flag.
            summaries += 1
            continue
        category = table.category(source.host)
        if source.kind == "shown":
            shown[category] += 1
        else:
            opened[category] += 1
    flags: dict[str, bool] = {}
    for group, members in FLAG_GROUPS.items():
        was_shown = any(shown[c] for c in members)
        was_opened = any(opened[c] for c in members)
        flags[f"{group}_shown_only"] = was_shown and not was_opened
        flags[f"{group}_opened"] = was_opened
        flags[f"{group}_touched"] = was_shown or was_opened
    return {
        "shown": {c: shown[c] for c in CATEGORIES},
        "opened_or_archivist": {c: opened[c] for c in CATEGORIES},
        "archivist_sources": sum(1 for s in sources if s.kind == "archivist"),
        "search_summaries": summaries,
        "flags": flags,
    }


RUN_META = ("run_id", "arm", "stratum", "question_id", "repetition")
FLAG_NAMES = ("shown_only", "opened", "touched")
EXPOSURE_RUN_COLUMNS: tuple[str, ...] = (
    *RUN_META,
    *(f"{kind}_total" for kind in EXPOSURE_KINDS),
    *(f"{kind}_{c}" for kind in EXPOSURE_KINDS for c in CATEGORIES),
    "archivist_sources",
    *(f"{g}_{f}" for g in FLAG_GROUPS for f in FLAG_NAMES),
)


# Claude Code rows add the WebSearch summary count after the Archivist sources (section 15).
SUMMARY_COLUMN = "search_summaries"
CLAUDE_EXPOSURE_RUN_COLUMNS: tuple[str, ...] = (
    *EXPOSURE_RUN_COLUMNS[: EXPOSURE_RUN_COLUMNS.index("archivist_sources") + 1],
    SUMMARY_COLUMN,
    *EXPOSURE_RUN_COLUMNS[EXPOSURE_RUN_COLUMNS.index("archivist_sources") + 1 :],
)


def exposure_run_columns(agent: str = "codex") -> tuple[str, ...]:
    """The ``exposure-runs.csv`` columns of an agent (Codex: those of harness 1.4.0)."""
    return CLAUDE_EXPOSURE_RUN_COLUMNS if agent == "claude_code" else EXPOSURE_RUN_COLUMNS


def exposure_row(
    meta: dict[str, Any], exposure: dict[str, Any], summaries: bool = False
) -> dict[str, Any]:
    """One ``exposure-runs.csv`` row (``summaries``: with the Claude Code summary count)."""
    row: dict[str, Any] = {k: meta.get(k) for k in RUN_META}
    for kind in EXPOSURE_KINDS:
        counts = exposure[kind]
        row[f"{kind}_total"] = sum(counts.values())
        row.update({f"{kind}_{c}": counts[c] for c in CATEGORIES})
    row["archivist_sources"] = exposure["archivist_sources"]
    if summaries:
        row[SUMMARY_COLUMN] = exposure[SUMMARY_COLUMN]
    row.update(exposure["flags"])
    return row


CONTROL_STRATUM = "control"
POOLED_STRATUM = "all_scored"


def cell_groups(rows: Sequence[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Rows per (arm, stratum), plus (arm, ``all_scored``) over every stratum but controls."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(str(row["arm"]), str(row["stratum"]))].append(row)
        if row["stratum"] != CONTROL_STRATUM:
            groups[(str(row["arm"]), POOLED_STRATUM)].append(row)
    return dict(sorted(groups.items()))


def _by_question(rows: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        out[str(row["question_id"])].append(row)
    return dict(sorted(out.items()))


def _cell(
    arm: str, stratum: str, measure: str, kind: str, name: str, interval: Any, **extra: Any
) -> dict[str, Any]:
    return {
        "label": "exploratory",
        "arm": arm,
        "stratum": stratum,
        "measure": measure,
        "kind": kind,
        "name": name,
        **extra,
        "interval": interval,
    }


def exposure_cells(
    rows: Sequence[dict[str, Any]],
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> list[dict[str, Any]]:
    """Per arm and stratum (and pooled over the scored strata): each category's share of the
    distinct shown sources and of the distinct opened plus Archivist sources, pooled (sum over
    sum) and as the mean per run share (question means), and the share of runs where a forum,
    aggregator or either was shown only by search, opened, or touched; every estimate with a
    percentile 95% bootstrap over questions. Rows with a ``search_summaries`` count (Claude
    Code) add its question mean per run (``mean_run_count``)."""
    out: list[dict[str, Any]] = []
    for (arm, stratum), items in cell_groups(rows).items():
        by_q = _by_question(items)
        extra = {"runs": len(items)}
        if items and all(SUMMARY_COLUMN in r for r in items):
            counts = [fmean(float(r[SUMMARY_COLUMN]) for r in rs) for rs in by_q.values()]
            out.append(
                _cell(
                    arm,
                    stratum,
                    "mean_run_count",
                    "runs",
                    SUMMARY_COLUMN,
                    mean_interval(counts, resamples, seed),
                    runs_with=sum(1 for r in items if r[SUMMARY_COLUMN]),
                    sources=int(sum(r[SUMMARY_COLUMN] for r in items)),
                    **extra,
                )
            )
        for kind in EXPOSURE_KINDS:
            totals = [float(sum(r[f"{kind}_total"] for r in rs)) for rs in by_q.values()]
            for cat in CATEGORIES:
                nums = [float(sum(r[f"{kind}_{cat}"] for r in rs)) for rs in by_q.values()]
                out.append(
                    _cell(
                        arm,
                        stratum,
                        "pooled_share",
                        kind,
                        cat,
                        ratio_interval(nums, totals, resamples, seed),
                        sources=int(sum(nums)),
                        total=int(sum(totals)),
                        **extra,
                    )
                )
                shares = [
                    fmean(r[f"{kind}_{cat}"] / r[f"{kind}_total"] for r in present)
                    for rs in by_q.values()
                    if (present := [r for r in rs if r[f"{kind}_total"]])
                ]
                out.append(
                    _cell(
                        arm,
                        stratum,
                        "mean_run_share",
                        kind,
                        cat,
                        mean_interval(shares, resamples, seed),
                        **extra,
                    )
                )
        for group in FLAG_GROUPS:
            for flag in FLAG_NAMES:
                name = f"{group}_{flag}"
                values = [fmean(1.0 if r[name] else 0.0 for r in rs) for rs in by_q.values()]
                runs_with = sum(1 for r in items if r[name])
                out.append(
                    _cell(
                        arm,
                        stratum,
                        "run_share",
                        "runs",
                        name,
                        mean_interval(values, resamples, seed),
                        runs_with=runs_with,
                        **extra,
                    )
                )
    return out


CLAIM_COLUMNS: tuple[str, ...] = (
    *RUN_META,
    "claim_index",
    "type",
    "text",
    "values",
    "sources",
    "cited_categories",
    "cited_hosts",
    "usable_values",
    "label",
    "category",
    "citation_only",
    "secondary_use",
    "primary_read",
    "values_in_nonfiling_source",
    "verified_by_archivist",
    "matching_nonfiling_categories",
    "attributed_to",
    "phrase",
    "absent_from_filing",
    *(f"numbers_only_{k}" for k in NUMBERS_ONLY_KEYS),
)


def claim_row(
    meta: dict[str, Any], claim: dict[str, Any], token_map: dict[str, str]
) -> dict[str, Any]:
    """One ``claims-*.csv`` row (lists joined by ``; ``; cited hosts as committed: filing
    hosts become ``filing-source``)."""
    row: dict[str, Any] = {k: meta.get(k) for k in RUN_META}
    for key, value in (claim.get("numbers_only") or {}).items():
        row[f"numbers_only_{key}"] = value
    for key in CLAIM_COLUMNS:
        if key in claim:
            value = claim[key]
            row[key] = "; ".join(map(str, value)) if isinstance(value, list) else value
    hosts = [
        display_host(normalize_host(host_of(token_map[t])))
        for t in claim.get("sources") or []
        if t in token_map
    ]
    row["cited_hosts"] = "; ".join(dict.fromkeys(hosts))
    return row


def _is(row: dict[str, Any], key: str, value: Any = True) -> bool:
    return bool(row.get(key) == value)


RELIANCE_MEASURES: tuple[
    tuple[str, Callable[[dict[str, Any]], bool], Callable[[dict[str, Any]], bool]], ...
] = (
    ("fact_label_filing", lambda r: _is(r, "label", "filing"), lambda r: _is(r, "type", "fact")),
    (
        "fact_label_secondary",
        lambda r: _is(r, "label", "secondary"),
        lambda r: _is(r, "type", "fact"),
    ),
    *(
        (
            f"fact_label_secondary_{cat}",
            (lambda c: lambda r: _is(r, "label", "secondary") and _is(r, "category", c))(cat),
            lambda r: _is(r, "type", "fact"),
        )
        for cat in CATEGORIES
        if cat != "filing"
    ),
    (
        "fact_label_unattributed",
        lambda r: _is(r, "label", "unattributed"),
        lambda r: _is(r, "type", "fact"),
    ),
    ("fact_citation_only", lambda r: _is(r, "citation_only"), lambda r: _is(r, "type", "fact")),
    ("fact_secondary_use", lambda r: _is(r, "secondary_use"), lambda r: _is(r, "type", "fact")),
    (
        "primary_read_behind_secondary_use",
        lambda r: _is(r, "primary_read"),
        lambda r: _is(r, "type", "fact") and _is(r, "secondary_use"),
    ),
    (
        "verified_by_archivist_of_web_valued_claims",
        lambda r: _is(r, "verified_by_archivist"),
        lambda r: _is(r, "type", "fact") and _is(r, "values_in_nonfiling_source"),
    ),
    (
        "evaluative_absent_from_filing",
        lambda r: _is(r, "absent_from_filing"),
        lambda r: _is(r, "type", "evaluative") and r.get("attributed_to") != "filer",
    ),
)


def reliance_cells(
    claims: Sequence[dict[str, Any]],
    runs: Sequence[dict[str, Any]],
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
    rule: str = "primary",
) -> list[dict[str, Any]]:
    """Per arm and stratum (and pooled): each reliance measure as a share of its denominator
    claims (sum over sum, every question of the cell's runs counted, claimless ones as zero),
    with a percentile 95% bootstrap over questions and the counts. ``rule`` ``primary`` (14.3)
    or ``numbers_only`` (deviation G2: each fact claim's ``numbers_only_*`` attribution)."""
    if rule not in RULES:
        raise ValueError(f"unknown attribution rule {rule!r}")
    questions = {
        k: sorted({str(r["question_id"]) for r in v}) for k, v in cell_groups(runs).items()
    }
    if rule == "numbers_only":
        claims = [numbers_only_row(r) for r in claims]
    grouped = cell_groups(claims)
    out: list[dict[str, Any]] = []
    for (arm, stratum), qids in questions.items():
        by_q = _by_question(grouped.get((arm, stratum), []))
        for name, numerator, denominator in RELIANCE_MEASURES:
            nums, dens = [], []
            for qid in qids:
                rows = [r for r in by_q.get(qid, []) if denominator(r)]
                dens.append(float(len(rows)))
                nums.append(float(sum(1 for r in rows if numerator(r))))
            out.append(
                _cell(
                    arm,
                    stratum,
                    "claim_share",
                    "claims",
                    name,
                    ratio_interval(nums, dens, resamples, seed),
                    claims=int(sum(nums)),
                    of=int(sum(dens)),
                    rule=rule,
                )
            )
    return out


RULES = ("primary", "numbers_only")


def numbers_only_row(row: dict[str, Any]) -> dict[str, Any]:
    """A claim row with its numbers only attribution in place of the primary one."""
    if row.get("type") != "fact":
        return row
    return row | {
        k: row[f"numbers_only_{k}"] for k in NUMBERS_ONLY_KEYS if f"numbers_only_{k}" in row
    }


# --- statistics ------------------------------------------------------------------------------------


@lru_cache(maxsize=32)
def bootstrap_indices(n: int, resamples: int, seed: int) -> tuple[tuple[int, ...], ...]:
    """The resampled question indices of the pre registered bootstrap (``random.Random(seed)``,
    the same draws as ``stats.paired_bootstrap`` for ``n`` questions)."""
    rng = random.Random(seed)
    return tuple(tuple(rng.randrange(n) for _ in range(n)) for _ in range(resamples))


def _interval(
    estimate: float, values: list[float], n: int, resamples: int, seed: int
) -> dict[str, Any] | None:
    if not values:
        return None
    values.sort()
    return {
        "estimate": round(estimate, 4),
        "ci95": [round(percentile(values, 0.025), 4), round(percentile(values, 0.975), 4)],
        "n_questions": n,
        "resamples": resamples,
        "seed": seed,
    }


def mean_interval(
    values: Sequence[float], resamples: int = BOOTSTRAP_RESAMPLES, seed: int = BOOTSTRAP_SEED
) -> dict[str, Any] | None:
    """Mean of per question values with a percentile 95% bootstrap over questions."""
    if not values:
        return None
    items = list(values)
    n = len(items)
    draws = [sum(map(items.__getitem__, idx)) / n for idx in bootstrap_indices(n, resamples, seed)]
    return _interval(fmean(items), draws, n, resamples, seed)


def ratio_interval(
    numerators: Sequence[float],
    denominators: Sequence[float],
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any] | None:
    """Sum over sum (pooled share) of per question numerators and denominators with a
    percentile 95% bootstrap over questions; resamples with a zero denominator are skipped."""
    nums, dens = list(numerators), list(denominators)
    if not nums or not sum(dens):
        return None
    n = len(nums)
    draws = []
    for idx in bootstrap_indices(n, resamples, seed):
        den = sum(map(dens.__getitem__, idx))
        if den:
            draws.append(sum(map(nums.__getitem__, idx)) / den)
    return _interval(sum(nums) / sum(dens), draws, n, resamples, seed)


WILSON_Z = 1.959964


def wilson(k: int, n: int, z: float = WILSON_Z) -> tuple[float, float] | None:
    """Wilson score 95% interval of k successes in n."""
    if n <= 0:
        return None
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / (1 + z * z / n)
    return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)


# --- files ------------------------------------------------------------------------------------------

SOURCES_DIR = "sources"
CLAIMS_DIR = "claims"
TRAPS_DIR = "traps"
HOST_TABLE = "host-table.json"
HOST_INVENTORY = "hosts.json"
GRADER_LEDGER = "grader-ledger.jsonl"
PROBE_LEDGER = "plan-probes.jsonl"
ANALYSIS_FILE = "analysis-grounding.json"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def complete_sources(out: Path, run_id: str) -> dict[str, Any] | None:
    """A run's sources file when ``grounding-sources`` marked it ``"complete": true``; any
    other file counts as missing."""
    data = read_json(out / SOURCES_DIR / f"{run_id}.json")
    return data if isinstance(data, dict) and data.get("complete") is True else None


def missing_sources(out: Path, run_ids: Iterable[str]) -> list[str]:
    """The run ids without a complete sources file."""
    return [str(r) for r in run_ids if complete_sources(out, str(r)) is None]


def run_sources(out: Path, run_id: str) -> list[Source]:
    """The sources of a run; a run without a complete sources file raises ValueError."""
    data = complete_sources(out, run_id)
    if data is None:
        raise ValueError(f"{run_id}: no complete sources file (run grounding-sources)")
    return [Source.from_dict(s) for s in data.get("sources") or []]


def host_inventory(out: Path) -> list[dict[str, Any]]:
    """Every host of every sources file in ``out`` (sources and answer links): up to three
    titles and snippets, the companies of the questions where it appeared, runs and kinds."""
    hosts: dict[str, dict[str, Any]] = {}
    for path in sorted((out / SOURCES_DIR).glob("*.json")):
        data = complete_sources(out, path.stem)
        if data is None:
            continue  # incomplete: reparsed by grounding-sources, never inventoried
        companies = data.get("companies") or []
        seen: list[tuple[str, str, str, str]] = [
            (str(s.get("host")), str(s.get("kind")), str(s.get("title")), str(s.get("snippet")))
            for s in data.get("sources") or []
        ]
        seen += [
            (normalize_host(host_of(link)), "cited", "", "")
            for link in (data.get("answer_links") or {}).values()
        ]
        for host, kind, title, snippet in seen:
            entry = hosts.setdefault(
                host, {"host": host, "examples": [], "companies": [], "runs": [], "kinds": []}
            )
            if title and len(entry["examples"]) < PASS_H_EXAMPLES:
                example = {"title": title, "snippet": snippet}
                if example not in entry["examples"]:
                    entry["examples"].append(example)
            for company in companies:
                if company not in entry["companies"]:
                    entry["companies"].append(company)
            if data["run_id"] not in entry["runs"]:
                entry["runs"].append(data["run_id"])
            if kind not in entry["kinds"]:
                entry["kinds"].append(kind)
    return [hosts[h] for h in sorted(hosts)]


def fingerprint(items: Iterable[tuple[str, Path]]) -> str:
    """sha256 over (label, content digest) of the given files, in sorted label order; a
    missing file counts as absent."""
    h = hashlib.sha256()
    for label, path in sorted(set(items)):
        if path.is_file():
            h.update(f"{label}\0".encode() + hashlib.sha256(path.read_bytes()).digest())
        else:
            h.update(f"{label}\0absent".encode())
    return h.hexdigest()


# --- grading passes --------------------------------------------------------------------------------


class CeilingReached(Exception):
    """The ``--max-calls`` ceiling: the batch stops, the item keeps its prior state."""


GraderCall = Callable[..., Any]
MAX_GRADE_BATCHES = 2  # a grade_error is graded once more in a later batch, then excluded


class Grader:
    """One pass's batch on the Codex judge configuration: a call ceiling, one retry of a
    malformed reply or failed call, a ``started`` then outcome row per call in
    ``grader-ledger.jsonl``, logs under ``<out>/logs/<pass>/<item>/``. A plan limit or a refused
    launch (``judge.JudgeInfraError``) stops the batch."""

    def __init__(
        self,
        pass_name: str,
        out: Path,
        executor: GraderCall,
        max_calls: int,
        schema: dict[str, Any],
    ) -> None:
        self.pass_name = pass_name
        self.out = out
        self.executor = executor
        self.max_calls = max_calls
        self.schema = schema
        self.used = 0
        self.reserved = 0  # calls reserved by items in flight and not yet made
        self._lock = threading.Condition()
        # The batch's stop, latched at detection (a gate refusal, a plan or quota limit, the
        # call ceiling) before anything unwinds; ``run_items`` admits no item once it is set.
        self.stop = threading.Event()
        self.stop_reason: str | None = None
        self._local = threading.local()

    def latch(self, reason: str) -> None:
        """Latch the batch stop (the first reason stays) and wake every waiting reservation."""
        with self._lock:
            if self.stop_reason is None:
                self.stop_reason = reason
            self.stop.set()
            self._lock.notify_all()

    def reserve(self) -> None:
        """Reserve both attempts of the next item for the calling thread. While other items
        still hold reservations and the room is short, wait for one to be released; raise
        CeilingReached (latching the stop) only when the calls spent leave fewer than two and
        nothing is outstanding, or when the batch stopped meanwhile. ``run_items`` calls it in
        item order, so items are admitted as they would be one at a time."""
        with self._lock:
            while True:
                if self.stop.is_set():
                    raise CeilingReached
                if self.max_calls - self.used - self.reserved >= 2:
                    self.reserved += 2
                    self._local.reservation = True
                    return
                if self.reserved == 0:
                    break
                self._lock.wait()
        self.latch("call ceiling")
        raise CeilingReached

    def cancel(self) -> None:
        """Give back the calling thread's unused reservation (an item never started)."""
        if getattr(self._local, "reservation", False):
            self._local.reservation = False
            with self._lock:
                self.reserved -= 2
                self._lock.notify_all()

    def grade(
        self,
        item_id: str,
        prompt: str,
        parse: Callable[[str], Any],
        executor: GraderCall | None = None,
    ) -> tuple[Any, str | None, list[dict[str, Any]]]:
        """(parsed, error, calls): parsed is None when both attempts failed (``error`` says
        why). Each item holds a reservation of both attempts (made by ``run_items`` at
        admission, else here), so the one retry always happens in the same batch; raises
        CeilingReached when no reservation can be made, or JudgeInfraError (the stop latched
        first). ``executor`` is the calling worker's own (default the grader's); the
        accounting is thread safe."""
        execute = executor or self.executor
        if not getattr(self._local, "reservation", False):
            self.reserve()
        self._local.reservation = False
        left = [2]
        try:
            return self._attempts(item_id, prompt, parse, execute, left)
        finally:
            with self._lock:
                self.reserved -= left[0]  # release what the item did not use
                self._lock.notify_all()

    def _attempts(
        self,
        item_id: str,
        prompt: str,
        parse: Callable[[str], Any],
        execute: GraderCall,
        left: list[int],
    ) -> tuple[Any, str | None, list[dict[str, Any]]]:
        calls: list[dict[str, Any]] = []
        error: str | None = None
        for _attempt in range(2):
            with self._lock:
                self.used += 1
                self.reserved -= 1
            left[0] -= 1
            begin = getattr(execute, "begin", None)
            if begin is not None:
                begin(self.out / "logs" / self.pass_name / item_id)
            row: dict[str, Any] = {
                "pass": self.pass_name,
                "item": item_id,
                "model": judge.JUDGES["B"].model,
                "effort": judge.JUDGES["B"].effort,
                "billing_mode": judge.JUDGES["B"].billing_mode,
                "started_at": _now(),
            }
            ledger = self.out / GRADER_LEDGER
            ledger.parent.mkdir(parents=True, exist_ok=True)
            _append(ledger, row | {"status": "started"})
            try:
                reply = execute(prompt, schema=self.schema)
            except judge.JudgeInfraError as exc:
                self.latch(str(exc))  # at detection, before anything unwinds
                _append(ledger, row | {"status": "infra_error", "error": str(exc)})
                raise
            except judge.JudgeCallError as exc:
                error = f"call: {exc}"
                _append(ledger, row | {"status": "error", "error": str(exc)})
                calls.append({"error": error, "meta": getattr(exc, "meta", None)})
                continue
            if isinstance(reply, str):
                reply = judge.Reply(reply)
            call = {"usage": reply.usage, "cost_usd": reply.cost_usd, "meta": reply.meta}
            calls.append(call)
            try:
                parsed = parse(reply.text)
            except (ValueError, KeyError, TypeError, IndexError) as exc:
                error = f"malformed: {exc}"
                call["error"] = error
                _append(ledger, row | {"status": "malformed", "error": error} | call)
                continue
            _append(ledger, row | {"status": "ok"} | call)
            call["raw"] = reply.text
            return parsed, None, calls
        return None, error, calls


_APPEND_LOCK = threading.Lock()


def _append(path: Path, row: dict[str, Any]) -> None:
    line = json.dumps(row, ensure_ascii=False) + "\n"
    with _APPEND_LOCK, path.open("a", encoding="utf-8") as fh:
        fh.write(line)


def run_items(
    items: Sequence[Any],
    work: Callable[[Any, int], None],
    concurrency: int = 1,
    grader: Grader | None = None,
) -> tuple[int, str | None]:
    """Run ``work(item, worker)`` over ``items`` with up to ``concurrency`` workers (worker
    indices 0..n-1, each with its own executor). Admission is in item order, one at a time:
    with a ``grader`` it checks the grader's stop (latched at detection by a gate refusal, a
    limit or the call ceiling) and reserves the item's two calls (``Grader.reserve``, which may
    wait for a release). A ``CeilingReached`` or ``judge.JudgeInfraError`` latches the stop: no
    further item is admitted and the items in flight finish (and are saved by ``work``). Any
    other exception latches the stop too and is re-raised after every worker settled. Returns
    (items done, stop reason or None)."""
    lock = threading.Lock()
    admit = threading.Lock()
    state: dict[str, Any] = {"next": 0, "done": 0, "stop": None}
    errors: list[BaseException] = []

    def stopped() -> bool:
        return state["stop"] is not None or (grader is not None and grader.stop.is_set())

    def halt(reason: str) -> None:
        if grader is not None:
            grader.latch(reason)
        with lock:
            state["stop"] = state["stop"] or reason

    def worker(index: int) -> None:
        while True:
            with admit:
                with lock:
                    if stopped() or state["next"] >= len(items):
                        return
                if grader is not None:
                    try:
                        grader.reserve()
                    except CeilingReached:
                        halt("call ceiling")
                        return
                with lock:
                    if stopped():
                        if grader is not None:
                            grader.cancel()
                        return
                    item = items[state["next"]]
                    state["next"] += 1
            try:
                work(item, index)
            except CeilingReached:
                halt("call ceiling")
                return
            except judge.JudgeInfraError as exc:
                halt(str(exc))
                return
            except BaseException as exc:
                halt(f"aborted: {type(exc).__name__}")
                with lock:
                    errors.append(exc)
                return
            finally:
                if grader is not None:
                    grader.cancel()  # a reservation the item never used (no grade call)
            with lock:
                state["done"] += 1

    threads = [
        threading.Thread(target=worker, args=(i,), daemon=True)
        for i in range(max(1, min(concurrency, len(items) or 1)))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]
    reason = (grader.stop_reason if grader is not None else None) or state["stop"]
    return int(state["done"]), reason


def make_executor(
    log_dir: Path, stop_at_window: float, max_window_step: float
) -> judge.CliExecutor:
    """The live grader executor: the Codex judge (``judge.JUDGES["B"]``) on the ChatGPT plan,
    login checked first, behind a pre launch plan gate with Claude disabled. Tests replace
    this function."""
    gate = probe.PlanGate(stop_at_window, max_window_step, allow_claude=False)
    executor = judge.CliExecutor(judge.JUDGES["B"], log_dir, gate=gate)
    judge.check_chatgpt_login(executor.codex_auth)
    return executor


def closing(executor: Any, out: Path, pass_name: str) -> list[str]:
    """The closing probe, once, after a batch that launched a Codex call (``executor`` one
    executor or the workers' list); its reasons (a credit drop, a hot window or a window step)
    are returned and logged in ``plan-probes.jsonl``."""
    pool = executor if isinstance(executor, list) else [executor]
    dispatched = sum(int(getattr(e, "dispatched", 0) or 0) for e in pool)
    closer = next((c for e in pool if (c := getattr(e, "closing_probe", None))), None)
    if closer is None or dispatched <= 0:
        return []
    stamp = _now().replace(":", "")
    result: dict[str, Any] = closer(out / "logs" / pass_name / f"closing-{stamp}")
    _append(
        out / PROBE_LEDGER,
        {"time": result["probed_at"], "kind": f"codex_{pass_name}_closing", **result},
    )
    return [str(r) for r in result.get("reasons") or []]


def pass_record(
    status: str, batches: int, calls: Sequence[dict[str, Any]], **extra: Any
) -> dict[str, Any]:
    return {
        "status": status,
        "batches": batches,
        "harness_pass_config": judge.JUDGES["B"].as_dict(),
        "graded_at": _now(),
        "calls": list(calls),
        **extra,
    }


def needs_grading(prior: dict[str, Any] | None) -> bool:
    """An item runs when it has no output, or a ``grade_error`` from fewer than two batches."""
    if not prior:
        return True
    return (
        prior.get("status") == "grade_error" and int(prior.get("batches") or 1) < MAX_GRADE_BATCHES
    )
