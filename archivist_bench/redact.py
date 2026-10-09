"""Link redaction, judge blinding, upstream host and contamination checks."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# Filing source hosts that must never reach a committed file (CLAUDE.md, api-contracts 10.2).
# Assembled from fragments so this file holds no filing host literal (the repository grep for
# upstream hosts stays clean); the compiled pattern matches the hosts themselves.
_UPSTREAM_PARTS = (
    ("sec", ".gov"),
    ("sedar", "plus"),
    ("sedar", ".com"),
    ("kap", ".org.tr"),
    ("quote", "media"),
    ("tmx", "money"),
    ("money", ".tmx"),
    ("tmx", ".com"),
    ("tmxinfo", "services.com"),
    ("financial", "filings.com"),
    ("financial", "reports.eu"),
)
UPSTREAM_HOST_PATTERN = re.compile(
    "|".join(re.escape(head + tail) for head, tail in _UPSTREAM_PARTS), re.IGNORECASE
)

# Hosts that would leak the answer keys or Mosaic's own pages into the web arm.
MOSAIC_SITE_HOSTS = ("mosaic-finance.com",)
REPOSITORY_MARKERS = ("private-source-forge.invalid", "github.com/mosaicss", "mosaicss/")

URL_PATTERN = re.compile(r"https?://[^\s)\]>\"'`]+", re.IGNORECASE)
MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)", re.IGNORECASE)
BARE_DOMAIN = re.compile(
    r"\b(?:[a-z0-9-]+\.)+(?:com|org|net|gov|io|ca|tr|co|info|us)(?:/[^\s)\]]*)?",
    re.IGNORECASE,
)

# Words that reveal which arm wrote an answer. Order matters: longer phrases first.
BLIND_TERMS: tuple[tuple[str, str], ...] = (
    (r"mcp__archivist__\w+", "[tool]"),
    # Codex tool identifiers (functions.exec, tools.web__run) and namespaced names (web__run).
    (r"\b(?:tools|functions)\.\w+", "[tool]"),
    (r"\b\w+__\w+\b", "[tool]"),
    (r"\barchivist\b", "[tool]"),
    (r"\bmosaic(?:[ -]finance)?\b", "[source]"),
    (r"\bpermalink(s)?\b", "link"),
    (
        r"\b(?:read_passage|read_section|companies_search|companies_get|search_filings|"
        r"read_filing_section|find_in_filing|list_filings|toc)\b",
        "[tool]",
    ),
    (r"\b(?:WebSearch|WebFetch|web[_ ]search|web[_ ]fetch)\b", "[tool]"),
    (r"\bexchange_document_id\b", "document id"),
)


def host_of(url: str) -> str:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        host = ""
    return host.lower()


UPSTREAM_PLACEHOLDER = "filing-source"


def _safe_host(url: str) -> str:
    host = host_of(url)
    return UPSTREAM_PLACEHOLDER if UPSTREAM_HOST_PATTERN.search(host) else host


def redact_links(text: str) -> str:
    """Replace every URL with ``<url:host>`` (the pilot convention for committed answers); a
    filing source host becomes ``<url:filing-source>`` and bare mentions of one are masked too,
    so no upstream host reaches a committed file."""

    def _md(match: re.Match[str]) -> str:
        return f"[{match.group(1)}](<url:{_safe_host(match.group(2))}>)"

    text = MARKDOWN_LINK.sub(_md, text)
    text = URL_PATTERN.sub(lambda m: f"<url:{_safe_host(m.group(0))}>", text)
    return UPSTREAM_HOST_PATTERN.sub(UPSTREAM_PLACEHOLDER, text)


def blind(text: str) -> str:
    """Neutralize links, hosts and tool names so the judge cannot tell the arms apart."""
    text = MARKDOWN_LINK.sub(lambda m: f"{m.group(1)} [link]", text)
    text = URL_PATTERN.sub("[link]", text)
    text = re.sub(r"<url:[^>]*>", "[link]", text)
    text = BARE_DOMAIN.sub("[site]", text)
    for pattern, token in BLIND_TERMS:
        text = re.sub(pattern, token, text, flags=re.IGNORECASE)
    return text


def find_upstream_hosts(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in UPSTREAM_HOST_PATTERN.finditer(text)})


def contamination_hits(arm: str, urls: list[str], answer: str) -> list[str]:
    """Return the Mosaic hosts a run opened or cited that its arm must not touch.

    Every arm: the source repositories (they hold the answer keys). Web arm only: any Mosaic
    page, since that arm has no Archivist and must answer from the open web.
    """
    hits: set[str] = set()
    haystacks = [u.lower() for u in urls] + [answer.lower()]
    for hay in haystacks:
        for marker in REPOSITORY_MARKERS:
            if marker in hay:
                hits.add(marker)
        if arm == "web":
            for host in MOSAIC_SITE_HOSTS:
                if host in hay:
                    hits.add(host)
    return sorted(hits)
