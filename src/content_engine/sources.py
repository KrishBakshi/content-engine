"""The source registry. Adding a source = one entry here (and a collector class if it isn't a feed).

Deviations from the spec's registry (docs/project_engine_instructions.md, section 3),
found by probing every endpoint on 2026-11-10:
- Reddit `hot.json` returns 403 to unauthenticated clients; `hot.rss` (Atom) works but has
  no score, so Reddit items carry metric "hot" instead of points. Both subreddits come from
  one request (see collectors/reddit.py for the rate limit).
- Anthropic publishes no feed (`/feed.xml`, `/rss.xml`, `/news/rss.xml` are all 404): not included.
- `ai.meta.com/blog/rss.xml` is 404; Meta's engineering blog feed is used instead.
- llama.cpp moved from `ggerganov/` to `ggml-org/`.
"""

from __future__ import annotations

from content_engine.collectors.api import GitHubSearchCollector, HackerNewsCollector, HuggingFacePapersCollector
from content_engine.collectors.base import BaseCollector
from content_engine.collectors.feed import FeedCollector
from content_engine.collectors.reddit import RedditCollector


def default_collectors() -> list[BaseCollector]:
    return [
        # community
        HackerNewsCollector(words="AI LLM", hours_back=24, min_points=25, limit=10),
        RedditCollector(["LocalLLaMA", "MachineLearning"], per_sub=8),
        FeedCollector("lobsters_ai", "community", "https://lobste.rs/t/ai.rss", 6, "Discussion"),
        FeedCollector("techmeme", "community", "https://www.techmeme.com/feed.xml", 6, "News"),
        # research (arXiv feeds are empty on days with no announcement, e.g. weekends)
        HuggingFacePapersCollector(limit=8),
        FeedCollector("arxiv_cs_ai", "research", "https://rss.arxiv.org/atom/cs.AI", 5, "Preprint", empty_ok=True),
        FeedCollector("arxiv_cs_lg", "research", "https://rss.arxiv.org/atom/cs.LG", 5, "Preprint", empty_ok=True),
        FeedCollector("arxiv_cs_cl", "research", "https://rss.arxiv.org/atom/cs.CL", 5, "Preprint", empty_ok=True),
        # tooling
        GitHubSearchCollector(),
        FeedCollector("llama_cpp_releases", "tooling", "https://github.com/ggml-org/llama.cpp/releases.atom", 3, "Release"),
        FeedCollector("vllm_releases", "tooling", "https://github.com/vllm-project/vllm/releases.atom", 3, "Release"),
        # engineering_blogs
        FeedCollector("google_research", "engineering_blogs", "https://research.google/blog/rss/", 4, "Blog"),
        FeedCollector("openai_news", "engineering_blogs", "https://openai.com/news/rss.xml", 4, "Blog"),
        FeedCollector("meta_engineering", "engineering_blogs", "https://engineering.fb.com/feed/", 4, "Blog"),
        FeedCollector("cloudflare_blog", "engineering_blogs", "https://blog.cloudflare.com/rss/", 4, "Blog"),
    ]
