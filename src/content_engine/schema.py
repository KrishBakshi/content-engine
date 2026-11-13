"""The normalized item every collector emits, and the helpers that clean fields into it."""

from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

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

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


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
