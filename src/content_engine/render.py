"""Markdown brief, written from the same item dicts that are saved as items.json.

The brief is a view of items.json and nothing else: storage writes items.json first, then
calls `to_markdown` on exactly that list. New items come first; items an earlier run
already carried follow under "Already delivered" (or are left out with `new_only`).
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
            lines.append(f"- **[{item['title']}]({item['url']})** (`{item['source']}` | {item['metric']})")
            if item["summary"]:
                lines.append(f"  > {item['summary']}")
        lines.append("")
    return lines


def to_markdown(items: list[dict], generated_at: datetime, failed_sources: Sequence[str] = (),
                new_only: bool = False) -> str:
    new = [i for i in items if not i.get("delivered")]
    delivered = [i for i in items if i.get("delivered")]
    lines = [f"# Daily Tech & AI Intelligence Brief ({generated_at.strftime('%Y-%m-%d %H:%M UTC')})", "",
             f"_{len(new)} new · {len(delivered)} already delivered_", ""]
    lines += ["## New", ""]
    lines += _sections(new) if new else ["_Nothing new since the last brief._", ""]
    if delivered and not new_only:
        lines += ["## Already delivered", ""] + _sections(delivered)
    if failed_sources:
        lines += [f"_Sources unavailable this run: {', '.join(failed_sources)}_", ""]
    return "\n".join(lines)
