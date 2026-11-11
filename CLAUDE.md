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
  `collectors/reddit.py`: one multireddit request split per subreddit.
- `sources.py`: the registry, and the only file that changes when adding or removing a feed. Its
  docstring lists where the registry differs from the spec and why. Keep that list current.
- `engine.py`: runs collectors concurrently (same-`host` collectors in sequence), records a
  `SourceReport` per source, and dedupes by URL. A failing source never fails the run.
- `enrich.py`: fills weak summaries (empty, a label under 40 chars, only links) from the
  linked page: meta description, else the first article paragraph, and only text that shares
  a word with the title (filters site-wide blurbs). Skips reddit/x hosts, refuses private IPs
  on every redirect hop, reads at most 1 MB per page. Results are cached in
  `summary_cache.json`; a miss is retried after 6 hours.
- `render.py`: markdown brief. `storage.py`: data layout, atomic writes, lock, retention.
  `cli.py`: `run` / `clean` / `sources` and the exit codes.
- `deploy/`: `templates/*.template` are the source of truth. `systemd/` and `cron/` hold them
  rendered with defaults, and `tests/test_deploy.py` checks they match. Edit the template,
  re-render, then copy the file over the committed one. `deploy/README.md` is the operator and
  agent guide.
- `tests/`: parsers run on `tests/fixtures/` files; storage tests use `tmp_path` and a fixed `NOW`.

## Invariants

- **No keys, no paid APIs, no browsers, no LLM calls.** A source that needs auth or JavaScript
  rendering doesn't go in. Probe a new endpoint with `curl` and the engine's User-Agent before
  adding it, and say in `sources.py` what you checked.
- **Summaries are quoted, never rewritten.** Feed text or the publisher's own preview text,
  cut at 250 characters. No abbreviating or paraphrasing; the link has the rest. (The spec said
  300; the user chose 250.)
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
  why both subreddits come from one request. Don't add a second Reddit collector. Add the
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
