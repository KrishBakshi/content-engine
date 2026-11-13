"""Run layout, the lock, and retention cleanup."""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from content_engine import storage
from content_engine.engine import RunResult, SourceReport
from content_engine.schema import Item

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def _result(ok=True):
    item = Item("1", "src", "research", "T", "https://x", "", "m", "")
    return RunResult(items=[item] if ok else [], sources=[SourceReport("src", "research", ok, int(ok), 5)])


def _save(data, at, ok=True):
    return storage.save_run(data, _result(ok), at, 1.0)


def test_save_run_layout(tmp_path):
    run_dir = _save(tmp_path, NOW)
    assert run_dir == tmp_path / "runs" / "20261110T120000Z"
    assert sorted(p.name for p in run_dir.iterdir()) == ["brief.md", "items.json", "report.json"]
    assert (tmp_path / "latest" / "brief.md").read_text() == (run_dir / "brief.md").read_text()
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["state"] == "ok" and status["items"] == 1 and status["sources_ok"] == 1
    assert not list(tmp_path.rglob(".tmp-*"))  # temp files were renamed into place


def test_history_appends_one_line_per_run(tmp_path):
    _save(tmp_path, NOW - timedelta(hours=1))
    _save(tmp_path, NOW, ok=False)
    lines = [json.loads(line) for line in (tmp_path / "history.jsonl").read_text().splitlines()]
    assert [entry["state"] for entry in lines] == ["ok", "failed"]
    assert lines[1]["failed_sources"] == ["src"]


def test_cleanup_removes_only_runs_past_retention(tmp_path):
    for days in (0, 3.9, 4.1, 10):
        _save(tmp_path, NOW - timedelta(days=days))
    (tmp_path / "runs" / "notes").mkdir()  # not a run id: never touched

    report = storage.cleanup(tmp_path, 4, now=NOW)

    assert (report.removed_runs, report.kept_runs) == (2, 2)
    assert sorted(p.name for p in (tmp_path / "runs").iterdir()) == ["20261106T142400Z", "20261110T120000Z", "notes"]
    assert report.pruned_history_lines == 2
    assert len((tmp_path / "history.jsonl").read_text().splitlines()) == 2


def test_cleanup_keeps_newest_run_even_when_old(tmp_path):
    _save(tmp_path, NOW - timedelta(days=30))
    _save(tmp_path, NOW - timedelta(days=20))
    report = storage.cleanup(tmp_path, 4, now=NOW)
    assert report.removed_runs == 1
    assert [p.name for p in (tmp_path / "runs").iterdir()] == ["20261021T120000Z"]


def test_cleanup_dry_run_removes_nothing(tmp_path):
    _save(tmp_path, NOW - timedelta(days=10))
    _save(tmp_path, NOW)
    report = storage.cleanup(tmp_path, 4, now=NOW, dry_run=True)
    assert report.removed_runs == 1
    assert len(list((tmp_path / "runs").iterdir())) == 2
    assert len((tmp_path / "history.jsonl").read_text().splitlines()) == 2


def test_cleanup_logs_and_stale_tmp(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    old, new, other = logs / "run-2026-11-01.log", logs / "run-2026-11-10.log", logs / "keep.txt"
    for p in (old, new, other):
        p.write_text("x")
    old_ts = (NOW - timedelta(days=9)).timestamp()
    os.utime(old, (old_ts, old_ts))
    os.utime(other, (old_ts, old_ts))
    (tmp_path / "runs").mkdir()
    stale, fresh = tmp_path / "runs" / ".tmp-a", tmp_path / "runs" / ".tmp-b"
    stale.mkdir()
    fresh.mkdir()
    os.utime(stale, (old_ts, old_ts))
    os.utime(fresh, (NOW.timestamp(), NOW.timestamp()))

    report = storage.cleanup(tmp_path, 4, now=NOW)

    assert (report.removed_logs, report.removed_tmp) == (1, 1)
    assert not old.exists() and new.exists() and other.exists()
    assert not stale.exists() and fresh.exists()


def test_cleanup_rejects_non_positive_retention(tmp_path):
    with pytest.raises(ValueError):
        storage.cleanup(tmp_path, 0)


def test_lock_is_exclusive(tmp_path):
    with storage.lock(tmp_path):
        with pytest.raises(storage.LockBusy):
            with storage.lock(tmp_path):
                pass
    with storage.lock(tmp_path):  # released after the first block
        pass


def test_summary_cache_round_trip_and_pruning(tmp_path):
    stamp = lambda d: (NOW - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
    storage.save_summary_cache(tmp_path, {
        "https://new": {"summary": "s", "at": stamp(1)},
        "https://old": {"summary": "s", "at": stamp(5)},
        "https://bad": {"summary": "s", "at": "garbage"},
    })
    report = storage.cleanup(tmp_path, 4, now=NOW)
    assert report.pruned_cache_entries == 2
    assert list(storage.load_summary_cache(tmp_path)) == ["https://new"]


def test_corrupt_summary_cache_reads_as_empty(tmp_path):
    (tmp_path / storage.CACHE_FILE).write_text("{not json")
    assert storage.load_summary_cache(tmp_path) == {}


def _report_result(name, ok, items, empty_ok=False):
    sources = [SourceReport(name, "research", ok, items, 5, "" if ok else "boom", empty_ok),
               SourceReport("steady", "research", True, 1, 5)]
    item = Item("1", "steady", "research", "T", "https://x", "", "m", "")
    return RunResult(items=[item], sources=sources)


def _status(data):
    return json.loads((data / "status.json").read_text())


def test_health_alerts_after_three_failed_runs_and_clears(tmp_path):
    for h in range(2):
        storage.save_run(tmp_path, _report_result("flaky", False, 0), NOW + timedelta(hours=h), 1)
    assert _status(tmp_path)["health"]["alert"] is False          # two in a row: not yet
    storage.save_run(tmp_path, _report_result("flaky", False, 0), NOW + timedelta(hours=2), 1)
    health = _status(tmp_path)["health"]
    assert health["alert"] is True
    assert health["degraded_sources"] == [{"name": "flaky", "kind": "failed", "streak": 3}]
    storage.save_run(tmp_path, _report_result("flaky", True, 4), NOW + timedelta(hours=3), 1)
    assert _status(tmp_path)["health"]["alert"] is False          # recovered


def test_empty_source_counts_unless_empty_is_normal(tmp_path):
    for h in range(3):
        storage.save_run(tmp_path, _report_result("moved_feed", True, 0), NOW + timedelta(hours=h), 1)
    status = _status(tmp_path)
    assert status["empty_sources"] == ["moved_feed"]
    assert status["health"]["degraded_sources"][0]["kind"] == "empty"

    other = tmp_path / "weekend"
    for h in range(3):
        storage.save_run(other, _report_result("arxiv", True, 0, empty_ok=True), NOW + timedelta(hours=h), 1)
    assert _status(other)["empty_sources"] == [] and _status(other)["health"]["alert"] is False


# --- delivered marking: items.json first, brief.md from it, counts in report.json ------------

def _run_with(urls, source="src"):
    items = [Item(u, source, "research", f"title {u}", f"https://x/{u}", "", "m", "") for u in urls]
    report = SourceReport(source, "research", True, len(items), 5, "", False, [i.url for i in items])
    return RunResult(items=items, sources=[report])


def _read(run_dir, name):
    return json.loads((run_dir / name).read_text())


def test_first_run_is_all_new_then_repeats_are_marked_delivered(tmp_path):
    first = storage.save_run(tmp_path, _run_with(["a", "b"]), NOW, 1)
    assert [i["delivered"] for i in _read(first, "items.json")] == [False, False]

    second = storage.save_run(tmp_path, _run_with(["a", "b", "c"]), NOW + timedelta(hours=1), 1)
    items = _read(second, "items.json")
    assert {i["id"]: i["delivered"] for i in items} == {"a": True, "b": True, "c": False}

    report = _read(second, "report.json")
    assert (report["items"], report["new_items"], report["already_delivered"]) == (3, 1, 2)
    (source,) = report["sources"]
    assert (source["items"], source["new"], source["already_delivered"]) == (3, 1, 2)
    assert "urls" not in source
    last_line = json.loads((tmp_path / "history.jsonl").read_text().splitlines()[-1])
    assert last_line["new_items"] == 1


def test_brief_is_a_view_of_items_json_with_new_items_first(tmp_path):
    storage.save_run(tmp_path, _run_with(["a"]), NOW, 1)
    run_dir = storage.save_run(tmp_path, _run_with(["a", "c"]), NOW + timedelta(hours=1), 1)
    brief = (run_dir / "brief.md").read_text()
    assert "_1 new · 1 already delivered_" in brief
    assert brief.index("## New") < brief.index("title c") < brief.index("## Already delivered") < brief.index("title a")
    for item in _read(run_dir, "items.json"):          # every item in the data is in the brief
        assert item["url"] in brief


def test_new_only_brief_leaves_delivered_items_out_but_items_json_keeps_them(tmp_path):
    storage.save_run(tmp_path, _run_with(["a"]), NOW, 1)
    run_dir = storage.save_run(tmp_path, _run_with(["a", "c"]), NOW + timedelta(hours=1), 1, new_only=True)
    brief = (run_dir / "brief.md").read_text()
    assert "title c" in brief and "title a" not in brief and "Already delivered" not in brief
    assert len(_read(run_dir, "items.json")) == 2


def test_item_that_drops_out_and_returns_is_still_delivered(tmp_path):
    storage.save_run(tmp_path, _run_with(["a"]), NOW, 1)
    storage.save_run(tmp_path, _run_with(["b"]), NOW + timedelta(hours=1), 1)          # "a" absent this run
    back = storage.save_run(tmp_path, _run_with(["a"]), NOW + timedelta(hours=2), 1)
    assert _read(back, "items.json")[0]["delivered"] is True


def test_delivered_record_is_rebuilt_from_saved_runs_when_missing(tmp_path):
    storage.save_run(tmp_path, _run_with(["a"]), NOW, 1)
    (tmp_path / storage.DELIVERED_FILE).unlink()
    run_dir = storage.save_run(tmp_path, _run_with(["a", "b"]), NOW + timedelta(hours=1), 1)
    assert {i["id"]: i["delivered"] for i in _read(run_dir, "items.json")} == {"a": True, "b": False}


def test_cleanup_prunes_delivered_record_by_last_seen(tmp_path):
    storage.save_run(tmp_path, _run_with(["old", "kept"]), NOW - timedelta(days=6), 1)
    storage.save_run(tmp_path, _run_with(["kept"]), NOW, 1)                              # "kept" refreshed today
    report = storage.cleanup(tmp_path, 4, now=NOW)
    assert report.pruned_delivered_entries == 1
    assert list(storage.load_delivered(tmp_path)) == ["https://x/kept"]
