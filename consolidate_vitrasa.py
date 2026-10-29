#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
  pip install duckdb
  python3 consolidate_vitrasa.py --db vitrasa.duckdb
  python3 consolidate_vitrasa.py --db vitrasa.duckdb --since 2025-03-01
  python3 consolidate_vitrasa.py --db vitrasa.sqlite3 --engine sqlite --append

Then analyze:
  python3 -c "import duckdb; print(duckdb.connect('vitrasa.duckdb').sql(
      \"SELECT line, count(*) n, round(avg(delay_min),2) avg_delay FROM v_delays GROUP BY 1 ORDER BY 3 DESC\"))"

Requires git; `pip install duckdb` for the DuckDB engine (SQLite = stdlib).
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import unicodedata
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    import duckdb  # optional
except ImportError:
    duckdb = None

MADRID = ZoneInfo("Europe/Madrid")


def norm_headsign(s: str) -> str:
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


def parse_gdate(s):
    return datetime.strptime(s, "%Y%m%d").date() if s else None


def service_day_and_second(local_dt: datetime, cutoff_hour: int):
    sec = local_dt.hour * 3600 + local_dt.minute * 60 + local_dt.second
    if local_dt.hour < cutoff_hour:
        return local_dt.date() - timedelta(days=1), 86400 + sec
    return local_dt.date(), sec


def iso(dt: datetime) -> str:
    return dt.isoformat(sep=" ", timespec="seconds")


# --------------------------------------------------------------------------
# Git plumbing
# --------------------------------------------------------------------------

class GitRepo:
    def __init__(self, path: str):
        self.path = path
        self._batch = None

    def _git(self, args) -> bytes:
        r = subprocess.run(["git", "-C", self.path] + args, capture_output=True)
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
        cmd += ["--"] + paths
        entries, cur = [], None
        for line in self._git(cmd).decode("utf-8", "replace").splitlines():
            if not line.strip():
                if cur:
                    entries.append(cur)
                    cur = None
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2 and len(parts[0]) == 40 and "T" in parts[1]:
                cur = (parts[0], parts[1], [])
            elif cur is not None:
                cur[2].append(line.strip())
        if cur:
            entries.append(cur)
        return entries

    def blob(self, sha: str, path: str):
        """File content at a commit via a persistent `git cat-file --batch`."""
        if self._batch is None:
            self._batch = subprocess.Popen(["git", "-C", self.path, "cat-file", "--batch"],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        req = f"{sha}:{path}\n".encode()
        self._batch.stdin.write(req)
        self._batch.stdin.flush()
        header = self._batch.stdout.readline().decode(errors="replace").strip()
        if header.endswith("missing") or " " not in header:
            return None
        size = int(header.rsplit(" ", 1)[1])
        buf = b""
        while len(buf) < size:
            chunk = self._batch.stdout.read(size - len(buf))
            if not chunk:
                return None
            buf += chunk
        self._batch.stdout.read(1)  # trailing newline
        return buf


# --------------------------------------------------------------------------
# Schedule state (services, per-stop arrivals) with service-day matching
# --------------------------------------------------------------------------

def parse_services(services_doc):
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


def make_svc_active(services_parsed):
    cache = {}

    def active(sid, d):
        key = (sid, d)
        if key in cache:
            return cache[key]
        svc = services_parsed.get(sid)
        r = False
        if svc:
            if d in svc["removed"]:
                r = False
            elif d in svc["added"]:
                r = True
            elif svc["start"] is not None and svc["end"] is not None:
                r = svc["start"] <= d <= svc["end"] and d.weekday() in svc["days"]
        cache[key] = r
        return r

    return active


def build_rt2stop(network_doc):
    rt2stop = {}
    for sid, st in (network_doc.get("stops") or {}).items():
        rt = st.get("realtime_id")
        if rt is not None:
            rt2stop[str(rt)] = sid
    return rt2stop


def match_arrival(sched_stops, svc_active, stop_id, linea, ruta, eta_local,
                  svc_day, tolerance):
    """Nearest scheduled arrival at stop for `linea` within +/- tolerance minutes.
       Prefers entries whose headsign matches the API's 'ruta'."""
    if not stop_id:
        return None
    entries = sched_stops.get(stop_id)
    if not entries:
        return None
    line = (linea or "").strip()
    cands = [e for e in entries if e.get("l") == line]
    if not cands:
        return None
    target = norm_headsign(ruta)
    best = None
    for e in cands:
        if not any(svc_active(s, svc_day) for s in e["s"]):
            continue
        cand_dt = datetime.combine(svc_day, dtime(0)) + timedelta(seconds=e["sec"])
        delay = (eta_local - cand_dt).total_seconds() / 60.0
        if abs(delay) > tolerance:
            continue
        quality = 0 if norm_headsign(e.get("h") or "") == target else 1
        key = (quality, abs(delay))
        if best is None or key < best[0]:
            best = (key, e, cand_dt, delay, quality)
    if best is None:
        return None
    _, e, cand_dt, delay, quality = best
    return (e["t"], iso(cand_dt), e["l"], e.get("h") or "", e.get("d") or "",
            "headsign" if quality == 0 else "line", round(delay, 2))


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS snapshots(
        commit TEXT PRIMARY KEY, ts_utc TIMESTAMP, ts_local TIMESTAMP,
        ok INTEGER, failed INTEGER, requested INTEGER,
        stops_with_estimations INTEGER, estimaciones INTEGER, n_stops INTEGER)""",
    """CREATE TABLE IF NOT EXISTS estimations(
        commit TEXT, ts_utc TIMESTAMP, ts_local TIMESTAMP,
        stop_id TEXT, realtime_id TEXT,
        line TEXT, headsign TEXT, minutes INTEGER, metros INTEGER,
        eta_utc TIMESTAMP,
        scheduled_time TEXT, scheduled_ts TIMESTAMP,
        sched_line TEXT, sched_headsign TEXT, sched_dir TEXT,
        match_type TEXT, delay_min REAL)""",
    """CREATE TABLE IF NOT EXISTS snapshot_errors(
        commit TEXT, realtime_id TEXT, error TEXT)""",
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


def open_db(engine, path, append):
    if engine == "duckdb":
        if append:
            con = duckdb.connect(path)
        else:
            if os.path.exists(path):
                os.remove(path)
            con = duckdb.connect(path)
    else:
        con = sqlite3.connect(path)
        if not append:
            for t in ("snapshots", "estimations", "snapshot_errors", "stops",
                      "schedule_arrivals", "schedule_services", "ingest_state", "v_delays"):
                con.execute(f"DROP TABLE IF EXISTS {t}")
                con.execute(f"DROP VIEW IF EXISTS {t}")
    for stmt in SCHEMA:
        con.execute(stmt)
    if engine == "sqlite":
        for stmt in SQLITE_INDEXES:
            con.execute(stmt)
    return con


def flush(con, buf):
    if buf["snapshots"]:
        con.executemany("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)", buf["snapshots"])
        buf["snapshots"].clear()
    if buf["estimations"]:
        con.executemany("INSERT INTO estimations VALUES (" + ",".join("?" * 17) + ")",
                        buf["estimations"])
        buf["estimations"].clear()
    if buf["errors"]:
        con.executemany("INSERT INTO snapshot_errors VALUES (?,?,?)", buf["errors"])
        buf["errors"].clear()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[2],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
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
    args = ap.parse_args()

    engine = args.engine or ("duckdb" if args.db.endswith((".duckdb", ".ddb")) else "sqlite")
    if engine == "duckdb" and duckdb is None:
        sys.exit("DuckDB engine selected but the package is missing:  pip install duckdb")

    repo = GitRepo(args.repo)
    con = open_db(engine, args.db, append=args.append)

    processed = set()
    if args.append:
        try:
            processed = {r[0] for r in con.execute("SELECT commit FROM snapshots").fetchall()}
        except Exception:
            processed = set()

    entries = repo.log_entries([args.snapshot, args.network, args.schedule],
                               branch=args.branch, since=args.since,
                               until=args.until, max_n=args.max_commits)
    sys.stderr.write(f"History: {len(entries)} commits touch the tracked files\n")

    state = {"net": None, "sched": None, "rt2stop": {}, "sched_stops": {}, "svc_active": lambda s, d: False}
    buf = {"snapshots": [], "estimations": [], "errors": []}
    n_snap = n_est = n_matched = 0

    def ingest(sha, ts_iso, doc):
        nonlocal n_snap, n_est, n_matched
        ts = datetime.fromisoformat(ts_iso)
        ts_utc = ts.astimezone(timezone.utc)
        local = ts.astimezone(MADRID).replace(tzinfo=None)
        svc_day, _now_sec = service_day_and_second(local, args.day_cutoff)

        stops_doc = doc.get("stops") or {}
        errors_doc = doc.get("errors") or {}
        with_est = n_est_here = 0

        for rid, payload in stops_doc.items():
            stop_id = state["rt2stop"].get(rid)
            payload = payload or {}
            est = payload.get("e") or []
            if est:
                with_est += 1
            for row in est:
                if not isinstance(row, (list, tuple)) or len(row) < 4:
                    continue
                minutos, ruta, linea, metros = row[0], row[1], row[2], row[3]
                try:
                    minutos = int(minutos)
                except (TypeError, ValueError):
                    minutos = None
                try:
                    metros = int(metros)
                except (TypeError, ValueError):
                    metros = None
                eta_local = local + timedelta(minutes=minutos) if minutos is not None else None
                m = None
                if eta_local is not None and state["sched_stops"]:
                    m = match_arrival(state["sched_stops"], state["svc_active"], stop_id,
                                      linea, ruta, eta_local, svc_day, args.tolerance)
                if m:
                    n_matched += 1
                sched_time, sched_ts, s_line, s_head, s_dir, mtype, delay = m if m else (None,) * 7
                buf["estimations"].append((
                    sha, iso(ts_utc.replace(tzinfo=None)), iso(local), stop_id, rid,
                    (linea or "").strip(), (ruta or "").strip(), minutos, metros,
                    iso(eta_local) if eta_local else None,
                    sched_time, sched_ts, s_line, s_head, s_dir, mtype, delay))
                n_est_here += 1
        n_est += n_est_here

        for rid, err in sorted(errors_doc.items()):
            buf["errors"].append((sha, rid, str(err)[:300]))

        buf["snapshots"].append((sha, iso(ts_utc.replace(tzinfo=None)), iso(local),
                                 len(stops_doc), len(errors_doc), None,
                                 with_est, n_est_here, len(stops_doc)))
        n_snap += 1

    try:
        for sha, ts_iso, files in entries:
            if args.network in files:
                raw = repo.blob(sha, args.network)
                if raw:
                    state["net"] = json.loads(raw)
                    state["rt2stop"] = build_rt2stop(state["net"])
            if args.schedule in files:
                raw = repo.blob(sha, args.schedule)
                if raw:
                    state["sched"] = json.loads(raw)
                    state["sched_stops"] = state["sched"].get("stops") or {}
                    state["svc_active"] = make_svc_active(
                        parse_services(state["sched"].get("services")))
            if args.snapshot not in files:
                continue
            if sha in processed:
                continue
            raw = repo.blob(sha, args.snapshot)
            if raw is None:
                continue
            ingest(sha, ts_iso, json.loads(raw))
            if n_snap % 200 == 0:
                flush(con, buf)
                sys.stderr.write(f"  ingested {n_snap} snapshots...\n")
        flush(con, buf)

        # dimensions: latest network + latest timetable
        if state["net"]:
            con.execute("DELETE FROM stops")
            con.executemany("INSERT INTO stops VALUES (?,?,?,?,?,?)", [
                (sid, st.get("realtime_id"), st.get("name"), st.get("lat"), st.get("lon"),
                 ",".join(st.get("lines") or []))
                for sid, st in (state["net"].get("stops") or {}).items()])
        if state["sched"]:
            con.execute("DELETE FROM schedule_arrivals")
            rows = []
            for sid, entries_ in (state["sched"].get("stops") or {}).items():
                for e in entries_:
                    rows.append((sid, e["l"], e["d"], e["h"], e["t"],
                                 parse_gtfs_time(e["t"]), ",".join(e["s"])))
            con.executemany("INSERT INTO schedule_arrivals VALUES (?,?,?,?,?,?,?)", rows)
            con.execute("DELETE FROM schedule_services")
            con.executemany("INSERT INTO schedule_services VALUES (?,?,?,?,?,?)", [
                (sid, ",".join(s.get("days") or []), s.get("start_date"), s.get("end_date"),
                 ",".join(s.get("added_dates") or []), ",".join(s.get("removed_dates") or []))
                for sid, s in (state["sched"].get("services") or {}).items()])

        con.executemany("INSERT OR REPLACE INTO ingest_state VALUES (?,?)", [
            ("last_commit", entries[-1][0] if entries else ""),
            ("snapshots", str(n_snap)),
            ("estimations", str(n_est)),
            ("matched", str(n_matched)),
            ("engine", engine),
        ])
    finally:
        try:
            con.commit()
        except Exception:
            pass
        if engine == "sqlite":
            con.commit()
            con.close()

    pct = (100.0 * n_matched / n_est) if n_est else 0.0
    sys.stderr.write(f"Done: {n_snap} snapshots, {n_est} estimations, "
                     f"{n_matched} matched to schedule ({pct:.1f}%) -> {args.db}\n")

    # Portable summary report
    try:
        print("\n=== Average delay by line (min; negative = ahead of schedule) ===")
        q = ("SELECT line, COUNT(*) n, ROUND(AVG(delay_min),2) avg_delay, "
             "ROUND(100.0*SUM(CASE WHEN delay_min BETWEEN -1 AND 3 THEN 1 ELSE 0 END)"
             "/COUNT(*),1) pct_on_time "
             "FROM estimations WHERE delay_min IS NOT NULL "
             "GROUP BY line ORDER BY avg_delay DESC LIMIT 15")
        for row in con.execute(q).fetchall():
            print("  ", row)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"report skipped: {exc}\n")
    if engine == "duckdb":
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
