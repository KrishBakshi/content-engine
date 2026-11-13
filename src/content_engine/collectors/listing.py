"""Collector for sites with no feed: read a listing page's article links, then each article's metadata.

Anthropic publishes no RSS or Atom feed, but its listing pages (/engineering, /research,
/science) are plain server-rendered HTML that link to every article, and each article page
carries og:title, og:description and article:published_time. That is the same data a feed
would give, at the cost of one request per article. Plain GETs only: no browser, no login,
and the site's robots.txt allows it (`Allow: /`, checked 2026-11-13).

This is more fragile than a feed: a redesign can change the link pattern or drop the meta
tags. When that happens the source comes back empty and the health alert picks it up.
"""

from __future__ import annotations

import asyncio
import logging
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from content_engine.collectors.base import BaseCollector
from content_engine.collectors.feed import newer_than
from content_engine.schema import Item, clean_text, to_iso, truncate

log = logging.getLogger(__name__)


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


class _Meta(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        if tag == "meta":
            a = dict(attrs)
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content") and key not in self.meta:
                self.meta[key] = a["content"]


def extract_links(html: str, base_url: str, pattern: str, limit: int) -> list[str]:
    """First `limit` distinct article URLs on a listing page, in page order, whose path
    fully matches `pattern`. Same-site links only."""
    parser = _Links()
    parser.feed(html)
    host = urlsplit(base_url).hostname
    wanted, out = re.compile(pattern), []
    for href in parser.hrefs:
        url = urljoin(base_url, href).split("#")[0].split("?")[0]
        parts = urlsplit(url)
        if parts.hostname == host and wanted.fullmatch(parts.path) and url not in out:
            out.append(url)
            if len(out) == limit:
                break
    return out


def parse_article(html: str, url: str, *, source: str, category: str, metric: str,
                  site_name: str = "", site_blurb: str = "") -> Item | None:
    """An Item from an article page's meta tags, or None if it has no og:title.

    `site_name` is removed from the title only when it follows a separator ("Title | Site"),
    never when it is part of the title ("Redesigning source control at Anthropic").
    `site_blurb` is how the site-wide description starts; a page that only has that gets an
    empty summary, which the enrichment step then fills from the article's first paragraph."""
    parser = _Meta()
    parser.feed(html)
    meta = parser.meta
    title = clean_text(meta.get("og:title") or meta.get("twitter:title"))
    if not title:
        return None
    if site_name:
        title = re.sub(rf"\s*[|\\–—-]\s*{re.escape(site_name)}$", "", title)
    summary = clean_text(meta.get("og:description") or meta.get("description"))
    if site_blurb and summary.startswith(site_blurb):
        summary = ""
    return Item(
        id=url,
        source=source,
        category=category,
        title=title,
        url=url,
        summary=truncate(summary),
        metric=metric,
        published_at=to_iso(meta.get("article:published_time")),
    )


class ListingPageCollector(BaseCollector):
    """`listings` is a list of (source_name, category, listing_url). An article that appears
    on several listings is fetched once and belongs to the first listing that has it, so
    order the listings by which section should win.

    A listing's page order is not reliably newest-first (featured articles lead), so the
    first `scan` links are read and the `per_listing` newest by their own publish date kept."""

    CONCURRENCY = 4

    def __init__(self, name: str, listings: list[tuple[str, str, str]], link_pattern: str,
                 per_listing: int = 4, scan: int = 6, metric: str = "Blog", site_name: str = "",
                 site_blurb: str = "", max_age_days: float | None = None):
        self.name = name
        self.category = listings[0][1]
        self.listings = listings
        self.link_pattern = link_pattern
        self.per_listing = per_listing
        self.scan = scan
        self.metric = metric
        self.site_name = site_name
        self.site_blurb = site_blurb
        self.max_age_days = max_age_days
        self.host = urlsplit(listings[0][2]).hostname

    async def fetch(self, client: httpx.AsyncClient) -> list[Item]:
        sem = asyncio.Semaphore(self.CONCURRENCY)

        async def article(url: str, source: str, category: str) -> Item | None:
            async with sem:
                try:
                    page = await self.get(client, url)
                except httpx.HTTPError as exc:  # one broken article doesn't lose the rest
                    log.debug("listing %s: %s failed: %s", self.name, url, exc)
                    return None
            return parse_article(page.text, url, source=source, category=category, metric=self.metric,
                                 site_name=self.site_name, site_blurb=self.site_blurb)

        items: list[Item] = []
        seen: set[str] = set()
        for source, category, listing_url in self.listings:
            listing = await self.get(client, listing_url)  # a listing that fails, fails the source
            urls = [u for u in extract_links(listing.text, listing_url, self.link_pattern, self.scan)
                    if u not in seen]
            seen.update(urls)
            found = [i for i in await asyncio.gather(*(article(u, source, category) for u in urls)) if i]
            found.sort(key=lambda i: i.published_at, reverse=True)  # ISO strings sort by time
            items += found[: self.per_listing]
        # The listing itself always has links, so an empty result after the age cutoff is only
        # "nothing recent"; a listing whose links stop matching fails the checks above instead.
        return newer_than(items, self.max_age_days) if self.max_age_days is not None else items
