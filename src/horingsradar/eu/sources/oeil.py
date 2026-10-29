"""Where a proposal stands in the European Parliament, from the Legislative Observatory (OEIL).

What OEIL gives an automated client (checked 29.10.2026):
- No robots.txt (404); oeil.secure.europarl.europa.eu redirects to oeil.europarl.europa.eu.
  Procedure pages answer quickly, without a bot check.
- A procedure page has a short title ("New Europol Regulation"), the status in Parliament
  ("Awaiting committee decision"), the committee responsible, and the key events.
- It also names rapporteurs, shadow rapporteurs and Commissioners. They are not read.
- New NLE procedures (Council decisions on agreements) are often not in OEIL yet (404).
- The Parliament's Open Data API has the same procedures but answered 500 to half the calls,
  and lacks the status text. It is not used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote

from selectolax.lexbor import LexborHTMLParser

from horingsradar.http import Fetcher, FetchError

BASE = "https://oeil.europarl.europa.eu"
PAGE_URL = BASE + "/oeil/en/procedure-file?reference={ref}"
PROCEDURE_RE = re.compile(r"^(\d{4})/(\d{4})/([A-Z]{3})$")
DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
# OEIL's statuses for procedures that will not move any more; they are not fetched again.
FINAL = ("Procedure completed", "Procedure lapsed or withdrawn", "Procedure rejected")

# OEIL's status texts in Norwegian; unknown ones are shown as OEIL writes them.
STATUS_LABELS = {
    "Preparatory phase in Parliament": "Forberedende fase i Parlamentet",
    "Awaiting committee decision": "Venter på komitébehandling",
    "Awaiting Parliament's position in 1st reading": "Venter på Parlamentets standpunkt, 1. lesning",
    "Awaiting Parliament's vote": "Venter på avstemning i Parlamentet",
    "Awaiting Council's 1st reading position": "Venter på Rådets standpunkt, 1. lesning",
    "Awaiting signature of act": "Venter på signering av rettsakten",
    "Procedure completed, awaiting publication in Official Journal": (
        "Ferdigbehandlet, venter på kunngjøring i EU-tidende"
    ),
    "Procedure completed": "Ferdigbehandlet",
    "Procedure lapsed or withdrawn": "Bortfalt eller trukket",
    "Procedure rejected": "Forkastet",
}


class OEILUnavailable(Exception):
    """OEIL refused or answered with a page that could not be read; the step stops."""


@dataclass
class Event:
    date: str  # ISO date
    event: str
    reference: str | None


@dataclass
class ProcedureFile:
    reference: str  # 2026/0165(COD)
    title: str | None
    status: str | None
    committees: list[str] = field(default_factory=list)  # "LIBE Civil Liberties, ..."
    events: list[Event] = field(default_factory=list)

    @property
    def final(self) -> bool:
        return self.status in FINAL


def oeil_ref(procedure_ref: str) -> str | None:
    """2026/0165/COD (Cellar) -> 2026/0165(COD) (OEIL)."""
    m = PROCEDURE_RE.match(procedure_ref or "")
    return f"{m.group(1)}/{m.group(2)}({m.group(3)})" if m else None


def page_url(ref: str) -> str:
    return PAGE_URL.format(ref=quote(ref, safe="/()"))


def status_label(status: str | None) -> str:
    return STATUS_LABELS.get(status or "", status or "–")


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.text(separator=" ")).strip() if node else ""


def _iso(value: str) -> str | None:
    m = DATE_RE.match(value)
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def parse_page(html: str) -> ProcedureFile:
    tree = LexborHTMLParser(html)
    ref = _text(tree.css_first("h2.es_title-h1"))
    if not ref:
        raise OEILUnavailable("ikke en prosedyreside")
    title = _text(tree.css_first("h2.es_title-h2.mb-3")) or None

    status = None
    for label in tree.css("#section1 p.font-weight-bold"):
        if _text(label) == "Status":
            value = label.next
            while value is not None and value.tag != "p":
                value = value.next
            status = _text(value) or None
            break

    # Committees responsible (also "Joint committee responsible"), not former ones or opinions.
    committees = []
    for table in tree.css("#section2 table"):
        head = _text(table.css_first("thead th"))
        if head not in ("Committee responsible", "Joint committee responsible"):
            continue
        for row in table.css("tbody tr"):
            code = _text(row.css_first(".es_badge-committee"))
            name = _text(row.css_first(".font-weight-normal"))
            if code:
                committees.append(f"{code} {name}".strip())

    events = []
    for row in tree.css("#section3 tbody tr"):
        cells = row.css("td")
        if len(cells) < 2:
            continue
        date = _iso(_text(cells[0]))
        if not date:
            continue
        link = cells[2].css_first("a") if len(cells) > 2 else None
        events.append(Event(date, _text(cells[1]), _text(link) or None))
    return ProcedureFile(ref, title, status, committees, events)


class OEILClient:
    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher

    def procedure(self, procedure_ref: str) -> ProcedureFile | None:
        """The procedure file, or None when OEIL has none (yet)."""
        ref = oeil_ref(procedure_ref)
        if ref is None:
            return None
        try:
            html = self.fetcher.get(page_url(ref)).text
        except FetchError as exc:
            if exc.status == 404:
                return None
            raise OEILUnavailable(f"{exc.status or 'ingen svar'}: {ref}") from exc
        return parse_page(html)
