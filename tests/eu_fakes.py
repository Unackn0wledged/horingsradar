"""Fake EU sources that answer from captured responses: the Cellar SPARQL endpoint
(fixtures/eurlex), Have Your Say (fixtures/haveyoursay) and EEA-Lex (fixtures/eealex)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

EURLEX = Path(__file__).parent / "fixtures" / "eurlex"


def load(name: str) -> dict:
    return json.loads((EURLEX / name).read_text(encoding="utf-8"))


EDPS = Path(__file__).parent / "fixtures" / "edps"
OEIL = Path(__file__).parent / "fixtures" / "oeil"


def load_edps(name: str) -> dict:
    return json.loads((EDPS / name).read_text(encoding="utf-8"))


def _response(bindings: list[dict]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"head": {"vars": []}, "results": {"bindings": bindings}},
        headers={"content-type": "application/sparql-results+json"},
    )


HYS = Path(__file__).parent / "fixtures" / "haveyoursay"


def hys_group(initiative_id: int) -> dict:
    return json.loads((HYS / f"group_{initiative_id}.json").read_text(encoding="utf-8"))


@dataclass
class HYSSite:
    """Fake Have Your Say API: the captured lookups and initiatives, with knobs for tests."""

    lookups: dict[str, str] = field(
        default_factory=lambda: {
            ref: text
            for ref, text in json.loads((HYS / "lookups.json").read_text("utf-8")).items()
            if not text.startswith("{")
        }
    )
    groups: dict[int, dict] = field(
        default_factory=lambda: {i: hys_group(i) for i in (19418, 14628, 15792)}
    )
    status: int = 200  # anything else: every API call answers with it
    requests: list[str] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        if url.path == "/robots.txt":
            return httpx.Response(200, text=(HYS / "robots.txt").read_text(encoding="utf-8"))
        self.requests.append(str(request.url))
        if self.status != 200:
            return httpx.Response(self.status, json={"code": "error"})
        api = "/info/law/better-regulation/brpapi/"
        assert url.path.startswith(api), url.path
        endpoint = url.path.removeprefix(api)
        if endpoint == "findInitiativePathByReference":
            ref = parse_qs(url.query)["reference"][0]
            if " " in ref:
                return httpx.Response(400, json={"code": "bad_request"})
            if ref not in self.lookups:
                return httpx.Response(404, json={"code": "not_found"})
            return httpx.Response(
                200, text=self.lookups[ref], headers={"content-type": "application/json"}
            )
        initiative_id = int(endpoint.removeprefix("groupInitiatives/"))
        if initiative_id not in self.groups:
            return httpx.Response(404, json={"code": "not_found"})
        return httpx.Response(200, json=self.groups[initiative_id])


@dataclass
class EUSite:
    proposals: list[dict] = field(
        default_factory=lambda: (
            load("proposals_2026-09.json")["results"]["bindings"]
            + load("proposals_2026-10.json")["results"]["bindings"]
        )
    )
    recall: list[dict] = field(
        default_factory=lambda: load("recall_set.json")["results"]["bindings"]
    )
    dossiers: list[dict] = field(
        default_factory=lambda: load("dossiers.json")["results"]["bindings"]
    )
    labels: list[dict] = field(default_factory=lambda: load("labels.json")["results"]["bindings"])
    english_pdfs: list[dict] = field(
        default_factory=lambda: load("english_pdfs.json")["results"]["bindings"]
    )
    edps_notes: list[dict] = field(default_factory=list)  # none unless a test adds them
    # OEIL procedure files by reference; anything else is OEIL's real 404 page.
    oeil_pages: dict[str, str] = field(
        default_factory=lambda: {
            p.stem.removeprefix("procedure_").replace("_", "/", 1).replace("_", "(", 1) + ")": (
                p.read_text(encoding="utf-8")
            )
            for p in OEIL.glob("procedure_*.html")
        }
    )
    oeil_requests: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    downloads: list[str] = field(default_factory=list)
    hys: HYSSite = field(default_factory=HYSSite)

    def add_edps(self, proposals: bool = True) -> None:
        """The real Council notes on EDPS opinions (24 months to 29.10.2026) and, optionally,
        the real proposals three of them are about (Cloud and AI, Eurojust and Europol, June
        2026) with their procedures."""
        self.edps_notes = load_edps("council_notes.json")["results"]["bindings"]
        if proposals:
            self.proposals += load_edps("proposals.json")["results"]["bindings"]
            self.dossiers += load_edps("dossiers.json")["results"]["bindings"]

    def pdf_for(self, url: str) -> bytes | None:
        """The fixture PDF behind an item URL from english_pdfs.json (by the work's CELEX)."""
        tail = url.split("//", 1)[1]
        work = next(
            (b["work"]["value"] for b in self.english_pdfs if b["item"]["value"].endswith(tail)),
            None,
        )
        celex = next(
            (b["celex"]["value"] for b in self.proposals if b["work"]["value"] == work), None
        )
        path = EURLEX / "pdf" / f"{celex}.pdf"
        return path.read_bytes() if celex and path.is_file() else None

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        if url.hostname == "ec.europa.eu":
            return self.hys.handler(request)
        if url.hostname == "oeil.europarl.europa.eu":
            return self.oeil_handler(url)
        if url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if url.path.startswith("/resource/cellar/"):
            self.downloads.append(str(request.url))
            pdf = self.pdf_for(str(request.url))
            if pdf is None:
                return httpx.Response(404, text="not found")
            return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
        query = parse_qs(url.query)["query"][0]
        assert parse_qs(url.query)["format"] == ["application/sparql-results+json"]
        self.queries.append(query)
        if "Data Protection Supervisor" in query:
            start, end = re.findall(r'"(\d{4}-\d{2}-\d{2})"\^\^xsd:date', query)
            return _response([b for b in self.edps_notes if start <= b["date"]["value"] < end])
        if "VALUES ?celex" in query:
            wanted = set(re.findall(r'"(\w+)"\^\^xsd:string', query))
            return _response([b for b in self.recall if b["celex"]["value"] in wanted])
        if "work_part_of_dossier" in query:
            wanted = set(re.findall(r"<(http[^>]+)>", query.split("VALUES ?work", 1)[1]))
            return _response([b for b in self.dossiers if b["work"]["value"] in wanted])
        if "manifestation_type" in query:
            wanted = set(re.findall(r"<(http[^>]+)>", query.split("VALUES ?work", 1)[1]))
            return _response([b for b in self.english_pdfs if b["work"]["value"] in wanted])
        if "skos:prefLabel" in query:
            wanted = set(re.findall(r"<(http://eurovoc[^>]+)>", query))
            return _response([b for b in self.labels if b["concept"]["value"] in wanted])
        start, end = re.findall(r'"(\d{4}-\d{2}-\d{2})"\^\^xsd:date', query)
        limit = int(re.search(r"LIMIT (\d+)", query).group(1))
        offset = int(re.search(r"OFFSET (\d+)", query).group(1))
        rows = sorted(
            (b for b in self.proposals if start <= b["date"]["value"] < end),
            key=lambda b: (b["date"]["value"], b["celex"]["value"]),
        )
        return _response(rows[offset : offset + limit])

    def oeil_handler(self, url) -> httpx.Response:
        if url.path == "/robots.txt":  # OEIL has none
            return httpx.Response(404, text="Not Found")
        assert url.path == "/oeil/en/procedure-file", url.path
        ref = parse_qs(url.query)["reference"][0]
        self.oeil_requests.append(ref)
        if ref in self.oeil_pages:
            return httpx.Response(
                200, text=self.oeil_pages[ref], headers={"content-type": "text/html"}
            )
        return httpx.Response(404, text=(OEIL / "not_found.html").read_text(encoding="utf-8"))

    def proposal(self, com_number: str) -> dict:
        """The binding for COM(2026) <number>, for tests that change it."""
        return next(b for b in self.proposals if b["celex"]["value"] == f"52026PC{com_number:0>4}")

    def dossier(self, work_uri: str) -> dict:
        return next(b for b in self.dossiers if b["work"]["value"] == work_uri)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler), follow_redirects=True)


def make_eu_fetcher(settings, site: EUSite):
    from horingsradar.http import Fetcher

    return Fetcher(settings, client=site.client(), sleep=lambda s: None)


EEALEX = Path(__file__).parent / "fixtures" / "eealex"
EMPTY_LIST = "<html><body><main><p>No results</p></main></body></html>"


def teaser(key: str, com_ref: str, title: str) -> str:
    """One row of the EEA-Lex list, in the site's own markup."""
    return (
        '<article data-component-id="storyworx:teaser" class="teaser lex-teaser">'
        f'<div class="teaser-body"><div class="teaser-main-content"><a href="/eea-lex/{key}"'
        f' class="quicklink"><h4 class="lex-teaser-title">Factsheet - {key.upper()}</h4></a>'
        f'<h5 class="lex-teaser-subtitle">{com_ref}</h5><div class="lex-teaser-documents-wrapper">'
        f'<a href="/eea-lex/{key}#anchor-eu"><div class="lex-teaser-document-wrapper">'
        f'<div class="lex-teaser-description">{title}</div></div></a></div></div></div></article>'
    )


@dataclass
class EEASite:
    """Fake efta.int: the real list pages and fact sheets, with knobs for failures."""

    pages: list[str] = field(
        default_factory=lambda: [
            (EEALEX / "list_page0.html").read_text(encoding="utf-8"),
            (EEALEX / "list_page1.html").read_text(encoding="utf-8"),
        ]
    )
    factsheets: dict[str, str] = field(
        default_factory=lambda: {
            "32016r0679": (EEALEX / "factsheet_32016r0679.html").read_text(encoding="utf-8"),
            "32024r1689": (EEALEX / "factsheet_32024r1689.html").read_text(encoding="utf-8"),
        }
    )
    loop: set[str] = field(default_factory=set)  # keys that redirect to themselves
    list_status: int = 200
    requests: list[str] = field(default_factory=list)

    def add_rows(self, rows: list[str]) -> None:
        """Extra rows at the top of the first list page."""
        self.pages[0] = self.pages[0].replace("<article", "".join(rows) + "<article", 1)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        self.requests.append(str(request.url))
        if url.path == "/robots.txt":
            return httpx.Response(200, text=(EEALEX / "robots.txt").read_text(encoding="utf-8"))
        if url.path == "/eea-lex":
            if self.list_status != 200:
                return httpx.Response(self.list_status, text="slow down")
            page = int(parse_qs(url.query)["page"][0])
            html = self.pages[page] if page < len(self.pages) else EMPTY_LIST
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})
        key = url.path.removeprefix("/eea-lex/")
        if key in self.loop:
            return httpx.Response(301, headers={"location": url.path})
        if key in self.factsheets:
            return httpx.Response(
                200, text=self.factsheets[key], headers={"content-type": "text/html"}
            )
        return httpx.Response(404, text="Page not found")


def make_eea_fetcher(settings, site: EEASite):
    from horingsradar.eu.sources.eealex import MAX_REDIRECTS
    from horingsradar.http import Fetcher

    client = httpx.Client(
        transport=httpx.MockTransport(site.handler),
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
    )
    return Fetcher(settings, client=client, sleep=lambda s: None)
