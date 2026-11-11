"""The contract every source implements."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

import httpx

from content_engine.schema import Item

DEFAULT_TIMEOUT = 15.0
RETRY_DELAY_S = 3.0


class BaseCollector(ABC):
    """One source. `name` must be unique across the registry; it keys the run report."""

    name: str
    category: str
    # Collectors sharing a host run one after another, spaced by engine.HOST_SPACING_S;
    # None means the collector runs in parallel with everything else.
    host: str | None = None
    # True when zero items is normal for this source (arXiv announces nothing on weekends),
    # so an empty result doesn't count towards the health alert.
    empty_ok: bool = False

    @abstractmethod
    async def fetch(self, client: httpx.AsyncClient) -> list[Item]:
        """Fetch and normalize. Raise on any failure; the engine records it and carries on."""

    async def get(self, client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
        """GET with one retry for transient trouble (timeout, connection error, 5xx).
        4xx is never retried: a 429 retried seconds later only digs the hole deeper, and a
        404 won't fix itself. raise_for_status makes either show up as a source error."""
        for attempt in (1, 2):
            try:
                res = await client.get(url, timeout=DEFAULT_TIMEOUT, **kwargs)
                if res.status_code < 500 or attempt == 2:
                    res.raise_for_status()
                    return res
            except httpx.TransportError:  # includes timeouts
                if attempt == 2:
                    raise
            await asyncio.sleep(RETRY_DELAY_S)
        raise AssertionError("unreachable")
