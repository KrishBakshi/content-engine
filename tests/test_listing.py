"""Sites without a feed: links from a listing page, metadata from each article page."""

import asyncio

import httpx

from content_engine.collectors.listing import ListingPageCollector, extract_links, parse_article

LISTING = """<html><body>
<a href="/engineering/featured-old"><img alt="x"></a>
<a href="/engineering/newest">Newest</a>
<a href="/engineering/newest#section">Newest again</a>
<a href="/engineering/featured-old">Featured old</a>
<a href="/engineering">The listing itself</a>
<a href="/careers/apply">Careers</a>
<a href="https://other.example/engineering/elsewhere">Other site</a>
<a href="/engineering/broken">Broken</a>
<a href="/engineering/blurb-only">Blurb only</a>
</body></html>"""


def article(title, published, description):
    return (f'<html><head><meta property="og:title" content="{title}">'
            f'<meta property="og:description" content="{description}">'
            f'<meta property="article:published_time" content="{published}"></head><body></body></html>')


def test_extract_links_keeps_page_order_and_only_matching_same_site_articles():
    links = extract_links(LISTING, "https://site.example/engineering", r"/engineering/[a-z0-9-]+", 10)
    assert links == ["https://site.example/engineering/featured-old", "https://site.example/engineering/newest",
                     "https://site.example/engineering/broken", "https://site.example/engineering/blurb-only"]
    assert len(extract_links(LISTING, "https://site.example/engineering", r"/engineering/[a-z0-9-]+", 2)) == 2


def test_parse_article_title_and_blurb_rules():
    kw = dict(source="s", category="agents", metric="Blog", site_name="Acme", site_blurb="Acme is a company")
    item = parse_article(article("Source control at Acme", "2026-10-13T00:00:00.000Z", "Merges grew tenfold."),
                         "https://x/a", **kw)
    assert item.title == "Source control at Acme"          # the name is part of the title: kept
    assert (item.summary, item.published_at) == ("Merges grew tenfold.", "2026-10-13T00:00:00Z")
    suffixed = parse_article(article("Agent memory | Acme", "", "Acme is a company that builds things."),
                             "https://x/b", **kw)
    assert suffixed.title == "Agent memory"                # "| Acme" is a site suffix: removed
    assert suffixed.summary == ""                          # site-wide blurb is not a summary
    assert parse_article("<html><head></head></html>", "https://x/c", **kw) is None


def test_collector_sorts_by_publish_date_dedupes_and_survives_a_broken_article(monkeypatch):
    from content_engine.collectors import base
    monkeypatch.setattr(base, "RETRY_DELAY_S", 0)
    pages = {
        "/engineering": LISTING,
        "/research": '<a href="/engineering/newest">dup</a><a href="/engineering/research-only">r</a>',
        "/engineering/featured-old": article("Featured old", "2026-03-24T00:00:00Z", "Old but featured."),
        "/engineering/newest": article("Newest", "2026-11-06T00:00:00Z", "The latest post."),
        "/engineering/blurb-only": article("Blurb only", "2026-10-01T00:00:00Z", "Acme is a company."),
        "/engineering/research-only": article("Research only", "2026-11-01T00:00:00Z", "A research post."),
    }
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path in pages:
            return httpx.Response(200, text=pages[request.url.path])
        return httpx.Response(404)

    collector = ListingPageCollector("acme", [
        ("acme_engineering", "agents", "https://site.example/engineering"),
        ("acme_research", "engineering_blogs", "https://site.example/research"),
    ], link_pattern=r"/engineering/[a-z0-9-]+", per_listing=2, scan=6, site_blurb="Acme is a company")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await collector.fetch(client)

    items = asyncio.run(go())
    assert [(i.source, i.title) for i in items] == [
        ("acme_engineering", "Newest"), ("acme_engineering", "Blurb only"),   # newest two; featured-old dropped
        ("acme_research", "Research only"),                                    # "newest" already taken above
    ]
    assert calls.count("/engineering/newest") == 1                             # fetched once despite two listings
    assert collector.host == "site.example"
