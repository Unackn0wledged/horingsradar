"""Pydantic models for the EU assessment, and the JSON schema sent to the model.

The relevance scale, the privacy categories and the check against the model's own stance are
the same as in the regjeringen.no radar; the sectors are the EU radar's own.
"""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import Field, field_validator

from horingsradar.llm.schema import (
    MAX_QUOTE_WORDS,
    Category,
    Relevance,
    _reject_own_stance,
    _sanitize,
    _Strict,
)

EU_SECTOR_LABELS: dict[str, str] = {
    "digitale_tjenester_plattformer": "Digitale tjenester og plattformer",
    "cybersikkerhet": "Cybersikkerhet",
    "digital_identitet": "Digital identitet og tillitstjenester",
    "kunstig_intelligens": "Kunstig intelligens",
    "data_datadeling": "Dataøkonomi og datadeling",
    "helse": "Helse",
    "justis_politi": "Justis, politi og rettshåndhevelse",
    "grensekontroll_migrasjon": "Grensekontroll, migrasjon og visum",
    "finans_betaling": "Finans og betaling",
    "transport_kjoretoy": "Transport og kjøretøy",
    "energi_miljo": "Energi og miljø",
    "arbeidsliv_trygd": "Arbeidsliv og trygd",
    "forbrukervern_indre_marked": "Forbrukervern og indre marked",
    "internasjonale_avtaler": "Internasjonale avtaler og samarbeid",
    "annet": "Annet",
}
EU_SECTORS: tuple[str, ...] = tuple(EU_SECTOR_LABELS)
EUSector = Literal[EU_SECTORS]  # type: ignore[valid-type]


class EUFinding(_Strict):
    category: Category
    description: str
    chapter: str
    page: int
    quote: str

    @field_validator("description")
    @classmethod
    def _description(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("empty description")
        return _reject_own_stance(v)

    @field_validator("quote")
    @classmethod
    def _quote(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("empty quote")
        if len(v.split()) > MAX_QUOTE_WORDS:
            raise ValueError(f"quote longer than {MAX_QUOTE_WORDS} words")
        return v


class EUAssessmentOutput(_Strict):
    relevance: Relevance
    relevance_reason: str
    sectors: list[EUSector] = Field(min_length=1, max_length=3)
    summary: str
    findings: list[EUFinding]

    @field_validator("relevance_reason", "summary")
    @classmethod
    def _text(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("empty text")
        return _reject_own_stance(v)


def eu_output_json_schema() -> dict[str, Any]:
    """JSON schema for `output_config.format`; unsupported constraints are enforced by Pydantic."""
    return _sanitize(copy.deepcopy(EUAssessmentOutput.model_json_schema()))
