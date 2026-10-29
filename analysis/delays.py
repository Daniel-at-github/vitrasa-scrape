# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "altair>=5.4",
#     "duckdb>=1.3",
#     "marimo>=0.18.1",
#     "pandas>=2.2",
# ]
# ///
"""Delay analysis for the Vitrasa git-scraping dataset.

Build the database first (needs full git history):
    uv run consolidate_snapshots.py --db vitrasa.duckdb

Then either edit interactively:
    uv run marimo edit analysis/delays.py
or run it headless (uses the UI defaults):
    uv run analysis/delays.py
"""

import marimo

__generated_with = "0.18.1"
app = marimo.App(width="medium", app_title="Vitrasa delays")


@app.cell
def _():
    import altair as alt
    import duckdb
    import marimo as mo
    import pandas as pd
    return alt, duckdb, mo, pd


@app.cell
def _(mo):
    mo.md(
        """
        # 🚌 Vitrasa delay analysis
        Interactive view over the DuckDB dataset built by
        `consolidate_snapshots.py` from the git-scraped realtime snapshots.

        `delay_min = (commit_time + minutos) − scheduled_time` → **positive = late**.
        """
    )
    return


@app.cell
def _(mo):
    db_path = mo.ui.text(value="vitrasa.duckdb", label="DuckDB file", full_width=True)
    db_path
    return (db_path,)


@app.cell
def _(db_path, duckdb, mo):
    import os

    mo.stop(
        not os.path.exists(db_path.value),
        mo.md(
            f"⚠️ `{db_path.value}` not found — build it first with "
            "`uv run consolidate_snapshots.py --db vitrasa.duckdb`."
        ),
    )
    con = duckdb.connect(db_path.value, read_only=True)
    return (con,)


@app.cell
def _(con, mo):
    coverage = mo.sql(
        f"""
        SELECT
            (SELECT count(*) FROM snapshots)      AS n_snapshots,
            (SELECT min(ts_local) FROM snapshots) AS first_snapshot,
            (SELECT max(ts_local) FROM snapshots) AS last_snapshot,
            (SELECT count(*) FROM estimations)    AS n_estimations,
            (SELECT count(*) FROM v_delays)       AS n_matched
        """,
        engine=con,
    )
    coverage
    return (coverage,)


@app.cell
def _(mo):
    days = mo.ui.slider(start=1, stop=365, value=30, label="Look-back window (days)")
    days
    return (days,)


@app.cell
def _(days):
    from datetime import datetime, timedelta, timezone

    cutoff_utc = (
        (datetime.now(timezone.utc) - timedelta(days=days.value))
        .replace(tzinfo=None, microsecond=0)
        .isoformat(sep=" ")
    )
    return (cutoff_utc,)


@app.cell
def _(con, mo):
    _df = mo.sql(
        "SELECT DISTINCT line FROM estimations WHERE line IS NOT NULL ORDER BY line",
        engine=con,
    )
    line_filter = mo.ui.dropdown(
        options=["(all)"] + list(_df["line"]), value="(all)", label="Line"
    )
    line_filter
    return (line_filter,)


@app.cell
def _(line_filter):
    line_clause = "TRUE" if line_filter.value == "(all)" else f"line = '{line_filter.value}'"
    return (line_clause,)


@app.cell
def _(con, cutoff_utc, line_clause, mo):
    by_line = mo.sql(
        f"""
        SELECT
            line,
            count(*) AS samples,
            round(avg(delay_min), 2) AS avg_delay_min,
            quantile_cont(delay_min, 0.5) AS p50_delay_min,
            quantile_cont(delay_min, 0.9) AS p90_delay_min,
            round(
                100.0 * sum(CASE WHEN delay_min BETWEEN -1 AND 3 THEN 1 ELSE 0 END)
                / count(*), 1
            ) AS pct_on_time
        FROM v_delays
        WHERE {line_clause}
          AND ts_utc > TIMESTAMP '{cutoff_utc}'
        GROUP BY line
        HAVING count(*) >= 30
        ORDER BY p90_delay_min DESC
        """,
        engine=con,
    )
    by_line
    return (by_line,)


@app.cell
def _(alt, by_line, mo):
    _chart = (
        alt.Chart(by_line, title="p90 delay by line (minutes late)")
        .mark_bar()
        .encode(
            x=alt.X("line:N", sort="-y", title="Line"),
            y=alt.Y("p90_delay_min:Q", title="p90 delay (min)"),
            color=alt.Color(
                "pct_on_time:Q",
                title="% on time",
                scale=alt.Scale(scheme="redyellowgreen", domain=[0, 100]),
            ),
            tooltip=[
                alt.Tooltip("line:N", title="Line"),
                alt.Tooltip("samples:Q", title="Samples"),
                alt.Tooltip("avg_delay_min:Q", title="Avg (min)"),
                alt.Tooltip("p50_delay_min:Q", title="p50 (min)"),
                alt.Tooltip("p90_delay_min:Q", title="p90 (min)"),
                alt.Tooltip("pct_on_time:Q", title="% on time"),
            ],
        )
        .properties(height=320)
    )
    by_line_chart = mo.ui.altair_chart(_chart)
    by_line_chart
    return (by_line_chart,)


@app.cell
def _(con, cutoff_utc, line_clause, mo):
    by_hour = mo.sql(
        f"""
        SELECT
            hour(ts_local) AS hour_of_day,
            count(*) AS samples,
            round(avg(delay_min), 2) AS avg_delay_min,
            quantile_cont(delay_min, 0.9) AS p90_delay_min
        FROM v_delays
        WHERE {line_clause}
          AND ts_utc > TIMESTAMP '{cutoff_utc}'
        GROUP BY 1
        ORDER BY 1
        """,
        engine=con,
    )
    by_hour
    return (by_hour,)


@app.cell
def _(alt, by_hour, mo):
    _chart = (
        alt.Chart(by_hour, title="Delay by hour of day (min late)")
        .transform_fold(
            ["avg_delay_min", "p90_delay_min"],
            as_=["metric", "delay_min"],
        )
        .mark_line(point=True)
        .encode(
            x=alt.X("hour_of_day:O", title="Hour of day (local)"),
            y=alt.Y("delay_min:Q", title="Delay (min)"),
            color=alt.Color(
                "metric:N",
                title="",
                sort=["avg_delay_min", "p90_delay_min"],
                scale=alt.Scale(domain=["avg_delay_min", "p90_delay_min"],
                                range=["#2a9d8f", "#e76f51"]),
            ),
            tooltip=["hour_of_day:O", "samples:Q", "avg_delay_min:Q", "p90_delay_min:Q"],
        )
        .properties(height=300)
    )
    hour_chart = mo.ui.altair_chart(_chart)
    hour_chart
    return (hour_chart,)


@app.cell
def _(con, cutoff_utc, line_clause, mo):
    daily = mo.sql(
        f"""
        SELECT
            CAST(ts_local AS DATE) AS day,
            count(*) AS samples,
            round(avg(delay_min), 2) AS avg_delay_min
        FROM v_delays
        WHERE {line_clause}
          AND ts_utc > TIMESTAMP '{cutoff_utc}'
        GROUP BY 1
        ORDER BY 1
        """,
        engine=con,
    )
    daily
    return (daily,)


@app.cell
def _(alt, daily, mo):
    _chart = (
        alt.Chart(daily, title="Average delay over time (min late)")
        .mark_line(point=True)
        .encode(
            x=alt.X("day:T", title="Day"),
            y=alt.Y("avg_delay_min:Q", title="Avg delay (min)"),
            tooltip=["day:T", "samples:Q", "avg_delay_min:Q"],
        )
        .properties(height=280)
    )
    daily_chart = mo.ui.altair_chart(_chart)
    daily_chart
    return (daily_chart,)


@app.cell
def _(con, cutoff_utc, mo):
    heat = mo.sql(
        f"""
        SELECT
            line,
            hour(ts_local) AS hour_of_day,
            round(avg(delay_min), 2) AS avg_delay_min,
            count(*) AS samples
        FROM v_delays
        WHERE ts_utc > TIMESTAMP '{cutoff_utc}'
        GROUP BY 1, 2
        """,
        engine=con,
    )
    heat
    return (heat,)


@app.cell
def _(alt, heat, mo):
    _chart = (
        alt.Chart(heat, title="Avg delay (min) by line × hour of day")
        .mark_rect()
        .encode(
            x=alt.X("hour_of_day:O", title="Hour of day (local)"),
            y=alt.Y("line:N", title="Line"),
            color=alt.Color(
                "avg_delay_min:Q",
                title="avg delay (min)",
                scale=alt.Scale(scheme="redblue", domain=[-5, 15]),
            ),
            tooltip=["line:N", "hour_of_day:O", "avg_delay_min:Q", "samples:Q"],
        )
        .properties(height=600)
    )
    heat_chart = mo.ui.altair_chart(_chart)
    heat_chart
    return (heat_chart,)


@app.cell
def _(con, cutoff_utc, line_clause, mo):
    worst_stops = mo.sql(
        f"""
        SELECT
            stop_id,
            any_value(stop_name) AS stop_name,
            count(*) AS samples,
            round(avg(delay_min), 2) AS avg_delay_min,
            quantile_cont(delay_min, 0.9) AS p90_delay_min
        FROM v_delays
        WHERE {line_clause}
          AND ts_utc > TIMESTAMP '{cutoff_utc}'
        GROUP BY stop_id
        HAVING count(*) >= 30
        ORDER BY avg_delay_min DESC
        LIMIT 25
        """,
        engine=con,
    )
    worst_stops
    return (worst_stops,)


@app.cell
def _(mo):
    mo.md(
        """
        ---
        **Caveats**

        * `delay_min` is *realtime prediction − schedule* sampled every ~15 min — a
          robust proxy for delays, not an exact arrival-event measurement. GitHub's
          cron can slip a few minutes; the commit timestamp is the source of truth.
        * `match_type = headsign` matched the API's `ruta` against the GTFS headsign;
          `line` fell back to nearest same-line arrival. Filter on it if you only
          trust exact matches.
        * The notebook is read-only (`read_only=True`); re-run the consolidator
          after new snapshot commits to refresh the database.
        """
    )
    return


if __name__ == "__main__":
    app.run()
