"""Markdown brief, written from the same item dicts that are saved as items.json.

The brief is a view of items.json and nothing else: storage writes items.json first, then
calls `to_markdown` on exactly that list. Trending topics lead (worked out from the same
list, see trending.py), then new items, then items an earlier run already carried under
"Already delivered" (or left out with `new_only`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from content_engine.schema import CATEGORIES, CATEGORY_LABELS


def _sections(items: list[dict]) -> list[str]:
    grouped: dict[str, list[dict]] = {c: [] for c in CATEGORIES}
    for item in items:
        grouped.setdefault(item["category"], []).append(item)
    lines: list[str] = []
    for category, group in grouped.items():
        if not group:
            continue
        lines += [f"### {CATEGORY_LABELS.get(category) or category.replace('_', ' ').title()}", ""]
        for item in group:
            lines += _bullet(item)
        lines.append("")
    return lines


def _bullet(item: dict, summary: bool = True) -> list[str]:
    lines = [f"- **[{item['title']}]({item['url']})** (`{item['source']}` | {item['metric']})"]
    for other in item.get("also_in", []):
        lines[0] += f" · also on `{other['source']}` ({other['metric']})"
    if summary and item["summary"]:
        lines.append(f"  > {item['summary']}")
    return lines


TOPIC_ITEMS = 3  # shown in full per topic; the rest are listed by title


def _trending(trending: dict, items: list[dict]) -> list[str]:
    by_url = {item["url"]: item for item in items}
    lines: list[str] = []
    if trending.get("topics"):
        lines += ["## Trending topics", ""]
        for number, topic in enumerate(trending["topics"], 1):
            found = [by_url[u] for u in topic["urls"] if u in by_url]
            meta = f"{topic['mentions']} mentions across {len(topic['sources'])} sources: {', '.join(topic['sources'])}"
            if topic["points"]:
                meta += f" · {topic['points']} pts, {topic['comments']} comments"
            lines += [f"### {number}. {topic['label']}", "", meta, ""]
            for item in found[:TOPIC_ITEMS]:
                lines += _bullet(item)
            if len(found) > TOPIC_ITEMS:
                rest = " · ".join(f"[{item['title']}]({item['url']})" for item in found[TOPIC_ITEMS:])
                lines.append(f"- _{len(found) - TOPIC_ITEMS} more:_ {rest}")
            lines.append("")
    discussed = [by_url[u] for u in trending.get("most_discussed", []) if u in by_url]
    if discussed:
        lines += ["## Most discussed", ""]
        for item in discussed:
            lines += _bullet(item, summary=False)
        lines.append("")
    return lines


def to_markdown(items: list[dict], generated_at: datetime, failed_sources: Sequence[str] = (),
                new_only: bool = False, trending: dict | None = None) -> str:
    """`trending` is the dict from trending.find_trending (also saved as trending.json); its
    section goes above everything else, so the most talked-about news is read first."""
    new = [i for i in items if not i.get("delivered")]
    delivered = [i for i in items if i.get("delivered")]
    lines = [f"# Daily Tech & AI Intelligence Brief ({generated_at.strftime('%Y-%m-%d %H:%M UTC')})", "",
             f"_{len(new)} new · {len(delivered)} already delivered_", ""]
    if trending:
        lines += _trending(trending, items)
    lines += ["## New", ""]
    lines += _sections(new) if new else ["_Nothing new since the last brief._", ""]
    if delivered and not new_only:
        lines += ["## Already delivered", ""] + _sections(delivered)
    if failed_sources:
        lines += [f"_Sources unavailable this run: {', '.join(failed_sources)}_", ""]
    return "\n".join(lines)
