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
