"""A fake Cellar SPARQL endpoint that answers from the captured responses in fixtures/eurlex."""

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


def _response(bindings: list[dict]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"head": {"vars": []}, "results": {"bindings": bindings}},
        headers={"content-type": "application/sparql-results+json"},
    )


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
    queries: list[str] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        if url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        query = parse_qs(url.query)["query"][0]
        assert parse_qs(url.query)["format"] == ["application/sparql-results+json"]
        self.queries.append(query)
        if "VALUES ?celex" in query:
            wanted = set(re.findall(r'"(\w+)"\^\^xsd:string', query))
            return _response([b for b in self.recall if b["celex"]["value"] in wanted])
        if "work_part_of_dossier" in query:
            wanted = set(re.findall(r"<(http[^>]+)>", query.split("VALUES ?work", 1)[1]))
            return _response([b for b in self.dossiers if b["work"]["value"] in wanted])
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
