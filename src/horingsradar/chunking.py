"""Splitting large documents into chapter-based parts that fit the input budget."""

from __future__ import annotations

import re
from dataclasses import dataclass

TOC_LINE = re.compile(r"(\.{4,}|…{2,})\s*\d+\s*$")
NUMBER_ONLY = re.compile(r"^(\d{1,2})\.?$")
NUMBER_TITLE = re.compile(r"^(\d{1,2})\.?\s+([A-ZÆØÅ][^\n]{2,118})$")
KAPITTEL = re.compile(r"^(?:kapittel|kap\.)\s+(\d{1,2})\.?\s*(.*)$", re.IGNORECASE)
TITLE_LINE = re.compile(r"^[A-ZÆØÅ«\"(][^\n]{2,118}$")
SUMMARY_TITLE = re.compile(
    r"sammendrag|samandrag|hovedinnhold|hovudinnhald|innledning|innleiing", re.IGNORECASE
)


@dataclass(frozen=True)
class Section:
    start: int  # first page (inclusive)
    end: int  # last page (inclusive)
    label: str

    @property
    def pages(self) -> range:
        return range(self.start, self.end + 1)


def _is_toc_page(text: str) -> bool:
    return sum(1 for line in text.splitlines() if TOC_LINE.search(line.strip())) >= 5


def find_chapters(pages: dict[int, str]) -> list[tuple[int, int, str]]:
    """Return (page, chapter number, title) for top-level numbered chapters, in order.

    Chapter numbers must increase by one, which filters out page numbers and footnotes.
    """
    found: list[tuple[int, int, str]] = []
    expected: int | None = None
    for page_no in sorted(pages):
        text = pages[page_no]
        if _is_toc_page(text):
            continue
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines and lines[0] == str(page_no):
            lines = lines[1:]
        for i, line in enumerate(lines):
            number: int | None = None
            title = ""
            if m := (KAPITTEL.match(line) or NUMBER_TITLE.match(line)):
                number, title = int(m.group(1)), m.group(2).strip()
            elif (m := NUMBER_ONLY.match(line)) and i + 1 < len(lines):
                nxt = lines[i + 1]
                if TITLE_LINE.match(nxt) and not nxt.endswith((".", ",")):
                    number, title = int(m.group(1)), nxt
            if number is None:
                continue
            if expected is None and number > 3:
                continue
            if expected is not None and number != expected:
                continue
            if title.endswith((".", ",", ";", "?", ":")) or TOC_LINE.search(title):
                continue  # sentences, questions and list items are not chapter headings
            if found and found[-1][0] == page_no:
                continue  # at most one top-level chapter starts on a page
            found.append((page_no, number, title))
            expected = number + 1
    return found


def sections_for(pages: dict[int, str], window: int = 25) -> list[Section]:
    """Chapter sections covering every page. Falls back to fixed page windows."""
    if not pages:
        return []
    first, last = min(pages), max(pages)
    chapters = find_chapters(pages)
    sections: list[Section] = []
    if len(chapters) >= 2:
        if chapters[0][0] > first:
            sections.append(Section(first, chapters[0][0], "forside og innhold"))
        for i, (page, number, title) in enumerate(chapters):
            end = chapters[i + 1][0] if i + 1 < len(chapters) else last
            sections.append(Section(page, max(page, end), f"kapittel {number} {title}".strip()))
        return sections
    for start in range(first, last + 1, window):
        end = min(start + window - 1, last)
        sections.append(Section(start, end, f"sidene {start}–{end}"))
    return sections


def split_section(section: Section, pages: dict[int, str], budget_chars: int) -> list[Section]:
    """Split a section that is too large into page windows that fit the budget."""
    parts: list[Section] = []
    start = section.start
    size = 0
    for page in section.pages:
        length = len(pages.get(page, "")) + 16
        if size and size + length > budget_chars:
            parts.append(Section(start, page - 1, f"{section.label}, s. {start}–{page - 1}"))
            start, size = page, 0
        size += length
    parts.append(Section(start, section.end, f"{section.label}, s. {start}–{section.end}"))
    return parts if len(parts) > 1 else [section]


def summary_section(sections: list[Section]) -> Section | None:
    for section in sections[:4]:
        if SUMMARY_TITLE.search(section.label):
            return section
    return None


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
