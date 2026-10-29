"""EDPS opinions on proposals, from the Council's documents in Cellar.

What is available to an automated client (checked 29.10.2026):
- edps.europa.eu answers 403 to this client, robots.txt included. It is not fetched.
- The Official Journal summaries of EDPS opinions, and EDPS events in the procedures in Cellar,
  stop in 2023.
- The Council registers many EDPS opinions as cover notes to the delegations ("Proposal for a
  ... - Opinion of the European Data Protection Supervisor"). They are in Cellar with the
  Council as author, often linked to the procedure (work_part_of_dossier). Not every opinion is
  registered, so the list is incomplete: a case without a flag may still have an opinion.

A note is linked to a case by procedure number; without one, only when the proposal title in
the note is the same as the case's title (normalised for case and whitespace).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta

from horingsradar.eu.sources.eurlex import PREFIXES, PROPOSAL_TYPES, EurLexClient
from horingsradar.quotes import normalize

WINDOW_DAYS = 183  # half a year answers in about a second
COUNCIL = "http://publications.europa.eu/resource/authority/corporate-body/CONSIL"
ENGLISH = "http://publications.europa.eu/resource/authority/language/ENG"
DOCUMENT_URL = "https://data.consilium.europa.eu/doc/document/{doc}/en/pdf"

# Council documents dated in a window whose English title is about an EDPS opinion, with the
# Council document number and the procedures they belong to.
NOTES = (
    PREFIXES
    + """SELECT ?work ?date (SAMPLE(?type_) AS ?type) (SAMPLE(?title_) AS ?title)
  (SAMPLE(?doc_) AS ?doc) (GROUP_CONCAT(DISTINCT STR(?procedure_); separator=" ") AS ?procedures)
  (GROUP_CONCAT(DISTINCT STR(?celex_); separator=" ") AS ?celexes)
WHERE {{
  ?work cdm:work_created_by_agent <{council}> .
  ?work cdm:work_date_document ?date .
  FILTER(?date >= "{start}"^^xsd:date && ?date < "{end}"^^xsd:date)
  ?expr cdm:expression_belongs_to_work ?work ;
    cdm:expression_uses_language <{english}> ;
    cdm:expression_title ?title_ .
  FILTER(CONTAINS(LCASE(STR(?title_)), "opinion")
    && (CONTAINS(STR(?title_), "Data Protection Supervisor") || CONTAINS(STR(?title_), "EDPS")))
  OPTIONAL {{ ?work cdm:work_has_resource-type ?type_ }}
  OPTIONAL {{ ?work cdm:work_id_document ?doc_ . FILTER(STRSTARTS(STR(?doc_), "consil:")) }}
  OPTIONAL {{
    ?work cdm:work_part_of_dossier ?dossier .
    OPTIONAL {{ ?dossier cdm:procedure_code_interinstitutional_reference_procedure ?procedure_ }}
    OPTIONAL {{
      ?proposal cdm:work_part_of_dossier ?dossier ;
        cdm:work_has_resource-type ?proposal_type ;
        cdm:resource_legal_id_celex ?celex_ .
      FILTER(?proposal_type IN ({types}))
    }}
  }}
}}
GROUP BY ?work ?date
ORDER BY ?date ?work"""
)

OPINION_SUFFIX_RE = re.compile(r"\s+[-–]\s+(?:[\w ]+ )?opinion of the\b.*$", re.IGNORECASE)
PROPOSAL_RE = re.compile(r"\bproposal for\b", re.IGNORECASE)


@dataclass
class CouncilNote:
    work_uri: str
    date: str
    title: str
    doc: str | None  # ST 12655 2026 INIT
    procedures: list[str] = field(default_factory=list)  # 2026/0165/COD
    celexes: list[str] = field(default_factory=list)  # the proposals in the same procedure

    @property
    def url(self) -> str | None:
        return document_url(self.doc)

    @property
    def proposal_title(self) -> str | None:
        """The proposal's title as the note gives it, normalised for comparison."""
        return proposal_title(self.title)


def document_url(doc: str | None) -> str | None:
    """ST 12655 2026 INIT -> the Council register's PDF (pattern checked 29.10.2026)."""
    return DOCUMENT_URL.format(doc=doc.replace(" ", "-")) if doc else None


def proposal_title(title: str) -> str | None:
    """The proposal's title in a note's title, normalised; None if there is none.

    "Proposal for a Regulation ... - Opinion of the European Data Protection Supervisor"
    "Opinion of the European Data Protection Supervisor on the Proposal for a Directive ..."
    """
    text = OPINION_SUFFIX_RE.sub("", title)
    m = PROPOSAL_RE.search(text)
    return normalize_title(text[m.start() :]) if m else None


def normalize_title(title: str) -> str:
    """Case, whitespace and typographic quotes normalised; no similarity measure."""
    return normalize(title).rstrip(" .")


def same_title(a: str, b: str | None) -> bool:
    return b is not None and normalize_title(a) == b


def notes_query(start: date, end: date) -> str:
    return NOTES.format(
        council=COUNCIL,
        english=ENGLISH,
        types=", ".join(f"rt:{t}" for t in PROPOSAL_TYPES),
        start=start.isoformat(),
        end=end.isoformat(),
    )


def _value(row: dict, key: str) -> str | None:
    cell = row.get(key)
    return cell["value"] if cell else None


def parse_notes(data: dict) -> list[CouncilNote]:
    notes = []
    for row in data["results"]["bindings"]:
        doc = _value(row, "doc")
        notes.append(
            CouncilNote(
                work_uri=_value(row, "work") or "",
                date=(_value(row, "date") or "")[:10],
                title=_value(row, "title") or "",
                doc=doc.removeprefix("consil:").replace("_", " ") if doc else None,
                procedures=sorted((_value(row, "procedures") or "").split()),
                celexes=sorted((_value(row, "celexes") or "").split()),
            )
        )
    return notes


def notes_since(client: EurLexClient, since: date, until: date) -> Iterator[CouncilNote]:
    """Council notes on EDPS opinions dated from `since` to `until` (inclusive)."""
    start = since
    while start <= until:
        end = min(start + timedelta(days=WINDOW_DAYS), until + timedelta(days=1))
        yield from parse_notes(client.select(notes_query(start, end)))
        start = end
