"""Collectors for the JSON APIs: Hacker News (Algolia), Hugging Face daily papers, GitHub search.

Each has a pure `parse_*` function so tests run it on saved payloads without the network.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from content_engine.collectors.base import BaseCollector
from content_engine.schema import Item, clean_text, to_iso, truncate


def parse_hacker_news(payload: dict[str, Any], *, source: str, category: str) -> list[Item]:
    items = []
    for h in payload.get("hits", []):
        oid = str(h.get("objectID", ""))
        discussion = f"https://news.ycombinator.com/item?id={oid}"
        items.append(Item(
            id=f"hn:{oid}",
            source=source,
            category=category,
            title=clean_text(h.get("title")),
            # Ask/Show HN posts have no external url; fall back to the discussion page.
            url=h.get("url") or discussion,
            summary=truncate(clean_text(h.get("story_text"))),
            metric=f"{h.get('points', 0)} pts | {h.get('num_comments', 0)} comments",
            published_at=to_iso(h.get("created_at_i") or h.get("created_at")),
        ))
    return items


class HackerNewsCollector(BaseCollector):
    """`title_only` is for narrow topic searches: by default Algolia also matches the words
    in the story's url and text, and near-spellings, which lets unrelated posts in. `empty_ok` is for topics
    that don't reach the front page every day."""

    def __init__(self, name: str = "hacker_news", words: str = "AI LLM", hours_back: int = 24,
                 min_points: int = 25, limit: int = 10, category: str = "community",
                 title_only: bool = False, empty_ok: bool = False):
        self.name = name
        self.category = category
        self.title_only = title_only
        self.empty_ok = empty_ok
        self.words = words
        self.hours_back = hours_back
        self.min_points = min_points
        self.limit = limit

    async def fetch(self, client: httpx.AsyncClient) -> list[Item]:
        since = int(time.time()) - self.hours_back * 3600
        params = {
            # Algolia has no OR operator ("AI OR LLM" matches nothing); optionalWords makes
            # every word optional, so a story matching any one of them is returned.
            "query": self.words,
            "optionalWords": self.words,
            "tags": "story",
            "numericFilters": f"created_at_i>{since},points>{self.min_points}",
            "hitsPerPage": self.limit,
        }
        if self.title_only:
            # Typo tolerance is off too: with it, "Adapting ..." matches "agentic".
            params["restrictSearchableAttributes"] = "title"
            params["typoTolerance"] = "false"
        res = await self.get(client, "https://hn.algolia.com/api/v1/search_by_date", params=params)
        return parse_hacker_news(res.json(), source=self.name, category=self.category)


def parse_hf_papers(payload: list[dict[str, Any]], *, source: str, category: str) -> list[Item]:
    items = []
    for entry in payload:
        paper = entry.get("paper", {})
        pid = str(paper.get("id", ""))
        items.append(Item(
            id=f"hf:{pid}",
            source=source,
            category=category,
            title=clean_text(paper.get("title")),
            url=f"https://huggingface.co/papers/{pid}",
            summary=truncate(clean_text(paper.get("summary"))),
            metric=f"{paper.get('upvotes', 0)} upvotes",
            published_at=to_iso(paper.get("publishedAt") or entry.get("publishedAt")),
        ))
    return items


class HuggingFacePapersCollector(BaseCollector):
    category = "research"

    def __init__(self, name: str = "huggingface_papers", limit: int = 8):
        self.name = name
        self.limit = limit

    async def fetch(self, client: httpx.AsyncClient) -> list[Item]:
        res = await self.get(client, "https://huggingface.co/api/daily_papers", params={"limit": self.limit})
        return parse_hf_papers(res.json(), source=self.name, category=self.category)


def parse_github_search(payload: dict[str, Any], *, source: str, category: str) -> list[Item]:
    return [
        Item(
            id=f"gh:{repo.get('id')}",
            source=source,
            category=category,
            title=repo.get("full_name", ""),
            url=repo.get("html_url", ""),
            summary=truncate(clean_text(repo.get("description"))),
            metric=f"{repo.get('stargazers_count', 0)} stars | {repo.get('forks_count', 0)} forks",
            published_at=to_iso(repo.get("created_at")),
        )
        for repo in payload.get("items", [])
    ]


class GitHubSearchCollector(BaseCollector):
    """New repos on a topic, by stars. `{since}` in the query becomes the date `days_back` ago.

    The spec sorted `topic:llm stars:>100` by last update, which returns the same big
    repos every run (any push bumps them). New-and-starred is the trending signal.
    Unauthenticated search allows 10 requests/minute, plenty for a scheduled sweep.
    """

    host = "api.github.com"

    def __init__(self, name: str = "github_new_llm_repos",
                 query: str = "topic:llm created:>{since} stars:>20", days_back: int = 7, limit: int = 6,
                 category: str = "tooling"):
        self.name = name
        self.category = category
        self.query = query
        self.days_back = days_back
        self.limit = limit

    async def fetch(self, client: httpx.AsyncClient) -> list[Item]:
        since = (datetime.now(timezone.utc) - timedelta(days=self.days_back)).strftime("%Y-%m-%d")
        res = await self.get(client, "https://api.github.com/search/repositories", params={
            "q": self.query.format(since=since), "sort": "stars", "order": "desc", "per_page": self.limit,
        }, headers={"Accept": "application/vnd.github+json"})
        return parse_github_search(res.json(), source=self.name, category=self.category)
