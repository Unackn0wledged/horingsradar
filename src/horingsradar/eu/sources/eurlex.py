"""EU proposals from EUR-Lex via the Cellar SPARQL endpoint (publications.europa.eu).

EUR-Lex's own web pages sit behind bot protection and are not fetched; the EUR-Lex links are
for people. The endpoint answers JSON when asked with the `format` parameter, so the shared
Fetcher (User-Agent, throttling, robots.txt) is used as it is. Queries are kept narrow: broad
ones (for example on labels) run into the endpoint's 60 second limit.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import quote, urlencode

from horingsradar.http import Fetcher

ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"
JSON_FORMAT = "application/sparql-results+json"
PAGE_SIZE = 200
# Discovery runs in date windows: one query over months takes minutes on a cold endpoint
# (above the HTTP timeout), while a 30-day window answers in about a second.
WINDOW_DAYS = 30
BATCH_SIZE = 40  # URIs per VALUES block; keeps the GET URL short
PROPOSAL_TYPES = ("PROP_REG", "PROP_DIR", "PROP_DEC")
EUROVOC_BASE = "http://eurovoc.europa.eu/"
CELEX_RE = re.compile(r"^5(\d{4})(PC|JC)(\d+)")
CELEX_PREFIXES = {"PC": "COM", "JC": "JOIN"}
WITHDRAWN = "WITHDRAWN"
# 52025PC0828 from 52025PC0828R(01) (corrigendum) or 52025PC0101(01) (an extra part).
BASE_CELEX_RE = re.compile(r"^\d{5}[A-Z]{1,2}\d{4}")

PREFIXES = """PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX cmr: <http://publications.europa.eu/ontology/cdm/cmr#>
PREFIX rt: <http://publications.europa.eu/resource/authority/resource-type/>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
"""

# One row per proposal, narrowed by {values} (a list of CELEX numbers) or {date_filter}.
PROPOSALS = (
    PREFIXES
    + """SELECT ?work ?celex ?type ?date
  (SAMPLE(?title_) AS ?title) (SAMPLE(?eea_) AS ?eea) (SAMPLE(?modified_) AS ?modified)
  (SAMPLE(?prefix_) AS ?prefix) (SAMPLE(?year_) AS ?year) (SAMPLE(?number_) AS ?number)
  (GROUP_CONCAT(DISTINCT STR(?eurovoc_); separator=" ") AS ?eurovoc)
WHERE {{
  {values}
  ?work cdm:work_has_resource-type ?type .
  FILTER(?type IN ({types}))
  ?work cdm:work_date_document ?date .
  {date_filter}
  ?work cdm:resource_legal_id_celex ?celex .
  OPTIONAL {{ ?work cdm:work_title ?title_ . FILTER(lang(?title_) = "en") }}
  OPTIONAL {{ ?work cdm:resource_legal_eea ?eea_ }}
  OPTIONAL {{ ?work cmr:lastModificationDate ?modified_ }}
  OPTIONAL {{ ?work cdm:resource_legal_internal_number_prefix ?prefix_ }}
  OPTIONAL {{ ?work cdm:resource_legal_internal_number_year ?year_ }}
  OPTIONAL {{ ?work cdm:resource_legal_internal_number_sequential_number ?number_ }}
  OPTIONAL {{ ?work cdm:work_is_about_concept_eurovoc ?eurovoc_ }}
}}
GROUP BY ?work ?celex ?type ?date
ORDER BY ?date ?celex
{paging}"""
)

DOSSIERS = (
    PREFIXES
    + """SELECT ?work (SAMPLE(?procedure_) AS ?procedure) (SAMPLE(?status_) AS ?status)
  (MAX(?withdrawn_) AS ?withdrawn)
WHERE {{
  VALUES ?work {{ {works} }}
  ?work cdm:work_part_of_dossier ?dossier .
  OPTIONAL {{ ?dossier cdm:procedure_code_interinstitutional_reference_procedure ?procedure_ }}
  OPTIONAL {{ ?dossier cdm:procedure_code_interinstitutional_has_status_file-status ?status_ }}
  OPTIONAL {{ ?dossier cdm:dossier_withdrawn-proposal ?withdrawn_ }}
}}
GROUP BY ?work"""
)

# The English PDF of each work: the first item of its PDF manifestation.
ENGLISH_PDF = (
    PREFIXES
    + """SELECT ?work (MIN(STR(?item_)) AS ?item)
WHERE {{
  VALUES ?work {{ {works} }}
  ?expression cdm:expression_belongs_to_work ?work ;
    cdm:expression_uses_language <http://publications.europa.eu/resource/authority/language/ENG> .
  ?manifestation cdm:manifestation_manifests_expression ?expression ;
    cdm:manifestation_type "pdf"^^xsd:string .
  ?item_ cdm:item_belongs_to_manifestation ?manifestation .
}}
GROUP BY ?work"""
)

LABELS = (
    PREFIXES
    + """SELECT ?concept ?label
WHERE {{
  VALUES ?concept {{ {concepts} }}
  ?concept skos:prefLabel ?label .
  FILTER(lang(?label) = "en")
}}"""
)


@dataclass
class Proposal:
    work_uri: str
    celex: str
    doc_type: str
    date_document: str
    title: str
    eea_flag: int | None
    com_ref: str | None
    modified: str | None
    eurovoc: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return eurlex_url(self.celex)


def base_celex(celex: str) -> str:
    m = BASE_CELEX_RE.match(celex)
    return m.group(0) if m else celex


def eurlex_url(celex: str) -> str:
    return f"https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{quote(celex)}"


def com_ref(prefix: str | None, year: str | None, number: str | None, celex: str) -> str | None:
    """COM(2026) 569 from the internal number, or from the CELEX number when that is missing."""
    if prefix and year and number and number.isdigit():
        return f"{prefix}({year}) {int(number)}"
    m = CELEX_RE.match(celex)
    if m and m.group(2) in CELEX_PREFIXES:
        return f"{CELEX_PREFIXES[m.group(2)]}({m.group(1)}) {int(m.group(3))}"
    return None


def _types() -> str:
    return ", ".join(f"rt:{t}" for t in PROPOSAL_TYPES)


def proposals_between_query(start: date, end: date, offset: int = 0, limit: int = PAGE_SIZE) -> str:
    """Proposals dated from `start` up to, but not including, `end`."""
    return PROPOSALS.format(
        values="",
        types=_types(),
        date_filter=(
            f'FILTER(?date >= "{start.isoformat()}"^^xsd:date'
            f' && ?date < "{end.isoformat()}"^^xsd:date)'
        ),
        paging=f"LIMIT {limit} OFFSET {offset}",
    )


def proposals_by_celex_query(celexes: list[str]) -> str:
    # CELEX numbers are stored as typed literals; plain strings do not match.
    values = " ".join(f'"{c}"^^xsd:string' for c in celexes)
    return PROPOSALS.format(
        values=f"VALUES ?celex {{ {values} }}", types=_types(), date_filter="", paging=""
    )


def dossiers_query(work_uris: list[str]) -> str:
    return DOSSIERS.format(works=" ".join(f"<{w}>" for w in work_uris))


def english_pdf_query(work_uris: list[str]) -> str:
    return ENGLISH_PDF.format(works=" ".join(f"<{w}>" for w in work_uris))


def labels_query(concept_ids: list[str]) -> str:
    return LABELS.format(concepts=" ".join(f"<{EUROVOC_BASE}{c}>" for c in concept_ids))


def _value(row: dict, key: str) -> str | None:
    cell = row.get(key)
    return cell["value"] if cell else None


def parse_proposals(data: dict) -> list[Proposal]:
    proposals = []
    for row in data["results"]["bindings"]:
        celex = _value(row, "celex") or ""
        eea = _value(row, "eea")
        eurovoc = (_value(row, "eurovoc") or "").split()
        proposals.append(
            Proposal(
                work_uri=_value(row, "work") or "",
                celex=celex,
                doc_type=(_value(row, "type") or "").rsplit("/", 1)[-1],
                date_document=_value(row, "date") or "",
                title=_value(row, "title") or celex,
                eea_flag=int(eea) if eea is not None and eea.isdigit() else None,
                com_ref=com_ref(
                    _value(row, "prefix"), _value(row, "year"), _value(row, "number"), celex
                ),
                modified=_value(row, "modified"),
                eurovoc=sorted(e.removeprefix(EUROVOC_BASE) for e in eurovoc),
            )
        )
    return proposals


def parse_dossiers(data: dict) -> dict[str, tuple[str | None, str | None]]:
    """work URI -> (procedure reference, status).

    Status is the file status code (PROP_PENDING, PROP_ADOPTED, PROP_REJECTED), or WITHDRAWN
    when the proposal is withdrawn, which Cellar marks with a separate flag.
    """
    result = {}
    for row in data["results"]["bindings"]:
        status = _value(row, "status")
        code = status.rsplit("/", 1)[-1] if status else None
        if _value(row, "withdrawn") == "1":
            code = WITHDRAWN
        result[_value(row, "work") or ""] = (_value(row, "procedure"), code)
    return result


def parse_english_pdfs(data: dict) -> dict[str, str]:
    """work URI -> URL of its English PDF (https; Cellar's identifiers are http)."""
    return {
        _value(row, "work") or "": (_value(row, "item") or "").replace("http://", "https://", 1)
        for row in data["results"]["bindings"]
        if _value(row, "item")
    }


def parse_labels(data: dict) -> dict[str, str]:
    return {
        (_value(row, "concept") or "").removeprefix(EUROVOC_BASE): _value(row, "label") or ""
        for row in data["results"]["bindings"]
    }


def _batches(items: list[str], size: int = BATCH_SIZE) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class EurLexClient:
    def __init__(self, fetcher: Fetcher, endpoint: str = ENDPOINT):
        self.fetcher = fetcher
        self.endpoint = endpoint

    def select(self, query: str) -> dict:
        url = f"{self.endpoint}?{urlencode({'query': query, 'format': JSON_FORMAT})}"
        return json.loads(self.fetcher.get(url).content)

    def proposals_since(
        self, since: date, until: date, page_size: int = PAGE_SIZE
    ) -> Iterator[Proposal]:
        """Proposals dated from `since` to `until` (inclusive), window by window."""
        start = since
        while start <= until:
            end = min(start + timedelta(days=WINDOW_DAYS), until + timedelta(days=1))
            offset = 0
            while True:
                query = proposals_between_query(start, end, offset, page_size)
                page = parse_proposals(self.select(query))
                yield from page
                if len(page) < page_size:
                    break
                offset += page_size
            start = end

    def proposals_by_celex(self, celexes: list[str]) -> list[Proposal]:
        return [
            p
            for b in _batches(celexes)
            for p in parse_proposals(self.select(proposals_by_celex_query(b)))
        ]

    def dossiers(self, work_uris: list[str]) -> dict[str, tuple[str | None, str | None]]:
        result: dict[str, tuple[str | None, str | None]] = {}
        for batch in _batches(work_uris):
            result.update(parse_dossiers(self.select(dossiers_query(batch))))
        return result

    def english_pdfs(self, work_uris: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for batch in _batches(work_uris):
            result.update(parse_english_pdfs(self.select(english_pdf_query(batch))))
        return result

    def labels(self, concept_ids: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for batch in _batches(concept_ids):
            result.update(parse_labels(self.select(labels_query(batch))))
        return result
