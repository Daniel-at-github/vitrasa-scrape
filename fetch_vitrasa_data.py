#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_vitrasa_data.py
=====================

Git-scraping toolkit for the Vitrasa (Vigo, Spain) urban bus network.
See: https://simonwillison.net/2020/Oct/9/git-scraping/

SUBCOMMANDS
-----------
  network    Download the GTFS feed and write:
               * the static network dump (lines, ordered stops, overlaps)
               * the theoretical/official timetable per stop (stop_times +
                 service calendar), for computing scheduled arrivals.
             (Invoking the script without a subcommand = `network`, so the
              original CLI keeps working, including legacy `--realtime ID,ID`.)

  schedule   Write only the theoretical timetable file.

  realtime   Query the realtime API for ALL stops (or a subset) and write one
             deterministic JSON snapshot. NO timestamp is stored in the file:
             the UTC timestamp of the git commit that first introduces the blob
             is the observation time (git-scraping pattern).

DATA SOURCES (all public, no API key)
-------------------------------------
  * Realtime estimations (ISO-8859-1 JSON, behind vigo.org):
        GET https://datos.vigo.org/vci_api_app/api2.jsp
            ?id={realtime_stop_id}&ttl=5&tipo=TRANSPORTE-ESTIMACION-PARADA
        -> {"parada":[{"latitud":..,"longitud":..,"stop_vitrasa":14135,
                       "nombre":"R\u00faa de Santo Amaro (Praza de Espa\u00f1a)"}],
            "estimaciones":[{"minutos":15,"ruta":"PRAZA AM\u00c9RICA",
                             "linea":"C1","metros":2321}, ...]}
  * Legacy HTML: http://infobus.vitrasa.es:8002/Default.aspx?parada={id}
  * GTFS zip:    https://datos.vigo.org/data/transporte/gtfs_vigo.zip
                 (stop_code P0014135 -> realtime id 14135 for api2.jsp)
  * CKAN portal: https://datos-ckan.vigo.org/group/transporte
  * Reference:   https://github.com/David-Lor/VigoBusAPI

USAGE
-----
  python3 fetch_vitrasa_data.py network --out data/vitrasa_lines.json \
          --schedule-out data/vitrasa_schedules.json
  python3 fetch_vitrasa_data.py schedule --out data/vitrasa_schedules.json
  python3 fetch_vitrasa_data.py realtime --out realtime/snapshot.json \
          --only-scheduled --window 120 --workers 8 --min-interval 0.05
  python3 fetch_vitrasa_data.py realtime --stops 14135,2440      # subset
  python3 fetch_vitrasa_data.py --realtime 14135                 # legacy spot-check

Only the Python standard library is required. Python >= 3.9 (zoneinfo).
"""

import argparse
import csv
import io
import json
import os
import sys
import threading
import time
import unicodedata
import urllib.request
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

FORMAT_VERSION = 1
SCHEDULE_FORMAT_VERSION = 1
SNAPSHOT_FORMAT_VERSION = 1
USER_AGENT = "vitrasa-network-dumper/1.1 (public open data; git scraping; for personal analysis)"

GTFS_URL = "https://datos.vigo.org/data/transporte/gtfs_vigo.zip"
REALTIME_URL = "https://datos.vigo.org/vci_api_app/api2.jsp"
REALTIME_TIPO = "TRANSPORTE-ESTIMACION-PARADA"

MADRID_TZ = ZoneInfo("Europe/Madrid")

SOURCES = {
    "realtime_estimations_api": {
        "url": REALTIME_URL,
        "method": "GET",
        "params": {"id": "<realtime_id>", "ttl": "5", "tipo": REALTIME_TIPO},
        "encoding": "ISO-8859-1",
        "returns": "next arrivals per stop: minutes, route headsign, line, meters from stop",
        "note": "realtime_id is embedded in the GTFS stop_code (P0014135 -> 14135)",
    },
    "legacy_realtime_html": "http://infobus.vitrasa.es:8002/Default.aspx?parada=<realtime_id>",
    "gtfs_feed_zip": GTFS_URL,
    "open_data_portal": "https://datos-ckan.vigo.org/group/transporte",
    "stops_open_data": "https://datos.vigo.org/data/transporte/paradas.json",
    "lines_geometry_geojson": "https://datos.vigo.org/data/transporte/lineas.geojson",
    "reference_projects": ["https://github.com/David-Lor/VigoBusAPI"],
}


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

def http_get(url: str, timeout: float = 60.0, retries: int = 3) -> bytes:
    """GET a URL and return raw bytes. Retries with a small backoff."""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001 - we want to retry on anything
            last_exc = exc
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"GET {url} failed after {retries} attempts: {last_exc}") from last_exc


def fetch_realtime(stop_realtime_id: int, timeout: float = 30.0) -> dict:
    """Live query of the realtime estimations API for one stop."""
    url = f"{REALTIME_URL}?id={stop_realtime_id}&ttl=5&tipo={REALTIME_TIPO}"
    raw = http_get(url, timeout=timeout)
    # The endpoint answers Content-Type: application/json;charset=ISO-8859-1
    return json.loads(raw.decode("latin-1"))


def _write_atomic(path: str, data: bytes) -> None:
    tmp = f"{path}.tmp"
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def get_gtfs_bytes(args) -> bytes:
    """Download the GTFS zip, with an optional on-disk cache (--gtfs-cache)."""
    cache = getattr(args, "gtfs_cache", None)
    max_age = float(getattr(args, "gtfs_cache_max_age", 4 * 3600))
    if cache and os.path.exists(cache) and (time.time() - os.path.getmtime(cache)) < max_age:
        with open(cache, "rb") as fh:
            return fh.read()
    data = http_get(GTFS_URL, timeout=args.timeout, retries=args.retries)
    if cache:
        _write_atomic(cache, data)
    return data


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def line_sort_key(name: str):
    """Natural-ish key for line names: numeric chunks compared numerically."""
    chunks = []
    num = ""
    for ch in name:
        if ch.isdigit():
            num += ch
        else:
            if num:
                chunks.append((0, int(num), ""))
                num = ""
            chunks.append((1, 0, ch))
    if num:
        chunks.append((0, int(num), ""))
    return chunks


def read_gtfs_table(zf: zipfile.ZipFile, name: str) -> csv.DictReader:
    return csv.DictReader(io.TextIOWrapper(zf.open(name), encoding="utf-8-sig"))


def parse_gtfs_time(s) -> int | None:
    """'HH:MM:SS' (H may be > 23, e.g. night buses '25:10:00') -> seconds."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        h, m, sec = s.split(":")
        return int(h) * 3600 + int(m) * 60 + int(sec)
    except ValueError:
        return None


def fmt_gtfs_time(sec: int) -> str:
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def parse_gtfs_date(s) -> date | None:
    s = (s or "").strip()
    return datetime.strptime(s, "%Y%m%d").date() if s else None


def norm_headsign(s: str) -> str:
    """Uppercase + accent-stripped + whitespace-collapsed, for matching."""
    s = unicodedata.normalize("NFD", (s or "").upper())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(s.split())


def service_day_and_second(now_local: datetime, cutoff_hour: int):
    """Map a wall-clock datetime onto (GTFS service day, seconds-since-midnight
    of that service day). Trips after midnight belong to the previous service
    day, so before `cutoff_hour` we stay on yesterday with seconds > 86400."""
    sec = now_local.hour * 3600 + now_local.minute * 60 + now_local.second
    if now_local.hour < cutoff_hour:
        return now_local.date() - timedelta(days=1), 86400 + sec
    return now_local.date(), sec


# --------------------------------------------------------------------------
# Core: static network model from the GTFS feed (unchanged behaviour)
# --------------------------------------------------------------------------

def build_network(gtfs_zip_bytes: bytes) -> dict:
    zf = zipfile.ZipFile(io.BytesIO(gtfs_zip_bytes))

    # ---- stops -----------------------------------------------------------
    stops = {}
    for row in read_gtfs_table(zf, "stops.txt"):
        stop_id = row["stop_id"].strip()
        code = row["stop_code"].strip()
        realtime_id = None
        if code.startswith("P") and code[1:].isdigit():
            realtime_id = int(code[1:])  # P0014135 -> 14135 (api2.jsp id)
        stops[stop_id] = {
            "code": code,
            "realtime_id": realtime_id,
            "name": row["stop_name"].strip(),
            "lat": round(float(row["stop_lat"]), 7),
            "lon": round(float(row["stop_lon"]), 7),
            "lines": set(),
        }

    # ---- routes -> merged lines -----------------------------------------
    routes_by_key = defaultdict(list)
    for row in read_gtfs_table(zf, "routes.txt"):
        short = row["route_short_name"].strip() or f"route-{row['route_id']}"
        routes_by_key[short].append(row)

    lines = {}
    for short, rows in routes_by_key.items():
        long_names = {r["route_long_name"].strip() for r in rows if r["route_long_name"].strip()}
        colors = {r["route_color"].strip().upper() for r in rows if r["route_color"].strip()}
        lines[short] = {
            "long_name": sorted(long_names)[0] if long_names else "",
            "color": sorted(colors)[0] if colors else "",
            "route_ids": sorted((r["route_id"].strip() for r in rows), key=int),
            "directions": {},
            "_all_stops": set(),
        }

    # ---- trips ------------------------------------------------------------
    trip_info = {}
    for row in read_gtfs_table(zf, "trips.txt"):
        route_id = row["route_id"].strip()
        key = next((k for k, v in routes_by_key.items()
                    if any(r["route_id"].strip() == route_id for r in v)), None)
        if key is None:
            continue
        direction = (row["direction_id"].strip() or "0")
        trip_info[row["trip_id"].strip()] = (key, direction, row["trip_headsign"].strip())

    # ---- stop_times -------------------------------------------------------
    trip_stops = defaultdict(list)
    for row in read_gtfs_table(zf, "stop_times.txt"):
        tid = row["trip_id"].strip()
        if tid in trip_info:
            trip_stops[tid].append((int(row["stop_sequence"]), row["stop_id"].strip()))

    # ---- per line+direction canonical stop sequence ------------------------
    best_trip = {}
    headsigns = defaultdict(Counter)
    for tid, (line_key, direction, headsign) in trip_info.items():
        seq = trip_stops.get(tid)
        if not seq:
            continue
        headsigns[(line_key, direction)][headsign] += 1
        cand = (len(seq), tid)
        cur = best_trip.get((line_key, direction))
        if cur is None or (-cand[0], cand[1]) < (-cur[0], cur[1]):
            best_trip[(line_key, direction)] = cand

    for (line_key, direction), (n_stops, trip_id) in sorted(best_trip.items()):
        ordered_ids = [sid for _, sid in sorted(trip_stops[trip_id])]
        headsign_counter = headsigns[(line_key, direction)]
        top = min(headsign_counter.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        lines[line_key]["directions"][direction] = {
            "headsign": top,
            "trip_id": trip_id,
            "stop_ids": ordered_ids,
        }
        lines[line_key]["_all_stops"].update(ordered_ids)

    # ---- membership: stop -> lines -----------------------------------------
    for line_key, line in lines.items():
        for sid in line["_all_stops"]:
            if sid in stops:
                stops[sid]["lines"].add(line_key)

    for sid, stop in stops.items():
        stop["lines"] = sorted(stop["lines"], key=line_sort_key)

    for line in lines.values():
        line.pop("_all_stops")
        line["has_service"] = bool(line["directions"])

    # ---- overlaps ----------------------------------------------------------
    line_names = sorted(lines, key=line_sort_key)
    stops_by_line = defaultdict(set)
    for sid, stop in stops.items():
        for lk in stop["lines"]:
            stops_by_line[lk].add(sid)

    overlaps = {}
    for i, a in enumerate(line_names):
        for b in line_names[i + 1:]:
            shared = stops_by_line[a] & stops_by_line[b]
            if shared:
                overlaps[f"{a}|{b}"] = sorted(shared, key=lambda s: (0, int(s)) if s.isdigit() else (1, 0, s))

    # ---- assemble ----------------------------------------------------------
    network = {
        "format_version": FORMAT_VERSION,
        "meta": {
            "description": "Vitrasa (Vigo, Spain) urban bus network: lines, ordered stops per line and overlapping stops. Deterministic dump (no timestamps) intended for git versioning.",
            "line_definition": "lines are GTFS route_short_name values, whitespace-stripped; duplicated route_ids (e.g. '15A' and '15A.') are merged under the same line.",
            "stop_sequence_rule": "per direction: stop sequence of the trip with the most stops; ties broken by smallest trip_id",
            "realtime_api": {
                "endpoint": REALTIME_URL,
                "params": {"id": "<stop.realtime_id>", "ttl": "5", "tipo": REALTIME_TIPO},
                "returns": "estimaciones[] with minutos / ruta / linea / metros and parada[] with stop name + coords",
                "encoding": "ISO-8859-1",
            },
            "sources": SOURCES,
            "counts": {
                "lines": len(lines),
                "lines_with_service": sum(1 for l in lines.values() if l["has_service"]),
                "stops": len(stops),
                "line_directions": sum(len(l["directions"]) for l in lines.values()),
                "overlapping_line_pairs": len(overlaps),
            },
        },
        "lines": lines,
        "stops": stops,
        "overlaps": overlaps,
    }
    return network


# --------------------------------------------------------------------------
# Theoretical / official timetable (from GTFS stop_times + calendar)
# --------------------------------------------------------------------------

def build_services(zf: zipfile.ZipFile) -> dict:
    """service_id -> calendar info, from calendar.txt + calendar_dates.txt."""
    services = {}
    names = set(zf.namelist())
    if "calendar.txt" in names:
        cols = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        for row in read_gtfs_table(zf, "calendar.txt"):
            services[row["service_id"].strip()] = {
                "days": {i for i, c in enumerate(cols) if (row.get(c) or "").strip() == "1"},
                "start": parse_gtfs_date(row.get("start_date")),
                "end": parse_gtfs_date(row.get("end_date")),
                "added": set(),
                "removed": set(),
            }
    if "calendar_dates.txt" in names:
        for row in read_gtfs_table(zf, "calendar_dates.txt"):
            sid = row["service_id"].strip()
            svc = services.setdefault(sid, {"days": set(), "start": None, "end": None,
                                            "added": set(), "removed": set()})
            d = parse_gtfs_date(row.get("date"))
            if d is None:
                continue
            if (row.get("exception_type") or "").strip() == "1":
                svc["added"].add(d)
            else:
                svc["removed"].add(d)
    return services


def service_active_on(svc: dict, d: date) -> bool:
    if d in svc["removed"]:
        return False
    if d in svc["added"]:
        return True
    if svc["start"] is None or svc["end"] is None:
        return False
    return svc["start"] <= d <= svc["end"] and d.weekday() in svc["days"]


def build_stop_schedule(zf: zipfile.ZipFile) -> dict:
    """stop_id -> sorted list of scheduled arrivals:
       {"sec": int, "t": "HH:MM:SS", "l": line, "d": dir, "h": headsign, "s": [service_ids]}
       Exact duplicates (same time/line/dir/headsign across trips) are merged."""
    route_short = {}
    for row in read_gtfs_table(zf, "routes.txt"):
        rid = row["route_id"].strip()
        route_short[rid] = row["route_short_name"].strip() or f"route-{rid}"

    trips = {}
    for row in read_gtfs_table(zf, "trips.txt"):
        rid = row["route_id"].strip()
        if rid not in route_short:
            continue
        trips[row["trip_id"].strip()] = (
            route_short[rid],
            (row.get("direction_id") or "").strip() or "0",
            (row.get("trip_headsign") or "").strip(),
            row["service_id"].strip(),
        )

    merged = defaultdict(lambda: defaultdict(set))   # stop -> (sec,l,d,h) -> {svc}
    for row in read_gtfs_table(zf, "stop_times.txt"):
        trip = trips.get(row["trip_id"].strip())
        if trip is None:
            continue
        sec = parse_gtfs_time(row.get("arrival_time"))
        if sec is None:
            sec = parse_gtfs_time(row.get("departure_time"))
        if sec is None:
            continue
        l, d, h, svc = trip
        merged[row["stop_id"].strip()][(sec, l, d, h)].add(svc)

    out = {}
    for sid, keys in merged.items():
        entries = [{"sec": k[0], "t": fmt_gtfs_time(k[0]), "l": k[1], "d": k[2],
                    "h": k[3], "s": sorted(v)} for k, v in keys.items()]
        entries.sort(key=lambda e: (e["sec"], line_sort_key(e["l"]), e["d"], e["h"]))
        out[sid] = entries
    return out


def build_schedule_doc(gtfs_bytes: bytes) -> dict:
    zf = zipfile.ZipFile(io.BytesIO(gtfs_bytes))
    services = build_services(zf)
    stop_sched = build_stop_schedule(zf)

    services_doc = {}
    for sid in sorted(services):
        svc = services[sid]
        services_doc[sid] = {
            "days": sorted(svc["days"]),   # 0=Monday .. 6=Sunday
            "start_date": svc["start"].strftime("%Y%m%d") if svc["start"] else None,
            "end_date": svc["end"].strftime("%Y%m%d") if svc["end"] else None,
            "added_dates": sorted(d.strftime("%Y%m%d") for d in svc["added"]),
            "removed_dates": sorted(d.strftime("%Y%m%d") for d in svc["removed"]),
        }

    stops_doc = {}
    n_entries = 0
    for sid in sorted(stop_sched):
        rows = []
        for e in stop_sched[sid]:
            rows.append({"t": e["t"], "l": e["l"], "d": e["d"], "h": e["h"], "s": e["s"]})
        stops_doc[sid] = rows
        n_entries += len(rows)

    return {
        "format_version": SCHEDULE_FORMAT_VERSION,
        "meta": {
            "description": "Theoretical/official arrival times per Vitrasa stop, derived from the official GTFS feed (stop_times.txt + calendar). Deterministic dump (no timestamps) intended for git versioning.",
            "time_semantics": "'t' is GTFS local time (Europe/Madrid), may exceed 24:00 for night services: a '25:30' entry on service day D departs D+1 at 01:30. Service day flips at ~04:00.",
            "services": "days: 0=Monday..6=Sunday; added_dates/removed_dates are calendar_dates.txt exceptions (1=add, 2=remove).",
            "sources": SOURCES,
            "counts": {"services": len(services_doc), "stops": len(stops_doc), "entries": n_entries},
        },
        "services": services_doc,
        "stops": stops_doc,
    }


# --------------------------------------------------------------------------
# Realtime all-stops snapshot
# --------------------------------------------------------------------------

class RateLimiter:
    """Global minimum interval between request starts across all workers."""

    def __init__(self, min_interval: float):
        self.min_interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.min_interval
        if delay > 0:
            time.sleep(delay)


def _to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def normalize_stop_response(data: dict) -> dict:
    """Compact, deterministic per-stop payload.
       'e' = estimaciones: [minutos, ruta, linea, metros] sorted by arrival.
       'p' = parada: {n: nombre, lat, lon, sv: stop_vitrasa}."""
    e = []
    for x in (data.get("estimaciones") or []):
        if not isinstance(x, dict):
            continue
        try:
            minutos = int(x.get("minutos"))
        except (TypeError, ValueError):
            continue
        metros = x.get("metros")
        try:
            metros = int(metros)
        except (TypeError, ValueError):
            metros = None
        e.append([minutos, (x.get("ruta") or "").strip(), (x.get("linea") or "").strip(), metros])
    e.sort(key=lambda r: (r[0], r[2], r[1]))

    p = None
    parada = data.get("parada")
    if isinstance(parada, list) and parada and isinstance(parada[0], dict):
        q = parada[0]
        p = {"n": (q.get("nombre") or "").strip(),
             "lat": _to_float(q.get("latitud")),
             "lon": _to_float(q.get("longitud")),
             "sv": q.get("stop_vitrasa")}
    return {"e": e, "p": p}


def cmd_realtime(args) -> int:
    sys.stderr.write(f"Downloading GTFS feed: {GTFS_URL}\n")
    gtfs_bytes = get_gtfs_bytes(args)
    zf = zipfile.ZipFile(io.BytesIO(gtfs_bytes))

    alias = {}        # realtime_id (str) -> GTFS stop_id
    total_stops = 0
    for row in read_gtfs_table(zf, "stops.txt"):
        total_stops += 1
        code = (row.get("stop_code") or "").strip()
        if code.startswith("P") and code[1:].isdigit():
            alias[code[1:]] = row["stop_id"].strip()
    by_stop = {sid: rt for rt, sid in alias.items()}
    skipped_no_realtime_id = total_stops - len(alias)

    def resolve(tok: str):
        if tok in alias:
            return tok
        if tok in by_stop:
            return by_stop[tok]
        return tok if tok.isdigit() else None

    if args.stops.strip():
        rt_ids = sorted({r for r in (resolve(t.strip()) for t in args.stops.split(",") if t.strip())
                         if r is not None}, key=int)
    else:
        rt_ids = sorted(alias, key=int)

    # Optionally query only stops with a scheduled arrival in the near window
    skipped_no_scheduled = 0
    if args.only_scheduled:
        services = build_services(zf)
        stop_sched = build_stop_schedule(zf)
        now_local = datetime.now(MADRID_TZ)
        svc_day, now_sec = service_day_and_second(now_local, args.day_cutoff)
        active = {sid for sid, svc in services.items() if service_active_on(svc, svc_day)}
        window_end = now_sec + int(args.window * 60)
        keep = set()
        for rt, stop_id in alias.items():
            for e in stop_sched.get(stop_id, []):
                if now_sec - 15 * 60 <= e["sec"] <= window_end and any(s in active for s in e["s"]):
                    keep.add(rt)
                    break
        skipped_no_scheduled = len(rt_ids) - len(keep & set(rt_ids))
        rt_ids = [rt for rt in rt_ids if rt in keep]

    if args.max_stops:
        rt_ids = rt_ids[:args.max_stops]

    sys.stderr.write(f"Querying realtime API for {len(rt_ids)} stops "
                     f"(workers={args.workers}, min_interval={args.min_interval}s)...\n")
    limiter = RateLimiter(args.min_interval)
    results, errors = {}, {}

    def work(rt: str):
        limiter.wait()
        try:
            return rt, normalize_stop_response(fetch_realtime(int(rt), timeout=args.timeout)), None
        except Exception as exc:  # noqa: BLE001
            return rt, None, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        for rt, data, err in ex.map(work, rt_ids):
            if err is not None:
                errors[rt] = err
            else:
                results[rt] = data

    ok = len(results)
    total_est = sum(len(v["e"]) for v in results.values())
    with_est = sum(1 for v in results.values() if v["e"])

    doc = {
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "meta": {
            "description": "Vitrasa realtime snapshot: next-arrival estimations for every queried stop. Git-scraped: one commit per run.",
            "observation_time": "intentionally NOT stored here - the UTC timestamp of the git commit that introduces this blob is the observation time.",
            "estimacion_schema": ["minutos", "ruta", "linea", "metros"],
            "parada_schema": {"n": "nombre", "lat": "latitud", "lon": "longitud", "sv": "stop_vitrasa"},
            "endpoint": REALTIME_URL,
            "params": {"id": "<realtime_stop_id>", "ttl": "5", "tipo": REALTIME_TIPO},
            "encoding": "ISO-8859-1",
            "sources": SOURCES,
            "counts": {
                "requested": len(rt_ids),
                "ok": ok,
                "failed": len(errors),
                "skipped_no_realtime_id": skipped_no_realtime_id,
                "skipped_no_scheduled_service": skipped_no_scheduled,
                "stops_with_estimations": with_est,
                "estimaciones": total_est,
            },
        },
        "stops": results,
        "errors": dict(sorted(errors.items())),
    }

    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")

    sys.stderr.write(
        f"Snapshot written to {args.out}: requested={len(rt_ids)} ok={ok} failed={len(errors)} "
        f"stops_with_estimations={with_est} estimaciones={total_est}\n")
    if ok == 0:
        sys.stderr.write("ERROR: every request failed; not writing a useful snapshot.\n")
        return 3
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _live_query(tokens, timeout: float, retries: int, alias_map: dict) -> dict:
    results = {}
    for tok in tokens:
        rt = alias_map.get(tok, int(tok) if tok.isdigit() else None)
        if rt is None:
            results[tok] = {"error": "unknown stop id"}
            continue
        results[tok] = {"realtime_id": rt, **fetch_realtime(rt, timeout=timeout)}
    return results


def cmd_network(args) -> int:
    sys.stderr.write(f"Downloading GTFS feed: {GTFS_URL}\n")
    gtfs_bytes = get_gtfs_bytes(args)
    sys.stderr.write(f"  {len(gtfs_bytes) / 1e6:.1f} MB received, parsing...\n")

    network = build_network(gtfs_bytes)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(network, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    sys.stderr.write(f"Wrote {args.out}\n")

    if not args.no_schedule:
        out = args.schedule_out
        if out is None:
            base, _ = os.path.splitext(args.out)
            out = base + "_schedules.json"
        sys.stderr.write(f"Building theoretical timetable -> {out}\n")
        schedule_doc = build_schedule_doc(gtfs_bytes)
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(schedule_doc, fh, indent=2, sort_keys=True, ensure_ascii=False)
            fh.write("\n")
        c = schedule_doc["meta"]["counts"]
        sys.stderr.write(f"Wrote {out}: {c['stops']} stops, {c['entries']} scheduled arrivals, "
                         f"{c['services']} services\n")

    counts = network["meta"]["counts"]
    sys.stderr.write("Done: {lines} lines, {stops} stops, {dirs} line-directions, {pairs} overlaps\n"
                     .format(lines=counts["lines"], stops=counts["stops"],
                             dirs=counts["line_directions"], pairs=counts["overlapping_line_pairs"]))

    if args.realtime.strip():  # legacy spot-check behaviour
        zf = zipfile.ZipFile(io.BytesIO(gtfs_bytes))
        alias_map = {}
        for row in read_gtfs_table(zf, "stops.txt"):
            code = (row.get("stop_code") or "").strip()
            if code.startswith("P") and code[1:].isdigit():
                alias_map[row["stop_id"].strip()] = int(code[1:])
        tokens = [t.strip() for t in args.realtime.split(",") if t.strip()]
        print(json.dumps(_live_query(tokens, args.timeout, args.retries, alias_map),
                         indent=2, sort_keys=True, ensure_ascii=False))
    return 0


def cmd_schedule(args) -> int:
    sys.stderr.write(f"Downloading GTFS feed: {GTFS_URL}\n")
    gtfs_bytes = get_gtfs_bytes(args)
    doc = build_schedule_doc(gtfs_bytes)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    c = doc["meta"]["counts"]
    sys.stderr.write(f"Wrote {args.out}: {c['stops']} stops, {c['entries']} arrivals, {c['services']} services\n")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-"):
        argv = ["network"] + argv   # backward compatible with the old flag-based CLI

    parser = argparse.ArgumentParser(
        prog="fetch_vitrasa_data.py",
        description="Vitrasa (Vigo) git-scraping fetcher: static network, theoretical "
                    "timetable and all-stops realtime snapshots.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_gtfs_cache(p):
        p.add_argument("--gtfs-cache", default=None,
                       help="path to cache the GTFS zip (reuse within/across runs)")
        p.add_argument("--gtfs-cache-max-age", type=float, default=4 * 3600,
                       help="cache freshness in seconds (default 14400)")

    p_net = sub.add_parser("network", help="write the static network dump (+ timetable)")
    p_net.add_argument("--out", default="vitrasa_lines.json")
    p_net.add_argument("--schedule-out", default=None)
    p_net.add_argument("--no-schedule", action="store_true")
    p_net.add_argument("--realtime", default="",
                       help="legacy: comma-separated stop ids -> live-query and print to stdout")
    p_net.add_argument("--timeout", type=float, default=60.0)
    p_net.add_argument("--retries", type=int, default=3)
    add_gtfs_cache(p_net)

    p_sched = sub.add_parser("schedule", help="write only the theoretical timetable")
    p_sched.add_argument("--out", default="vitrasa_schedules.json")
    p_sched.add_argument("--timeout", type=float, default=60.0)
    p_sched.add_argument("--retries", type=int, default=3)
    add_gtfs_cache(p_sched)

    p_rt = sub.add_parser("realtime", help="all-stops realtime snapshot (git-scraped file)")
    p_rt.add_argument("--out", default="realtime/snapshot.json")
    p_rt.add_argument("--stops", default="", help="comma-separated realtime ids / GTFS stop ids (default: all)")
    p_rt.add_argument("--only-scheduled", action="store_true",
                      help="skip stops with no scheduled arrival within --window minutes (big saver at night)")
    p_rt.add_argument("--window", type=float, default=120.0,
                      help="scheduled-arrival lookahead window in minutes (default 120)")
    p_rt.add_argument("--day-cutoff", type=int, default=4,
                      help="hour at which the GTFS service day flips (default 4)")
    p_rt.add_argument("--workers", type=int, default=8)
    p_rt.add_argument("--min-interval", type=float, default=0.05,
                      help="global min seconds between requests (be polite; default 0.05)")
    p_rt.add_argument("--max-stops", type=int, default=0, help="limit stops (testing)")
    p_rt.add_argument("--timeout", type=float, default=20.0)
    p_rt.add_argument("--retries", type=int, default=2)
    add_gtfs_cache(p_rt)

    args = parser.parse_args(argv)
    return {"network": cmd_network, "schedule": cmd_schedule, "realtime": cmd_realtime}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
