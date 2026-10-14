"""Verification of verbatim quotes against the extracted source text."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz

THRESHOLD = 95.0
_QUOTES = str.maketrans(
    {
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "«": '"',
        "»": '"',
        "″": '"',
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "′": "'",
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "―": "-",
        "−": "-",
        " ": " ",
    }
)
_LINEBREAK_HYPHEN = re.compile(r"(\w)-[ \t]*\n\s*(\w)")
_INTRAWORD_HYPHEN = re.compile(r"(?<=\w)-(?=\w)")
_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Normalise whitespace, hyphenation at line breaks, ligatures and typographic quotes."""
    text = unicodedata.normalize("NFKC", text).replace("­", "")
    text = text.translate(_QUOTES)
    text = _LINEBREAK_HYPHEN.sub(r"\1\2", text)
    text = _INTRAWORD_HYPHEN.sub("", text)
    return _WS.sub(" ", text).strip().casefold()


@dataclass
class QuoteCheck:
    verified: bool
    score: float
    page: int | None  # page where the quote was found (best match if not verified)


def _best_on_page(quote: str, pages: dict[int, str], page: int) -> tuple[float, int]:
    """Score `quote` against page and page+1 (quotes may cross a page break)."""
    here = pages.get(page)
    if here is None:
        return 0.0, page
    after = pages.get(page + 1, "")
    window = f"{here} {after}".strip()
    alignment = fuzz.partial_ratio_alignment(quote, window)
    if alignment is None:
        return 0.0, page
    found_page = page if alignment.dest_start < len(here) else page + 1
    return alignment.score, found_page


def verify_quote(quote: str, pages: dict[int, str], stated_page: int | None) -> QuoteCheck:
    """`pages` must already be normalised with `normalize`."""
    q = normalize(quote)
    if not q or not pages:
        return QuoteCheck(False, 0.0, stated_page)
    best_score, best_page = 0.0, stated_page
    if stated_page is not None:
        for candidate in (stated_page, stated_page - 1, stated_page + 1):
            score, found = _best_on_page(q, pages, candidate)
            if score > best_score:
                best_score, best_page = score, found
        if best_score >= THRESHOLD:
            return QuoteCheck(True, round(best_score, 1), best_page)
    for page in sorted(pages):
        score, found = _best_on_page(q, pages, page)
        if score > best_score:
            best_score, best_page = score, found
            if score >= 100:
                break
    return QuoteCheck(best_score >= THRESHOLD, round(best_score, 1), best_page)


def normalize_pages(pages: dict[int, str]) -> dict[int, str]:
    return {n: normalize(text) for n, text in pages.items()}
