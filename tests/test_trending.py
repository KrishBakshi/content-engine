"""Trending topics: names, independent sources, shared links, and the brief's top section."""

import asyncio
from datetime import datetime, timezone

import httpx

from content_engine import engine
from content_engine.collectors.base import BaseCollector
from content_engine.render import to_markdown
from content_engine.schema import Item, canonical_url, source_family
from content_engine.trending import find_trending, learn_names

NOW = datetime(2026, 11, 13, tzinfo=timezone.utc)


def item(source, title, metric="Blog", summary="", url=None, also_in=()):
    return {"id": title, "source": source, "category": "community", "title": title,
            "url": url or f"https://x/{abs(hash(title))}", "summary": summary, "metric": metric,
            "published_at": "", "delivered": False, "also_in": list(also_in)}


def test_canonical_url_ignores_scheme_www_slash_fragment_and_tracking():
    a = canonical_url("https://www.example.com/post/?utm_source=hn&id=7#top")
    assert a == canonical_url("http://example.com/post?id=7") == "example.com/post?id=7"
    assert canonical_url("https://example.com/a") != canonical_url("https://example.com/b")


def test_source_family_groups_one_outlet_under_several_names():
    assert source_family("hacker_news_agents") == source_family("hacker_news") == "hacker_news"
    assert source_family("reddit_localllama") == source_family("reddit_robotics") == "reddit"
    assert source_family("techmeme") == "techmeme"


def test_names_are_learned_from_how_feeds_write_them():
    items = [item("a", "We talked to Jensen Huang about chips", summary="Then Jensen Huang left. The market moved."),
             item("b", "Notes on search and builds", summary="We did some search on our builds today."),
             item("c", "NVIDIA ships a thing")]
    names = learn_names(items)
    assert "jensen" in names and "huang" in names     # capitalised mid-sentence, never lowercase
    assert "nvidia" in names                          # inner capitals mark a name anywhere
    assert "search" not in names and "builds" not in names


def test_title_case_headlines_do_not_make_ordinary_words_names():
    items = [item("a", "How Our Team Builds Faster Search For Everyone"),
             item("b", "Why Search Builds Matter More Than Ever Before")]
    assert not learn_names(items) & {"search", "builds", "faster"}


def test_same_name_in_independent_sources_is_a_topic_but_one_source_is_not():
    items = [item("reddit_localllama", "Jensen Huang resigns as CEO"),
             item("latent_space", "Jensen Huang steps down after 33 years"),
             item("the_robot_report", "Jensen Huang exit shakes the sector"),
             item("hacker_news", "Show HN: Lunar compass for hikers"),
             item("reddit_robotics", "Lunar compass build log")]
    topics = find_trending(items)["topics"]
    assert [t["label"] for t in topics] == ["Jensen Huang"]
    assert sorted(topics[0]["sources"]) == ["Latent Space", "Reddit", "The Robot Report"]


def test_one_outlet_under_several_names_is_one_voice():
    reddit_only = [item("reddit_localllama", "Tessellate 4 is out and fast"),
                   item("reddit_robotics", "Tessellate 4 on a robot arm"),
                   item("reddit_ai_agents", "Tessellate 4 for agents")]
    assert find_trending(reddit_only)["topics"] == []             # three subreddits are one voice
    both = reddit_only + [item("hacker_news", "Tessellate 4 benchmarks"),
                          item("hacker_news_agents", "Tessellate 4 on HN again")]
    (topic,) = find_trending(both)["topics"]
    assert topic["sources"] == ["Reddit", "Hacker News"]          # two voices, not five


def test_a_name_deep_in_a_summary_is_a_passing_mention():
    opening = "Zero-shot results from big models are hard to dismiss. " * 3
    items = [item("reddit_robotics", "Dexterous hands close a zipper", summary=opening + "A paper used GPT-6 Astra."),
             item("huggingface_papers", "JustRUA: a study of GPT-6 robot agents"),
             item("latent_space", "GPT-6 is out for everyone")]
    topics = find_trending(items)["topics"]
    assert [t["label"] for t in topics] == ["GPT 6"]
    assert len(topics[0]["urls"]) == 2 and items[0]["url"] not in topics[0]["urls"]


def test_a_shared_word_does_not_pull_an_unrelated_item_into_a_topic():
    items = [item("reddit_localllama", "Jensen Huang resigns as CEO"),
             item("latent_space", "Jensen Huang steps down after 33 years"),
             item("techmeme", "Jensen Huang biographer speaks (Stephen Witt/New York Times)"),
             item("techmeme", "Jensen Huang exit explained (Reporter/Financial Times)"),
             item("techmeme", "A chip maker tapes out a new part (Reporter/EE Times)")]
    (topic,) = find_trending(items)["topics"]
    assert topic["label"] == "Jensen Huang"
    assert items[4]["url"] not in topic["urls"]


def test_one_story_under_two_names_is_one_topic():
    items = [item("reddit_localllama", "Jensen Huang resigns, Colette Kress named interim CEO"),
             item("latent_space", "Jensen Huang out; Colette Kress takes over"),
             item("techmeme", "Colette Kress steps in as Jensen Huang leaves")]
    assert len(find_trending(items)["topics"]) == 1


def test_a_source_talking_about_itself_does_not_count():
    own = [item("nvidia_robotics", "NVIDIA Holoscan 5 makes arms weightless"),
           item("nvidia_developer_robotics", "NVIDIA Holoscan guide for surgeons")]
    assert find_trending(own + [item("reddit_localllama", "NVIDIA Holoscan question")])["topics"] == []
    topics = find_trending(own + [item("reddit_localllama", "NVIDIA Holoscan question"),
                                  item("techmeme", "NVIDIA Holoscan wins a contract")])["topics"]
    assert [t["label"] for t in topics] == ["NVIDIA Holoscan"]
    assert topics[0]["sources"] == ["Reddit", "Techmeme"]                  # NVIDIA's own blog is not a voice


def test_a_link_carried_by_two_sources_is_a_topic_on_its_own():
    shared = item("langchain_blog", "Quillbook adds durable agents",
                  also_in=[{"source": "hacker_news", "metric": "212 pts | 98 comments"}])
    topics = find_trending([shared, item("techmeme", "Unrelated headline")])["topics"]
    assert [t["label"] for t in topics] == ["Quillbook adds durable agents"]
    assert topics[0]["points"] == 212 and topics[0]["mentions"] == 2


def test_most_discussed_is_by_hn_points_above_a_floor_and_excludes_topic_items():
    items = [item("hacker_news", "Zephyr kernel lands", "400 pts | 90 comments"),
             item("hacker_news", "Quiet post", "30 pts | 2 comments"),
             item("hacker_news", "Other noisy post", "120 pts | 60 comments")]
    assert find_trending(items)["most_discussed"] == [items[0]["url"], items[2]["url"]]


def test_at_most_five_topics():
    items = []
    for n in range(8):
        name = f"Zork{chr(65 + n)}Dyn Quibble{chr(65 + n)}Dyn"
        items += [item("reddit_localllama", f"{name} arrives"), item("latent_space", f"{name} explained")]
    assert len(find_trending(items)["topics"]) == 5


def test_brief_leads_with_trending_then_most_discussed_then_new():
    items = [item("reddit_localllama", "Jensen Huang resigns"), item("latent_space", "Jensen Huang steps down"),
             item("hacker_news", "Some big post", "300 pts | 100 comments")]
    brief = to_markdown(items, NOW, trending=find_trending(items))
    headings = [l for l in brief.splitlines() if l.startswith("## ")]
    assert headings == ["## Trending topics", "## Most discussed", "## New"]
    assert "### 1. Jensen Huang" in brief and "2 mentions across 2 sources: Reddit, Latent Space" in brief


def test_brief_without_trending_is_unchanged():
    items = [item("reddit_localllama", "Only post")]
    assert "Trending" not in to_markdown(items, NOW) and "Most discussed" not in to_markdown(items, NOW)


def test_engine_keeps_a_repeated_link_as_also_in_instead_of_dropping_it():
    class Fake(BaseCollector):
        category = "community"

        def __init__(self, name, url, metric):
            self.name, self.url, self.metric = name, url, metric

        async def fetch(self, client: httpx.AsyncClient):
            return [Item(self.name, self.name, "community", "Same story", self.url, "s" * 60, self.metric, "")]

    result = asyncio.run(engine.run([
        Fake("langchain_blog", "https://blog.example/post/", "Blog"),
        Fake("hacker_news", "http://www.blog.example/post?utm_source=hn", "212 pts | 98 comments"),
        Fake("hacker_news_agents", "https://blog.example/post", "50 pts | 5 comments")]))
    assert len(result.items) == 1                                   # still listed once
    (kept,) = result.items
    assert kept.source == "langchain_blog"
    assert [a["source"] for a in kept.also_in] == ["hacker_news", "hacker_news_agents"]
    assert kept.to_dict()["also_in"][0]["metric"] == "212 pts | 98 comments"
