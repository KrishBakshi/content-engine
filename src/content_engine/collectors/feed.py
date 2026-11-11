"""Generic RSS 2.0 / Atom collector: covers the blogs, release feeds, arXiv, Lobsters and Techmeme."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

import httpx
from defusedxml.ElementTree import fromstring

from content_engine.collectors.base import BaseCollector
from content_engine.schema import Item, clean_text, to_iso, truncate

ATOM = "{http://www.w3.org/2005/Atom}"


def _text(el: ET.Element | None, path: str) -> str:
    # itertext, not .text: a title with inline markup (<b>x</b>) would otherwise be cut at the tag.
    found = el.find(path) if el is not None else None
    return "".join(found.itertext()) if found is not None else ""


def _atom_link(entry: ET.Element) -> str:
    # Prefer rel="alternate" (the human page); Atom allows several links per entry.
    links = entry.findall(f"{ATOM}link")
    for link in links:
        if link.get("rel", "alternate") == "alternate" and link.get("href"):
            return link.get("href", "")
    return links[0].get("href", "") if links else ""


# Descriptions that are only a link label carry no content (Lobsters' is just "Comments").
_EMPTY_SUMMARIES = {"comments", "read more", "continue reading"}


# Headings in release notes ("v0.33.0", "Highlights") and commit trailers repeat the title or
# name people; neither says what the item is.
_HEADING_RE = re.compile(r"<h[1-6][^>]*>.*?</h[1-6]>", re.I | re.S)
# Matched after HTML is stripped: "Signed-off-by: Name <a>email</a>" becomes "...: Name email".
_TRAILER_RE = re.compile(r"(Signed-off-by|Co-authored-by|Reviewed-by):.*?\S+@\S+", re.I)
# arXiv prefixes every abstract with "arXiv:2611.10045v1 Announce Type: new Abstract:".
_ARXIV_PREFIX_RE = re.compile(r"^arXiv:\S+\s+Announce Type:\s*\S+\s+Abstract:\s*", re.I)
# Reddit ends every entry "submitted by /u/name [to r/sub] [link] [comments]"; for link posts
# that is the whole body. Stripped here, before truncation, so a long post can't keep half of it.
_REDDIT_TRAILER_RE = re.compile(r"\s*submitted by\s+/u/\S+(\s+to\s+r/\S+)?\s*\[link\]\s*\[comments\]\s*$", re.I)


def _summary(raw: str) -> str:
    text = clean_text(_HEADING_RE.sub(" ", raw))
    text = _ARXIV_PREFIX_RE.sub("", _TRAILER_RE.sub(" ", text))
    text = _REDDIT_TRAILER_RE.sub("", text).strip()
    return "" if text.lower() in _EMPTY_SUMMARIES else truncate(text)


def parse_feed(content: bytes, *, source: str, category: str, limit: int, metric: str) -> list[Item]:
    """Parse RSS 2.0 or Atom bytes into items. Pure, so tests run it on fixtures."""
    root = fromstring(content)
    items: list[Item] = []

    if root.tag == f"{ATOM}feed":
        for entry in root.findall(f"{ATOM}entry")[:limit]:
            url = _atom_link(entry) or _text(entry, f"{ATOM}id").strip()
            summary = _text(entry, f"{ATOM}summary") or _text(entry, f"{ATOM}content")
            published = _text(entry, f"{ATOM}published") or _text(entry, f"{ATOM}updated")
            items.append(Item(
                id=_text(entry, f"{ATOM}id").strip() or url,
                source=source,
                category=category,
                title=clean_text(_text(entry, f"{ATOM}title")),
                url=url,
                summary=_summary(summary),
                metric=metric,
                published_at=to_iso(published),
            ))
        return items

    for it in root.findall("./channel/item")[:limit]:
        url = _text(it, "link").strip()
        items.append(Item(
            id=_text(it, "guid").strip() or url,
            source=source,
            category=category,
            title=clean_text(_text(it, "title")),
            url=url,
            summary=_summary(_text(it, "description")),
            metric=metric,
            published_at=to_iso(_text(it, "pubDate")),
        ))
    return items


class FeedCollector(BaseCollector):
    def __init__(self, name: str, category: str, url: str, limit: int = 5, metric: str = "Article",
                 empty_ok: bool = False):
        self.name = name
        self.category = category
        self.url = url
        self.limit = limit
        self.metric = metric
        self.host = urlsplit(url).hostname
        self.empty_ok = empty_ok

    async def fetch(self, client: httpx.AsyncClient) -> list[Item]:
        res = await self.get(client, self.url)
        return parse_feed(res.content, source=self.name, category=self.category,
                          limit=self.limit, metric=self.metric)
