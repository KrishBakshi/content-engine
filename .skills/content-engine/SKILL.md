---
name: content-engine
description: Read today's tech and AI news from the content-engine's saved output (trending topics, new items, agent AI, physical AI, research, tooling), run a fresh fetch, check that the engine is healthy, or add a news source. Use when asked "what's new / trending in AI, agents or robotics", for a daily brief, or to keep a news feed running.
---

# content-engine

A scheduled job that collects tech and AI news from about 40 free public sources (no API keys,
no LLM calls) and saves it as JSON and Markdown. You will mostly **read** what it saved; you
rarely need to run it.

Everything lives under one data folder: `./data`, or `$CONTENT_ENGINE_DATA_DIR` on a server
(the deployed default is `/var/lib/content-engine`). Paths below are relative to it.

## 1. Answer a news question: read, don't fetch

Read these files in this order and stop as soon as you can answer.

| File | What it is | Use it for |
|---|---|---|
| `latest/trending.json` | Up to 5 trending topics, up to 3 most-discussed urls | "What's the big story?" Read this first |
| `latest/brief.md` | The readable brief: Trending, Most discussed, New, Already delivered | Pasting into a prompt or summarising |
| `latest/items.json` | Every item, one flat list | Filtering by section, source or date |
| `status.json` | Outcome of the last run, per-source results, `health` | Checking freshness and failures |

`latest/` is replaced each run. Earlier runs from the last 4 days are in `runs/<id>/` (the id is
the run's UTC start time, `20261113T145233Z`), each with its own `items.json`,
`trending.json`, `brief.md` and `report.json`.

### Items (`items.json`)

Every item has the same fields:

```json
{"id": "...", "source": "latent_space", "category": "agents", "title": "...", "url": "...",
 "summary": "up to 250 chars, the source's own words", "metric": "353 pts | 310 comments",
 "published_at": "2026-11-12T07:19:38Z", "delivered": false,
 "also_in": [{"source": "hacker_news", "metric": "212 pts | 98 comments"}]}
```

- **`category`** is the section: `agents` (backend agent AI), `physical_ai` (robotics, embodied
  AI), `community`, `research`, `tooling`, `engineering_blogs`.
- **`delivered`**: `false` = no earlier run had this url (new); `true` = already in an earlier
  brief. For "what's new" filter `delivered == false`.
- **`also_in`**: other sources that carried the same link this run. A non-empty list means
  the item is being picked up elsewhere.
- **`summary`** is quoted, never rewritten, and may be empty. The url has the rest.
- **`metric`** is a plain string and differs by source ("N pts | N comments", "N upvotes",
  "N stars | N forks", or a label like "Blog" or "Preprint").

### Trending (`trending.json`)

```json
{"topics": [{"label": "Jensen Huang · Nvidia", "mentions": 9, "sources": ["Latent Space", "Reddit"],
             "points": 0, "comments": 0, "score": 16.0, "urls": ["https://..."]}],
 "most_discussed": ["https://..."]}
```

`urls` and `most_discussed` are item urls: look them up in `items.json` for titles and
summaries. The list is empty on a quiet day. That is correct, not an error. Topics are a
**snapshot of the current run**: they are recomputed every run and not remembered, so compare
`runs/*/trending.json` if you need to know whether something is rising or fading.

## 2. Check it is alive before trusting it

Read `status.json`:

- `health.alert == true`: something is wrong. `health.reasons` says what, in words. Report it.
- `started_at` older than about 2 hours (the job is hourly): the scheduler stopped and the
  data is stale. Say so before presenting the news as current.
- `state`: `ok` (every source answered), `partial` (some failed, normal now and then),
  `failed` (none answered).
- `failed_sources` / `empty_sources`: which sources are down or returning nothing.

## 3. Run it (only when asked, or the data is stale)

```sh
uv run content-engine run                # fetch, save, delete data older than 4 days
uv run content-engine run --new-only     # brief.md without the already-delivered part
uv run content-engine run --plain        # no spinner (automatic when not in a terminal)
uv run content-engine sources            # list every source
uv run content-engine clean --dry-run    # show what retention would delete
```

Other options: `--print` (also write the brief to stdout), `--no-enrich` (skip reading linked
pages for missing summaries), `--no-clean`, `--retention-days N`, `--data-dir PATH`, `-v`.
Environment: `CONTENT_ENGINE_DATA_DIR`, `CONTENT_ENGINE_RETENTION_DAYS`,
`CONTENT_ENGINE_NEW_ONLY=1`.

Exit codes: `0` finished (even if `partial`), `1` every source failed or it crashed, `75`
another run holds the lock, which means skip, not retry in a loop.

**Don't run it more than about once an hour.** The server already runs it on a timer, so check
`started_at` first: if it is recent, just read the files.

## 4. Add or remove a source

Sources live in `src/content_engine/sources.py` only. For any RSS or Atom feed it is one line:

```python
FeedCollector("name", "agents", "https://example.com/feed.xml", 3, "Blog"),
```

Arguments: a unique name, a category (see section 1), the feed url, how many items to take, a
label. For a feed that posts rarely add `max_age_days=30`. Before adding one:

1. Fetch the url once with `curl` and confirm it is real XML with recent items.
2. Run `uv run content-engine run --plain` and confirm it shows `ok` in `status.json`.
3. Run `uv run pytest -q` (no network needed).
4. Note anything you tried and rejected in the `sources.py` docstring.

A site with no feed can use `ListingPageCollector` (see `collectors/listing.py`); it is more
fragile, so prefer a feed.

## 5. Rules for working in this repo

- **No API keys, paid search, headless browsers or LLM calls** in the engine. A source that
  needs one doesn't go in.
- **`items.json` is the source of truth.** `brief.md` is only a view of it. Never put content
  in the brief that isn't in the data.
- **Don't edit files under `data/`** and don't hand-edit `delivered.json` to "reset" news:
  delete `data/runs/` and `data/delivered.json` together if you really need a clean slate.
- Keep field names and file names stable: other agents read them.
- Read `CLAUDE.md` before changing code; `deploy/README.md` before touching the server setup.
  Commit subjects use `add:`, `update:` or `fix:`, and commits are made only when asked.
