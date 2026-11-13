#!/usr/bin/env -S uv run --script
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "duckdb",
#     "pyarrow",
#     "rich",
# ]
# ///
"""
consolidate_vitrasa.py
======================

Rebuilds an analytics database (DuckDB or SQLite) from the git history of the
git-scraping repository produced by fetch_vitrasa_data.py.

Walks the history of three versioned files (oldest -> newest):
    realtime/snapshot.json        one blob per 15-min run  -> the time series
    data/vitrasa_lines.json       static network (rarely changes)
    data/vitrasa_schedules.json   theoretical timetable (changes when GTFS does)

For every snapshot commit it records:
  * observation time = the git commit timestamp (UTC) + Europe/Madrid wall clock
  * every realtime estimation (stop, line, headsign/ruta, minutos, metros)
  * the best-matching *scheduled* arrival from the timetable version in effect
    at that point of history (same line, matching headsign, service active on
    the snapshot's service day)
  * delay_min = (commit_time + minutos) - scheduled_time
    (negative = the realtime prediction is ahead of the official timetable)

USAGE
-----
  ./consolidate_vitrasa.py --db vitrasa.duckdb            # uv resolves deps (PEP 723)
  ./consolidate_vitrasa.py --db vitrasa.duckdb --since 2025-03-01
  ./consolidate_vitrasa.py --db vitrasa.sqlite3 --engine sqlite --append

Then analyze:
  python3 -c "import duckdb; print(duckdb.connect('vitrasa.duckdb').sql(
      \"SELECT line, count(*) n, round(avg(delay_min),2) avg_delay FROM v_delays GROUP BY 1 ORDER BY 3 DESC\"))"

Requires git. DuckDB is needed for the DuckDB engine; SQLite uses the stdlib.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import unicodedata
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta, timezone
from functools import lru_cache
from typing import NamedTuple
from zoneinfo import ZoneInfo

import time as clock

from rich.console import Console
from rich.progress import (BarColumn, MofNCompleteColumn, Progress, TaskProgressColumn,
                           TextColumn, TimeElapsedColumn, TimeRemainingColumn, track)
from rich.table import Table

try:
    import duckdb  # optional at import time; checked in main()
except ImportError:
    duckdb = None

try:
    import pyarrow as pa  # used for fast DuckDB bulk inserts
except ImportError:
    pa = None

MADRID = ZoneInfo("Europe/Madrid")
ERR = Console(stderr=True)
OUT = Console()


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

@lru_cache(maxsize=None)
def norm_headsign(s: str | None) -> str:
    """Uppercase, strip accents and collapse whitespace (cached: few distinct values)."""
    s = unicodedata.normalize("NFD", (s or "").upper())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(s.split())


def parse_gtfs_time(s) -> int | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        h, m, sec = s.split(":")
        return int(h) * 3600 + int(m) * 60 + int(sec)
    except ValueError:
        return None


def parse_gdate(s) -> date | None:
    return datetime.strptime(s, "%Y%m%d").date() if s else None


def service_day_and_second(local_dt: datetime, cutoff_hour: int):
    sec = local_dt.hour * 3600 + local_dt.minute * 60 + local_dt.second
    if local_dt.hour < cutoff_hour:
        return local_dt.date() - timedelta(days=1), 86400 + sec
    return local_dt.date(), sec


def iso(dt: datetime) -> str:
    return dt.isoformat(sep=" ", timespec="seconds")


def to_int(x) -> int | None:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def parse_estimate(row):
    """(minutos, ruta, linea, metros) from a realtime row, or None if malformed."""
    if not isinstance(row, (list, tuple)) or len(row) < 4:
        return None
    return to_int(row[0]), row[1], row[2], to_int(row[3])


SLOW_COMMIT_SECONDS = 5.0


def commit_progress(entries, description="Ingesting commits"):
    """Yield commits while showing a bar with the current commit's local time and hash.

    A commit that takes longer than SLOW_COMMIT_SECONDS is logged above the bar,
    so a stall shows up as a named commit instead of a frozen percentage.
    """
    columns = [
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
    ]
    with Progress(*columns, console=ERR) as progress:
        task = progress.add_task(description, total=len(entries))
        prev_sha, started = None, None
        for sha, ts_iso, files in entries:
            if prev_sha is not None:
                elapsed = clock.perf_counter() - started
                if elapsed > SLOW_COMMIT_SECONDS:
                    progress.console.log(f"slow commit {prev_sha[:8]}: {elapsed:.1f}s")
                progress.advance(task)
            when = datetime.fromisoformat(ts_iso).astimezone(MADRID)
            progress.update(task, description=f"{when:%Y-%m-%d %H:%M} {sha[:8]}")
            prev_sha, started = sha, clock.perf_counter()
            yield sha, ts_iso, files
        if prev_sha is not None:
            progress.advance(task)


# --------------------------------------------------------------------------
# Git plumbing
# --------------------------------------------------------------------------

class GitRepo:
    def __init__(self, path: str):
        self.path = path
        self._batch: subprocess.Popen | None = None

    def _git(self, args: list[str]) -> bytes:
        r = subprocess.run(["git", "-C", self.path, *args], capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: "
                               f"{r.stderr.decode(errors='replace').strip()}")
        return r.stdout

    def log_entries(self, paths, branch="HEAD", since=None, until=None, max_n=0):
        """Oldest -> newest list of (sha, committer_iso_ts, [changed files])."""
        cmd = ["log", branch, "--reverse", "--name-only", "--format=%H %cI"]
        if since:
            cmd.append(f"--since={since}")
        if until:
            cmd.append(f"--until={until}")
        if max_n:
            cmd.append(f"--max-count={max_n}")
        cmd += ["--", *paths]

        lines = self._git(cmd).decode("utf-8", "replace").splitlines()
        entries: list[tuple[str, str, list[str]]] = []
        cur = None
        for line in track(lines, description="Reading git log", console=ERR, transient=True):
            line = line.strip()
            if not line:  # git separates header and file list with a blank line
                continue
            head, sep, rest = line.partition(" ")
            if len(head) == 40 and sep and "T" in rest:  # commit header
                if cur:
                    entries.append(cur)
                cur = (head, rest, [])
            elif cur is not None:
                cur[2].append(line)
        if cur:
            entries.append(cur)
        return entries

    def blob(self, sha: str, path: str) -> bytes | None:
        """File content at a commit via a persistent `git cat-file --batch`."""
        if self._batch is None:
            self._batch = subprocess.Popen(["git", "-C", self.path, "cat-file", "--batch"],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self._batch.stdin.write(f"{sha}:{path}\n".encode())
        self._batch.stdin.flush()
        header = self._batch.stdout.readline().decode(errors="replace").strip()
        if not header or header.endswith("missing") or " " not in header:
            return None
        size = int(header.rsplit(" ", 1)[1])
        buf = self._batch.stdout.read(size)
        self._batch.stdout.read(1)  # trailing newline
        return buf if len(buf) == size else None

    def close(self) -> None:
        if self._batch is not None:
            self._batch.stdin.close()
            self._batch.wait()
            self._batch = None


# --------------------------------------------------------------------------
# Timetable index: (stop, line) -> arrivals, with service-day matching
# --------------------------------------------------------------------------

class Arrival(NamedTuple):
    t: str           # "HH:MM:SS" as published
    line: str
    headsign: str
    direction: str
    sec: int         # seconds since midnight of the service day (may exceed 86400)
    hn: str          # normalised headsign, precomputed for matching
    services: tuple[str, ...]


class ScheduleIndex:
    """Timetable indexed by (stop, line). Built once per timetable version."""

    def __init__(self, doc: dict):
        self.doc = doc
        self._services = self._parse_services(doc.get("services"))
        self._active_cache: dict[tuple[str, date], bool] = {}
        self._by_stop_line: dict[tuple[str, str], list[Arrival]] = defaultdict(list)

        for sid, arrivals in (doc.get("stops") or {}).items():
            for e in arrivals:
                sec = e.get("sec")
                if sec is None:
                    sec = parse_gtfs_time(e.get("t"))
                if sec is None:
                    continue
                arr = Arrival(t=e["t"], line=e["l"], headsign=e.get("h") or "",
                              direction=e.get("d") or "", sec=sec,
                              hn=norm_headsign(e.get("h")),
                              services=tuple(e.get("s") or ()))
                self._by_stop_line[(sid, e["l"])].append(arr)

    @staticmethod
    def _parse_services(services_doc) -> dict[str, dict]:
        out = {}
        for sid, s in (services_doc or {}).items():
            out[sid] = {
                "days": set(s.get("days") or []),
                "start": parse_gdate(s.get("start_date")),
                "end": parse_gdate(s.get("end_date")),
                "added": {parse_gdate(x) for x in s.get("added_dates") or []},
                "removed": {parse_gdate(x) for x in s.get("removed_dates") or []},
            }
        return out

    def service_active(self, sid: str, day: date) -> bool:
        key = (sid, day)
        hit = self._active_cache.get(key)
        if hit is None:
            hit = self._compute_active(sid, day)
            self._active_cache[key] = hit
        return hit

    def _compute_active(self, sid: str, day: date) -> bool:
        svc = self._services.get(sid)
        if not svc:
            return False
        if day in svc["removed"]:
            return False
        if day in svc["added"]:
            return True
        return (svc["start"] is not None and svc["end"] is not None
                and svc["start"] <= day <= svc["end"] and day.weekday() in svc["days"])

    def match(self, stop_id, linea, ruta, eta_local: datetime, svc_day: date,
              tolerance: float):
        """Nearest scheduled arrival for `linea` at `stop_id` within +/- tolerance
        minutes; prefers entries whose headsign matches the API's `ruta`."""
        if not stop_id:
            return None
        cands = self._by_stop_line.get((stop_id, (linea or "").strip()))
        if not cands:
            return None

        target = norm_headsign(ruta)
        midnight = datetime.combine(svc_day, dtime(0))
        best = None
        for arr in cands:
            if not any(self.service_active(s, svc_day) for s in arr.services):
                continue
            cand_dt = midnight + timedelta(seconds=arr.sec)
            delay = (eta_local - cand_dt).total_seconds() / 60.0
            if abs(delay) > tolerance:
                continue
            quality = 0 if arr.hn == target else 1
            key = (quality, abs(delay))
            if best is None or key < best[0]:
                best = (key, arr, cand_dt, delay, quality)

        if best is None:
            return None
        _, arr, cand_dt, delay, quality = best
        return (arr.t, iso(cand_dt), arr.line, arr.headsign, arr.direction,
                "headsign" if quality == 0 else "line", round(delay, 2))


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS snapshots(
        "commit" TEXT PRIMARY KEY, ts_utc TIMESTAMP, ts_local TIMESTAMP,
        ok INTEGER, failed INTEGER, requested INTEGER,
        stops_with_estimations INTEGER, estimaciones INTEGER, n_stops INTEGER)""",
    """CREATE TABLE IF NOT EXISTS estimations(
        "commit" TEXT, ts_utc TIMESTAMP, ts_local TIMESTAMP,
        stop_id TEXT, realtime_id TEXT,
        line TEXT, headsign TEXT, minutes INTEGER, metros INTEGER,
        eta_utc TIMESTAMP,
        scheduled_time TEXT, scheduled_ts TIMESTAMP,
        sched_line TEXT, sched_headsign TEXT, sched_dir TEXT,
        match_type TEXT, delay_min REAL)""",
    """CREATE TABLE IF NOT EXISTS snapshot_errors(
        "commit" TEXT, realtime_id TEXT, error TEXT)""",
    """CREATE TABLE IF NOT EXISTS stops(
        stop_id TEXT PRIMARY KEY, realtime_id TEXT, name TEXT,
        lat REAL, lon REAL, lines TEXT)""",
    """CREATE TABLE IF NOT EXISTS schedule_arrivals(
        stop_id TEXT, line TEXT, dir TEXT, headsign TEXT,
        time TEXT, time_sec INTEGER, service_ids TEXT)""",
    """CREATE TABLE IF NOT EXISTS schedule_services(
        service_id TEXT PRIMARY KEY, days TEXT, start_date TEXT, end_date TEXT,
        added_dates TEXT, removed_dates TEXT)""",
    """CREATE TABLE IF NOT EXISTS ingest_state(key TEXT PRIMARY KEY, value TEXT)""",
    """CREATE VIEW IF NOT EXISTS v_delays AS
       SELECT e.*, s.name AS stop_name, s.lat, s.lon
       FROM estimations e LEFT JOIN stops s ON s.stop_id = e.stop_id
       WHERE e.delay_min IS NOT NULL""",
]

SQLITE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_est_ts ON estimations(ts_utc)",
    "CREATE INDEX IF NOT EXISTS idx_est_stop ON estimations(stop_id)",
    "CREATE INDEX IF NOT EXISTS idx_est_line ON estimations(line)",
]

TABLES = ("snapshots", "estimations", "snapshot_errors", "stops",
          "schedule_arrivals", "schedule_services", "ingest_state")
VIEWS = ("v_delays",)

REPORT_SQL = (
    "SELECT line, COUNT(*) n, ROUND(AVG(delay_min), 2) avg_delay, "
    "ROUND(100.0 * SUM(CASE WHEN delay_min BETWEEN -1 AND 3 THEN 1 ELSE 0 END) / COUNT(*), 1) pct_on_time "
    "FROM estimations WHERE delay_min IS NOT NULL "
    "GROUP BY line ORDER BY avg_delay DESC LIMIT 15"
)


MAX_PARAMS = 30000  # stay under SQLite's bound-parameter limit per statement


def bulk_insert(con, table: str, rows: list, ncols: int) -> None:
    """Insert rows into `table` (columns matched by position).

    DuckDB: rows are turned into one Arrow table and inserted with a single
    INSERT ... SELECT. Parsing one huge VALUES list is what made flushes slow,
    and Arrow avoids it (about 100x faster on 15k rows).

    SQLite (or DuckDB values Arrow cannot convert): multi-row VALUES chunks.
    """
    if not rows:
        return
    if pa is not None and duckdb is not None and isinstance(con, duckdb.DuckDBPyConnection):
        try:
            tbl = pa.table({f"c{i}": pa.array(col) for i, col in enumerate(zip(*rows))})
        except (pa.ArrowInvalid, pa.ArrowTypeError):
            pass  # mixed types in a column: fall back to the chunked path
        else:
            con.register("_staging", tbl)
            try:
                con.execute(f"INSERT INTO {table} SELECT * FROM _staging")
            finally:
                con.unregister("_staging")
            return

    per_stmt = max(1, MAX_PARAMS // ncols)
    one = "(" + ",".join("?" * ncols) + ")"
    for i in range(0, len(rows), per_stmt):
        chunk = rows[i:i + per_stmt]
        sql = f"INSERT INTO {table} VALUES " + ",".join([one] * len(chunk))
        con.execute(sql, [v for r in chunk for v in r])


def open_db(engine: str, path: str, append: bool):
    if engine == "duckdb":
        if not append and os.path.exists(path):
            os.remove(path)
        con = duckdb.connect(path)
    else:
        con = sqlite3.connect(path)
        if not append:
            for name in VIEWS:
                con.execute(f"DROP VIEW IF EXISTS {name}")
            for name in TABLES:
                con.execute(f"DROP TABLE IF EXISTS {name}")
    for stmt in SCHEMA:
        con.execute(stmt)
    if engine == "sqlite":
        for stmt in SQLITE_INDEXES:
            con.execute(stmt)
    return con


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------

class Ingestor:
    """Turns snapshots into estimation rows, buffers them, and writes them out."""

    FLUSH_EVERY = 50  # smaller batches keep each database pause short

    def __init__(self, con, tolerance: float, day_cutoff: int):
        self.con = con
        self.tolerance = tolerance
        self.day_cutoff = day_cutoff
        self.net: dict | None = None
        self.rt2stop: dict[int, str] = {}
        self.sched: ScheduleIndex | None = None
        self.buf = {"snapshots": [], "estimations": [], "errors": []}
        self.n_snap = self.n_est = self.n_matched = 0

    # -- reference data -----------------------------------------------------

    def set_network(self, doc: dict) -> None:
        self.net = doc
        # Snapshot keys are zero-padded strings ("00100"); the network stores ints (100).
        # Compare as integers so the two forms meet.
        self.rt2stop = {}
        for sid, st in (doc.get("stops") or {}).items():
            key = to_int(st.get("realtime_id"))
            if key is not None:
                self.rt2stop[key] = sid

    def set_schedule(self, doc: dict) -> None:
        self.sched = ScheduleIndex(doc)

    # -- per-snapshot -------------------------------------------------------

    def ingest(self, sha: str, ts_iso: str, doc: dict) -> None:
        ts_utc = datetime.fromisoformat(ts_iso).astimezone(timezone.utc).replace(tzinfo=None)
        local = datetime.fromisoformat(ts_iso).astimezone(MADRID).replace(tzinfo=None)
        svc_day, _ = service_day_and_second(local, self.day_cutoff)

        stops_doc = doc.get("stops") or {}
        errors_doc = doc.get("errors") or {}
        with_est = n_here = 0

        for rid, payload in stops_doc.items():
            stop_id = self.rt2stop.get(to_int(rid))
            est = (payload or {}).get("e") or []
            if est:
                with_est += 1
            for row in est:
                parsed = parse_estimate(row)
                if parsed is None:
                    continue
                minutos, ruta, linea, metros = parsed
                eta_local = local + timedelta(minutes=minutos) if minutos is not None else None
                m = None
                if eta_local is not None and self.sched is not None:
                    m = self.sched.match(stop_id, linea, ruta, eta_local, svc_day, self.tolerance)
                if m:
                    self.n_matched += 1
                sched_time, sched_ts, s_line, s_head, s_dir, mtype, delay = m if m else (None,) * 7
                self.buf["estimations"].append((
                    sha, iso(ts_utc), iso(local), stop_id, rid,
                    (linea or "").strip(), (ruta or "").strip(), minutos, metros,
                    iso(eta_local) if eta_local else None,
                    sched_time, sched_ts, s_line, s_head, s_dir, mtype, delay))
                n_here += 1
        self.n_est += n_here

        for rid, err in sorted(errors_doc.items()):
            self.buf["errors"].append((sha, rid, str(err)[:300]))

        self.buf["snapshots"].append((sha, iso(ts_utc), iso(local),
                                      len(stops_doc), len(errors_doc), None,
                                      with_est, n_here, len(stops_doc)))
        self.n_snap += 1
        if self.n_snap % self.FLUSH_EVERY == 0:
            self.flush()

    def flush(self) -> None:
        b = self.buf
        bulk_insert(self.con, "snapshots", b["snapshots"], 9)
        bulk_insert(self.con, "estimations", b["estimations"], 17)
        bulk_insert(self.con, "snapshot_errors", b["errors"], 3)
        for rows in b.values():
            rows.clear()

    # -- reference tables and state -----------------------------------------

    def write_dimensions(self) -> None:
        """Replace the stops/timetable tables with the latest versions seen."""
        if self.net:
            stops = (self.net.get("stops") or {}).items()
            rows = [(sid, st.get("realtime_id"), st.get("name"), st.get("lat"), st.get("lon"),
                     ",".join(st.get("lines") or []))
                    for sid, st in track(stops, description="Writing stops", console=ERR,
                                         transient=True)]
            self.con.execute("DELETE FROM stops")
            bulk_insert(self.con, "stops", rows, 6)

        if self.sched is not None:
            doc = self.sched.doc
            rows = []
            for sid, arrivals in track((doc.get("stops") or {}).items(),
                                       description="Writing timetable", console=ERR,
                                       transient=True):
                for e in arrivals:
                    rows.append((sid, e.get("l"), e.get("d"), e.get("h"), e.get("t"),
                                 parse_gtfs_time(e.get("t")),
                                 ",".join(e.get("s") or [])))
            self.con.execute("DELETE FROM schedule_arrivals")
            bulk_insert(self.con, "schedule_arrivals", rows, 7)

            svc_rows = [(sid, ",".join(s.get("days") or []), s.get("start_date"), s.get("end_date"),
                         ",".join(s.get("added_dates") or []), ",".join(s.get("removed_dates") or []))
                        for sid, s in (doc.get("services") or {}).items()]
            self.con.execute("DELETE FROM schedule_services")
            bulk_insert(self.con, "schedule_services", svc_rows, 6)

    def write_state(self, last_commit: str, engine: str) -> None:
        self.con.executemany("INSERT OR REPLACE INTO ingest_state VALUES (?,?)", [
            ("last_commit", last_commit),
            ("snapshots", str(self.n_snap)),
            ("estimations", str(self.n_est)),
            ("matched", str(self.n_matched)),
            ("engine", engine),
        ])


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def print_report(con) -> None:
    try:
        rows = con.execute(REPORT_SQL).fetchall()
    except Exception as exc:  # noqa: BLE001
        ERR.print(f"report skipped: {exc}")
        return
    table = Table(title="Average delay by line (min; negative = ahead of schedule)")
    table.add_column("line", justify="left")
    for col in ("n", "avg_delay", "pct_on_time"):
        table.add_column(col, justify="right")
    for row in rows:
        table.add_row(*(str(v) for v in row))
    OUT.print(table)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Rebuild a DuckDB/SQLite analytics DB from the vitrasa git history.")
    ap.add_argument("--db", default="vitrasa.duckdb")
    ap.add_argument("--engine", choices=("duckdb", "sqlite"), default=None,
                    help="default: inferred from --db extension")
    ap.add_argument("--repo", default=".", help="path to the git-scraping repository")
    ap.add_argument("--branch", default="HEAD")
    ap.add_argument("--snapshot", default="realtime/snapshot.json")
    ap.add_argument("--network", default="data/vitrasa_lines.json")
    ap.add_argument("--schedule", default="data/vitrasa_schedules.json")
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--max-commits", type=int, default=0)
    ap.add_argument("--tolerance", type=float, default=75.0,
                    help="max |delay| in minutes for matching a scheduled arrival (default 75)")
    ap.add_argument("--day-cutoff", type=int, default=4,
                    help="hour at which the GTFS service day flips (default 4)")
    ap.add_argument("--append", action="store_true",
                    help="keep existing rows, skip commits already ingested")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    engine = args.engine or ("duckdb" if args.db.endswith((".duckdb", ".ddb")) else "sqlite")
    if engine == "duckdb" and duckdb is None:
        sys.exit("DuckDB engine selected but the package is missing (run via uv, or pip install duckdb)")

    repo = GitRepo(args.repo)
    con = open_db(engine, args.db, append=args.append)
    try:
        processed: set[str] = set()
        if args.append:
            processed = {r[0] for r in con.execute('SELECT "commit" FROM snapshots').fetchall()}

        entries = repo.log_entries([args.snapshot, args.network, args.schedule],
                                   branch=args.branch, since=args.since,
                                   until=args.until, max_n=args.max_commits)
        ERR.print(f"History: {len(entries)} commits touch the tracked files")

        ing = Ingestor(con, args.tolerance, args.day_cutoff)
        for sha, ts_iso, files in commit_progress(entries):
            if args.network in files:
                raw = repo.blob(sha, args.network)
                if raw:
                    ing.set_network(json.loads(raw))
            if args.schedule in files:
                raw = repo.blob(sha, args.schedule)
                if raw:
                    ing.set_schedule(json.loads(raw))
            if args.snapshot not in files or sha in processed:
                continue
            raw = repo.blob(sha, args.snapshot)
            if raw is None:
                continue
            ing.ingest(sha, ts_iso, json.loads(raw))
        ing.flush()

        ing.write_dimensions()
        ing.write_state(entries[-1][0] if entries else "", engine)
        con.commit()
    finally:
        repo.close()
        con.close()

    pct = (100.0 * ing.n_matched / ing.n_est) if ing.n_est else 0.0
    ERR.print(f"Done: {ing.n_snap} snapshots, {ing.n_est} estimations, "
              f"{ing.n_matched} matched to schedule ({pct:.1f}%) -> {args.db}")

    con = duckdb.connect(args.db, read_only=True) if engine == "duckdb" else sqlite3.connect(args.db)
    try:
        print_report(con)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
