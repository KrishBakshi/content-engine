"""Reddit via the public Atom feed, all subreddits in one request.

Anonymous `hot.json` is 403, and `hot.rss` allows about one request per minute
(`x-ratelimit-remaining: 0.0` after a single call), so fetching each subreddit
separately gets the second one a 429. A multireddit (`/r/A+B/hot.rss`) costs one
request; items are split back out per subreddit from their permalink.
"""

from __future__ import annotations

import re

import httpx

from content_engine.collectors.base import BaseCollector
from content_engine.collectors.feed import parse_feed
from content_engine.schema import Item

_SUB_RE = re.compile(r"/r/([^/]+)/", re.IGNORECASE)
# The combined hot list is ranked across subreddits, so big ones crowd small ones out;
# fetch wide and cap each subreddit at `per_sub`.
FETCH_LIMIT = 50


def split_by_subreddit(items: list[Item], subreddits: list[str], per_sub: int) -> list[Item]:
    wanted = {s.lower(): s for s in subreddits}
    counts: dict[str, int] = {}
    out = []
    for item in items:
        m = _SUB_RE.search(item.url)
        sub = m.group(1).lower() if m else ""
        if sub not in wanted or counts.get(sub, 0) >= per_sub:
            continue
        counts[sub] = counts.get(sub, 0) + 1
        out.append(Item(**{**item.to_dict(), "source": f"reddit_{sub}"}))
    return out


class RedditCollector(BaseCollector):
    category = "community"
    host = "www.reddit.com"

    def __init__(self, subreddits: list[str], per_sub: int = 8, name: str = "reddit"):
        self.name = name
        self.subreddits = subreddits
        self.per_sub = per_sub

    async def fetch(self, client: httpx.AsyncClient) -> list[Item]:
        url = f"https://www.reddit.com/r/{'+'.join(self.subreddits)}/hot.rss"
        res = await self.get(client, url, params={"limit": FETCH_LIMIT})
        items = parse_feed(res.content, source=self.name, category=self.category, limit=FETCH_LIMIT, metric="hot")
        return split_by_subreddit(items, self.subreddits, self.per_sub)
