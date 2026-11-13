"""Runs every collector concurrently and gathers items plus a per-source report."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Callable

import httpx

from content_engine import enrich as enrich_mod
from content_engine.collectors.base import BaseCollector
from content_engine.schema import Item, canonical_url, source_family

log = logging.getLogger(__name__)

# Reddit throttles anonymous clients that send a generic or missing User-Agent.
USER_AGENT = "content-engine/0.1 (personal tech news aggregator)"
# Seconds between consecutive requests to one host (none needed so far: same-host collectors
# are only serialized). Reddit is the strict one; it is handled by one combined request.
HOST_SPACING_S: dict[str, float] = {}


@dataclass
class SourceReport:
    name: str
    category: str
    ok: bool
    items: int
    duration_ms: int
    error: str = ""
    empty_ok: bool = False
    # The urls this source contributed after de-duplication. Used to count new vs already
    # delivered per source; not written to the report.
    urls: list[str] = field(default_factory=list, repr=False)


@dataclass
class RunResult:
    items: list[Item] = field(default_factory=list)
    sources: list[SourceReport] = field(default_factory=list)
    enrichment: enrich_mod.EnrichStats | None = None

    @property
    def state(self) -> str:
        """ok = every source answered; partial = some failed; failed = none answered."""
        ok = sum(s.ok for s in self.sources)
        if ok == len(self.sources):
            return "ok"
        return "partial" if ok else "failed"


async def _timed(collector: BaseCollector, client: httpx.AsyncClient) -> tuple[list[Item] | BaseException, int]:
    start = time.monotonic()
    try:
        result: list[Item] | BaseException = await collector.fetch(client)
    except Exception as exc:  # one source's outage must not stop the run
        result = exc
    return result, int((time.monotonic() - start) * 1000)


async def _host_queue(group: list[BaseCollector], client: httpx.AsyncClient, spacing: float,
                      done: Callable[[BaseCollector], None]):
    outcomes = []
    for i, collector in enumerate(group):
        if i and spacing:
            await asyncio.sleep(spacing)
        outcomes.append(await _timed(collector, client))
        done(collector)
    return outcomes


# Called as on_progress(stage, done, total, detail). Stages: "fetch" (detail = the source that
# just finished) and "enrich" (weak summaries looked up so far).
Progress = Callable[[str, int, int, str], None]


async def run(collectors: list[BaseCollector], summary_cache: dict[str, dict] | None = None,
              on_progress: Progress | None = None) -> RunResult:
    """Different hosts run concurrently; collectors on one host run in sequence.
    With `summary_cache` (url -> entry, updated in place), weak summaries are then filled
    from the linked pages (enrich.py); without it, items are returned as the feeds gave them."""
    result = RunResult()
    finished = 0

    def source_done(collector: BaseCollector) -> None:
        nonlocal finished
        finished += 1
        if on_progress:
            on_progress("fetch", finished, len(collectors), collector.name)

    seen: dict[str, int] = {}          # canonical url -> index in result.items
    echoes: dict[str, list[dict]] = {}  # canonical url -> other sources that carried it
    groups: dict[object, list[BaseCollector]] = {}
    for c in collectors:
        groups.setdefault(c.host or id(c), []).append(c)

    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
        per_group = await asyncio.gather(*(
            _host_queue(g, client, HOST_SPACING_S.get(key, 0.0) if isinstance(key, str) else 0.0, source_done)
            for key, g in groups.items()
        ))
    by_collector = {id(c): o for g, outs in zip(groups.values(), per_group) for c, o in zip(g, outs)}
    # Report in registry order, not completion order, so output is stable run to run.
    outcomes = [by_collector[id(c)] for c in collectors]

    for collector, (outcome, ms) in zip(collectors, outcomes):
        if isinstance(outcome, BaseException):
            error = f"{type(outcome).__name__}: {outcome}"[:300]
            log.warning("source %s failed in %dms: %s", collector.name, ms, error)
            result.sources.append(SourceReport(collector.name, collector.category, False, 0, ms, error,
                                               collector.empty_ok))
            continue
        # A link another source already carried is not listed twice, but the repeat is kept as
        # a signal: being picked up elsewhere is what makes a story trend (trending.py).
        fresh = []
        for item in outcome:
            if not item.url:
                continue
            key = canonical_url(item.url)
            if key in seen:
                echoes.setdefault(key, []).append({"source": item.source, "metric": item.metric})
            else:
                seen[key] = len(result.items) + len(fresh)
                fresh.append(item)
        result.items.extend(fresh)
        log.info("source %s: %d items in %dms", collector.name, len(fresh), ms)
        result.sources.append(SourceReport(collector.name, collector.category, True, len(fresh), ms, "",
                                           collector.empty_ok, [i.url for i in fresh]))

    for key, others in echoes.items():
        item = result.items[seen[key]]
        # The same outlet under another name (two Hacker News queries) is not a second voice.
        others = [o for o in others if source_family(o["source"]) != source_family(item.source)]
        if others:
            result.items[seen[key]] = replace(item, also_in=tuple(others))

    if summary_cache is not None:
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
            result.items, result.enrichment = await enrich_mod.enrich(
                result.items, client, summary_cache,
                on_progress=(lambda d, t: on_progress("enrich", d, t, "")) if on_progress else None)
    return result
