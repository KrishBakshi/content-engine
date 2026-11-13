"""Terminal spinner for interactive runs: one line showing what the engine is doing right now.

Only used when stderr is a terminal. Under systemd or cron there is no spinner and log
lines stream as they happen, because a journal full of carriage returns is unreadable.
Standard library only.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
from typing import TextIO

FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
INTERVAL_S = 0.08
_CLEAR_LINE = "\r\033[2K"
_HIDE_CURSOR, _SHOW_CURSOR = "\033[?25l", "\033[?25h"


def use_spinner(stream: TextIO, plain: bool = False) -> bool:
    """A spinner needs a real terminal. `TERM=dumb` and --plain turn it off."""
    return not plain and stream.isatty() and os.environ.get("TERM") != "dumb"


def _paint(text: str, code: str, stream: TextIO) -> str:
    # https://no-color.org: any non-empty NO_COLOR means no colour.
    return text if os.environ.get("NO_COLOR") or not stream.isatty() else f"\033[{code}m{text}\033[0m"


class Spinner:
    """`start()`, any number of `update(text)`, then `stop()`. The frame is redrawn by a
    background thread, so the spinner keeps moving while the main thread waits on the network."""

    def __init__(self, stream: TextIO = sys.stderr) -> None:
        self.stream = stream
        self._text = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, text: str = "") -> None:
        self._text = text
        self.stream.write(_HIDE_CURSOR)
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def update(self, text: str) -> None:
        self._text = text  # a single attribute write: safe to call from any thread

    def _spin(self) -> None:
        i = 0
        while not self._stop.is_set():
            # Cut to the terminal's width: a wrapped line can't be cleared with one "\r".
            width = shutil.get_terminal_size((80, 24)).columns
            text = self._text[: max(10, width - 3)]
            self.stream.write(f"{_CLEAR_LINE}{_paint(FRAMES[i % len(FRAMES)], '36', self.stream)} {text}")
            self.stream.flush()
            i += 1
            self._stop.wait(INTERVAL_S)

    def stop(self) -> None:
        """Erase the spinner line and give the cursor back. Safe to call twice."""
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join()
        self._thread = None
        self.stream.write(_CLEAR_LINE + _SHOW_CURSOR)
        self.stream.flush()


def result_line(state: str, sources_ok: int, sources_total: int, items: int, seconds: float,
                brief_path: str, stream: TextIO = sys.stderr) -> str:
    mark, code = {"ok": ("✔", "32"), "partial": ("!", "33")}.get(state, ("✖", "31"))
    return (f"{_paint(mark, code, stream)} {sources_ok}/{sources_total} sources · {items} items · "
            f"{seconds:.1f}s · brief: {brief_path}")
