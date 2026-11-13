"""`content-engine run | clean | sources`.

Exit codes (what systemd, cron wrappers and dashboards key on):
  0   run finished; every source answered, or some did (state "partial" in status.json)
  1   every source failed, or an unexpected error
  75  another content-engine process holds the lock (EX_TEMPFAIL; safe to retry next tick)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from content_engine import engine, progress, storage
from content_engine.render import to_markdown
from content_engine.sources import default_collectors

EXIT_LOCKED = 75
DEFAULT_RETENTION_DAYS = 4.0

log = logging.getLogger("content_engine")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def _progress_text(stage: str, done: int, total: int, detail: str) -> str:
    if stage == "fetch":
        return f"Fetching sources {done}/{total} · {detail}"
    return f"Reading linked pages for missing summaries {done}/{total}"


def _cmd_run(args: argparse.Namespace) -> int:
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    spinner: progress.Spinner | None = getattr(args, "spinner", None)
    say = spinner.update if spinner else (lambda text: None)
    on_progress = (lambda *event: say(_progress_text(*event))) if spinner else None

    collectors = default_collectors()
    say(f"Fetching sources 0/{len(collectors)}")
    cache = None if args.no_enrich else storage.load_summary_cache(args.data_dir)
    result = asyncio.run(engine.run(collectors, summary_cache=cache, on_progress=on_progress))
    brief = to_markdown(result, started)

    with storage.lock(args.data_dir):
        if cache is not None:
            storage.save_summary_cache(args.data_dir, cache)
        say(f"Deleting data older than {args.retention_days:g} days")
        report = None if args.no_clean else storage.cleanup(args.data_dir, args.retention_days)
        say("Saving the run")
        run_dir = storage.save_run(args.data_dir, result, brief, started, time.monotonic() - t0, report)

    ok = sum(s.ok for s in result.sources)
    log.info("run %s: state=%s items=%d sources=%d/%d dir=%s", run_dir.name, result.state,
             len(result.items), ok, len(result.sources), run_dir)
    if report is not None:
        log.info("cleanup: %s", asdict(report))
    if spinner:
        # Interactive: spinner off, then any warnings held back during the run, then the result.
        args.finish_interactive()
        print(progress.result_line(result.state, ok, len(result.sources), len(result.items),
                                   time.monotonic() - t0, str(args.data_dir / "latest" / "brief.md")),
              file=sys.stderr)
    if args.print:
        sys.stdout.write(brief)
    return 1 if result.state == "failed" else 0


def _cmd_clean(args: argparse.Namespace) -> int:
    with storage.lock(args.data_dir):
        report = storage.cleanup(args.data_dir, args.retention_days, dry_run=args.dry_run)
    print(json.dumps(asdict(report)))
    return 0


def _cmd_sources(_: argparse.Namespace) -> int:
    for c in default_collectors():
        print(f"{c.category:<18} {c.name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", type=Path,
                        default=Path(os.environ.get("CONTENT_ENGINE_DATA_DIR", "data")),
                        help="output root (env CONTENT_ENGINE_DATA_DIR, default ./data)")
    common.add_argument("--retention-days", type=float,
                        default=_env_float("CONTENT_ENGINE_RETENTION_DAYS", DEFAULT_RETENTION_DAYS),
                        help="delete runs and logs older than this (env CONTENT_ENGINE_RETENTION_DAYS, default 4)")
    common.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    parser = argparse.ArgumentParser(prog="content-engine", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", parents=[common], help="fetch all sources, save the run, then clean old data")
    p_run.add_argument("--no-clean", action="store_true", help="skip the retention cleanup after the run")
    p_run.add_argument("--no-enrich", action="store_true",
                       help="keep feed summaries as they are; don't fetch linked pages for weak ones")
    p_run.add_argument("--plain", action="store_true",
                       help="no spinner; print log lines as they happen (automatic when not in a terminal)")
    p_run.add_argument("--print", action="store_true", help="also write the markdown brief to stdout")
    p_run.set_defaults(func=_cmd_run)

    p_clean = sub.add_parser("clean", parents=[common], help="only apply retention cleanup")
    p_clean.add_argument("--dry-run", action="store_true", help="report what would be removed, remove nothing")
    p_clean.set_defaults(func=_cmd_clean)

    p_src = sub.add_parser("sources", help="list registered sources")
    p_src.set_defaults(func=_cmd_sources)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Logs go to stderr: the journal under systemd, the dated log file under cron.
    handler: logging.Handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    finish = lambda: None
    if args.command == "run" and progress.use_spinner(sys.stderr, args.plain):
        # In a terminal: show a spinner with the current step, and hold the log lines back
        # until it is gone (a log line printed mid-spin would tear the spinner's line).
        # Only warnings and errors are shown afterwards; the per-source INFO lines are noise
        # next to the one-line result. -v shows everything.
        held = logging.handlers.MemoryHandler(capacity=100_000, flushLevel=logging.CRITICAL + 1, target=handler)
        if not args.verbose:
            # The level goes on the buffer, not its target: flush() hands held records to the
            # target directly, which skips the target's own level check.
            held.setLevel(logging.WARNING)
        args.spinner = progress.Spinner(sys.stderr)

        def finish() -> None:
            args.spinner.stop()
            held.flush()

        args.finish_interactive = finish
        handler = held
        args.spinner.start("Starting")
    logging.basicConfig(level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
                        handlers=[handler])
    # -v is for the engine's own detail, not the HTTP libraries' connection chatter.
    for noisy in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        return args.func(args)
    except storage.LockBusy as exc:
        log.warning("%s; skipping this run", exc)
        return EXIT_LOCKED
    except Exception:
        log.exception("content-engine failed")
        return 1
    finally:
        finish()  # also on Ctrl-C or a crash: give the cursor back and show what was logged


if __name__ == "__main__":
    sys.exit(main())
