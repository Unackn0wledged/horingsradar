"""Prefilter for EU proposals, without LLM. Favours recall: extra candidates cost little,
missed cases are never seen.

A proposal is a candidate if it has one of the EuroVoc concepts below or a keyword in its
title. Proposals that are not indexed in EuroVoc yet are judged on the title alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Filter B (docs/EU-SPEC.md). IDs verified against the Cellar SPARQL endpoint 29.10.2026.
EUROVOC_FILTER: dict[str, str] = {
    # Core: data protection, privacy, security
    "5181": "data protection",
    "5595": "personal data",
    "2828": "protection of privacy",
    "c_04ae3ba8": "information security",
    "3027": "computer crime",
    "6370": "electronic signature",
    "4488": "data processing",
    "3028": "data-processing law",
    "3030": "artificial intelligence",
    "7410": "biometrics",
    # Broader digital topics
    "7219": "digital technology",
    "6088": "Internet",
    "3011": "cross-frontier data flow",
    "4502": "data transmission",
    "3024": "information storage",
    "6030": "data collection",
    "5334": "disclosure of information",
    "616": "exchange of information",
    "c_31da5694": "e-Health",
    "c_6f7bbe63": "digital single market",
}

# docs/EU-SPEC.md. Keywords with a capital letter are matched case-sensitively as whole words
# (so "AI" does not match "aid"); the others case-insensitively from the start of a word, so
# plurals match too.
DEFAULT_KEYWORDS: tuple[str, ...] = (
    "personal data",
    "data protection",
    "privacy",
    "GDPR",
    "ePrivacy",
    "surveillance",
    "monitoring",
    "tracking",
    "biometric",
    "facial recognition",
    "profiling",
    "identity",
    "eID",
    "digital identity wallet",
    "age verification",
    "data sharing",
    "data access",
    "data space",
    "data retention",
    "metadata",
    "logging",
    "encryption",
    "end-to-end",
    "lawful access",
    "cybersecurity",
    "NIS",
    "incident reporting",
    "connected devices",
    "IoT",
    "cloud",
    "AI",
    "algorithm",
    "automated decision",
    "interoperability",
    "health data",
    "location data",
    "vehicle data",
    "smart meter",
    "child sexual abuse detection",
)


def load_keywords(path: Path | None) -> tuple[str, ...]:
    """Keywords from a file (one per line, # for comments), or the default list."""
    if path is None:
        return DEFAULT_KEYWORDS
    lines = (line.split("#", 1)[0].strip() for line in path.read_text("utf-8").splitlines())
    return tuple(line for line in lines if line)


def _pattern(keyword: str) -> re.Pattern[str]:
    escaped = re.escape(keyword)
    if any(ch.isupper() for ch in keyword):
        return re.compile(rf"(?<!\w){escaped}(?!\w)")
    return re.compile(rf"(?<!\w){escaped}", re.IGNORECASE)


@dataclass
class Prefilter:
    keywords: tuple[str, ...] = DEFAULT_KEYWORDS
    eurovoc: dict[str, str] = field(default_factory=lambda: dict(EUROVOC_FILTER))

    def __post_init__(self) -> None:
        self._patterns = [(k, _pattern(k)) for k in self.keywords]

    def reasons(self, title: str, eurovoc_ids: list[str]) -> dict[str, list[str]]:
        """Why a proposal is a candidate; empty lists mean it is not."""
        return {
            "eurovoc": [c for c in eurovoc_ids if c in self.eurovoc],
            "keywords": [k for k, p in self._patterns if p.search(title or "")],
        }

    def is_candidate(self, title: str, eurovoc_ids: list[str]) -> bool:
        return any(self.reasons(title, eurovoc_ids).values())
