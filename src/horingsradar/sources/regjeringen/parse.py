"""HTML parsers for regjeringen.no hearing pages.

Built against real pages stored in tests/fixtures/regjeringen/. Handles both bokmål and nynorsk
labels (Høringsfrist/Høyringsfrist, På høring/På høyring, Ferdigbehandlet/Ferdigbehandla).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urljoin, urlparse

from selectolax.lexbor import LexborHTMLParser, LexborNode

DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
ID_RE = re.compile(r"/id(\d+)/?$")
COUNT_RE = re.compile(r"(\d[\d\s ]*)\s+treff")
FILE_EXT_RE = re.compile(r"\.(pdf|docx?|odt|rtf|xlsx?|txt)$", re.IGNORECASE)
PDF_SIZE_RE = re.compile(r"\((?:PDF|DOCX?|ODT)[^)]*\)", re.IGNORECASE)
NO_COMMENTS_RE = re.compile(r"\(\s*uten\s+merknad(?:er)?\s*\)", re.IGNORECASE)
DEADLINE_LABEL_RE = re.compile(r"h(?:ø|o|øy|oy)ringsfrist", re.IGNORECASE)
NOTAT_TITLE_RE = re.compile(
    r"(h(?:ø|øy)ringsnotat|notat|\bNOU\b|rapport|utgreiing|utredning)", re.I
)
DRAFT_TITLE_RE = re.compile(
    r"(utkast|lovforslag|forskriftsforslag|lovtekst|forskriftstekst|stortingsvedtak|"
    r"(forslag|framlegg)\s+til\s+(ny\s+|nye\s+|endring(ar|er)?\s+i\s+)?(lov|forskrift))",
    re.IGNORECASE,
)
INLINE_DOC_MIN_CHARS = 500
SKIPPED_FACTBOXES = {"horingsinstanser", "horingssvar", "horingsresultat"}

STATUS_MAP = {
    "på høring": "open",
    "på høyring": "open",
    "under behandling": "under_behandling",
    "ferdigbehandlet": "ferdigbehandlet",
    "ferdigbehandla": "ferdigbehandlet",
}


@dataclass
class ListingItem:
    id: int
    url: str
    title: str
    published_date: date | None
    doc_type: str | None
    department: str | None
    excerpt: str | None
    deadline: date | None
    status_raw: str | None
    status: str


@dataclass
class ListingPage:
    items: list[ListingItem]
    total: int | None
    next_url: str | None


@dataclass
class DocumentLink:
    url: str
    title: str
    section: str
    kind: str
    position: int
    inline_html: str | None = None

    @property
    def is_inline(self) -> bool:
        return self.inline_html is not None


@dataclass
class HearingPage:
    id: int
    url: str
    title: str
    doc_type: str | None
    published_date: date | None
    department: str | None
    original_department: str | None
    ingress: str | None
    status_raw: str | None
    status: str
    deadline: date | None
    letter_html: str | None
    address_list: list[str] = field(default_factory=list)
    documents: list[DocumentLink] = field(default_factory=list)
    responses_url: str | None = None

    @property
    def dt_on_address_list(self) -> bool:
        return any("datatilsynet" in name.lower() for name in self.address_list)


@dataclass
class ResponseEntry:
    name: str
    no_comments: bool
    category: str | None


@dataclass
class ResponsesPage:
    total: int | None
    entries: list[ResponseEntry]

    def find(self, organisation: str) -> ResponseEntry | None:
        target = organisation.lower()
        for entry in self.entries:
            if entry.name.lower() == target or entry.name.lower().startswith(target + " "):
                return entry
        return None


# ---------------------------------------------------------------------------
# Helpers


def parse_date(text: str | None) -> date | None:
    if not text:
        return None
    match = DATE_RE.search(text)
    if not match:
        return None
    day, month, year = (int(g) for g in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def normalize_status(raw: str | None) -> str:
    if not raw:
        return "unknown"
    key = " ".join(raw.split()).lower()
    return STATUS_MAP.get(key, "unknown")


def hearing_id_from_url(url: str) -> int | None:
    match = ID_RE.search(urlparse(url).path)
    return int(match.group(1)) if match else None


def clean(text: str | None) -> str | None:
    if text is None:
        return None
    text = " ".join(text.replace(" ", " ").split())
    return text or None


def html_to_text(html: str) -> str:
    """Convert an HTML fragment to plain text, keeping line structure from block elements."""
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|li|h[1-6]|tr|table|ul|ol|blockquote)>", "\n", html)
    html = re.sub(r"(?i)<li[^>]*>", "\n• ", html)
    tree = LexborHTMLParser(f"<body>{html}</body>")
    body = tree.body
    text = body.text(deep=True, separator="", strip=False) if body else ""
    lines = [" ".join(line.replace(" ", " ").split()) for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def _text(node: LexborNode | None) -> str | None:
    return clean(node.text(deep=True, separator=" ", strip=False)) if node else None


def _count(tree: LexborHTMLParser) -> int | None:
    node = tree.css_first("p.count")
    if not node:
        return None
    match = COUNT_RE.search(node.text())
    if not match:
        # "Viser 1-20 av 7007 treff."
        match = re.search(r"av\s+(\d[\d\s ]*)\s+treff", node.text())
    return int(re.sub(r"\D", "", match.group(1))) if match else None


# ---------------------------------------------------------------------------
# Listing page


def parse_listing(html: str, base_url: str) -> ListingPage:
    tree = LexborHTMLParser(html)
    items: list[ListingItem] = []
    for li in tree.css("li.listItem"):
        link = li.css_first("h2.title a")
        if not link or not link.attributes.get("href"):
            continue
        url = urljoin(base_url, link.attributes["href"])
        hearing_id = hearing_id_from_url(url)
        if hearing_id is None:
            continue
        deadline = None
        for span in li.css("span.event-details"):
            text = span.text()
            if DEADLINE_LABEL_RE.search(text):
                deadline = parse_date(text)
        status_raw = None
        status_node = li.css_first("span.event-details-open")
        if status_node:
            status_raw = clean(status_node.text().split(":", 1)[-1])
        items.append(
            ListingItem(
                id=hearing_id,
                url=url,
                title=_text(link) or "",
                published_date=parse_date(_text(li.css_first("span.date"))),
                doc_type=_text(li.css_first("span.type")),
                department=_text(li.css_first("span.department")),
                excerpt=_text(li.css_first("p.excerpts")),
                deadline=deadline,
                status_raw=status_raw,
                status=normalize_status(status_raw),
            )
        )
    # Only page 1 carries <link rel="next">; the visible pager is present on every page.
    next_node = tree.css_first("ul.pagination li.next a") or tree.css_first('link[rel="next"]')
    href = next_node.attributes.get("href") if next_node else None
    next_url = urljoin(base_url, href) if href else None
    return ListingPage(items=items, total=_count(tree), next_url=next_url)


# ---------------------------------------------------------------------------
# Hearing page


def _classify(section: str, title: str, section_count: int) -> str:
    if section == "horingsbrev":
        return "horingsnotat" if NOTAT_TITLE_RE.search(title) else "annet"
    if section == "horingsnotater":
        if section_count == 1:
            return "horingsnotat"
        if DRAFT_TITLE_RE.search(title) and not NOTAT_TITLE_RE.search(title):
            return "lovutkast"
        if NOTAT_TITLE_RE.search(title) and "vedlegg" not in title.lower():
            return "horingsnotat"
        return "annet"
    return "annet"


def _is_document_link(href: str, page_url: str, hearing_id: int) -> bool:
    if not href or href.startswith(("#", "mailto:", "javascript:")):
        return False
    absolute = urljoin(page_url, href)
    parsed = urlparse(absolute)
    if parsed.netloc and not parsed.netloc.endswith("regjeringen.no"):
        return False
    path = parsed.path
    if path.startswith(("/contentassets/", "/globalassets/")):
        return True
    return bool(FILE_EXT_RE.search(path)) and hearing_id_from_url(absolute) != hearing_id


def _factbox_documents(
    content: LexborNode, section: str, page_url: str, hearing_id: int, start: int
) -> list[DocumentLink]:
    raw: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in content.css("a"):
        href = a.attributes.get("href") or ""
        if not _is_document_link(href, page_url, hearing_id):
            continue
        absolute = urljoin(page_url, href).split("#")[0]
        if absolute in seen:
            continue
        seen.add(absolute)
        title = clean(a.attributes.get("title")) or _text(a) or absolute.rsplit("/", 1)[-1]
        raw.append((absolute, title))
    docs: list[DocumentLink] = []
    has_notat = any(NOTAT_TITLE_RE.search(t) for _, t in raw)
    for i, (absolute, title) in enumerate(raw):
        kind = _classify(section, title, len(raw))
        if section == "horingsnotater" and not has_notat and i == 0 and kind != "lovutkast":
            kind = "horingsnotat"
        docs.append(DocumentLink(absolute, title, section, kind, start + i))
    return docs


def _inline_text_length(content: LexborNode) -> int:
    text = content.text(deep=True, separator=" ", strip=False)
    link_text = sum(len(a.text()) for a in content.css("a"))
    return len(" ".join(text.split())) - link_text


def parse_hearing(html: str, url: str) -> HearingPage:
    tree = LexborHTMLParser(html)
    hearing_id = hearing_id_from_url(url)
    if hearing_id is None:
        raise ValueError(f"No hearing id in URL: {url}")

    title = _text(tree.css_first("header.article-header h1")) or _text(tree.css_first("h1")) or ""

    info = tree.css_first("div.article-info")
    doc_type = published = department = original_department = None
    if info:
        type_node = info.css_first("span.type")
        if type_node:
            doc_type = clean(type_node.text().replace("|", " "))
        for span in info.css("span.date"):
            text = span.text()
            if "opprinnelig" in text.lower() or "opphavleg" in text.lower():
                original_department = clean(text.split(":", 1)[-1])
            elif published is None:
                published = parse_date(text)
        owner = info.css_first("span.owner a") or info.css_first("span.owner")
        if owner:
            department = clean(owner.text().replace("|", " "))

    status_raw = None
    deadline = None
    meta = tree.css_first("div.horing-meta")
    if meta:
        for p in meta.css("p"):
            text = p.text()
            label, _, value = text.partition(":")
            if label.strip().lower() == "status":
                status_raw = clean(value)
            elif DEADLINE_LABEL_RE.search(label):
                deadline = parse_date(value)

    ingress = _text(tree.css_first("div.article-ingress"))

    letter_html = None
    address_list: list[str] = []
    documents: list[DocumentLink] = []
    responses_url = None

    for box in tree.css("div.factbox"):
        content = box.css_first(".factbox-content")
        if not content:
            continue
        section = content.attributes.get("id") or ""
        if section == "horingsinstanser":
            lines = html_to_text(content.html or "").splitlines()
            address_list = [
                line.strip(" •")
                for line in lines
                if line.strip(" •")
                and not DEADLINE_LABEL_RE.search(line)
                and not re.fullmatch(r"h(ø|øy)ringsinstans(er|ar)\s*:?", line.strip(), re.I)
            ]
            continue
        if section == "horingssvar":
            responses_url = url.split("?")[0].split("#")[0] + "?showSvar=true"
            continue
        if section in SKIPPED_FACTBOXES:
            continue
        if section == "horingsbrev":
            letter_html = content.html
            documents.append(
                DocumentLink(
                    url=url.split("#")[0] + "#horingsbrev",
                    title="Høringsbrev",
                    section=section,
                    kind="horingsbrev",
                    position=len(documents),
                    inline_html=content.html,
                )
            )
        elif section == "horingsnotater" and _inline_text_length(content) >= INLINE_DOC_MIN_CHARS:
            documents.append(
                DocumentLink(
                    url=url.split("#")[0] + "#horingsnotat",
                    title="Høringsnotat (på nettsiden)",
                    section=section,
                    kind="horingsnotat",
                    position=len(documents),
                    inline_html=content.html,
                )
            )
        documents.extend(_factbox_documents(content, section, url, hearing_id, len(documents)))

    # If an inline notat exists, file links in the same section are attachments.
    if any(d.is_inline and d.kind == "horingsnotat" for d in documents):
        for d in documents:
            if not d.is_inline and d.section == "horingsnotater" and d.kind == "horingsnotat":
                d.kind = "annet"
    # Files linked from the letter are attachments when the notat section has its own notat.
    if any(d.section == "horingsnotater" and d.kind == "horingsnotat" for d in documents):
        for d in documents:
            if d.section == "horingsbrev" and not d.is_inline and d.kind == "horingsnotat":
                d.kind = "annet"

    return HearingPage(
        id=hearing_id,
        url=url.split("?")[0].split("#")[0],
        title=title,
        doc_type=doc_type,
        published_date=published,
        department=department,
        original_department=original_department,
        ingress=ingress,
        status_raw=status_raw,
        status=normalize_status(status_raw),
        deadline=deadline,
        letter_html=letter_html,
        address_list=address_list,
        documents=documents,
        responses_url=responses_url,
    )


# ---------------------------------------------------------------------------
# Published responses (?showSvar=true)


def parse_responses(html: str) -> ResponsesPage:
    tree = LexborHTMLParser(html)
    entries: list[ResponseEntry] = []
    for li in tree.css("ul[data-horingssvar-list] li"):
        raw = _text(li) or ""
        no_comments = bool(NO_COMMENTS_RE.search(raw))
        name = NO_COMMENTS_RE.sub("", PDF_SIZE_RE.sub("", raw))
        name = clean(name) or ""
        if name:
            entries.append(ResponseEntry(name, no_comments, li.attributes.get("data-instans")))
    return ResponsesPage(total=_count(tree), entries=entries)
