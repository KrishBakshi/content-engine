"""On-disk layout, atomic writes, the run lock, and retention cleanup.

    <data_dir>/
      runs/<YYYYmmddTHHMMSSZ>/   one directory per run: items.json, brief.md, report.json
      latest/                    copy of the newest run's items.json and brief.md
      status.json                the last run's outcome, for dashboards and health checks
      history.jsonl              one line per run (state, counts), pruned to the retention window
      summary_cache.json         url -> summary taken from the linked page (enrich.py), pruned the same way
      delivered.json             url -> first run that carried it and when it was last seen; this is
                                 what marks an item `delivered` in later runs
      logs/                      cron output (systemd logs go to the journal instead)
      .lock                      held while a run or cleanup is in progress

Cleanup only touches what this module creates: run directories whose name parses as a
run id, stale `.tmp-*` directories, `*.log` files in logs/, history lines, summary-cache
entries, and delivered-record entries. Anything else placed under the data dir is left alone.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from content_engine.engine import RunResult
from content_engine.render import to_markdown

log = logging.getLogger(__name__)

RUN_ID_FORMAT = "%Y%m%dT%H%M%SZ"
TMP_PREFIX = ".tmp-"
# A temp dir older than this belongs to a run that crashed; a live run finishes in seconds.
STALE_TMP = timedelta(hours=1)
# Consecutive failed-or-empty runs before a source raises the health alert. One bad run is
# normal (a blip, a 429 after a manual run); three in a row on an hourly schedule is not.
DEGRADED_AFTER = 3


class LockBusy(RuntimeError):
    pass


@dataclass
class CleanupReport:
    removed_runs: int = 0
    removed_tmp: int = 0
    removed_logs: int = 0
    kept_runs: int = 0
    pruned_history_lines: int = 0
    pruned_cache_entries: int = 0
    pruned_delivered_entries: int = 0


def run_id(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime(RUN_ID_FORMAT)


def parse_run_id(name: str) -> datetime | None:
    try:
        return datetime.strptime(name, RUN_ID_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@contextmanager
def lock(data_dir: Path) -> Iterator[None]:
    """Exclusive, non-blocking: a cron tick that overlaps a slow run exits instead of piling up."""
    data_dir.mkdir(parents=True, exist_ok=True)
    with open(data_dir / ".lock", "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockBusy(f"another content-engine process holds {data_dir / '.lock'}") from None
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f"{TMP_PREFIX}{path.name}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _dump(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


CACHE_FILE = "summary_cache.json"


def load_summary_cache(data_dir: Path) -> dict[str, dict]:
    """A missing or corrupt cache is an empty one: it only saves refetching pages."""
    try:
        cache = json.loads((data_dir / CACHE_FILE).read_text(encoding="utf-8"))
        return cache if isinstance(cache, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_summary_cache(data_dir: Path, cache: dict[str, dict]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    _write_atomic(data_dir / CACHE_FILE, json.dumps(cache, ensure_ascii=False, sort_keys=True) + "\n")


def _parse_stamp(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


DELIVERED_FILE = "delivered.json"


def load_delivered(data_dir: Path) -> dict[str, dict]:
    """url -> {"first_run", "last_seen"} for every item an earlier run carried.

    If the record is missing (first run after an upgrade, or it was deleted) it is rebuilt
    from the runs still on disk, so existing history counts as delivered."""
    path = data_dir / DELIVERED_FILE
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(record, dict):
            return record
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError):
        log.warning("%s is unreadable; rebuilding it from saved runs", path)
    record = {}
    runs = data_dir / "runs"
    for run in sorted(p for p in runs.iterdir() if parse_run_id(p.name)) if runs.is_dir() else []:
        try:
            items = json.loads((run / "items.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        stamp = parse_run_id(run.name).strftime("%Y-%m-%dT%H:%M:%SZ")
        for item in items:
            record.setdefault(item["url"], {"first_run": run.name})["last_seen"] = stamp
    return record


def save_run(data_dir: Path, result: RunResult, started: datetime, duration_s: float,
             cleanup: CleanupReport | None = None, new_only: bool = False) -> Path:
    """Write the run directory, then refresh latest/, status.json and history.jsonl.

    The files are made in a chain: items are marked `delivered` against the record of
    earlier runs and written as items.json; brief.md is then rendered from that same list;
    report.json counts, per source, how many items were new and how many already delivered."""
    rid = run_id(started)
    stamp = started.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    runs = data_dir / "runs"
    runs.mkdir(parents=True, exist_ok=True)

    record = load_delivered(data_dir)
    payload = [{**i.to_dict(), "delivered": i.url in record} for i in result.items]
    items_json = _dump(payload)
    brief = to_markdown(payload, started, [s.name for s in result.sources if not s.ok], new_only=new_only)

    sources = []
    for s in result.sources:
        entry = {k: v for k, v in asdict(s).items() if k != "urls"}
        entry["already_delivered"] = sum(url in record for url in s.urls)
        entry["new"] = len(s.urls) - entry["already_delivered"]
        sources.append(entry)
    already = sum(item["delivered"] for item in payload)
    report = {
        "run_id": rid,
        "started_at": stamp,
        "duration_s": round(duration_s, 2),
        "state": result.state,
        "items": len(payload),
        "new_items": len(payload) - already,
        "already_delivered": already,
        "sources_ok": sum(s.ok for s in result.sources),
        "sources_total": len(result.sources),
        "failed_sources": [s.name for s in result.sources if not s.ok],
        # Answered but gave nothing: the quiet way a source breaks (changed format, moved feed).
        "empty_sources": [s.name for s in result.sources if s.ok and s.items == 0 and not s.empty_ok],
        "sources": sources,
    }
    if result.enrichment is not None:
        report["enrichment"] = result.enrichment.to_dict()

    # Build in a temp dir and rename, so a reader never sees a half-written run.
    tmp = runs / f"{TMP_PREFIX}{rid}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    (tmp / "items.json").write_text(items_json, encoding="utf-8")
    (tmp / "brief.md").write_text(brief, encoding="utf-8")
    (tmp / "report.json").write_text(_dump(report), encoding="utf-8")
    final = runs / rid
    if final.exists():  # two runs in the same second: the later one wins
        shutil.rmtree(final)
    os.rename(tmp, final)

    latest = data_dir / "latest"
    latest.mkdir(exist_ok=True)
    _write_atomic(latest / "items.json", items_json)
    _write_atomic(latest / "brief.md", brief)

    # Only now, with the run safely on disk, do its items count as delivered.
    for item in payload:
        record.setdefault(item["url"], {"first_run": rid})["last_seen"] = stamp
    _write_atomic(data_dir / DELIVERED_FILE, json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    keys = ("run_id", "started_at", "duration_s", "state", "items", "new_items", "already_delivered",
            "sources_ok", "sources_total", "failed_sources", "empty_sources")
    line = {k: report[k] for k in keys}
    with open(data_dir / "history.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")

    status = {**report, "run_dir": str(final), "health": health(data_dir, report["state"])}
    if cleanup is not None:
        status["cleanup"] = asdict(cleanup)
    _write_atomic(data_dir / "status.json", _dump(status))
    return final


def _read_history(data_dir: Path, last: int = 200) -> list[dict]:
    path = data_dir / "history.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines()[-last:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def health(data_dir: Path, state: str) -> dict:
    """One flag a dashboard can alert on, plus the reasons. Streaks are counted back from the
    newest history line. It can't detect "the job stopped running": nothing writes then, so
    the monitor has to check the age of `started_at` itself."""
    history = _read_history(data_dir)
    degraded = []
    if history:
        latest = history[-1]
        for kind in ("failed", "empty"):
            for name in latest.get(f"{kind}_sources", []):
                streak = 0
                for entry in reversed(history):
                    if name not in entry.get(f"{kind}_sources", []):
                        break
                    streak += 1
                if streak >= DEGRADED_AFTER:
                    degraded.append({"name": name, "kind": kind, "streak": streak})
    reasons = []
    if state == "failed":
        reasons.append("every source failed this run")
    reasons += [f"{d['name']} {d['kind']} {d['streak']} runs in a row" for d in degraded]
    return {"alert": bool(reasons), "reasons": reasons, "degraded_sources": degraded,
            "degraded_after": DEGRADED_AFTER}


def cleanup(data_dir: Path, retention_days: float, now: datetime | None = None, dry_run: bool = False) -> CleanupReport:
    """Delete runs, logs and history lines older than `retention_days`. The newest run is always kept,
    so latest/ stays backed by a run even after a long outage or a clock jump."""
    if retention_days <= 0:
        raise ValueError("retention_days must be positive")
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention_days)
    report = CleanupReport()
    act = "would remove" if dry_run else "removed"

    runs_dir = data_dir / "runs"
    if runs_dir.is_dir():
        dated = sorted(((parse_run_id(p.name), p) for p in runs_dir.iterdir() if p.is_dir()),
                       key=lambda t: t[0] or datetime.min.replace(tzinfo=timezone.utc))
        dated = [(ts, p) for ts, p in dated if ts is not None]
        newest = dated[-1][1] if dated else None
        for ts, path in dated:
            if ts < cutoff and path != newest:
                log.info("%s run %s", act, path.name)
                if not dry_run:
                    shutil.rmtree(path)
                report.removed_runs += 1
            else:
                report.kept_runs += 1
        for path in runs_dir.glob(f"{TMP_PREFIX}*"):
            age = now - datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if age > STALE_TMP:
                log.info("%s stale temp dir %s", act, path.name)
                if not dry_run:
                    shutil.rmtree(path, ignore_errors=True)
                report.removed_tmp += 1

    logs_dir = data_dir / "logs"
    if logs_dir.is_dir():
        for path in logs_dir.glob("*.log"):
            if datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) < cutoff:
                log.info("%s log %s", act, path.name)
                if not dry_run:
                    path.unlink()
                report.removed_logs += 1

    history = data_dir / "history.jsonl"
    if history.is_file():
        kept = []
        for line in history.read_text(encoding="utf-8").splitlines():
            try:
                ts = parse_run_id(json.loads(line).get("run_id", ""))
            except (json.JSONDecodeError, AttributeError):
                ts = None
            if ts is not None and ts < cutoff:
                report.pruned_history_lines += 1
            else:
                kept.append(line)
        if report.pruned_history_lines and not dry_run:
            _write_atomic(history, "".join(f"{line}\n" for line in kept))

    if (data_dir / CACHE_FILE).is_file():
        cache = load_summary_cache(data_dir)
        # Entries are keyed by url and stamped when fetched; an unreadable stamp counts as old.
        fresh = {}
        for url, entry in cache.items():
            at = _parse_stamp(entry.get("at", "")) if isinstance(entry, dict) else None
            if at is not None and at >= cutoff:
                fresh[url] = entry
        report.pruned_cache_entries = len(cache) - len(fresh)
        if report.pruned_cache_entries and not dry_run:
            save_summary_cache(data_dir, fresh)

    if (data_dir / DELIVERED_FILE).is_file():
        record = load_delivered(data_dir)
        # Pruned by when a url was last seen, not first seen: a post that is still in its feed
        # is refreshed every run and so can never expire and come back marked as new.
        kept = {}
        for url, entry in record.items():
            seen = _parse_stamp(entry.get("last_seen", "")) if isinstance(entry, dict) else None
            if seen is not None and seen >= cutoff:
                kept[url] = entry
        report.pruned_delivered_entries = len(record) - len(kept)
        if report.pruned_delivered_entries and not dry_run:
            _write_atomic(data_dir / DELIVERED_FILE, json.dumps(kept, ensure_ascii=False, sort_keys=True) + "\n")

    return report
