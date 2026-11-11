#!/usr/bin/env python3
"""Render the systemd and cron templates in deploy/templates/ for one machine.

    python3 deploy/render.py                               # defaults, into deploy/out/
    python3 deploy/render.py --install-dir /srv/ce --user ce --on-calendar '*-*-* 00/2:00:00' \\
        --cron '0 */2 * * *' --retention-days 4 --out /tmp/ce-units

Every placeholder `@@NAME@@` in a template must be filled, or rendering fails, so a
template edit can't ship a unit with a literal `@@...@@` in it. Standard library only:
this runs on the target host before anything is installed.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path, PurePosixPath

TEMPLATES = Path(__file__).resolve().parent / "templates"
PLACEHOLDER = re.compile(r"@@([A-Z_]+)@@")

DEFAULTS = {
    "install_dir": "/opt/content-engine",
    "data_dir": "/var/lib/content-engine",
    "user": "content-engine",
    "retention_days": "4",
    "on_calendar": "hourly",
    "cron": "0 * * * *",
}


def values(install_dir: str, data_dir: str, user: str, retention_days: str, on_calendar: str,
           cron: str, bin_path: str | None = None) -> dict[str, str]:
    data = PurePosixPath(data_dir)
    if data.parent == PurePosixPath("/var/lib"):
        # systemd creates /var/lib/<name> owned by User= on start; no manual mkdir/chown.
        state_block = (f"# systemd creates {data_dir} owned by {user} on first start.\n"
                       f"StateDirectory={data.name}")
    else:
        state_block = f"# {data_dir} is outside /var/lib: create it and chown it to {user} before the first start."
    return {
        "INSTALL_DIR": install_dir,
        "DATA_DIR": data_dir,
        "RUN_USER": user,
        "RETENTION_DAYS": retention_days,
        "ON_CALENDAR": on_calendar,
        "CRON_SCHEDULE": cron,
        "BIN": bin_path or f"{install_dir}/.venv/bin/content-engine",
        "STATE_DIRECTORY_BLOCK": state_block,
    }


def render(text: str, vals: dict[str, str]) -> str:
    missing = sorted(set(PLACEHOLDER.findall(text)) - set(vals))
    if missing:
        raise KeyError(f"template uses unknown placeholders: {', '.join(missing)}")
    return PLACEHOLDER.sub(lambda m: vals[m.group(1)], text)


def render_all(vals: dict[str, str]) -> dict[str, str]:
    """Output filename -> rendered text, for every *.template."""
    return {p.name.removesuffix(".template"): render(p.read_text(), vals) for p in sorted(TEMPLATES.glob("*.template"))}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--install-dir", default=DEFAULTS["install_dir"], help="repo checkout on the host")
    ap.add_argument("--data-dir", default=DEFAULTS["data_dir"], help="runs, latest/, status.json, logs/")
    ap.add_argument("--user", default=DEFAULTS["user"], help="unprivileged user the job runs as")
    ap.add_argument("--retention-days", default=DEFAULTS["retention_days"], help="delete data older than this")
    ap.add_argument("--on-calendar", default=DEFAULTS["on_calendar"], help="systemd OnCalendar= expression")
    ap.add_argument("--cron", default=DEFAULTS["cron"], help="five-field cron schedule")
    ap.add_argument("--bin", default=None, help="content-engine executable (default <install-dir>/.venv/bin/content-engine)")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "out", help="output directory")
    args = ap.parse_args(argv)

    vals = values(args.install_dir, args.data_dir, args.user, args.retention_days, args.on_calendar,
                  args.cron, args.bin)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, text in render_all(vals).items():
        (args.out / name).write_text(text)
        print(args.out / name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
