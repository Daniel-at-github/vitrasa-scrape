# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "altair>=5.4",
#     "duckdb>=1.3",
#     "ipython",
#     "marimo>=0.18.1",
#     "pandas>=2.2",
# ]
# ///
"""Análisis de retrasos de Vitrasa (Vigo).

Requiere `vitrasa.duckdb` en la raíz del repositorio, generado con
    uv run consolidate_vitrasa.py --db vitrasa.duckdb

Abrir para editar:  uv run marimo edit analysis/delays.py
"""

import marimo

__generated_with = "0.18.1"
app = marimo.App(width="medium", app_title="Vitrasa: retrasos")


@app.cell
def _():
    from pathlib import Path

    import altair as alt
    import duckdb
    import marimo as mo

    return Path, alt, duckdb, mo


@app.cell
def _():
    # Un bus cuenta como "cercano" si su predicción es de NEAR_MAX minutos o menos.
    NEAR_MAX = 10
    # Mínimo de predicciones para mostrar un grupo (línea, parada, hora).
    MIN_N = 30

    LANG = {"Español": "es", "English": "en", "Galego": "gl"}

    T = {
        "es": {
            "title": "🚌 Retrasos de Vitrasa (Vigo)",
            "intro": "Qué tan puntuales son los autobuses, según las predicciones en tiempo real que muestran las paradas. Cada apartado explica qué se mide.",
            "ontime_label": "**Puntual hasta (minutos de retraso)**",
            "ontime_help": "Un bus cuenta como puntual si llega entre 1 minuto antes y este número de minutos tarde. Por defecto: 3.",
            "no_db": "No encuentro `vitrasa.duckdb`. Genéralo con `uv run consolidate_vitrasa.py --db vitrasa.duckdb`.",
            "headline_title": "Lo esencial",
            "k_typical": "Retraso típico",
            "k_ontime": "Buses puntuales",
            "k_p90": "1 de cada 10 llega más tarde de",
            "headline_caption": "Basado en {n} predicciones de buses a {near} minutos o menos de la parada.",
            "lines_title": "Puntualidad por línea",
            "lines_axis": "% de buses puntuales",
            "line_axis": "Línea",
            "lines_caption": "Más puntual: **{best}** ({best_pct} %). Menos puntual: **{worst}** ({worst_pct} %). Solo líneas con al menos {min_n} predicciones.",
            "hours_title": "Retraso típico según la hora",
            "hours_axis_x": "Hora del día",
            "hours_axis_y": "Retraso típico (min)",
            "legend": "Tipo de día",
            "weekday": "Laborable",
            "weekend": "Fin de semana",
            "hours_caption": "Retraso típico = el valor central: la mitad de los buses llega más tarde que eso.",
            "stops_title": "Paradas con más retraso",
            "stops_caption": "Paradas con al menos {min_n} predicciones, ordenadas por retraso medio.",
            "col_stop": "Parada",
            "col_avg": "Retraso medio (min)",
            "col_n": "Predicciones",
            "countdown_title": "¿Es fiable la cuenta atrás de la parada?",
            "countdown_text": "Entre dos consultas separadas unos 15 minutos, la hora prevista de llegada suele retrasarse {med} min. Una de cada 10 veces se retrasa más de {p90} min.",
            "part2_title": "Explora",
            "part2_intro": "Filtra por línea, parada, periodo o tipo de día. Estos filtros solo afectan a este apartado.",
            "f_lines": "Líneas (vacío = todas)",
            "f_stops": "Paradas (vacío = todas)",
            "f_days": "Periodo (últimos N días)",
            "f_daytype": "Tipo de día",
            "all_days": "Todos",
            "wd": "Laborables",
            "we": "Fin de semana",
            "f_summary": "Con estos filtros: **{n}** predicciones · retraso típico **{median} min** · puntuales **{ontime} %** · 1 de cada 10 más de **{p90} min** tarde.",
            "no_data": "Sin datos con estos filtros.",
            "map_title": "Retraso típico por parada (mapa)",
            "legend_delay": "Retraso típico (min)",
            "map_caption": "Naranja = llega tarde · azul = llega pronto · gris = a tiempo. Mapa sin fondo: solo puntos con coordenadas.",
            "filt_hour_title": "Retraso típico por hora (con filtros)",
            "n_label": "Predicciones",
            "part3_title": "Cómo se ha medido",
            "dist_title": "¿Por qué solo contar buses cercanos?",
            "dist_axis": "Distancia a la parada",
            "dist_caption": "Cuando el bus está lejos, la predicción apenas se diferencia del horario oficial, así que el retraso sale casi cero. Por eso las cifras principales usan solo buses a 10 minutos o menos.",
            "health_title": "Estado de los datos",
            "health_text": "- {n} consultas, del {first} al {last}\n- Huecos de más de 30 min entre consultas: {gaps}\n- Consultas con errores en alguna parada: {errors}",
            "meth_title": "Definiciones",
            "meth_text": "- **Retraso** = predicción de la pantalla menos el horario oficial. No es una hora de llegada medida en el bus.\n- **Retraso típico** = la mediana: la mitad de los buses llega más tarde y la otra mitad antes.\n- **Puntual** = entre 1 minuto antes y el umbral elegido arriba.\n- Las consultas son cada ~15 min, así que un mismo bus puede aparecer varias veces.\n- En el {line_pct} % de las comparaciones no coincidió el destino y se usó el horario más cercano de la misma línea.\n- Solo {days} días de datos: sirve para ver horas y tipos de día, no tendencias largas.",
            "dec": ",",
        },
        "en": {
            "title": "🚌 Vitrasa delays (Vigo)",
            "intro": "How punctual the buses are, based on the real-time predictions shown at the stops. Each section explains what is measured.",
            "ontime_label": "**On time up to (minutes late)**",
            "ontime_help": "A bus counts as on time if it arrives between 1 minute early and this many minutes late. Default: 3.",
            "no_db": "I can't find `vitrasa.duckdb`. Build it with `uv run consolidate_vitrasa.py --db vitrasa.duckdb`.",
            "headline_title": "The essentials",
            "k_typical": "Typical delay",
            "k_ontime": "On-time buses",
            "k_p90": "1 bus in 10 is later than",
            "headline_caption": "Based on {n} predictions for buses {near} minutes or less from the stop.",
            "lines_title": "Punctuality by line",
            "lines_axis": "% of buses on time",
            "line_axis": "Line",
            "lines_caption": "Most punctual: **{best}** ({best_pct}%). Least punctual: **{worst}** ({worst_pct}%). Only lines with at least {min_n} predictions.",
            "hours_title": "Typical delay by hour",
            "hours_axis_x": "Hour of day",
            "hours_axis_y": "Typical delay (min)",
            "legend": "Day type",
            "weekday": "Weekday",
            "weekend": "Weekend",
            "hours_caption": "Typical delay = the middle value: half of the buses are later than this.",
            "stops_title": "Stops with the largest delays",
            "stops_caption": "Stops with at least {min_n} predictions, sorted by average delay.",
            "col_stop": "Stop",
            "col_avg": "Average delay (min)",
            "col_n": "Predictions",
            "countdown_title": "Can you trust the stop countdown?",
            "countdown_text": "Between two checks about 15 minutes apart, the predicted arrival usually moves later by {med} min. 1 time in 10 it moves later by more than {p90} min.",
            "part2_title": "Explore",
            "part2_intro": "Filter by line, stop, period or day type. These filters only affect this section.",
            "f_lines": "Lines (empty = all)",
            "f_stops": "Stops (empty = all)",
            "f_days": "Period (last N days)",
            "f_daytype": "Day type",
            "all_days": "All",
            "wd": "Weekdays",
            "we": "Weekend",
            "f_summary": "With these filters: **{n}** predictions · typical delay **{median} min** · on time **{ontime}%** · 1 in 10 more than **{p90} min** late.",
            "no_data": "No data with these filters.",
            "map_title": "Typical delay by stop (map)",
            "legend_delay": "Typical delay (min)",
            "map_caption": "Orange = late · blue = early · grey = on time. Map without background: points only.",
            "filt_hour_title": "Typical delay by hour (filtered)",
            "n_label": "Predictions",
            "part3_title": "How this was measured",
            "dist_title": "Why only count buses that are close?",
            "dist_axis": "Distance to stop",
            "dist_caption": "When a bus is far away, its prediction barely differs from the official timetable, so the delay shows near zero. That is why the main figures use only buses 10 minutes away or less.",
            "health_title": "Data status",
            "health_text": "- {n} checks, from {first} to {last}\n- Gaps of more than 30 min between checks: {gaps}\n- Checks with errors at some stops: {errors}",
            "meth_title": "Definitions",
            "meth_text": "- **Delay** = the screen's prediction minus the official timetable. It is not a recorded arrival time.\n- **Typical delay** = the median: half of the buses are later, half are earlier.\n- **On time** = between 1 minute early and the threshold you chose above.\n- Checks happen about every 15 min, so the same bus can appear several times.\n- In {line_pct} % of comparisons the destination did not match, so the nearest same-line timetable entry was used.\n- Only {days} days of data: good for hours and weekday vs weekend, not for long-term trends.",
            "dec": ".",
        },
        "gl": {
            "title": "🚌 Atrasos de Vitrasa (Vigo)",
            "intro": "Ata que punto son puntuais os autobuses, segundo as predicións en tempo real que mostran as paradas. Cada apartado explica o que se mide.",
            "ontime_label": "**Puntual ata (minutos de atraso)**",
            "ontime_help": "Un bus conta como puntual se chega entre 1 minuto antes e este número de minutos tarde. Por defecto: 3.",
            "no_db": "Non atopo `vitrasa.duckdb`. Xérao con `uv run consolidate_vitrasa.py --db vitrasa.duckdb`.",
            "headline_title": "O esencial",
            "k_typical": "Atraso típico",
            "k_ontime": "Buses puntuais",
            "k_p90": "1 de cada 10 chega máis tarde de",
            "headline_caption": "Baseado en {n} predicións de buses a {near} minutos ou menos da parada.",
            "lines_title": "Puntualidade por liña",
            "lines_axis": "% de buses puntuais",
            "line_axis": "Liña",
            "lines_caption": "Máis puntual: **{best}** ({best_pct} %). Menos puntual: **{worst}** ({worst_pct} %). Só liñas con polo menos {min_n} predicións.",
            "hours_title": "Atraso típico segundo a hora",
            "hours_axis_x": "Hora do día",
            "hours_axis_y": "Atraso típico (min)",
            "legend": "Tipo de día",
            "weekday": "Laborable",
            "weekend": "Fin de semana",
            "hours_caption": "Atraso típico = o valor central: a metade dos buses chega máis tarde que iso.",
            "stops_title": "Paradas con maior atraso",
            "stops_caption": "Paradas con polo menos {min_n} predicións, ordenadas por atraso medio.",
            "col_stop": "Parada",
            "col_avg": "Atraso medio (min)",
            "col_n": "Predicións",
            "countdown_title": "Son fiables as contas atrás da parada?",
            "countdown_text": "Entre dúas consultas separadas uns 15 minutos, a hora prevista de chegada adoita correrse {med} min máis tarde. Unha de cada 10 veces córrese máis de {p90} min.",
            "part2_title": "Explora",
            "part2_intro": "Filtra por liña, parada, período ou tipo de día. Estes filtros só afectan a este apartado.",
            "f_lines": "Liñas (baleiro = todas)",
            "f_stops": "Paradas (baleiro = todas)",
            "f_days": "Período (últimos N días)",
            "f_daytype": "Tipo de día",
            "all_days": "Todos",
            "wd": "Laborables",
            "we": "Fin de semana",
            "f_summary": "Con estes filtros: **{n}** predicións · atraso típico **{median} min** · puntuais **{ontime} %** · 1 de cada 10 máis de **{p90} min** tarde.",
            "no_data": "Sen datos con estes filtros.",
            "map_title": "Atraso típico por parada (mapa)",
            "legend_delay": "Atraso típico (min)",
            "map_caption": "Laranxa = chega tarde · azul = chega cedo · gris = a tempo. Mapa sen fondo: só puntos con coordenadas.",
            "filt_hour_title": "Atraso típico por hora (con filtros)",
            "n_label": "Predicións",
            "part3_title": "Como se mediu",
            "dist_title": "Por que só contar buses próximos?",
            "dist_axis": "Distancia á parada",
            "dist_caption": "Cando o bus está lonxe, a predición apenas difire do horario oficial, así que o atraso sae case cero. Por iso as cifras principais usan só buses a 10 minutos ou menos.",
            "health_title": "Estado dos datos",
            "health_text": "- {n} consultas, do {first} ao {last}\n- Ocos de máis de 30 min entre consultas: {gaps}\n- Consultas con erros nalgunha parada: {errors}",
            "meth_title": "Definicións",
            "meth_text": "- **Atraso** = predición da pantalla menos o horario oficial. Non é unha hora de chegada medida no bus.\n- **Atraso típico** = a mediana: a metade dos buses chega máis tarde e a outra metade antes.\n- **Puntual** = entre 1 minuto antes e o límite escollido arriba.\n- As consultas son cada ~15 min, así que un mesmo bus pode aparecer varias veces.\n- No {line_pct} % das comparacións non coincidiu o destino e usouse o horario máis próximo da mesma liña.\n- Só {days} días de datos: serve para ver horas e tipos de día, non tendencias longas.",
            "dec": ",",
        },
    }
    return LANG, MIN_N, NEAR_MAX, T


@app.cell
def _(LANG, T, mo):
    lang = mo.ui.dropdown(
        options=list(LANG), value="Español", label="Idioma · Language · Lingua"
    )
    mo.vstack([lang])
    return (lang,)


@app.cell
def _(LANG, T, lang):
    t = T[LANG[lang.value]]
    return (t,)


@app.cell
def _(alt, t):
    def fmt(x, digits=1):
        return f"{x:.{digits}f}".replace(".", t["dec"])

    def hourly(df, by_daytype, min_n):
        grp = (
            df["is_weekend"].map({False: t["weekday"], True: t["weekend"]})
            if by_daytype
            else t["all_days"]
        )
        out = (
            df.assign(group=grp)
            .groupby(["hour_of_day", "group"])["delay_min"]
            .agg(typical="median", n="size")
            .reset_index()
        )
        return out[out["n"] >= min_n]

    def hour_chart(rows, title, x_title, y_title, legend_title):
        # Altair rejects title=None, so only pass a title when there is one.
        chart_kwargs = {"title": title} if title else {}
        return (
            alt.Chart(rows, **chart_kwargs)
            .mark_line(point=True)
            .encode(
                x=alt.X("hour_of_day:O", title=x_title),
                y=alt.Y("typical:Q", title=y_title),
                color=alt.Color(
                    "group:N",
                    title=legend_title,
                    scale=alt.Scale(range=["#e76f51", "#264653"]),
                ),
                tooltip=[
                    alt.Tooltip("hour_of_day:O", title=x_title),
                    alt.Tooltip("group:N", title=legend_title),
                    alt.Tooltip("typical:Q", title=y_title, format=".1f"),
                    alt.Tooltip("n:Q", title=t["n_label"]),
                ],
            )
            .properties(height=280)
        )

    return fmt, hour_chart, hourly


@app.cell
def _(mo, t):
    mo.md(f"# {t['title']}\n\n{t['intro']}")


@app.cell
def _(mo):
    # Sin etiqueta traducida: así el valor no se reinicia al cambiar de idioma.
    ontime = mo.ui.slider(start=0, stop=10, step=1, value=3, show_value=True)
    return (ontime,)


@app.cell
def _(mo, ontime, t):
    mo.vstack(
        [
            mo.md(f"{t['ontime_label']}\n\n{t['ontime_help']}"),
            mo.hstack([ontime, mo.md(f"**{ontime.value} min**")], justify="start"),
        ]
    )


@app.cell
def _(Path, mo, t):
    _nd = getattr(mo, "notebook_dir", lambda: None)()
    _base = Path(_nd) if _nd else Path.cwd()
    _cands = [
        _base / "vitrasa.duckdb",
        _base.parent / "vitrasa.duckdb",
        Path.cwd() / "vitrasa.duckdb",
    ]
    db_file = next((p for p in _cands if p.is_file()), None)
    mo.stop(db_file is None, mo.md(f"⚠️ {t['no_db']}"))
    return (db_file,)


@app.cell
def _(db_file, duckdb):
    con = duckdb.connect(str(db_file), read_only=True)
    return (con,)


@app.cell
def _(NEAR_MAX, con):
    near = con.execute(
        """
        SELECT ts_local, line, stop_id, stop_name, lat, lon, delay_min,
               hour(ts_local) AS hour_of_day,
               dayofweek(ts_local) IN (0, 6) AS is_weekend
        FROM v_delays
        WHERE minutes <= ?
        """,
        [NEAR_MAX],
    ).df()
    return (near,)


@app.cell
def _(near):
    _max = near["ts_local"].max()
    data_days = int((_max.normalize() - near["ts_local"].min().normalize()).days) + 1
    return (data_days,)


@app.cell
def _(fmt, mo, near, NEAR_MAX, ontime, t):
    _d = near["delay_min"]
    _on = _d.between(-1, ontime.value).mean() * 100
    _n = f"{len(_d):,}"
    _cap = t["headline_caption"].format(n=_n, near=NEAR_MAX)
    mo.md(
        f"""
### {t["headline_title"]}

| {t["k_typical"]} | {t["k_ontime"]} | {t["k_p90"]} |
|---|---|---|
| **{fmt(_d.median())} min** | **{_on:.0f} %** | **{fmt(_d.quantile(0.9))} min** |

<small>{_cap}</small>
"""
    )


@app.cell
def _(MIN_N, alt, mo, near, ontime, t):
    _g = (
        near.assign(on=near["delay_min"].between(-1, ontime.value))
        .groupby("line")
        .agg(n=("delay_min", "size"), on_time=("on", "mean"))
        .reset_index()
    )
    _g = _g[_g["n"] >= MIN_N].copy()
    _g["on_time"] = (_g["on_time"] * 100).round(0)
    _best = _g.sort_values("on_time", ascending=False).iloc[0]
    _worst = _g.sort_values("on_time").iloc[0]
    _chart = (
        alt.Chart(_g, title=t["lines_title"])
        .mark_bar(color="#264653")
        .encode(
            x=alt.X(
                "on_time:Q", title=t["lines_axis"], scale=alt.Scale(domain=[0, 100])
            ),
            y=alt.Y(
                "line:N",
                title=t["line_axis"],
                sort=alt.EncodingSortField("on_time", order="descending"),
            ),
            tooltip=[
                alt.Tooltip("line:N", title=t["line_axis"]),
                alt.Tooltip("n:Q", title=t["n_label"]),
                alt.Tooltip("on_time:Q", title=t["lines_axis"], format=".0f"),
            ],
        )
        .properties(height=max(200, 18 * len(_g)))
    )
    _cap = t["lines_caption"].format(
        best=_best["line"],
        best_pct=int(_best["on_time"]),
        worst=_worst["line"],
        worst_pct=int(_worst["on_time"]),
        min_n=MIN_N,
    )
    mo.vstack([mo.md(_cap), _chart])


@app.cell
def _(MIN_N, hour_chart, hourly, mo, near, t):
    _rows = hourly(near, True, MIN_N)
    _rows["typical"] = _rows["typical"].round(2)
    mo.vstack(
        [
            mo.md(f"### {t['hours_title']}"),
            hour_chart(_rows, None, t["hours_axis_x"], t["hours_axis_y"], t["legend"]),
            mo.md(f"<small>{t['hours_caption']}</small>"),
        ]
    )


@app.cell
def _(MIN_N, mo, near, t):
    _s = (
        near.groupby(["stop_id", "stop_name"], dropna=False)["delay_min"]
        .agg(n="size", mean="mean")
        .reset_index()
    )
    _s = _s[_s["n"] >= MIN_N].sort_values("mean", ascending=False).head(10)
    _tbl = _s.assign(mean=_s["mean"].round(1)).rename(
        columns={
            "stop_name": t["col_stop"],
            "mean": t["col_avg"],
            "n": t["col_n"],
        }
    )[[t["col_stop"], t["col_avg"], t["col_n"]]]
    mo.vstack(
        [
            mo.md(f"### {t['stops_title']}"),
            mo.ui.table(_tbl, selection=None),
            mo.md(f"<small>{t['stops_caption'].format(min_n=MIN_N)}</small>"),
        ]
    )


@app.cell
def _(con, mo):
    # Scans every estimate, so it is computed once per session and reused.
    @mo.cache
    def slip_minutes():
        return con.execute(
            """
            WITH s AS (
                SELECT eta_utc, minutes,
                       lag(eta_utc) OVER w AS prev_eta,
                       lag(minutes) OVER w AS prev_min
                FROM v_delays
                WHERE match_type = 'headsign'
                WINDOW w AS (PARTITION BY stop_id, line, scheduled_ts ORDER BY ts_local)
            )
            SELECT datediff('second', prev_eta, eta_utc) / 60.0 AS slip
            FROM s
            WHERE prev_eta IS NOT NULL AND prev_min <= 30
            """
        ).df()

    return (slip_minutes,)


@app.cell
def _(fmt, mo, slip_minutes, t):
    _slip = slip_minutes()["slip"]
    _slip = _slip[_slip.between(-30, 30)]
    _txt = t["countdown_text"].format(
        med=fmt(_slip.median()), p90=fmt(_slip.quantile(0.9))
    )
    mo.md(f"### {t['countdown_title']}\n\n{_txt}")


@app.cell
def _(mo, near, t):
    _line_opts = sorted(near["line"].dropna().unique().tolist())
    _stops = near[["stop_id", "stop_name"]].dropna().drop_duplicates()
    stop_labels = {
        f"{r.stop_name} [{r.stop_id}]": r.stop_id for r in _stops.itertuples()
    }
    line_f = mo.ui.multiselect(options=_line_opts, value=[], label=t["f_lines"])
    stop_f = mo.ui.multiselect(options=list(stop_labels), value=[], label=t["f_stops"])
    return line_f, stop_f, stop_labels


@app.cell
def _(data_days, mo, stop_f, line_f, t):
    days_f = mo.ui.slider(start=1, stop=data_days, value=data_days, label=t["f_days"])
    daytype_f = mo.ui.radio(
        options=[t["all_days"], t["wd"], t["we"]],
        value=t["all_days"],
        inline=True,
        label=t["f_daytype"],
    )
    mo.vstack(
        [
            mo.md(f"### {t['part2_title']}\n\n{t['part2_intro']}"),
            line_f,
            stop_f,
            days_f,
            daytype_f,
        ]
    )
    return daytype_f, days_f


@app.cell
def _(daytype_f, days_f, line_f, near, stop_f, stop_labels, t):
    from datetime import timedelta

    _df = near
    if line_f.value:
        _df = _df[_df["line"].isin(line_f.value)]
    if stop_f.value:
        _ids = [stop_labels[label] for label in stop_f.value]
        _df = _df[_df["stop_id"].isin(_ids)]
    _cut = near["ts_local"].max() - timedelta(days=days_f.value)
    _df = _df[_df["ts_local"] >= _cut]
    if daytype_f.value == t["wd"]:
        _df = _df[~_df["is_weekend"]]
    elif daytype_f.value == t["we"]:
        _df = _df[_df["is_weekend"]]
    filtered = _df
    return (filtered,)


@app.cell
def _(filtered, fmt, mo, ontime, t):
    _d = filtered["delay_min"]
    mo.md(
        t["f_summary"].format(
            n=f"{len(_d):,}",
            median=fmt(_d.median()),
            ontime=f"{_d.between(-1, ontime.value).mean() * 100:.0f}",
            p90=fmt(_d.quantile(0.9)),
        )
    ) if len(_d) else mo.md(t["no_data"])


@app.cell
def _(MIN_N, alt, filtered, mo, t):
    _m = (
        filtered.groupby(["stop_id", "stop_name", "lat", "lon"])["delay_min"]
        .agg(n="size", typical="median")
        .reset_index()
    )
    _m = _m[_m["n"] >= MIN_N]
    _map = (
        alt.Chart(_m, title=t["map_title"])
        .mark_circle(size=60, opacity=0.85)
        .encode(
            x=alt.X("lon:Q", scale=alt.Scale(zero=False), axis=None),
            y=alt.Y("lat:Q", scale=alt.Scale(zero=False), axis=None),
            color=alt.Color(
                "typical:Q",
                title=t["legend_delay"],
                scale=alt.Scale(
                    domain=[-2, 0, 3, 6],
                    range=["#457b9d", "#8d99ae", "#f4a261", "#e76f51"],
                ),
            ),
            tooltip=[
                alt.Tooltip("stop_name:N", title=t["col_stop"]),
                alt.Tooltip("n:Q", title=t["n_label"]),
                alt.Tooltip("typical:Q", title=t["legend_delay"], format=".1f"),
            ],
        )
        .properties(width=560, height=420)
    )
    mo.vstack([_map, mo.md(f"<small>{t['map_caption']}</small>")])


@app.cell
def _(MIN_N, filtered, hour_chart, hourly, mo, t):
    _rows = hourly(filtered, False, MIN_N)
    mo.vstack(
        [
            mo.md(f"**{t['filt_hour_title']}**"),
            hour_chart(_rows, None, t["hours_axis_x"], t["hours_axis_y"], t["legend"]),
        ]
    )


@app.cell
def _(alt, con, mo, t):
    _b = con.execute(
        """
        SELECT
          CASE WHEN minutes <= 5 THEN 1 WHEN minutes <= 10 THEN 2
               WHEN minutes <= 20 THEN 3 WHEN minutes <= 30 THEN 4 ELSE 5 END AS ord,
          count(*) AS n,
          quantile_cont(delay_min, 0.5) AS typical
        FROM v_delays
        WHERE minutes IS NOT NULL
        GROUP BY ord
        ORDER BY ord
        """
    ).df()
    _labels = {
        1: "0–5 min",
        2: "6–10 min",
        3: "11–20 min",
        4: "21–30 min",
        5: "31+ min",
    }
    _b["distance"] = _b["ord"].map(_labels)
    _chart = (
        alt.Chart(_b, title=t["dist_title"])
        .mark_bar(color="#264653")
        .encode(
            x=alt.X("distance:N", sort=list(_labels.values()), title=t["dist_axis"]),
            y=alt.Y("typical:Q", title=t["hours_axis_y"]),
            tooltip=[
                alt.Tooltip("distance:N", title=t["dist_axis"]),
                alt.Tooltip("n:Q", title=t["n_label"]),
                alt.Tooltip("typical:Q", title=t["hours_axis_y"], format=".1f"),
            ],
        )
        .properties(height=260)
    )
    mo.vstack(
        [
            mo.md(f"### {t['part3_title']}"),
            mo.md(t["dist_caption"]),
            _chart,
        ]
    )


@app.cell
def _(con, fmt, mo, t):
    _snap = con.execute(
        "SELECT count(*), min(ts_local), max(ts_local) FROM snapshots"
    ).fetchone()
    _gaps = con.execute(
        """
        SELECT count(*) FROM (
            SELECT ts_utc - lag(ts_utc) OVER (ORDER BY ts_utc) AS g FROM snapshots
        ) WHERE g > INTERVAL 30 MINUTE
        """
    ).fetchone()[0]
    _errs = con.execute(
        'SELECT count(DISTINCT "commit") FROM snapshot_errors'
    ).fetchone()[0]
    _line_pct = (
        con.execute(
            """
        SELECT 100.0 * count(*) FILTER (WHERE match_type = 'line')
               / NULLIF(count(*) FILTER (WHERE match_type IS NOT NULL), 0)
        FROM estimations
        """
        ).fetchone()[0]
        or 0.0
    )
    _days = (_snap[2] - _snap[1]).days + 1
    _health = t["health_text"].format(
        n=f"{_snap[0]:,}",
        first=f"{_snap[1]:%Y-%m-%d}",
        last=f"{_snap[2]:%Y-%m-%d}",
        gaps=_gaps,
        errors=_errs,
    )
    _meth = t["meth_text"].format(line_pct=f"{_line_pct:.0f}", days=_days)
    mo.md(f"### {t['health_title']}\n\n{_health}\n\n### {t['meth_title']}\n\n{_meth}")


if __name__ == "__main__":
    app.run()
