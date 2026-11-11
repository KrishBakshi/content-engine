# Project Specification: Autonomous Zero-Token Tech & AI Intelligence Engine

## 1. Project Overview & Architecture
Build an autonomous ingestion engine that continuously aggregates, normalizes, and filters high-signal technical news, research papers, open-source repositories, and practitioner discussions.

### Invariants & Guardrails
1. **Zero Token Cost / No Paid Search APIs:** Never rely on LLM scraping passes, paid search tokens (Tavily, Exa, Serper), or headless browser automation (Playwright, Selenium).
2. **Zero Authentication Required:** All data must be fetched exclusively from free public JSON endpoints, Atom feeds, and RSS feeds without requiring API keys or user logins.
3. **Deterministic Output:** Normalize all incoming items into a uniform JSON schema so downstream agent loops or prompt injectors can consume them reliably.
4. **Horizontal Extensibility:** Every provider inherits from an abstract `BaseCollector`. Adding a new source requires only writing an isolated collector without changing engine coordination or formatting logic.

---

## 2. Standard Normalized Item Schema

All collectors normalize their payload into this dictionary structure:

```python
{
    "id": str,           # Unique ID or URL
    "source": str,       # Identifier: e.g., "hacker_news", "huggingface", "arxiv_cs_ai", "reddit_localllama"
    "category": str,     # Bucket: "community", "research", "tooling", "engineering_blogs"
    "title": str,        # Clean item title
    "url": str,          # Direct canonical destination link
    "summary": str,      # Abstract, description, or post body (truncated to 300 chars)
    "metric": str,       # Contextual traction: "250 pts | 80 comments", "45 upvotes", "1200 stars"
    "published_at": str  # ISO timestamp or UTC string
}
```

---

## 3. Master Source & Free Endpoint Registry

| Category | Source Name | Endpoint / Protocol | Notes & Requirements |
| :--- | :--- | :--- | :--- |
| **Community & News** | **Hacker News (Algolia)** | `https://hn.algolia.com/api/v1/search_by_date` (JSON) | Filter by query, date (`created_at_i`), and min points (`points>25`). No auth. |
| **Community & News** | **Reddit (r/LocalLLaMA)** | `https://www.reddit.com/r/LocalLLaMA/hot.json?limit=15` (JSON) | Requires distinct custom `User-Agent` header to prevent 429 throttling. |
| **Community & News** | **Reddit (r/MachineLearning)** | `https://www.reddit.com/r/MachineLearning/hot.json?limit=15` (JSON) | Same custom `User-Agent` requirement. |
| **Community & News** | **Lobste.rs (AI tag)** | `https://lobste.rs/t/ai.rss` (RSS XML) | High-signal systems programming and compiler discussions. |
| **Community & News** | **Techmeme** | `https://www.techmeme.com/feed.xml` (RSS XML) | Executive-level industry shifts and macro reporting. |
| **Research & Papers** | **Hugging Face Daily Papers** | `https://huggingface.co/api/daily_papers?limit=10` (JSON) | Curated and community-upvoted ML preprints. |
| **Research & Papers** | **arXiv (cs.AI, cs.LG, cs.CL)** | `https://rss.arxiv.org/atom/{category}` (Atom XML) | Official zero-delay preprint announcements directly from arXiv. |
| **Tooling & Code** | **GitHub Search API** | `https://api.github.com/search/repositories` (JSON) | Query by topics (e.g. `topic:llm+stars:>100`). Unauthenticated rate limit is generous for scheduled sweeps. |
| **Tooling & Code** | **llama.cpp Releases** | `https://github.com/ggerganov/llama.cpp/releases.atom` (Atom XML) | Tracks quantization breakthroughs and edge runtime additions. |
| **Tooling & Code** | **vLLM Releases** | `https://github.com/vllm-project/vllm/releases.atom` (Atom XML) | Tracks inference throughput and GPU memory optimization milestones. |
| **Engineering Blogs** | **Google Research** | `https://research.google/blog/rss/` (RSS XML) | Foundational models, multimodal systems, and TPU infra. |
| **Engineering Blogs** | **Anthropic Research** | `https://www.anthropic.com/feed.xml` (RSS XML) | Safety, interpretability, and system architectures. |
| **Engineering Blogs** | **Meta AI Blog** | `https://ai.meta.com/blog/rss.xml` (RSS XML) | Open-source foundation models and infrastructure updates. |
| **Engineering Blogs** | **Cloudflare Tech Blog** | `https://blog.cloudflare.com/rss/` (RSS XML) | Real-world edge computing, latency, and networking pipelines. |
| **Engineering Blogs** | **OpenAI Research** | `https://openai.com/news/rss.xml` (RSS XML) | Model releases, systems evaluations, and platform shifts. |

---

## 4. Complete Implementation (`engine.py`)

Save and run this modular collector script. It executes parallel asynchronous network requests across all sources and renders clean Markdown ready for LLM prompt injection.

```python
import asyncio
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List
import httpx

DEFAULT_USER_AGENT = "AutonomousTechIntelligenceEngine/1.0 (+https://github.com/news-agent)"

class BaseCollector:
    name: str = "base"
    category: str = "general"

    async def fetch(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        raise NotImplementedError

class HackerNewsCollector(BaseCollector):
    name = "hacker_news"
    category = "community"

    def __init__(self, query: str = "AI OR LLM", hours_back: int = 24, min_points: int = 25, limit: int = 10):
        self.query = query
        self.hours_back = hours_back
        self.min_points = min_points
        self.limit = limit

    async def fetch(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        min_timestamp = int(time.time()) - (self.hours_back * 3600)
        url = "https://hn.algolia.com/api/v1/search_by_date"
        params = {
            "query": self.query,
            "tags": "story",
            "numericFilters": f"created_at_i>{min_timestamp},points>{self.min_points}",
            "hitsPerPage": self.limit,
        }
        res = await client.get(url, params=params, timeout=10.0)
        hits = res.json().get("hits", [])
        return [
            {
                "id": str(h.get("objectID")),
                "source": self.name,
                "category": self.category,
                "title": (h.get("title") or "").strip(),
                "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                "summary": "",
                "metric": f"{h.get('points', 0)} pts | {h.get('num_comments', 0)} comments",
                "published_at": h.get("created_at", "")
            }
            for h in hits
        ]

class HuggingFacePapersCollector(BaseCollector):
    name = "huggingface_papers"
    category = "research"

    def __init__(self, limit: int = 8):
        self.limit = limit

    async def fetch(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        url = "https://huggingface.co/api/daily_papers"
        res = await client.get(url, params={"limit": self.limit}, timeout=10.0)
        items = []
        for entry in res.json():
            paper = entry.get("paper", {})
            summary = (paper.get("summary") or "").replace("\n", " ").strip()
            items.append({
                "id": str(paper.get("id")),
                "source": self.name,
                "category": self.category,
                "title": (paper.get("title") or "").strip(),
                "url": f"https://huggingface.co/papers/{paper.get('id')}",
                "summary": summary[:280] + ("..." if len(summary) > 280 else ""),
                "metric": f"{paper.get('upvotes', 0)} upvotes",
                "published_at": paper.get("publishedAt", "")
            })
        return items

class ArxivAtomCollector(BaseCollector):
    name = "arxiv"
    category = "research"

    def __init__(self, subcategory: str = "cs.AI", limit: int = 6):
        self.subcategory = subcategory
        self.limit = limit

    async def fetch(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        url = f"https://rss.arxiv.org/atom/{self.subcategory}"
        res = await client.get(url, timeout=10.0)
        root = ET.fromstring(res.content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = []
        for entry in root.findall("atom:entry", ns)[:self.limit]:
            title = (entry.find("atom:title", ns).text or "").strip().replace("\n", " ")
            summary = (entry.find("atom:summary", ns).text or "").strip().replace("\n", " ")
            url_val = (entry.find("atom:id", ns).text or "").strip()
            items.append({
                "id": url_val,
                "source": f"arxiv_{self.subcategory}",
                "category": self.category,
                "title": title,
                "url": url_val,
                "summary": summary[:280] + ("..." if len(summary) > 280 else ""),
                "metric": "Preprint",
                "published_at": entry.find("atom:published", ns).text if entry.find("atom:published", ns) is not None else ""
            })
        return items

class RedditCollector(BaseCollector):
    name = "reddit"
    category = "community"

    def __init__(self, subreddit: str = "LocalLLaMA", limit: int = 8):
        self.subreddit = subreddit
        self.limit = limit

    async def fetch(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        url = f"https://www.reddit.com/r/{self.subreddit}/hot.json"
        res = await client.get(url, params={"limit": self.limit}, timeout=10.0)
        posts = res.json().get("data", {}).get("children", [])
        items = []
        for p in posts:
            data = p.get("data", {})
            if data.get("stickied"):
                continue  # skip pinned megathreads
            selftext = (data.get("selftext") or "").replace("\n", " ").strip()
            items.append({
                "id": data.get("id"),
                "source": f"r/{self.subreddit}",
                "category": self.category,
                "title": data.get("title", "").strip(),
                "url": f"https://reddit.com{data.get('permalink')}",
                "summary": selftext[:250] + ("..." if len(selftext) > 250 else ""),
                "metric": f"{data.get('score', 0)} score | {data.get('num_comments', 0)} comments",
                "published_at": str(data.get("created_utc", ""))
            })
        return items

class GitHubTrendingCollector(BaseCollector):
    name = "github_trending"
    category = "tooling"

    def __init__(self, query: str = "topic:llm stars:>100", limit: int = 6):
        self.query = query
        self.limit = limit

    async def fetch(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        url = "https://api.github.com/search/repositories"
        params = {"q": self.query, "sort": "updated", "order": "desc", "per_page": self.limit}
        res = await client.get(url, params=params, timeout=10.0)
        repos = res.json().get("items", [])
        return [
            {
                "id": str(repo.get("id")),
                "source": self.name,
                "category": self.category,
                "title": repo.get("full_name", ""),
                "url": repo.get("html_url", ""),
                "summary": (repo.get("description") or "").strip()[:250],
                "metric": f"{repo.get('stargazers_count', 0)} stars | {repo.get('forks_count', 0)} forks",
                "published_at": repo.get("updated_at", "")
            }
            for repo in repos
        ]

class RSSFeedCollector(BaseCollector):
    def __init__(self, name: str, category: str, feed_url: str, limit: int = 5):
        self.name = name
        self.category = category
        self.feed_url = feed_url
        self.limit = limit

    async def fetch(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        res = await client.get(self.feed_url, timeout=10.0)
        root = ET.fromstring(res.content)
        items = []

        # Standard RSS 2.0 channel/item
        channel_items = root.findall("./channel/item")
        if channel_items:
            for it in channel_items[:self.limit]:
                title = (it.find("title").text or "").strip() if it.find("title") is not None else ""
                link = (it.find("link").text or "").strip() if it.find("link") is not None else ""
                desc = (it.find("description").text or "").strip() if it.find("description") is not None else ""
                items.append({
                    "id": link,
                    "source": self.name,
                    "category": self.category,
                    "title": title,
                    "url": link,
                    "summary": desc[:250] + ("..." if len(desc) > 250 else ""),
                    "metric": "Blog/Article",
                    "published_at": it.find("pubDate").text if it.find("pubDate") is not None else ""
                })
        return items

class IntelligenceEngine:
    """Master orchestrator running collectors in parallel without tokens or paid APIs."""

    def __init__(self):
        self.collectors: List[BaseCollector] = [
            HackerNewsCollector(query="AI OR LLM", hours_back=24, min_points=25, limit=8),
            HuggingFacePapersCollector(limit=6),
            ArxivAtomCollector(subcategory="cs.AI", limit=5),
            ArxivAtomCollector(subcategory="cs.LG", limit=5),
            RedditCollector(subreddit="LocalLLaMA", limit=6),
            GitHubTrendingCollector(query="topic:llm stars:>100", limit=5),
            RSSFeedCollector(name="google_research", category="engineering_blogs", feed_url="https://research.google/blog/rss/", limit=4),
            RSSFeedCollector(name="cloudflare_tech", category="engineering_blogs", feed_url="https://blog.cloudflare.com/rss/", limit=4),
            RSSFeedCollector(name="techmeme", category="community", feed_url="https://www.techmeme.com/feed.xml", limit=6),
            RSSFeedCollector(name="lobsters_ai", category="community", feed_url="https://lobste.rs/t/ai.rss", limit=5),
        ]

    async def run_pipeline(self) -> Dict[str, List[Dict[str, Any]]]:
        headers = {"User-Agent": DEFAULT_USER_AGENT}
        results_by_category: Dict[str, List[Dict[str, Any]]] = {
            "community": [],
            "research": [],
            "tooling": [],
            "engineering_blogs": []
        }

        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
            tasks = [collector.fetch(client) for collector in self.collectors]
            responses = await asyncio.gather(*tasks, return_exceptions=True)

            for collector, response in zip(self.collectors, responses):
                if isinstance(response, Exception):
                    continue  # Fail-safe: source outages do not crash ingestion
                category = collector.category
                results_by_category.setdefault(category, []).extend(response)

        return results_by_category

    def to_markdown(self, data: Dict[str, List[Dict[str, Any]]]) -> str:
        lines = [f"# Daily Tech & AI Intelligence Brief ({time.strftime('%Y-%m-%d %H:%M UTC')})\n"]
        for category, items in data.items():
            if not items:
                continue
            lines.append(f"## {category.replace('_', ' ').title()}")
            for item in items:
                summary_line = f"  > {item['summary']}\n" if item["summary"] else ""
                lines.append(f"- **[{item['title']}]({item['url']})** (`{item['source']}` | {item['metric']})")
                if summary_line:
                    lines.append(summary_line)
            lines.append("")
        return "\n".join(lines)

if __name__ == "__main__":
    engine = IntelligenceEngine()
    data = asyncio.run(engine.run_pipeline())
    markdown_output = engine.to_markdown(data)
    print(markdown_output)
```

---

## 5. Execution & Integration Guide

### Requirements
Install the only required low-level asynchronous HTTP client:
```bash
pip install httpx
```

### Feeding Directly into Your Agent Loop
Import `IntelligenceEngine` into your agent initialization routine:
```python
from engine import IntelligenceEngine
import asyncio

async def inject_daily_context():
    engine = IntelligenceEngine()
    data = await engine.run_pipeline()
    agent_brief = engine.to_markdown(data)
    
    # Ready to prepend directly to your agent's system prompt or context window:
    # agent.set_system_message(f"{BASE_PROMPT}\n\n{agent_brief}")
```

### Adding New Sources (Horizontal Extension)
To add a new source (e.g. `vLLM` releases), add a 6-line class inheriting `BaseCollector` without modifying existing collectors:
```python
class VLLMReleaseCollector(BaseCollector):
    name = "vllm_releases"
    category = "tooling"

    async def fetch(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        res = await client.get("https://github.com/vllm-project/vllm/releases.atom", timeout=10.0)
        root = ET.fromstring(res.content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        return [
            {
                "id": entry.find("atom:id", ns).text,
                "source": self.name,
                "category": self.category,
                "title": entry.find("atom:title", ns).text,
                "url": entry.find("atom:link", ns).attrib.get("href"),
                "summary": "Official framework runtime release",
                "metric": "Release",
                "published_at": entry.find("atom:updated", ns).text
            }
            for entry in root.findall("atom:entry", ns)[:5]
        ]
```