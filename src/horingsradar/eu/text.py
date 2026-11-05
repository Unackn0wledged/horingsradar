"""The explanatory memorandum of a Commission proposal, cut out of the English PDF.

A COM proposal opens with the explanatory memorandum (context, legal basis, consultations and
impact assessment, fundamental rights, the provisions explained), followed by the legal act,
which starts on a page headed "Proposal for a ...". The memorandum is what the model reads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from horingsradar.extract import marker

# The memorandum's heading; a few proposals leave it out and start with section 1.
MEMO_HEADINGS = ("EXPLANATORY MEMORANDUM", "CONTEXT OF THE PROPOSAL")
# The legal act starts on its own page with "Proposal for a REGULATION/DIRECTIVE/DECISION ...".
LEGAL_ACT_RE = re.compile(r"^\s*Proposal for an?\s", re.MULTILINE)
LEGAL_ACT_WINDOW = 600  # characters from the top of a page
MAX_MEMO_CHARS = 90_000


@dataclass
class Memorandum:
    pages: dict[int, str] = field(default_factory=dict)
    found: bool = True  # False: no memorandum heading; the first pages are used instead
    truncated: bool = False

    @property
    def first_page(self) -> int | None:
        return min(self.pages) if self.pages else None

    @property
    def last_page(self) -> int | None:
        return max(self.pages) if self.pages else None

    @property
    def chars(self) -> int:
        return sum(len(t) for t in self.pages.values())

    @property
    def text(self) -> str:
        """The pages with their `[[side N]]` markers, numbered as in the whole PDF."""
        return "\n".join(f"{marker(n)}\n{self.pages[n].strip()}\n" for n in sorted(self.pages))


def memorandum(pages: dict[int, str], max_chars: int = MAX_MEMO_CHARS) -> Memorandum:
    numbers = sorted(pages)
    start = next((n for n in numbers if any(h in pages[n] for h in MEMO_HEADINGS)), None)
    found = start is not None
    if start is None:
        start = numbers[0] if numbers else 1
    end = next(
        (n for n in numbers if n > start and LEGAL_ACT_RE.search(pages[n][:LEGAL_ACT_WINDOW])),
        None,
    )
    memo = Memorandum(found=found)
    total = 0
    for n in numbers:
        if n < start or (end is not None and n >= end):
            continue
        if total + len(pages[n]) > max_chars and memo.pages:
            memo.truncated = True
            break
        memo.pages[n] = pages[n]
        total += len(pages[n])
    return memo
