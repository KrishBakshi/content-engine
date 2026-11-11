"""Summary enrichment: picking text from pages, and the fetch/caching loop (network mocked)."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from content_engine import enrich
from content_engine.collectors.feed import parse_feed
from content_engine.schema import Item

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
ARTICLE = "This new compiler pass cuts LLM inference latency by fusing attention kernels. It ships in v2."


def _item(title="Fusing attention kernels for faster LLM inference", url="https://blog.example/post", summary=""):
    return Item("1", "hacker_news", "community", title, url, summary, "10 pts", "")


def _page(meta="", body=""):
    return f"<html><head><title>x</title>{meta}</head><body>{body}</body></html>"


# --- deciding what needs help, and where to look ---------------------------------------------

@pytest.mark.parametrize("summary, weak", [
    ("", True),
    ("Natural Language Processing", True),                          # a topic label
    ("https://a.example/x https://b.example/y", True),              # nothing but links
    ("Fusing attention kernels for faster LLM inference", True),    # same as the title
    (ARTICLE, False),
])
def test_is_weak(summary, weak):
    assert enrich.is_weak(summary, _item().title) is weak


def test_target_prefers_link_in_summary_and_skips_reddit_and_x():
    reddit_link_post = _item(url="https://www.reddit.com/r/LocalLLaMA/comments/1/x/",
                             summary="https://x.com/a/status/1 https://blog.example/news")
    assert enrich.target_url(reddit_link_post) == "https://blog.example/news"
    assert enrich.target_url(_item(url="https://old.reddit.com/r/a/comments/1/")) is None
    assert enrich.target_url(_item()) == "https://blog.example/post"


# --- extracting from a page ------------------------------------------------------------------

def test_meta_description_wins_when_on_topic():
    html = _page(f'<meta property="og:description" content="{ARTICLE}">',
                 "<article><p>Some other paragraph about kernels that is long enough to count here.</p></article>")
    assert enrich.extract_summary(html, _item().title) == ARTICLE


def test_site_wide_meta_loses_to_article_paragraph():
    # A help page whose meta tag describes the whole product, not this notice.
    html = _page('<meta name="description" content="A tool that connects everyday work into one space for teams.">',
                 f"<nav><p>Pricing, product tour, and other links that are long enough to look real.</p></nav>"
                 f"<main><p>Posted by the team</p><p>{ARTICLE}</p></main>")
    assert enrich.extract_summary(html, _item().title) == ARTICLE


def test_github_boilerplate_and_suffixes():
    title = "acme/fastattn"
    boiler = _page('<meta property="og:description" content="Contribute to acme/fastattn development by creating an account on GitHub.">')
    assert enrich.extract_summary(boiler, title) == ""
    tagline = _page('<meta property="og:description" content="Fast attention kernels for LLM serving on consumer GPUs - Release v1.2 · acme/fastattn">')
    assert enrich.extract_summary(tagline, "v1.2") == "Fast attention kernels for LLM serving on consumer GPUs"
    rc = _page('<meta property="og:description" content="Fast attention kernels for LLM serving on consumer GPUs - Release v1.3rc0: [Backend] Migrate Gemma (#66) · acme/fastattn">')
    assert enrich.extract_summary(rc, "v1.3rc0") == "Fast attention kernels for LLM serving on consumer GPUs"


def test_cookie_banners_and_short_bylines_are_not_summaries():
    html = _page(body="<p>We use cookies to improve your experience on this site, please accept them now.</p>"
                      "<p>By Jane Doe</p>")
    assert enrich.extract_summary(html, _item().title) == ""


def test_malformed_html_does_not_raise():
    assert enrich.extract_summary("<p>unclosed <b>tags " + "<" * 50, "t") == ""


# --- the fetch loop --------------------------------------------------------------------------

def _run(items, cache, handler, monkeypatch, public=True):
    async def fake_is_public(host):
        return public
    monkeypatch.setattr(enrich, "_is_public", fake_is_public)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await enrich.enrich(items, client, cache, now=NOW)
    return asyncio.run(go())


def test_enrich_fills_weak_items_and_caches(monkeypatch):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/post"})
        return httpx.Response(200, headers={"content-type": "text/html"},
                              text=_page(f'<meta name="description" content="{ARTICLE}">'))

    strong = _item(summary=ARTICLE + " Already fine.")
    weak = _item(url="https://blog.example/old")
    cache = {}
    (s_out, w_out), stats = _run([strong, weak], cache, handler, monkeypatch)

    assert s_out is strong                      # strong summaries are not touched
    assert w_out.summary == ARTICLE
    assert calls == ["https://blog.example/old", "https://blog.example/post"]  # followed the redirect
    assert (stats.weak, stats.fetched, stats.filled) == (1, 1, 1)
    assert cache["https://blog.example/old"]["summary"] == ARTICLE

    calls.clear()
    (again,), stats = _run([weak], cache, handler, monkeypatch)
    assert again.summary == ARTICLE and calls == [] and stats.cached == 1


def test_recent_miss_is_not_refetched_but_old_miss_is(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404)

    url = "https://blog.example/post"
    recent = {url: {"summary": "", "at": (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}}
    _, stats = _run([_item()], recent, handler, monkeypatch)
    assert calls == [] and stats.cached == 1 and stats.failed == 1

    old = {url: {"summary": "", "at": (NOW - timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%SZ")}}
    _run([_item()], old, handler, monkeypatch)
    assert calls == [1]


def test_private_hosts_and_non_html_are_refused(monkeypatch):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")

    (out,), stats = _run([_item()], {}, handler, monkeypatch, public=False)
    assert out.summary == "" and stats.failed == 1
    (out,), stats = _run([_item()], {}, handler, monkeypatch)
    assert out.summary == "" and stats.failed == 1


def test_is_public_rejects_loopback():
    assert asyncio.run(enrich._is_public("localhost")) is False


# --- feed-side cleanup that runs before enrichment -------------------------------------------

def test_feed_drops_headings_trailers_and_reddit_footer():
    feed = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>1</id><title>v1</title><link href="https://x/1"/><content type="html">&lt;p&gt;Signed-off-by: Jo Doe &lt;a href="mailto:1+jo@users.noreply.github.com"&gt;1+jo@users.noreply.github.com&lt;/a&gt;&lt;br&gt;Co-authored-by: Bot &lt;a href="mailto:bot@x.com"&gt;bot@x.com&lt;/a&gt;&lt;/p&gt;</content></entry>
<entry><id>2</id><title>v2</title><link href="https://x/2"/><content type="html">&lt;h1&gt;v2&lt;/h1&gt;&lt;h2&gt;Highlights&lt;/h2&gt;&lt;p&gt;Faster decoding.&lt;/p&gt;</content></entry>
<entry><id>4</id><title>p</title><link href="https://x/4"/><summary>arXiv:2611.10045v1 Announce Type: new Abstract: AI sycophancy is studied.</summary></entry>
<entry><id>3</id><title>r</title><link href="https://x/3"/><content type="html">https://news.example/a submitted by /u/someone to r/LocalLLaMA [link] [comments]</content></entry>
</feed>"""
    items = parse_feed(feed, source="s", category="tooling", limit=5, metric="m")
    assert [i.summary for i in items] == ["", "Faster decoding.", "AI sycophancy is studied.", "https://news.example/a"]


def test_bot_check_pages_are_rejected_everywhere():
    title = "Fusing attention kernels for faster LLM inference"
    by_title = "<html><head><title>Just a moment...</title></head><body><p>" + ARTICLE + "</p></body></html>"
    assert enrich.extract_summary(by_title, title) == ""
    by_meta = _page('<meta name="description" content="Please enable JavaScript and cookies to continue to this site.">')
    assert enrich.extract_summary(by_meta, title) == ""


# --- collector GET: one retry for transient errors, none for 4xx -----------------------------

def _get(statuses, monkeypatch):
    from content_engine.collectors import base
    monkeypatch.setattr(base, "RETRY_DELAY_S", 0)
    seen = []

    def handler(request):
        seen.append(1)
        status = statuses[min(len(seen), len(statuses)) - 1]
        if status == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(status)

    class C(base.BaseCollector):
        name, category = "c", "research"
        async def fetch(self, client):
            return []

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await C().get(client, "https://x.example/feed")
    return go, seen


@pytest.mark.parametrize("statuses, calls, ok", [
    ([200], 1, True),
    ([503, 200], 2, True),          # server blip: retried once
    (["timeout", 200], 2, True),    # timeout: retried once
    ([503, 503], 2, False),         # still down: gives up after the retry
    ([429], 1, False),              # rate limited: never retried
    ([404], 1, False),
])
def test_get_retries_only_transient_errors(statuses, calls, ok, monkeypatch):
    go, seen = _get(statuses, monkeypatch)
    if ok:
        asyncio.run(go())
    else:
        with pytest.raises((httpx.HTTPStatusError, httpx.TransportError)):
            asyncio.run(go())
    assert len(seen) == calls
