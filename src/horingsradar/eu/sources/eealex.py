"""EEA status from EEA-Lex (efta.int).

What EEA-Lex gives an automated client (checked 29.10.2026):
- The list sorted by date of EU proposal (filters and search are disallowed in robots.txt).
  Each row has the EEA-Lex key, the COM reference and the title, but no status.
- Fact sheets for adopted acts, keyed by CELEX, with the status, area and JCD.
- Fact sheets for proposals (numeric keys) redirect to themselves and are not fetched. A
  proposal that is in the list has the first status, "Proposed act with possible EEA
  relevance"; that status is derived from the list, and marked as such.

robots.txt asks for 5 seconds between requests. Redirects are capped, so a redirect loop is a
handful of requests and not a burst; a loop or 429 stops the EEA-Lex step for the run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx
from selectolax.lexbor import LexborHTMLParser

from horingsradar.config import Settings
from horingsradar.http import Fetcher, FetchError

BASE = "https://www.efta.int"
LIST_URL = BASE + "/eea-lex?items_per_page=60&sort_bef_combine=proposal_DESC&page={page}"
CRAWL_DELAY = 5.0  # robots.txt
MAX_REDIRECTS = 2
COM_RE = re.compile(r"\b(COM|JOIN)\s*\((\d{4})\)\s*0*(\d+)")
CELEX_KEY_RE = re.compile(r"^\d{5}[a-z]{1,2}\d{4}", re.IGNORECASE)

# Status codes, in the order of the EEA procedure, with EEA-Lex's own labels.
STATUSES: dict[str, str] = {
    "PROPOSED": "Proposed act with possible EEA relevance",
    "SCRUTINY": "Act under scrutiny by EEA EFTA",
    "DRAFT_JCD": "Draft Joint Committee Decision (JCD) under consideration",
    "JCD_PENDING": "Entry into force of Joint Committee Decision (JCD) pending",
    "IN_FORCE": "Incorporated into the EEA Agreement and in force",
    "NO_LONGER_IN_FORCE": "Incorporated into the EEA Agreement but no longer in force",
}
NOT_REGISTERED = "NOT_REGISTERED"
CODES = {label.lower(): code for code, label in STATUSES.items()}


class EEALexUnavailable(Exception):
    """EEA-Lex refused or looped; the step stops for this run."""


@dataclass
class ListEntry:
    key: str  # numeric for proposals, CELEX for acts
    com_refs: list[str]
    title: str

    @property
    def is_act(self) -> bool:
        return bool(CELEX_KEY_RE.match(self.key))

    @property
    def url(self) -> str:
        return f"{BASE}/eea-lex/{self.key.lower()}"


@dataclass
class Factsheet:
    status: str | None
    legal_status: str | None = None
    areas: list[str] = field(default_factory=list)
    jcd: str | None = None
    in_force: str | None = None


def com_refs(text: str) -> list[str]:
    """COM(2026) 569 and the like, normalised the same way as the EUR-Lex references."""
    return [f"{kind}({year}) {int(number)}" for kind, year, number in COM_RE.findall(text)]


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.text(separator=" ")).strip() if node else ""


def parse_list(html: str) -> list[ListEntry]:
    tree = LexborHTMLParser(html)
    entries = []
    for article in tree.css("article.lex-teaser"):
        link = article.css_first("a.quicklink")
        href = (link.attributes.get("href") or "") if link else ""
        m = re.match(r"^/eea-lex/([^/?#]+)$", href)
        if not m:
            continue
        entries.append(
            ListEntry(
                key=m.group(1).upper() if CELEX_KEY_RE.match(m.group(1)) else m.group(1),
                com_refs=com_refs(_text(article.css_first(".lex-teaser-subtitle"))),
                title=_text(article.css_first(".lex-teaser-description")),
            )
        )
    return entries


def _field(tree, name: str) -> list[str]:
    node = tree.css_first(f".field--name-{name}")
    return [_text(item) for item in node.css(".field-item")] if node else []


def parse_factsheet(html: str) -> Factsheet:
    tree = LexborHTMLParser(html)
    active = tree.css_first(".field--name-field-case-status .timeline-item.active .timeline-text")
    jcd = _field(tree, "field-joint-com-decision-jcd")
    legal = _field(tree, "field-eea-status")
    in_force = _field(tree, "field-force-in-eea")
    return Factsheet(
        status=CODES.get(_text(active).lower()) if active else None,
        legal_status=legal[0] if legal else None,
        areas=_field(tree, "field-annex-chapter"),
        jcd=jcd[0] if jcd else None,
        in_force=in_force[0] if in_force else None,
    )


def make_fetcher(settings: Settings) -> Fetcher:
    """A Fetcher whose client follows at most a couple of redirects."""
    if settings.request_interval_seconds < CRAWL_DELAY:
        raise ValueError(
            f"EEA-Lex ber om minst {CRAWL_DELAY:g} sekunder mellom kall (robots.txt); "
            f"REQUEST_INTERVAL_SECONDS er {settings.request_interval_seconds:g}"
        )
    client = httpx.Client(
        timeout=settings.http_timeout_seconds, follow_redirects=True, max_redirects=MAX_REDIRECTS
    )
    return Fetcher(settings, client=client)


class EEALexClient:
    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher

    def _get(self, url: str) -> str:
        try:
            return self.fetcher.get(url).text
        except httpx.TooManyRedirects as exc:
            raise EEALexUnavailable(f"omdirigeringsløkke: {url}") from exc
        except FetchError as exc:
            if exc.status in (403, 429) or exc.status is None:
                raise EEALexUnavailable(f"{exc.status or 'ingen svar'}: {url}") from exc
            raise

    def list_page(self, page: int) -> list[ListEntry]:
        return parse_list(self._get(LIST_URL.format(page=page)))

    def factsheet(self, celex: str) -> Factsheet | None:
        """The fact sheet of an adopted act, or None if EEA-Lex has none."""
        try:
            return parse_factsheet(self._get(f"{BASE}/eea-lex/{celex.lower()}"))
        except FetchError as exc:
            if exc.status == 404:
                return None
            raise
