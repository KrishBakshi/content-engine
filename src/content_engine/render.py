"""Markdown brief for prompt injection."""

from __future__ import annotations

from datetime import datetime

from content_engine.engine import RunResult


def to_markdown(result: RunResult, generated_at: datetime) -> str:
    lines = [f"# Daily Tech & AI Intelligence Brief ({generated_at.strftime('%Y-%m-%d %H:%M UTC')})", ""]
    for category, items in result.by_category().items():
        if not items:
            continue
        lines += [f"## {category.replace('_', ' ').title()}", ""]
        for item in items:
            lines.append(f"- **[{item.title}]({item.url})** (`{item.source}` | {item.metric})")
            if item.summary:
                lines.append(f"  > {item.summary}")
        lines.append("")
    failed = [s.name for s in result.sources if not s.ok]
    if failed:
        lines += [f"_Sources unavailable this run: {', '.join(failed)}_", ""]
    return "\n".join(lines)
