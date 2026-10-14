"""Pydantic models for the LLM's assessment output and the JSON schema sent to the API."""

from __future__ import annotations

import copy
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Relevance = Literal["hoy", "middels", "lav", "ingen"]
RELEVANCE_ORDER: dict[str, int] = {"ingen": 0, "lav": 1, "middels": 2, "hoy": 3}
RELEVANCE_LABELS: dict[str, str] = {
    "hoy": "Høy",
    "middels": "Middels",
    "lav": "Lav",
    "ingen": "Ingen",
}

# TODO: tilpass sektorlisten til den interne ansvarsfordelingen (docs/SPEC.md).
SECTORS: tuple[str, ...] = (
    "helse_omsorg",
    "justis_politi",
    "arbeid_velferd",
    "utdanning_forskning",
    "barn_familie",
    "finans_skatt",
    "digitalisering_forvaltning",
    "samferdsel_kommunikasjon",
    "innvandring_integrering",
    "forsvar_sikkerhet",
    "naering",
    "kultur_medier",
    "kommune_bolig",
    "annet",
)

CATEGORIES: tuple[str, ...] = (
    "behandlingsgrunnlag_hjemmel",
    "register",
    "deling_sammenstilling",
    "taushetsplikt",
    "overvaking_kontroll",
    "automatiserte_avgjorelser_ki",
    "saerlige_kategorier",
    "barn",
    "lagring_sletting",
    "rettigheter_innsyn",
    "informasjonssikkerhet",
    "testing_med_personopplysninger",
    "personvernregelverk_tilsyn",
    "annet",
)

Sector = Literal[SECTORS]  # type: ignore[valid-type]
Category = Literal[CATEGORIES]  # type: ignore[valid-type]

MAX_QUOTE_WORDS = 80  # the prompt asks for about 40; this only rejects clearly broken output
NORMATIVE_RE = re.compile(
    r"\b(datatilsynet\s+(bør|burde|anbefaler)|vi\s+(anbefaler|bør|mener)|det\s+anbefales)\b",
    re.IGNORECASE,
)


def _reject_normative(text: str) -> str:
    if NORMATIVE_RE.search(text):
        raise ValueError("normative wording is not allowed (describe, do not recommend)")
    return text


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PageRange(_Strict):
    start: int
    end: int

    @model_validator(mode="after")
    def _ordered(self) -> PageRange:
        if self.start < 1 or self.end < self.start:
            raise ValueError("invalid page range")
        return self


class Finding(_Strict):
    category: Category
    description: str
    document: str
    chapter: str
    page: int
    quote: str

    @field_validator("description")
    @classmethod
    def _description(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("empty description")
        return _reject_normative(v)

    @field_validator("quote")
    @classmethod
    def _quote(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("empty quote")
        if len(v.split()) > MAX_QUOTE_WORDS:
            raise ValueError(f"quote longer than {MAX_QUOTE_WORDS} words")
        return v


class ReadingGuideEntry(_Strict):
    document: str
    pages: list[PageRange]


class PrivacySection(_Strict):
    exists: bool
    document: str | None
    chapter: str | None
    pages: PageRange | None


class AssessmentOutput(_Strict):
    relevance: Relevance
    relevance_reason: str
    sectors: list[Sector] = Field(min_length=1, max_length=3)
    summary: str
    findings: list[Finding]
    reading_guide: list[ReadingGuideEntry]
    privacy_section: PrivacySection

    @field_validator("relevance_reason", "summary")
    @classmethod
    def _text(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("empty text")
        return _reject_normative(v)


UNSUPPORTED_SCHEMA_KEYS = {
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "title",
    "default",
}


def _sanitize(node: Any) -> Any:
    if isinstance(node, dict):
        out = {k: _sanitize(v) for k, v in node.items() if k not in UNSUPPORTED_SCHEMA_KEYS}
        if out.get("type") == "object" and "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out["properties"].keys())
        return out
    if isinstance(node, list):
        return [_sanitize(v) for v in node]
    return node


def output_json_schema() -> dict[str, Any]:
    """JSON schema for `output_config.format`. Constraints the API does not support are dropped
    here and enforced by Pydantic after the response arrives."""
    return _sanitize(copy.deepcopy(AssessmentOutput.model_json_schema()))
