"""The normalized item every collector emits, and the helpers that clean fields into it."""

from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

SUMMARY_MAX = 250  # the spec said 300; 250 is enough to say what an item is
# Order is the order of sections in the brief: the two focus topics first.
CATEGORIES = ("agents", "physical_ai", "community", "research", "tooling", "engineering_blogs")
CATEGORY_LABELS = {"agents": "Agent AI", "physical_ai": "Physical AI"}  # others are title-cased

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Item:
    id: str
    source: str
    category: str
    title: str
    url: str
    summary: str
    metric: str
    published_at: str  # ISO 8601 UTC, or "" when the source gives no date
    # True when an earlier run already carried this url. Collectors leave it False; storage
    # sets it when the run is saved, by checking the record of delivered urls.
    delivered: bool = False
    # Other sources that carried the same link this run, as {"source", "metric"} (a blog post
    # that is also on Hacker News). Set by the engine where it used to drop the duplicate.
    also_in: tuple[dict, ...] = ()

    def to_dict(self) -> dict:
        return {**asdict(self), "also_in": [dict(a) for a in self.also_in]}


_TRACKING = ("utm_", "ref=", "ref_src=", "source=", "fbclid=", "gclid=")


def canonical_url(url: str) -> str:
    """A key for "same link": no scheme, no www, no trailing slash, no fragment or tracking
    parameters. Only for comparing; items keep the url their source gave."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    query = "&".join(p for p in parts.query.split("&") if p and not p.lower().startswith(_TRACKING))
    return host + parts.path.rstrip("/") + (f"?{query}" if query else "")


def source_family(source: str) -> str:
    """Sources that are one outlet under several names count as one voice: three Hacker News
    queries, four subreddits, five arXiv categories."""
    for family in ("hacker_news", "reddit", "arxiv", "github_new", "anthropic", "nvidia"):
        if source.startswith(family):
            return family
    return source


def clean_text(text: str | None) -> str:
    """Strip HTML tags and entities and collapse whitespace; feeds put markup in titles and descriptions."""
    if not text:
        return ""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", text))).strip()


def truncate(text: str, limit: int = SUMMARY_MAX) -> str:
    """Keep at most `limit` characters, cut at a word boundary, with "..." when cut.
    No rewording or abbreviating: the item's url has the rest."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 3]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:-") + "..."


def to_iso(value: str | int | float | None) -> str:
    """Normalize epoch seconds, RFC 822 (RSS) or ISO 8601 (Atom/JSON) into ISO 8601 UTC."""
    if value is None or value == "":
        return ""
    try:
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        else:
            s = value.strip()
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError, OverflowError):
        # An unparseable date is kept as-is rather than dropping the item.
        return str(value)
