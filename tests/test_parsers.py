"""Parsers and schema helpers, on saved payloads (no network)."""

from pathlib import Path

import pytest
from defusedxml import EntitiesForbidden

from content_engine.collectors.api import parse_github_search, parse_hacker_news, parse_hf_papers
from content_engine.collectors.feed import parse_feed
from content_engine.collectors.reddit import split_by_subreddit
from content_engine.schema import SUMMARY_MAX, Item, clean_text, to_iso, truncate

FIXTURES = Path(__file__).parent / "fixtures"
FIELDS = set(Item.__dataclass_fields__)


def test_rss_items_are_normalized():
    items = parse_feed((FIXTURES / "rss.xml").read_bytes(), source="ex", category="engineering_blogs",
                       limit=10, metric="Blog")
    assert [i.title for i in items] == ["First & bold post", "Second", "Third"]
    first = items[0]
    assert first.summary == "Hello world"
    assert first.published_at == "2026-11-10T07:30:00Z"  # +0100 converted to UTC
    assert items[1].summary == ""  # a bare "Comments" link label is not a summary
    assert items[2].published_at == ""
    assert all(set(i.to_dict()) == FIELDS for i in items)


def test_rss_respects_limit():
    items = parse_feed((FIXTURES / "rss.xml").read_bytes(), source="ex", category="c", limit=2, metric="m")
    assert len(items) == 2


def test_atom_releases_and_arxiv():
    items = parse_feed((FIXTURES / "atom.xml").read_bytes(), source="ex", category="tooling", limit=10,
                       metric="Release")
    release, paper = items
    assert release.url == "https://github.com/ggml-org/llama.cpp/releases/tag/b7000"
    assert release.summary == "Faster Q4 kernels"  # falls back to <content> without <summary>
    assert release.published_at == "2026-11-09T12:00:00Z"  # falls back to <updated>
    assert paper.title == "A Paper Title"
    assert paper.published_at == "2026-11-10T05:00:00Z"


def test_feed_parser_rejects_entity_expansion():
    with pytest.raises(EntitiesForbidden):
        parse_feed((FIXTURES / "billion_laughs.xml").read_bytes(), source="x", category="c", limit=1, metric="m")


def test_reddit_split_caps_each_subreddit():
    items = parse_feed((FIXTURES / "reddit.xml").read_bytes(), source="reddit", category="community",
                       limit=50, metric="hot")
    out = split_by_subreddit(items, ["LocalLLaMA", "MachineLearning"], per_sub=2)
    assert [(i.source, i.title) for i in out] == [
        ("reddit_localllama", "L1"), ("reddit_localllama", "L2"), ("reddit_machinelearning", "M1"),
    ]


def test_hacker_news_falls_back_to_discussion_url():
    payload = {"hits": [
        {"objectID": "1", "title": "Ext", "url": "https://x.dev", "points": 30, "num_comments": 4,
         "created_at_i": 1794297600},
        {"objectID": "2", "title": "Ask HN: y?", "url": None, "story_text": "<p>body</p>", "points": 40},
    ]}
    ext, ask = parse_hacker_news(payload, source="hacker_news", category="community")
    assert ext.metric == "30 pts | 4 comments"
    assert ext.published_at == "2026-11-10T08:00:00Z"
    assert ask.url == "https://news.ycombinator.com/item?id=2"
    assert ask.summary == "body"


def test_hf_papers_and_github():
    hf = parse_hf_papers([{"paper": {"id": "2611.1", "title": "T", "summary": "s " * 400, "upvotes": 9,
                                     "publishedAt": "2026-11-10T00:00:00.000Z"}}],
                         source="huggingface_papers", category="research")[0]
    assert hf.url == "https://huggingface.co/papers/2611.1"
    assert len(hf.summary) <= SUMMARY_MAX and hf.summary.endswith("...")
    gh = parse_github_search({"items": [{"id": 5, "full_name": "a/b", "html_url": "https://github.com/a/b",
                                         "description": None, "stargazers_count": 120, "forks_count": 3,
                                         "created_at": "2026-11-08T00:00:00Z"}]},
                             source="gh", category="tooling")[0]
    assert (gh.title, gh.summary, gh.metric) == ("a/b", "", "120 stars | 3 forks")


@pytest.mark.parametrize("raw, iso", [
    (1794297600, "2026-11-10T08:00:00Z"),
    ("2026-11-10T08:00:00Z", "2026-11-10T08:00:00Z"),
    ("Tue, 10 Nov 2026 08:00:00 GMT", "2026-11-10T08:00:00Z"),
    ("", ""),
    ("not a date", "not a date"),
])
def test_to_iso(raw, iso):
    assert to_iso(raw) == iso


def test_text_helpers():
    assert clean_text("<p>a&nbsp;&amp;\n b</p>") == "a & b"
    assert truncate("x" * 10, 10) == "x" * 10
    assert truncate("x" * 11, 10) == "xxxxxxx..."           # no space to cut at: hard cut
    assert truncate("alpha beta gamma delta", 15) == "alpha beta..."  # never mid-word
    long = "word " * 100
    assert len(truncate(long)) <= SUMMARY_MAX == 250


# --- topic sections: agents and physical AI ---------------------------------------------------

def test_reddit_split_puts_each_subreddit_in_its_category():
    feed = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>1</id><title>A</title><link href="https://www.reddit.com/r/AI_Agents/comments/1/a/"/></entry>
<entry><id>2</id><title>R</title><link href="https://www.reddit.com/r/robotics/comments/2/r/"/></entry>
<entry><id>3</id><title>L</title><link href="https://www.reddit.com/r/LocalLLaMA/comments/3/l/"/></entry>
</feed>"""
    items = parse_feed(feed, source="reddit", category="community", limit=10, metric="hot")
    out = split_by_subreddit(items, {"LocalLLaMA": "community", "AI_Agents": "agents", "robotics": "physical_ai"}, 5)
    assert [(i.source, i.category) for i in out] == [
        ("reddit_ai_agents", "agents"), ("reddit_robotics", "physical_ai"), ("reddit_localllama", "community")]


def test_wordpress_footer_is_stripped():
    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel><item><title>Viam Launchpad</title>
<link>https://x/1</link><description>Launchpad helps startups ship robots. The post Viam Launchpad appeared
first on The Robot Report.</description></item></channel></rss>"""
    (item,) = parse_feed(feed, source="s", category="physical_ai", limit=1, metric="News")
    assert item.summary == "Launchpad helps startups ship robots."


def test_newer_than_drops_old_items_and_keeps_undated():
    from datetime import datetime, timezone
    from content_engine.collectors.feed import newer_than
    now = datetime(2026, 11, 13, tzinfo=timezone.utc)
    mk = lambda published: Item("1", "s", "agents", "t", "https://x", "", "m", published)
    items = [mk("2026-11-10T00:00:00Z"), mk("2026-07-28T00:00:00Z"), mk(""), mk("not a date")]
    assert [i.published_at for i in newer_than(items, 30, now)] == ["2026-11-10T00:00:00Z", "", "not a date"]


def test_slow_feed_with_age_cap_may_be_empty():
    from content_engine.collectors.feed import FeedCollector
    assert FeedCollector("mcp", "agents", "https://x/f", max_age_days=45).empty_ok is True
    assert FeedCollector("blog", "agents", "https://x/f").empty_ok is False


def test_registry_names_are_unique_and_categories_known():
    from content_engine.schema import CATEGORIES
    from content_engine.sources import default_collectors
    collectors = default_collectors()
    names = [c.name for c in collectors]
    assert len(names) == len(set(names))
    assert {c.category for c in collectors} <= set(CATEGORIES)
    assert {"agents", "physical_ai"} <= {c.category for c in collectors}


def test_brief_puts_focus_sections_first_with_readable_headings():
    from datetime import datetime, timezone
    from content_engine.render import to_markdown
    mk = lambda cat: Item(cat, "s", cat, f"title {cat}", f"https://x/{cat}", "", "m", "").to_dict()
    brief = to_markdown([mk("community"), mk("physical_ai"), mk("agents")],
                        datetime(2026, 11, 13, tzinfo=timezone.utc))
    headings = [line for line in brief.splitlines() if line.startswith("#")][1:]
    assert headings == ["## New", "### Agent AI", "### Physical AI", "### Community"]
