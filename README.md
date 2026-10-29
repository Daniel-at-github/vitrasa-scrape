# vitrasa-git-scrape

Git scraping of the **Vitrasa** urban bus network (Vigo, Galicia, Spain): a GitHub Action
snapshots real-time arrivals every 15 minutes, the git history becomes a time-series
database, and a marimo notebook turns it into delay analytics.

Built on public open data — no API keys, no third-party runtime dependencies for scraping.

## What it does

| Artifact | Source | Cadence |
|---|---|---|
| `data/vitrasa_lines.json` — lines, ordered stops per direction, overlapping stops | GTFS feed | committed only when the feed changes |
| `data/vitrasa_schedules.json` — theoretical/official arrivals per stop (GTFS `stop_times` + calendar) | GTFS feed | ditto |
| `realtime/snapshot.json` — next-arrival estimations for every stop | Vitrasa realtime API | new commit every 15 min |
| `vitrasa.duckdb` — consolidated analytics DB | git history of the above | on demand, locally |

**The key idea** ([git scraping](https://simonwillison.net/2020/Oct/9/git-scraping/)):
the snapshot file contains **no timestamps** — the UTC timestamp of the git commit that
introduces it *is* the observation time. Output is deterministic (sorted keys), so
`git log -p realtime/snapshot.json` is a readable, diffable history of the entire bus
network.

## How it works

```
GitHub Action (cron */15 UTC, uv + stdlib only)
  │
  ├─ fetch_vitrasa_data.py network  ──►  data/vitrasa_lines.json      (commit if changed)
  │                                 ──►  data/vitrasa_schedules.json  (commit if changed)
  └─ fetch_vitrasa_data.py realtime ──►  realtime/snapshot.json      (commit every run)
                │
                ▼
        git history = time-series database
                │
                ▼
  consolidate_snapshots.py  ──►  vitrasa.duckdb (or SQLite)
                │                matches each realtime estimation against the
                ▼                timetable version in effect at that moment
  analysis/delays.py (marimo) ──►  delay dashboards by line / hour / stop
```

## Repository layout

```
.
├── fetch_vitrasa_data.py        # GTFS → network + timetable; all-stops realtime snapshotter
├── consolidate_snapshots.py     # walks git history → SQLite/DuckDB
├── analysis/
│   └── delays.py                # marimo notebook (delay dashboards)
├── data/
│   ├── vitrasa_lines.json       # static network (versioned)
│   └── vitrasa_schedules.json   # theoretical timetable (versioned)
├── realtime/
│   └── snapshot.json            # latest snapshot (one commit per 15 min)
├── .github/workflows/scrape.yml # the scraper
├── pyproject.toml               # uv project — fetcher is stdlib-only; dev group = analysis tools
└── uv.lock
```

## Data sources (all public)

- **GTFS feed**: <https://datos.vigo.org/data/transporte/gtfs_vigo.zip>
  (`stop_code` `P0014135` → realtime id `14135` for the API below)
- **Realtime estimations**: `https://datos.vigo.org/vci_api_app/api2.jsp?id=<realtime_id>&ttl=5&tipo=TRANSPORTE-ESTIMACION-PARADA`
  (JSON, `ISO-8859-1`; returns `estimaciones[]` with `minutos / ruta / linea / metros`)
- **Legacy HTML board**: `http://infobus.vitrasa.es:8002/Default.aspx?parada=<realtime_id>`
- **Open data portal (CKAN)**: <https://datos-ckan.vigo.org/group/transporte>
  (`paradas.json`, `lineas.geojson`, `ocupacion`, `itinerarios`)

## Requirements

- [uv](https://docs.astral.sh/uv/) (manages Python ≥ 3.12 and all tooling)
- `git` — the consolidator needs a **full** clone (not shallow)

---

# ⌨️ Cheat sheet

## Setup (once)

```bash
# install uv — macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows: powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

git clone https://github.com/<you>/vitrasa-git-scrape && cd vitrasa-git-scrape
uv sync                # .venv + dev group (duckdb, pandas, altair, marimo); writes uv.lock
```

## Scraping — `fetch_vitrasa_data.py`

| Command | What it does |
|---|---|
| `uv run fetch_vitrasa_data.py network --out data/vitrasa_lines.json --schedule-out data/vitrasa_schedules.json` | Download GTFS → static network **and** theoretical timetable |
| `uv run fetch_vitrasa_data.py schedule --out data/vitrasa_schedules.json` | Refresh only the theoretical timetable |
| `uv run fetch_vitrasa_data.py realtime --out realtime/snapshot.json --only-scheduled` | All-stops realtime snapshot (skips stops with no scheduled arrival within the window) |
| `uv run fetch_vitrasa_data.py realtime --stops 14135,2440` | Realtime for specific stops (realtime or GTFS ids) |
| `uv run fetch_vitrasa_data.py --realtime 14135` | Legacy spot-check: raw API response printed to stdout |
| `uv run fetch_vitrasa_data.py realtime --only-scheduled --max-stops 25` | Smoke test on 25 stops |

Handy flags: `--workers 8` · `--min-interval 0.05` (global request pacing, be polite) ·
`--window 120` (scheduled-arrival lookahead, min) · `--day-cutoff 4` (service-day flip hour) ·
`--gtfs-cache .cache/gtfs.zip` (avoid re-downloading the feed) · `--max-stops N` (testing).

Exit code `3` if **every** request failed → CI then skips the commit instead of archiving garbage.

## Analytics — `consolidate_snapshots.py`

| Command | What it does |
|---|---|
| `uv run consolidate_snapshots.py --db vitrasa.duckdb` | Full rebuild: walk git history → DuckDB |
| `uv run consolidate_snapshots.py --db vitrasa.sqlite3 --engine sqlite` | Same, into SQLite (stdlib fallback) |
| `uv run consolidate_snapshots.py --db vitrasa.duckdb --append` | Incremental: skip already-ingested commits |
| `uv run consolidate_snapshots.py --db vitrasa.duckdb --since 2025-03-01` | Only commits after a date |
| `uv run consolidate_snapshots.py --db vitrasa.duckdb --tolerance 60` | Max ±minutes when matching an estimation to the timetable |

## Notebook & exports — marimo

| Command | What it does |
|---|---|
| `uv run marimo edit analysis/delays.py` | Open the notebook in the interactive editor |
| `uv run analysis/delays.py` | Run headless with UI defaults (self-contained via PEP 723 metadata) |
| `uv run marimo export html analysis/delays.py -o delays.html` | Static HTML export of the dashboard |

## SQL one-liners & export

```bash
uv run python - <<'EOF'
import duckdb
con = duckdb.connect("vitrasa.duckdb")
print(con.sql("""
    SELECT line, count(*) AS n, round(avg(delay_min), 2) AS avg_delay,
           quantile_cont(delay_min, 0.9) AS p90
    FROM v_delays GROUP BY line ORDER BY p90 DESC LIMIT 15
"""))
con.sql("COPY v_delays TO 'delays.parquet' (FORMAT PARQUET)")   # for pandas/polars/R
EOF
```

## CI — `.github/workflows/scrape.yml`

| Command | What it does |
|---|---|
| `gh workflow run scrape.yml` | Trigger the scraper manually (`workflow_dispatch`) |
| `gh run watch` | Follow the latest run |
| `git log --format='%h %cI %s' -- realtime/snapshot.json` | One line per observation |
| `git log -p -- realtime/snapshot.json` | The time series, straight from git |
| `git show HEAD:realtime/snapshot.json` | Latest raw snapshot |

## First deployment

```bash
gh repo create vitrasa-git-scrape --public --source=. --push
# then: repo → Actions tab → enable the "vitrasa-git-scrape" scheduled workflow
```

---

# Reference

## Snapshot format (`realtime/snapshot.json`)

```jsonc
{
  "format_version": 1,
  "meta": { "counts": { "requested": 612, "ok": 609, "failed": 3,
                        "stops_with_estimations": 240, "estimaciones": 812 },
            "observation_time": "NOT stored — the git commit timestamp is the clock" },
  "stops": {
    "14135": { "e": [[3, "PRAZA AMÉRICA", "C1", 812], [17, "PRAZA AMÉRICA", "C1", 4210]],
               "p": { "n": "Rúa de Santo Amaro (Praza de España)", "lat": 42.23, "lon": -8.72, "sv": 14135 } }
  },
  "errors": { "2440": "RuntimeError: GET ... failed after 2 attempts: ..." }
}
```

`e` = estimaciones as `[minutos, ruta, linea, metros]`, sorted by arrival.

## Theoretical timetable (`data/vitrasa_schedules.json`)

- `services`: `days` (0=Mon … 6=Sun), `start_date`/`end_date`, `added_dates`/`removed_dates`
  (GTFS `calendar_dates.txt` exceptions).
- `stops[stop_id]`: sorted list of `{t, l, d, h, s}` = time, line, direction, headsign, service ids.
- `t` is GTFS local time (Europe/Madrid) and **may exceed 24:00** for night services:
  `25:30` on service day *D* departs *D+1* at `01:30`. The service day flips at 04:00.

## Database schema (DuckDB/SQLite)

| Table | Contents |
|---|---|
| `snapshots` | one row per snapshot commit (counts, ok/failed) |
| `estimations` | one row per realtime estimation, with matched scheduled arrival |
| `snapshot_errors` | per-stop fetch failures |
| `stops` | dim: id, realtime id, name, coords, lines (latest network) |
| `schedule_arrivals`, `schedule_services` | latest theoretical timetable |
| **`v_delays`** | the analysis view: matched estimations ⋈ stop metadata |

**`delay_min = (commit_time + minutos) − scheduled_time` → positive = late.**
`match_type` is `headsign` (API `ruta` matched the GTFS headsign) or `line` (nearest
same-line arrival fallback); unmatched rows have `delay_min IS NULL`.

## The notebook (`analysis/delays.py`)

Reactive marimo dashboard: coverage stats, delay by line (p50/p90/on-time %), by hour of
day, daily trend, line × hour heatmap, worst stops — all cross-filtered by the **line**
dropdown and **look-back window** slider. Read-only (`read_only=True`); re-run the
consolidator after new snapshot commits to refresh.

## GitHub Actions notes

- `cron: */15 * * * *` is **best-effort**: runs can slip a few minutes — harmless, since
  the commit timestamp is the observation time.
- `concurrency` prevents overlapping runs; commits are created only when the file changed.
- Scheduled workflows are **disabled after 60 days of repo inactivity** — irrelevant while
  the bot commits, but re-enable in the Actions tab after a long pause.
- The job uses `uv run --frozen --no-dev`, so CI installs **zero packages** (fetcher is
  stdlib-only). If it fails with a lock error, run `uv lock` locally and commit `uv.lock`.

## Operations & caveats

- **`delay_min` is a proxy**: realtime *prediction* minus schedule, sampled every ~15 min —
  great for aggregates, not an exact arrival-event measurement.
- **Repo growth**: ~100–300 KB per snapshot; git's delta compression keeps a year in the
  hundreds of MB. Mitigations: `--only-scheduled` (default in CI), a quiet-hours guard
  step, `*/30` cadence, or archive via the consolidator and rotate the snapshot file.
- **Be polite**: it's open data behind the official city app. Default pacing
  (`--min-interval 0.05`, 8 workers) and `--only-scheduled` keep it to a few hundred
  requests per run; contact Concello de Vigo if you need bulk access.
- **DuckDB single-writer**: close the notebook before re-running the consolidator.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Notebook says database not found | `uv run consolidate_snapshots.py --db vitrasa.duckdb` |
| `uv run --frozen` fails in CI | `uv.lock` stale → `uv lock` and commit it |
| Many `failed` stops in `snapshot.json` | check `errors` map; API outage → CI exits 3, no commit |
| Workflow not running | enable scheduled workflows; check the 60-day rule |

## Credits

- [Simon Willison — Git scraping: track changes over time](https://simonwillison.net/2020/Oct/9/git-scraping/)
  (the pattern this repo implements)
- [David-Lor/VigoBusAPI](https://github.com/David-Lor/VigoBusAPI) — reference for the realtime endpoint
- Concello de Vigo open data portal (`datos.vigo.org`, `datos-ckan.vigo.org`) — data by Vitrasa/EMTUSA de Vigo

## License

MIT — see `LICENSE`.
