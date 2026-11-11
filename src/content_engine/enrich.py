"""Fill weak summaries from the linked page, deterministically and without tokens.

Many sources give a title and a link but no usable summary: Hacker News links, Lobsters
("Comments"), Google Research (only a topic label), Reddit link posts. For those, fetch the
page once and take the summary its publisher wrote for link previews (og:description,
twitter:description, meta description). If that's missing or generic, take the first
real paragraph of the article. Both are the publisher's own words, kept as written up
to the summary limit; the link has the rest. No LLM: an LLM-written summary would break
the zero-token invariant.

A candidate only wins if it shares a content word with the title. That's what filters out
site-wide blurbs such as a help page whose meta description describes the whole product.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from content_engine.schema import Item, clean_text, truncate

log = logging.getLogger(__name__)

WEAK_MIN_CHARS = 40          # "Natural Language Processing", "vLLM v0.33.0" are labels, not summaries
MAX_PAGE_BYTES = 1_000_000   # article text sits well inside the first MB; stop reading there
PAGE_TIMEOUT = 8.0
CONCURRENCY = 8
MAX_REDIRECTS = 4
MISS_RETRY = timedelta(hours=6)  # a page that gave nothing is retried after this, not every run
# Reddit allows ~1 anonymous request/minute (spent on the feed); X needs JavaScript to render.
SKIP_HOSTS = ("reddit.com", "redd.it", "x.com", "twitter.com")

_URL_RE = re.compile(r"https?://\S+")
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'+.-]{2,}")
_STOPWORDS = frozenset(
    "the and for with from that this into your our their they you are was were has have how what "
    "why when who its it's not but can will new more about over than then just all any via use using "
    "show ask".split()
)
# Descriptions sites put on every page, which say nothing about the item.
_BOILERPLATE = (
    re.compile(r"^contribute to .+ by creating an account on github", re.I),
    re.compile(r"^(enjoy the videos and music you love|share your videos with friends)", re.I),
)
# Bot-check and block pages served with status 200 instead of the article. Matched against the
# page title and every candidate, so neither a meta tag nor a paragraph from one gets through.
_CHALLENGE = re.compile(r"just a moment|checking your browser|verify you are (a )?human|attention required|"
                        r"enable javascript and cookies|access denied|are you a robot|captcha|"
                        r"unusual traffic|request blocked", re.I)
_BAD_PARAGRAPH = re.compile(r"cookie|javascript|subscribe|sign in|log in|newsletter|all rights reserved", re.I)


@dataclass
class EnrichStats:
    weak: int = 0        # items whose summary needed help
    cached: int = 0      # answered from the cache
    fetched: int = 0     # pages requested this run
    filled: int = 0      # weak items that now have a real summary
    skipped: int = 0     # no fetchable link (reddit/x, or nothing but a title)
    failed: int = 0      # fetch errors or pages with nothing usable

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


# --- pure helpers (tested on strings) ---------------------------------------------------------

def is_weak(summary: str, title: str) -> bool:
    s = summary.strip()
    if len(s) < WEAK_MIN_CHARS or s.lower() == title.strip().lower():
        return True
    return not _URL_RE.sub("", s).strip()  # nothing but links


def target_url(item: Item) -> str | None:
    """The page to read: a link quoted in the summary (a Reddit link post), else the item's own url."""
    for url in [*_URL_RE.findall(item.summary), item.url]:
        host = (urlsplit(url).hostname or "").lower()
        if host and not any(host == h or host.endswith("." + h) for h in SKIP_HOSTS):
            return url
    return None


def content_words(text: str) -> set[str]:
    return {w.strip(".-'") for w in _WORD_RE.findall(text.lower())} - _STOPWORDS


class _PageParser(HTMLParser):
    """Collects meta descriptions and the first paragraphs, noting which are inside <article>/<main>."""

    META_KEYS = ("og:description", "twitter:description", "description")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.paragraphs: list[tuple[bool, str]] = []
        self._skip = 0       # inside script/style/nav/footer
        self._main = 0       # inside article/main
        self._p: list[str] | None = None
        self.page_title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key in self.META_KEYS and key not in self.meta and a.get("content"):
                self.meta[key] = a["content"]
        elif tag in ("script", "style", "nav", "footer", "noscript"):
            self._skip += 1
        elif tag in ("article", "main"):
            self._main += 1
        elif tag == "p" and not self._skip:
            self._p = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in ("script", "style", "nav", "footer", "noscript"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("article", "main"):
            self._main = max(0, self._main - 1)
        elif tag == "p" and self._p is not None:
            text = clean_text("".join(self._p))
            if text and len(self.paragraphs) < 40:
                self.paragraphs.append((self._main > 0, text))
            self._p = None

    def handle_data(self, data):
        if self._in_title:
            self.page_title += data
        if self._p is not None and not self._skip:
            self._p.append(data)


def _good_paragraph(text: str) -> bool:
    return (len(text) >= 80 and bool(re.search(r"[.!?](\s|$)", text))
            and not _BAD_PARAGRAPH.search(text) and not _CHALLENGE.search(text))


def _clean_meta(text: str) -> str:
    text = clean_text(text)
    # GitHub appends " - owner/repo" (or "· owner/repo") to repo descriptions, and
    # " - Release v1.2" to release pages; what's left is the repo's own tagline.
    text = re.sub(r"\s+[-·]\s+[\w.-]+/[\w.-]+$", "", text)
    text = re.sub(r"\s+-\s+Release\s+.*$", "", text)  # release names can contain spaces
    return "" if any(p.search(text) for p in _BOILERPLATE) else text


def extract_summary(html: str, title: str) -> str:
    """Best summary from a page, or "" if nothing on it is about the title."""
    parser = _PageParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed HTML: use whatever was collected before the error
        pass
    if _CHALLENGE.search(parser.page_title):
        return ""  # a bot-check page: nothing on it is about the item
    metas = [m for m in (_clean_meta(parser.meta.get(k, "")) for k in _PageParser.META_KEYS)
             if len(m) >= WEAK_MIN_CHARS and not _CHALLENGE.search(m)]
    in_main = [t for inside, t in parser.paragraphs if inside and _good_paragraph(t)]
    paragraphs = in_main or [t for _, t in parser.paragraphs if _good_paragraph(t)]

    title_words = content_words(title)
    for candidate in [*metas, *paragraphs[:5]]:
        if not title_words or title_words & content_words(candidate):
            return truncate(candidate)
    # Nothing echoes the title (titles can be oblique: "Let's build a brain"). An article
    # paragraph beats a meta tag here: meta tags are where site-wide blurbs live.
    fallback = paragraphs[:1] or metas[:1]
    return truncate(fallback[0]) if fallback else ""


# --- network -----------------------------------------------------------------------------------

async def _is_public(host: str) -> bool:
    """Refuse hosts that resolve to private, loopback or link-local addresses. A feed controls
    these URLs, so without this check a crafted item could make the server probe its own network."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return all(ipaddress.ip_address(info[4][0]).is_global for info in infos)


async def fetch_page(client: httpx.AsyncClient, url: str) -> str:
    """GET an HTML page, following redirects by hand so every hop passes the public-host check."""
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or not await _is_public(parts.hostname):
            raise ValueError(f"refusing to fetch {url}")
        async with client.stream("GET", url, timeout=PAGE_TIMEOUT, follow_redirects=False,
                                 headers={"Accept": "text/html,application/xhtml+xml"}) as res:
            if res.is_redirect and "location" in res.headers:
                url = urljoin(url, res.headers["location"])
                continue
            res.raise_for_status()
            if "html" not in res.headers.get("content-type", ""):
                raise ValueError(f"not html: {res.headers.get('content-type')}")
            body = bytearray()
            async for chunk in res.aiter_bytes():
                body += chunk
                if len(body) >= MAX_PAGE_BYTES:
                    break
            return body.decode(res.encoding or "utf-8", errors="replace")
    raise ValueError(f"too many redirects for {url}")


def _cache_hit(entry: dict | None, now: datetime) -> str | None:
    if not entry:
        return None
    if entry.get("summary"):
        return entry["summary"]
    at = datetime.fromisoformat(entry.get("at", "1970-01-01T00:00:00+00:00").replace("Z", "+00:00"))
    return "" if now - at < MISS_RETRY else None  # recent miss: don't refetch yet


async def enrich(items: list[Item], client: httpx.AsyncClient, cache: dict[str, dict],
                 now: datetime | None = None) -> tuple[list[Item], EnrichStats]:
    """Return items with weak summaries filled where possible. Updates `cache` in place
    (url -> {"summary", "at"}). Items that stay weak keep their original summary."""
    now = now or datetime.now(timezone.utc)
    stats = EnrichStats()
    sem = asyncio.Semaphore(CONCURRENCY)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    async def one(item: Item) -> Item:
        if not is_weak(item.summary, item.title):
            return item
        stats.weak += 1
        url = target_url(item)
        if url is None:
            stats.skipped += 1
            return item
        hit = _cache_hit(cache.get(url), now)
        if hit is not None:
            stats.cached += 1
            summary = hit
        else:
            async with sem:
                stats.fetched += 1
                try:
                    summary = extract_summary(await fetch_page(client, url), item.title)
                except Exception as exc:
                    log.debug("enrich %s failed: %s: %s", url, type(exc).__name__, exc)
                    summary = ""
            cache[url] = {"summary": summary, "at": stamp}
        if summary and not is_weak(summary, item.title):
            stats.filled += 1
            return replace(item, summary=summary)
        stats.failed += 1
        return item

    out = await asyncio.gather(*(one(i) for i in items))
    log.info("enrich: %s", stats.to_dict())
    return list(out), stats
