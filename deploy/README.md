# Deploying content-engine on Linux

Each run fetches every source once, saves the result, and deletes data older than the
retention window (4 days by default). The process exits when it's done. A scheduler starts
it again: a **systemd timer** (recommended) or **cron**. Both run the same command:

```sh
<install-dir>/.venv/bin/content-engine run
```

| File | What it is |
|---|---|
| `templates/*.template` | The source of truth, with `@@PLACEHOLDER@@`s |
| `render.py` | Fills the placeholders for one host (standard library only) |
| `systemd/content-engine.{service,timer}` | Rendered with the defaults below, ready to copy |
| `cron/content-engine.cron` | Rendered with the defaults, for `/etc/cron.d/` |

Defaults: install dir `/opt/content-engine`, data dir `/var/lib/content-engine`, user
`content-engine`, hourly, 4-day retention. If those fit your host, use the files in
`systemd/` or `cron/` as they are. Otherwise render your own:

```sh
python3 deploy/render.py --install-dir /srv/content-engine --data-dir /srv/content-engine-data \
    --user ce --on-calendar '*-*-* 00/2:00:00' --cron '0 */2 * * *' --retention-days 4 \
    --out deploy/out          # deploy/out/ is git-ignored
```

Edit the templates, not the rendered files. `tests/test_deploy.py` fails if the committed
files no longer match the templates.

## 1. Install (common to both modes)

```sh
sudo useradd --system --home-dir /var/lib/content-engine --shell /usr/sbin/nologin content-engine
sudo git clone <repo-url> /opt/content-engine        # or copy the checkout
cd /opt/content-engine
sudo uv sync --frozen --no-dev                        # creates /opt/content-engine/.venv
sudo -u content-engine .venv/bin/content-engine sources   # smoke test: lists the sources
```

If `uv` isn't on the host: `python3 -m venv .venv && .venv/bin/pip install .`
(Python 3.10 or newer).

**First-day check, before enabling any schedule.** Some sources treat a server's IP
differently from a home connection (Reddit in particular blocks many datacenter IPs), so
check from the real host:

```sh
sudo -u content-engine env CONTENT_ENGINE_DATA_DIR=/var/lib/content-engine \
    .venv/bin/content-engine run
sudo jq '{state, failed_sources, empty_sources, enrichment}' /var/lib/content-engine/status.json
```

Expect `state: "ok"` and empty `failed_sources`. If one source fails here but worked
locally, it's blocking this host. Leave it failing (the rest still run), or remove it from
`src/content_engine/sources.py`. Don't retry in a loop to test it: that's how a 429 turns
into a longer block.

## 2a. systemd timer (recommended)

```sh
sudo cp deploy/systemd/content-engine.service deploy/systemd/content-engine.timer /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/content-engine.service   # catches typos
sudo systemctl daemon-reload
sudo systemctl enable --now content-engine.timer
sudo systemctl start content-engine.service          # first run now, don't wait for the tick
```

Check it:

```sh
systemctl list-timers content-engine.timer            # next and last trigger
systemctl status content-engine.service               # last run's result
journalctl -u content-engine.service -n 50            # per-source log lines
```

The service is a `oneshot` started by the timer. It is not a long-running daemon: nothing
sits in memory between runs, and an overlapping start can't stack up because the engine
takes a lock. `Persistent=true` catches up on a run missed while the machine was off.
Logs go to the journal, so journald's retention applies to them, not this tool's.

The unit is sandboxed (`ProtectSystem=strict`, `ProtectHome=true`): it can write only to the
data dir. **Don't install under `/home`**. If you have to, drop `ProtectHome=true`.

## 2b. cron

```sh
sudo install -d -o content-engine -g content-engine /var/lib/content-engine
sudo cp deploy/cron/content-engine.cron /etc/cron.d/content-engine   # filename: no dots
```

Cron writes one log per day, `<data-dir>/logs/run-YYYY-MM-DD.log`, and `run` deletes log files
older than the retention window. For a per-user crontab (`crontab -e`), drop the user field
(`content-engine`) from the schedule line and put the two `CONTENT_ENGINE_*` variables at the top.

## 3. Monitoring and dashboards

Everything a monitor needs is in files under the data dir. No extra service has to run.

| Path | Use |
|---|---|
| `status.json` | Last run: **`health`** (see below), `state` (`ok` / `partial` / `failed`), `started_at`, `items`, `sources_ok`/`sources_total`, `failed_sources`, `empty_sources`, per-source `ok`/`items`/`error`/`duration_ms`, `enrichment` counts (weak summaries filled from linked pages), `cleanup` counts |
| `history.jsonl` | One line per run (`run_id`, `state`, `items`, `new_items`, `already_delivered`, `failed_sources`, `empty_sources`, ...), pruned to the retention window. Use it for success-rate and item-count charts |
| `latest/brief.md` | The newest markdown brief, for prompt injection |
| `latest/items.json` | The newest normalized items. Each has `delivered`: `true` if an earlier run already carried it, `false` if it is new |
| `runs/<id>/report.json` | That run's outcome, with `new_items` / `already_delivered` totals and `new` / `already_delivered` per source |
| `runs/<YYYYmmddTHHMMSSZ>/` | Every run in the window: `items.json`, `brief.md`, `report.json` |

Exit codes: `0` the run finished (state `ok` or `partial`); `1` every source failed, or a
crash; `75` skipped because another run held the lock (systemd treats it as success).

### What to alert on

Two checks cover it:

1. **`status.json` → `health.alert` is `true`.** The engine sets it, with human-readable
   `health.reasons`, when:
   - every source failed this run, or
   - one source has **failed** 3 runs in a row (endpoint gone, or blocking us), or
   - one source has come back **empty** 3 runs in a row. This is the quiet failure: a 200
     with nothing parseable because a feed moved or changed format. Sources where empty is
     normal (arXiv on weekends) are exempt.

   A single `partial` run doesn't raise it: blips and one-off 429s are normal.
2. **`status.json` → `started_at` is older than 2× the schedule interval.** The engine
   can't report this itself: if the timer is disabled, cron is missing or the host is down,
   nothing writes. The monitor has to check the age.

Example check for a cron-based monitor (exit non-zero = alert):

```sh
jq -e '.health.alert == false and ((now - (.started_at | fromdateiso8601)) < 7200)' \
    /var/lib/content-engine/status.json
```

### Maintenance routine

- **When an alert fires:** read `health.reasons` and the source's `error` in `status.json`,
  then follow "When a source breaks" in `CLAUDE.md`.
- **Weekly (2 minutes):** skim `latest/brief.md`. The monitors catch failures and empty
  sources, but not a source that still returns items that have turned useless (e.g. a feed
  that now only posts job ads). Only a human glance catches that.
- **Monthly:** `git pull && uv sync --frozen --no-dev` on the host to pick up registry fixes,
  then run the first-day check again.

### Instructions for an agent integrating this into a cron dashboard

1. Install as in section 1. Pick 2a or 2b, depending on what the dashboard watches
   (systemd units, or cron jobs).
2. If the dashboard wraps cron jobs itself (e.g. Cronicle, healthchecks.io, a custom
   runner), register the command `<install-dir>/.venv/bin/content-engine run` with
   `CONTENT_ENGINE_DATA_DIR` and `CONTENT_ENGINE_RETENTION_DAYS` set, and treat exit `75`
   as success. Don't add a separate cleanup job: `run` already cleans up.
   `content-engine clean [--dry-run]` exists only for one-off manual cleanup.
3. For a heartbeat-style monitor, ping it only on exit `0`, e.g.
   `content-engine run && curl -fsS <ping-url>`.
4. To read health, use `status.json` (`health.alert`, `health.reasons`, `started_at` age)
   and `history.jsonl` as described under "What to alert on". Don't parse log lines. The JSON
   keys are the stable interface.
