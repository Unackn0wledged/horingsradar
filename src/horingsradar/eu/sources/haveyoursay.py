"""Feedback periods from the Commission's Have Your Say portal (better regulation).

Only EUR-Lex candidates are looked up, by their COM number (docs/EU-SPEC.md, Have Your Say).
Consultations that have no COM proposal yet are not fetched.

What the portal gives an automated client (checked 29.10.2026):
- robots.txt for ec.europa.eu does not disallow /info/law/better-regulation/. Content owned by
  the EU is CC BY 4.0. The JSON endpoints below are the ones the site's own JavaScript calls.
- `findInitiativePathByReference?reference=COM(2026)502` answers `14628-Cloud-and-AI-...`, the
  initiative ID and the path of its page, or 404. The reference has no space; with one it is 400.
- `groupInitiatives/{id}` is the initiative with one publication per stage (call for evidence,
  public consultation, the adopted proposal with its COM number, planned stages), each with its
  own feedback period. The initiative's own reference is often the Ares number of the first
  stage, so the COM number is read from the publications.
- The initiative JSON also names the Commission officials responsible (name, e-mail, phone).
  Only the fields below go into the database; the raw answer stays in the HTTP cache.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from horingsradar.eu.sources.eealex import com_refs
from horingsradar.http import Fetcher, FetchError

BASE = "https://ec.europa.eu/info/law/better-regulation"
API = BASE + "/brpapi"
LOOKUP_URL = API + "/findInitiativePathByReference?reference={ref}"
GROUP_URL = API + "/groupInitiatives/{id}"
PAGE_URL = BASE + "/have-your-say/initiatives/{path}_en"
PATH_RE = re.compile(r"^(\d+)(-[\w-]*)?$")
DATE_RE = re.compile(r"^(\d{4})/(\d{2})/(\d{2})")
PLANNED_RE = re.compile(r"^Q-(\d{4})-([1-4])$")

OPEN = "OPEN"
UPCOMING = "UPCOMING"
# The stage of a Commission proposal that has been adopted (8 weeks of feedback, or so).
ADOPTION = "ADOPTION_WORKFLOW"


class HaveYourSayUnavailable(Exception):
    """The portal refused or answered unexpectedly; the step stops for this run."""


@dataclass
class Publication:
    id: int
    type: str | None
    stage: str | None  # frontEndStage
    reference: str | None
    published: str | None  # ISO date
    deadline: str | None  # ISO date, the last day of the feedback period
    status: str | None  # OPEN, CLOSED, UPCOMING or DISABLED
    planned_period: str | None  # "Q-2027-4" for planned stages
    is_current: bool


@dataclass
class Initiative:
    id: int
    path: str
    reference: str | None
    short_title: str | None
    publications: list[Publication] = field(default_factory=list)

    @property
    def url(self) -> str:
        return PAGE_URL.format(path=self.path)

    def has_com_ref(self, com_ref: str) -> bool:
        refs = [self.reference] + [p.reference for p in self.publications]
        return any(com_ref in com_refs(r) for r in refs if r)

    @property
    def still_open(self) -> bool:
        """Has a stage that is open or still to come, so it is worth fetching again."""
        return any(p.status in (OPEN, UPCOMING) for p in self.publications)


def api_ref(com_ref: str) -> str:
    """COM(2026) 681 -> COM(2026)681, the form the lookup accepts."""
    return re.sub(r"\s+", "", com_ref)


def iso_date(value: str | None) -> str | None:
    m = DATE_RE.match(value or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def planned_label(value: str | None) -> str | None:
    """Q-2027-4 -> 4. kvartal 2027."""
    m = PLANNED_RE.match(value or "")
    return f"{m.group(2)}. kvartal {m.group(1)}" if m else None


def parse_path(text: str) -> str | None:
    path = text.strip().strip('"')
    return path if PATH_RE.match(path) else None


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_initiative(data: dict, path: str) -> Initiative:
    pubs = []
    for p in data.get("publications") or []:
        pub_id = _int(p.get("id"))
        if pub_id is None:
            continue
        pubs.append(
            Publication(
                id=pub_id,
                type=p.get("type"),
                stage=p.get("frontEndStage"),
                reference=p.get("reference"),
                published=iso_date(p.get("publishedDate")),
                deadline=iso_date(p.get("endDate")),
                status=p.get("receivingFeedbackStatus"),
                planned_period=p.get("plannedPeriod") or None,
                is_current=bool(p.get("isCurrent")),
            )
        )
    initiative_id = _int(data.get("id"))
    if initiative_id is None:
        raise HaveYourSayUnavailable(f"initiativ uten ID: {path}")
    return Initiative(
        id=initiative_id,
        path=path,
        reference=data.get("reference"),
        short_title=data.get("shortTitle"),
        publications=pubs,
    )


class HaveYourSayClient:
    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher

    def _get(self, url: str) -> str:
        try:
            return self.fetcher.get(url).text
        except FetchError as exc:
            if exc.status == 404:
                raise
            raise HaveYourSayUnavailable(f"{exc.status or 'ingen svar'}: {url}") from exc

    def find(self, com_ref: str) -> str | None:
        """The path of the initiative for a COM number (`14628-Cloud-and-...`), or None."""
        url = LOOKUP_URL.format(ref=api_ref(com_ref))
        try:
            text = self._get(url)
        except FetchError:  # 404: no initiative for this reference
            return None
        path = parse_path(text)
        if path is None:
            raise HaveYourSayUnavailable(f"uventet svar på oppslag: {url}")
        return path

    def initiative(self, path: str) -> Initiative:
        initiative_id = PATH_RE.match(path).group(1)
        url = GROUP_URL.format(id=initiative_id)
        try:
            data = json.loads(self._get(url))
        except FetchError as exc:
            raise HaveYourSayUnavailable(f"404: {url}") from exc
        except ValueError as exc:
            raise HaveYourSayUnavailable(f"ugyldig JSON: {url}") from exc
        if not isinstance(data, dict):
            raise HaveYourSayUnavailable(f"uventet svar: {url}")
        return parse_initiative(data, path)
