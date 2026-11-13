"""The terminal spinner, and the progress events the engine feeds it."""

import asyncio
import io
import time

import httpx

from content_engine import engine, progress
from content_engine.collectors.base import BaseCollector
from content_engine.schema import Item


class FakeTerminal(io.StringIO):
    def isatty(self):
        return True


def test_spinner_is_only_for_real_terminals(monkeypatch):
    monkeypatch.delenv("TERM", raising=False)
    assert progress.use_spinner(FakeTerminal()) is True
    assert progress.use_spinner(io.StringIO()) is False          # a pipe, a file, the journal
    assert progress.use_spinner(FakeTerminal(), plain=True) is False
    monkeypatch.setenv("TERM", "dumb")
    assert progress.use_spinner(FakeTerminal()) is False


def test_spinner_draws_the_current_step_then_cleans_up(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(progress, "INTERVAL_S", 0.005)
    term = FakeTerminal()
    spinner = progress.Spinner(term)
    spinner.start("Starting")
    time.sleep(0.03)
    spinner.update("Fetching sources 3/37 · arxiv_cs_ai")
    time.sleep(0.03)
    spinner.stop()
    spinner.stop()  # a second stop (the finally block after a normal finish) is harmless
    drawn = term.getvalue()
    assert "Starting" in drawn and "Fetching sources 3/37 · arxiv_cs_ai" in drawn
    assert any(frame in drawn for frame in progress.FRAMES)
    assert drawn.startswith("\033[?25l")                           # cursor hidden while spinning
    assert drawn.endswith("\r\033[2K\033[?25h")                    # line erased, cursor given back


def test_result_line_marks_each_state(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    line = progress.result_line("ok", 37, 37, 163, 14, 6.84, "data/latest/brief.md", io.StringIO())
    assert line == "✔ 37/37 sources · 163 items (14 new) · 6.8s · brief: data/latest/brief.md"
    assert progress.result_line("partial", 36, 37, 150, 3, 5, "b", io.StringIO()).startswith("! 36/37")
    assert progress.result_line("failed", 0, 37, 0, 0, 5, "b", io.StringIO()).startswith("✖ 0/37")


def test_engine_reports_each_finished_source():
    class Fake(BaseCollector):
        category = "research"

        def __init__(self, name, fail=False):
            self.name, self.fail = name, fail

        async def fetch(self, client: httpx.AsyncClient):
            if self.fail:
                raise RuntimeError("down")
            return [Item(self.name, self.name, "research", "T", f"https://x/{self.name}", "s" * 60, "m", "")]

    events = []
    result = asyncio.run(engine.run([Fake("a"), Fake("b", fail=True), Fake("c")],
                                    on_progress=lambda *e: events.append(e)))
    assert [(stage, done, total) for stage, done, total, _ in events] == [
        ("fetch", 1, 3), ("fetch", 2, 3), ("fetch", 3, 3)]       # a failed source still counts as finished
    assert {name for *_, name in events} == {"a", "b", "c"}
    assert result.state == "partial"
