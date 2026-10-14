"""Text extraction. PDFs page by page with `[[side N]]` markers; HTML as a single page."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from horingsradar.sources.regjeringen.parse import html_to_text

log = logging.getLogger(__name__)

MARKER_RE = re.compile(r"^\[\[side (\d+)\]\]$", re.MULTILINE)
LOW_TEXT_CHARS = 50


def marker(page: int) -> str:
    return f"[[side {page}]]"


@dataclass
class Extracted:
    text: str
    page_count: int
    low_text_pages: int = 0

    @property
    def probably_scanned(self) -> bool:
        return self.page_count > 0 and self.low_text_pages / self.page_count > 0.5


def join_pages(pages: list[str]) -> str:
    return "\n".join(f"{marker(i)}\n{page.strip()}\n" for i, page in enumerate(pages, start=1))


def extract_pdf(path: Path) -> Extracted:
    pymupdf.TOOLS.mupdf_display_errors(False)
    with pymupdf.open(path) as doc:
        pages = [page.get_text("text") for page in doc]
    low = sum(1 for p in pages if len(p.strip()) < LOW_TEXT_CHARS)
    result = Extracted(join_pages(pages), len(pages), low)
    if result.probably_scanned:
        log.warning(
            "%s: %s of %s pages have little or no text (scanned?). No OCR in the pilot.",
            path.name,
            low,
            len(pages),
        )
    return result


def extract_html(html: str) -> Extracted:
    return Extracted(join_pages([html_to_text(html)]), 1, 0)


def split_pages(text: str) -> dict[int, str]:
    """Inverse of join_pages: {page number: page text}."""
    pages: dict[int, str] = {}
    matches = list(MARKER_RE.finditer(text))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        pages[int(match.group(1))] = text[match.end() : end].strip("\n")
    return pages


def text_path_for(text_dir: Path, hearing_id: int, document_id: int) -> Path:
    return text_dir / str(hearing_id) / f"{document_id}.txt"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
