# CLAUDE.md

Zero-token tech & AI news ingestion. Each run fetches free public JSON / RSS / Atom endpoints,
normalizes everything into one item schema, writes a run directory plus a markdown brief, and
deletes data older than the retention window (default 4 days). It is scheduled on Linux by a
systemd timer or cron. The spec it was built from is `docs/project_engine_instructions.md`.

## Commands

```sh
uv sync                                     # install (Python >= 3.10; .python-version pins 3.12)
uv run pytest -q                            # tests; no network needed
uv run content-engine run --print           # live run into ./data, prints the brief
uv run content-engine run --data-dir /tmp/x # keep a test run out of ./data
uv run content-engine clean --dry-run       # what retention would delete
uv run content-engine sources               # registered sources
python3 deploy/render.py --out deploy/out   # render systemd/cron files for a host
```

Before committing, run `uv run pytest -q`. If you changed a collector or the registry, also do one
live run and check `status.json`: every source should show `ok: true`.

## Layout

- `src/content_engine/schema.py`: `Item` (the 8-field normalized schema) plus `clean_text`,
  `truncate` (250 chars, word boundary, no rewording) and `to_iso` (epoch / RFC 822 / ISO → ISO 8601 UTC). Every collector
  goes through these helpers. Don't format fields by hand.
- `collectors/base.py`: `BaseCollector`. Subclasses set `name`, `category`, and optionally `host`,
  and implement `fetch`. Use `self.get()`: it raises on non-2xx, so a 429 is reported as an error
  and not silently counted as zero items.
- `collectors/feed.py`: generic RSS 2.0 + Atom (`FeedCollector`, `parse_feed`). Most sources are
  just a `FeedCollector(...)` line. `collectors/api.py`: HN Algolia, HF papers, GitHub search.
  `collectors/reddit.py`: one multireddit request split per subreddit; not registered since
  Reddit ended RSS (2026-11-13).
  `collectors/listing.py`: for sites with no feed (Anthropic): article links from a listing
  page, then title, summary and date from each article's meta tags. More fragile than a feed
  and one request per article, so use it only when there is no feed.
- Categories (`schema.CATEGORIES`, also the section order of the brief): `agents` and
  `physical_ai` are the user's focus topics and come first, then `community`, `research`,
  `tooling`, `engineering_blogs`. A category is a topic, so `agents` holds blogs, papers,
  repos and releases about agents together.
- `sources.py`: the registry, and the only file that changes when adding or removing a feed.
  Order matters: it is the order within a section, and on a duplicate URL the earlier source
  keeps the item. `max_age_days` is only for feeds that post rarely (a weekly newsletter, a
  spec with two releases a year), so their last posts don't repeat in every brief for months.
  It is not a topic filter and the general sections don't use it. Its
  docstring lists where the registry differs from the spec and why. Keep that list current.
- `engine.py`: runs collectors concurrently (same-`host` collectors in sequence), records a
  `SourceReport` per source, and dedupes by URL. A failing source never fails the run.
- `enrich.py`: fills weak summaries (empty, a label under 40 chars, only links) from the
  linked page: meta description, else the first article paragraph, and only text that shares
  a word with the title (filters site-wide blurbs). Skips reddit/x hosts, refuses private IPs
  on every redirect hop, reads at most 1 MB per page. Results are cached in
  `summary_cache.json`; a miss is retried after 6 hours.
- `progress.py`: the terminal spinner. `run` shows it only when stderr is a terminal (and not
  with `--plain`): log lines are held back while it spins, then only warnings and errors are
  printed (everything with `-v`), then a one-line result. Under systemd or cron nothing changes: no spinner, logs stream as they happen. The
  engine reports progress through `on_progress(stage, done, total, detail)`; it never prints.
- `trending.py`: trending topics from the item dicts (`find_trending`), no LLM. A topic is a
  name that independent sources share: names are learned from how feeds capitalise them
  (capitalised mid-sentence more often than lowercase, inner capitals, or a never-lowercase
  word that starts two items); title words only, plus the first 160 characters of a summary
  for two-word names and "Name 4"-style versions. A source's own name doesn't count for it
  (`OWN_NAMES`); three subreddits or three HN queries are one voice (`schema.source_family`);
  one-word names need 3 voices, two-word names 2; broad names (`BROAD`) never start a topic.
  A shorter name folds into a topic only if it is part of the topic's own name; topics that
  are mostly the same items are merged. The engine records a link carried by several sources
  as `also_in` on the item instead of dropping it. Tune only with a failing test first: an
  earlier version let "search" and "Times" create topics.
- `storage.py`: data layout, atomic writes, lock, retention, and the save chain. `save_run`
  marks each item `delivered` against `delivered.json` (urls earlier runs carried), writes
  `items.json`, works out `trending.json` from it, renders `brief.md` from both (`render.py`: New first, then Already
  delivered), and writes `report.json` with `new` / `already_delivered` per source.
  `cli.py`: `run` / `clean` / `sources` and the exit codes.
- `deploy/`: `templates/*.template` are the source of truth. `systemd/` and `cron/` hold them
  rendered with defaults, and `tests/test_deploy.py` checks they match. Edit the template,
  re-render, then copy the file over the committed one. `deploy/README.md` is the operator and
  agent guide.
- `assets/`: `banner.svg` and `logo.svg` (plus `logo-512.png`, `logo-128.png` for listings
  that need PNG) are generated by `assets/make_art.py` in a hand-drawn Excalidraw style, with
  lettering stored as outlines so no font is needed to view them. Don't edit the SVGs by hand:
  change the script and regenerate (its docstring has the command and the font URL).
- `.skills/content-engine/SKILL.md`: the skill that tells another agent how to use the engine (read the news, check health, run,
  add a source). Update it whenever a file name, field, flag or exit code changes.
- `tests/`: parsers run on `tests/fixtures/` files; storage tests use `tmp_path` and a fixed `NOW`.

## Invariants

- **No keys, no paid APIs, no browsers, no LLM calls.** A source that needs auth or JavaScript
  rendering doesn't go in. Probe a new endpoint with `curl` and the engine's User-Agent before
  adding it, and say in `sources.py` what you checked.
- **Summaries are quoted, never rewritten.** Feed text or the publisher's own preview text,
  cut at 250 characters. No abbreviating or paraphrasing; the link has the rest. (The spec said
  300; the user chose 250.)
- **items.json first, brief.md from it.** The brief is only a view of items.json; don't give
  it content the data doesn't have. `delivered` means "an earlier saved run carried this url",
  judged against every run in the retention window, not just the previous one, so an item
  that drops out for an hour and returns isn't announced as new again. The record is pruned
  by last-seen date for the same reason.
- **Schema is the contract.** Downstream agents read `items.json`, `status.json` and
  `history.jsonl`. Add keys if you need to, but don't rename or remove them.
- **Retention only deletes what the engine made**: directories named as run ids, `.tmp-*` dirs
  older than an hour, `logs/*.log`, `history.jsonl` lines, and `summary_cache.json` entries. The newest run is always kept.
  Cutoffs come from the run id in the directory name, not from mtime (logs excepted). Any new
  output type must either live inside a run dir or get its own cleanup rule plus a test.
- **Writes are atomic** (temp + rename), and `run`/`clean` hold `data/.lock`, so a dashboard or
  agent reading mid-run never sees half a file.
- **Health is computed, not inferred.** `health.alert` in `status.json` is the one flag monitors
  read. A source that fails or comes back empty `DEGRADED_AFTER` (3) runs in a row raises it.
  A new source where zero items is normal sets `empty_ok=True`. Collector GETs retry once on
  timeouts and 5xx, never on 4xx.
- Exit codes `0` / `1` / `75` are documented in `cli.py` and `deploy/README.md`. Schedulers and
  monitors depend on them, so keep both in step.
- **Be polite to the endpoints.** Reddit allows about one anonymous request per minute, which is
  why all subreddits come from one request. Don't add a second Reddit collector. Add the
  subreddit to the existing one.

## When a source breaks

`status.json` names it in `health.reasons` (or in `failed_sources` / `empty_sources`).

1. Probe it once with `curl -s -o /dev/null -w '%{http_code}' -A "<USER_AGENT from engine.py>" <url>`.
   Once, not in a loop: Reddit's quota is about one request a minute.
2. **404 / moved:** look for the new feed URL (the site's `<link rel="alternate">`, `/feed`,
   `/rss.xml`). Update `sources.py` and add a line to its docstring's deviation list.
3. **403 / 429 every run:** the host is blocked. Remove the source, or make it less frequent
   if that's built by then (not yet: one request an hour hasn't needed it).
4. **200 but empty, or a parse error:** the format changed. Save the real response as a
   fixture in `tests/fixtures/`, write the failing test first, then fix the parser.
5. **Gone for good** (like Anthropic's feed): remove it and note why in the docstring.

## Code style

- Comments explain *why* (a rate limit, a broken spec endpoint, a fallback), not what the next
  line does. Each module starts with a docstring saying what it's for.
- Parsing is a pure function (`parse_*`) that `fetch` calls, so it can be tested on fixtures.
  A new collector comes with a fixture and a test.
- Python via uv (`uv add`, `uv run`). `uv.lock` is committed. Runtime deps are `httpx` and
  `defusedxml` only (defusedxml blocks entity-expansion attacks in feeds). Ask before adding more.

## Git

- Commit subjects use a lowercase type and a colon, then a short plain summary: `add:` (new file,
  source, feature, test), `update:` (change to existing behaviour, config, docs), `fix:` (bug fix),
  or the same `word:` form otherwise (`remove:`, `refactor:`). Examples: `add: lobsters ai feed`,
  `update: retention default stays 4 days for cron too`, `fix: reddit 429 from parallel requests`.
- Keep the subject short with no trailing period. The body, after a blank line, says what changed,
  why, and what was verified (tests, a live run's `sources_ok`).
- Commit only when asked. Leave existing commits as they are.

## Git-ignored

`data/` (all scraped output), `deploy/out/`, `.venv/`, caches.
