#!/usr/bin/env python3
"""
SQLite lock state dashboard — live, all runs, percent histograms.
Adds: Run Sweep / Clear Data buttons, sweep status indicator.
      Adhoc command runner with Gantt.
      Concurrent multi-statement runner with wall-clock Gantt, no WAL.

Usage:
  python dashboard2.py traces/
  python dashboard2.py traces/ --script run_sweep.fish
  python dashboard2.py lock_trace.log [more_logs...]
"""
import json
import sys
import subprocess
import threading
import time
import tempfile
from pathlib import Path
import re
import os

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from dash import Dash, html, dcc, callback, Input, Output, State, no_update

# --------------------------------------------------------------------------- #
# Palette
# --------------------------------------------------------------------------- #
STATE_COLORS = {
    "UNLOCKED":  "#4a4a4a",
    "SHARED":    "#2ec4b6",
    "RESERVED":  "#ff6b35",
    "PENDING":   "#e63946",
    "EXCLUSIVE": "#9b5de5",
}
STATES     = list(STATE_COLORS.keys())
BG_COLOR   = "#faf8f5"
TEXT_COLOR = "#3a3a3a"
CARD_BG    = "#f0ede8"
ACCENT     = "#ff6b35"

LINE_RE = re.compile(
    r"(LOCK|UNLOCK)\s+pid=(\d+)\s+fd=(-?\d+):\s+(\w+)\s*->\s*(\w+)"
)

# --------------------------------------------------------------------------- #
# PID -> worker name mapping
# --------------------------------------------------------------------------- #

def load_pid_map(search_dirs: list[str]) -> dict[int, str]:
    """Load pid_map.json from the first directory that contains it."""
    for d in search_dirs:
        p = Path(d) / "pid_map.json"
        if p.is_file():
            try:
                raw = json.loads(p.read_text())
                # Keys in JSON are strings; convert to int for lookup
                return {int(k): v for k, v in raw.items()}
            except (json.JSONDecodeError, ValueError):
                pass
    return {}


def pid_label(pid: int, fd: int, pid_map: dict[int, str]) -> str:
    """Format a gantt row label using worker name if available."""
    name = pid_map.get(pid)
    if name:
        return f"{name} (pid={pid} fd={fd})"
    return f"pid={pid} fd={fd}"


# --------------------------------------------------------------------------- #
# Global sweep process tracker (server-side only)
# --------------------------------------------------------------------------- #
_sweep_proc: subprocess.Popen | None = None
_sweep_lock = threading.Lock()


def sweep_status() -> dict:
    with _sweep_lock:
        if _sweep_proc is None:
            return {"running": False, "pid": None, "rc": None}
        rc = _sweep_proc.poll()
        return {"running": rc is None, "pid": _sweep_proc.pid, "rc": rc}


# --------------------------------------------------------------------------- #
# Parse / build (line-number based — used for sweep logs)
# --------------------------------------------------------------------------- #

def normalize(s: str) -> str:
    return "UNLOCKED" if s == "NO_LOCK" else s


def parse_log(filepath: str) -> list[dict]:
    events = []
    with open(filepath) as f:
        for i, line in enumerate(f):
            m = LINE_RE.search(line)
            if not m:
                continue
            events.append({
                "line": i,
                "pid":  int(m.group(2)),
                "fd":   int(m.group(3)),
                "from": normalize(m.group(4)),
                "to":   normalize(m.group(5)),
            })
    return events


def build_intervals(events: list[dict]) -> pd.DataFrame:
    current: dict = {}
    rows: list[dict] = []
    for ev in events:
        key = (ev["pid"], ev["fd"])
        if key in current:
            st, start = current[key]
            rows.append({
                "pid": ev["pid"], "fd": ev["fd"],
                "state": st, "start": start,
                "end": ev["line"],
                "duration": ev["line"] - start,
            })
        current[key] = (ev["to"], ev["line"])
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["pid", "fd", "state", "start", "end", "duration"]
    )


# --------------------------------------------------------------------------- #
# Wall-clock variants (used for concurrent adhoc runner)
# --------------------------------------------------------------------------- #

def parse_log_wallclock(filepath: str, wall_start: float, wall_end: float) -> list[dict]:
    """Parse log and interpolate line positions to wall-clock seconds."""
    raw = parse_log(filepath)
    if not raw:
        return []
    n = len(raw)
    span = wall_end - wall_start
    for i, ev in enumerate(raw):
        ev["ts"] = wall_start + (i / max(n - 1, 1)) * span
    return raw


def build_intervals_wallclock(events: list[dict]) -> pd.DataFrame:
    """Build intervals using 'ts' (float seconds) instead of line numbers."""
    current: dict = {}
    rows: list[dict] = []
    for ev in events:
        key = (ev["pid"], ev["fd"])
        if key in current:
            st, start_ts = current[key]
            rows.append({
                "pid": ev["pid"], "fd": ev["fd"],
                "state": st,
                "start": start_ts,
                "end": ev["ts"],
                "duration": ev["ts"] - start_ts,
            })
        current[key] = (ev["to"], ev["ts"])
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["pid", "fd", "state", "start", "end", "duration"]
    )


# --------------------------------------------------------------------------- #
# Stat helpers
# --------------------------------------------------------------------------- #

def pct_by_state(df: pd.DataFrame) -> pd.DataFrame:
    base = {s: 0 for s in STATES}
    if not df.empty:
        for s, g in df.groupby("state"):
            base[s] = g["duration"].sum()
    total = sum(base.values()) or 1
    return pd.DataFrame([
        {"state": s, "duration": base[s], "pct": 100.0 * base[s] / total}
        for s in STATES
    ])


def pct_contended(df: pd.DataFrame) -> float:
    if df.empty:
        return 0.0
    total = df["duration"].sum()
    if total == 0:
        return 0.0
    unlocked = df.loc[df["state"] == "UNLOCKED", "duration"].sum()
    return 100.0 * (total - unlocked) / total


def _num_key(stem: str) -> tuple:
    m = re.search(r"(\d+)$", stem)
    return (int(m.group(1)), stem) if m else (10**9, stem)


def discover_logs(paths: list[str]) -> dict[str, str]:
    logs: dict[str, str] = {}
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            for f in sorted(pp.glob("*.log"), key=lambda f: _num_key(f.stem)):
                if re.search(r"\d+$", f.stem):
                    logs[f.stem] = str(f)
        elif pp.is_file():
            logs[pp.stem] = str(pp)
    return logs


# --------------------------------------------------------------------------- #
# Layout helpers
# --------------------------------------------------------------------------- #

def base_layout() -> dict:
    return dict(
        paper_bgcolor=BG_COLOR,
        plot_bgcolor=BG_COLOR,
        font_color=TEXT_COLOR,
        margin=dict(l=50, r=20, t=40, b=40),
    )


def empty_fig(title: str = "waiting for data...") -> go.Figure:
    f = go.Figure()
    f.update_layout(**base_layout(), title=title, uirevision="f-zoom")
    return f


BTN_BASE = {
    "padding": "8px 18px",
    "borderRadius": "6px",
    "border": "none",
    "cursor": "pointer",
    "fontFamily": "monospace",
    "fontWeight": "bold",
    "fontSize": "0.9em",
}


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

def create_app(log_sources: dict[str, str], sweep_script: str | None = None,
               pid_map: dict[int, str] | None = None) -> Dash:
    labels = list(log_sources.keys())
    trace_dir = str(Path(next(iter(log_sources.values()))).parent) if log_sources else "."
    _pid_map = pid_map or {}

    app = Dash(__name__)

    gantt_slots = [
        html.Div(
            [
                html.H5(lbl, style={"margin": "4px 0 2px", "color": ACCENT}),
                dcc.Graph(id={"type": "gantt", "index": lbl}, style={"height": "260px"}),
            ],
            id={"type": "gantt-wrap", "index": lbl},
            style={"marginBottom": "8px", "borderRadius": "8px", "padding": "4px"},
        )
        for lbl in labels
    ]

    # ------------------------------------------------------------------ #
    # Adhoc SQL runner section
    # ------------------------------------------------------------------ #
    adhoc_section = html.Div(
        style={
            "marginTop": "32px",
            "borderTop": f"2px solid {ACCENT}",
            "paddingTop": "20px",
        },
        children=[
            html.H4("SQL Runner — Concurrent", style={"margin": "0 0 4px", "color": ACCENT}),
            html.P(
                "One statement per line (semicolons also work). All statements run concurrently in separate threads. "
                "Stagger (ms): each thread N sleeps N*stagger before connecting — 0 = fully simultaneous. "
                "No WAL: rollback journal enforced, real EXCLUSIVE/PENDING contention visible on Gantt. "
                "X-axis is real elapsed wall-clock seconds.",
                style={"fontSize": "0.82em", "color": "#888", "margin": "0 0 10px"},
            ),
            # DB path row
            html.Div(
                style={"display": "flex", "gap": "8px", "alignItems": "center", "marginBottom": "8px"},
                children=[
                    html.Label("DB:", style={"fontWeight": "bold", "whiteSpace": "nowrap"}),
                    dcc.Input(
                        id="adhoc-db",
                        type="text",
                        value="lab04.db",
                        debounce=False,
                        style={
                            "flex": "1",
                            "padding": "7px 12px",
                            "fontFamily": "monospace",
                            "fontSize": "0.9em",
                            "backgroundColor": CARD_BG,
                            "color": TEXT_COLOR,
                            "border": "1px solid #ccc",
                            "borderRadius": "6px",
                        },
                    ),
                ],
            ),
            # SQL textarea + buttons row
            html.Div(
                style={"display": "flex", "gap": "8px", "alignItems": "flex-start"},
                children=[
                    dcc.Textarea(
                        id="adhoc-sql",
                        value=(
                            "SELECT * FROM students LIMIT 100;\n"
                            "INSERT INTO students VALUES (999, 'test', 'A');\n"
                            "SELECT count(*) FROM students;"
                        ),
                        style={
                            "flex": "1",
                            "height": "110px",
                            "padding": "8px 12px",
                            "fontFamily": "monospace",
                            "fontSize": "0.9em",
                            "backgroundColor": CARD_BG,
                            "color": TEXT_COLOR,
                            "border": "1px solid #ccc",
                            "borderRadius": "6px",
                            "resize": "vertical",
                        },
                    ),
                    html.Div(
                        style={"display": "flex", "flexDirection": "column", "gap": "8px"},
                        children=[
                            html.Button(
                                "Run Concurrent",
                                id="btn-adhoc-run",
                                n_clicks=0,
                                style={**BTN_BASE, "backgroundColor": "#9b5de5", "color": "#fff"},
                            ),
                            html.Button(
                                "Reset",
                                id="btn-adhoc-reset",
                                n_clicks=0,
                                style={**BTN_BASE, "backgroundColor": CARD_BG, "color": TEXT_COLOR},
                            ),
                            html.Div(
                                style={"display": "flex", "alignItems": "center", "gap": "6px"},
                                children=[
                                    html.Label(
                                        "Stagger (ms):",
                                        style={"fontSize": "0.82em", "whiteSpace": "nowrap"},
                                    ),
                                    dcc.Input(
                                        id="adhoc-delay",
                                        type="number",
                                        value=0,
                                        min=0,
                                        step=50,
                                        style={
                                            "width": "72px",
                                            "padding": "4px 8px",
                                            "fontFamily": "monospace",
                                            "fontSize": "0.85em",
                                            "backgroundColor": CARD_BG,
                                            "color": TEXT_COLOR,
                                            "border": "1px solid #ccc",
                                            "borderRadius": "6px",
                                        },
                                    ),
                                ],
                            ),
                            html.Span(id="adhoc-status", children="", style={
                                "fontFamily": "monospace",
                                "fontSize": "0.82em",
                                "color": "#888",
                            }),
                        ],
                    ),
                ],
            ),
            dcc.Graph(id="adhoc-gantt", style={"height": "340px", "marginTop": "12px"}),
            dcc.Store(id="adhoc-store", data=[]),
        ],
    )
    # ------------------------------------------------------------------ #

    app.layout = html.Div(
        style={
            "backgroundColor": BG_COLOR,
            "color": TEXT_COLOR,
            "fontFamily": "monospace",
            "padding": "20px",
            "maxWidth": "1500px",
            "margin": "0 auto",
        },
        children=[
            # ---- Header -------------------------------------------------- #
            html.Div(
                style={"display": "flex", "alignItems": "center", "gap": "14px", "marginBottom": "16px", "flexWrap": "wrap"},
                children=[
                    html.H2("SQLite Lock State Dashboard", style={"margin": 0}),
                    html.Span("LIVE", style={
                        "backgroundColor": "#2ec4b6", "color": "#fff",
                        "padding": "3px 10px", "borderRadius": "12px", "fontSize": "0.8em",
                    }),
                    html.Button(
                        "Run Sweep",
                        id="btn-run-sweep",
                        n_clicks=0,
                        style={**BTN_BASE, "backgroundColor": "#2ec4b6", "color": "#fff"},
                        disabled=sweep_script is None,
                        title=sweep_script or "No --script provided",
                    ),
                    html.Button(
                        "Clear Data",
                        id="btn-clear-data",
                        n_clicks=0,
                        style={**BTN_BASE, "backgroundColor": CARD_BG, "color": TEXT_COLOR},
                    ),
                    html.Span(id="sweep-status-badge", children="idle", style={
                        "padding": "3px 10px", "borderRadius": "12px",
                        "fontSize": "0.8em", "border": "1px solid #ccc",
                    }),
                ],
            ),

            # ---- Summary cards ------------------------------------------- #
            html.Div(id="summary-cards", style={"display": "flex", "gap": "10px", "flexWrap": "wrap", "marginBottom": "20px"}),

            # ---- % histogram --------------------------------------------- #
            html.Div([
                html.H4("% Time in Each Lock State — all runs", style={"margin": "4px 0"}),
                dcc.Graph(id="pct-histogram", style={"height": "380px"}),
            ], style={"marginBottom": "20px"}),

            # ---- Contention bar ------------------------------------------ #
            html.Div([
                html.H4("% Time Contended (not UNLOCKED) — all runs", style={"margin": "4px 0"}),
                dcc.Graph(id="contention-bar", style={"height": "260px"}),
            ], style={"marginBottom": "20px"}),

            # ---- Gantt per run ------------------------------------------- #
            html.Div([
                html.H4("Lock State Gantt — per run", style={"margin": "4px 0"}),
                *gantt_slots,
            ]),

            # ---- Concurrent SQL runner ------------------------------------ #
            adhoc_section,

            dcc.Interval(id="interval", interval=1000, n_intervals=0),
            dcc.Store(id="interval-store", data={lbl: [] for lbl in labels}),
            dcc.Store(id="config-store", data={
                "sweep_script": sweep_script,
                "trace_dir": trace_dir,
                "log_sources": log_sources,
            }),
        ],
    )

    # ----------------------------------------------------------------------- #
    # Callback: Run Sweep button
    # ----------------------------------------------------------------------- #
    @callback(
        Output("sweep-status-badge", "children", allow_duplicate=True),
        Output("sweep-status-badge", "style",    allow_duplicate=True),
        Input("btn-run-sweep", "n_clicks"),
        State("config-store", "data"),
        prevent_initial_call=True,
    )
    def trigger_sweep(n_clicks, config):
        global _sweep_proc
        if not n_clicks:
            return no_update, no_update
        script = config.get("sweep_script")
        if not script:
            return "no script", _badge_style("warn")
        with _sweep_lock:
            if _sweep_proc and _sweep_proc.poll() is None:
                _sweep_proc.terminate()
                try:
                    _sweep_proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    _sweep_proc.kill()
            _sweep_proc = subprocess.Popen(
                ["fish", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return f"running pid={_sweep_proc.pid}", _badge_style("run")

    # ----------------------------------------------------------------------- #
    # Callback: Clear Data button
    # ----------------------------------------------------------------------- #
    @callback(
        Output("interval-store", "data", allow_duplicate=True),
        Input("btn-clear-data", "n_clicks"),
        State("config-store", "data"),
        prevent_initial_call=True,
    )
    def clear_data(n_clicks, config):
        if not n_clicks:
            return no_update
        log_sources_ = config.get("log_sources", {})
        for fp in log_sources_.values():
            try:
                open(fp, "w").close()
            except OSError:
                pass
        return {lbl: [] for lbl in log_sources_}

    # ----------------------------------------------------------------------- #
    # Callback: Sweep status badge (polled)
    # ----------------------------------------------------------------------- #
    @callback(
        Output("sweep-status-badge", "children"),
        Output("sweep-status-badge", "style"),
        Input("interval", "n_intervals"),
        prevent_initial_call=False,
    )
    def update_sweep_status(_tick):
        st = sweep_status()
        if st["running"]:
            return f"running pid={st['pid']}", _badge_style("run")
        elif st["rc"] is not None:
            return f"done (rc={st['rc']})", _badge_style("ok" if st["rc"] == 0 else "err")
        return "idle", _badge_style("idle")

    # ----------------------------------------------------------------------- #
    # Callback: accumulate sweep log data + reload pid_map
    # ----------------------------------------------------------------------- #
    @callback(
        Output("interval-store", "data"),
        Input("interval", "n_intervals"),
        State("interval-store", "data"),
        State("config-store", "data"),
    )
    def accumulate(_tick, store, config):
        nonlocal _pid_map
        log_sources_ = config.get("log_sources", {})
        td = config.get("trace_dir", ".")

        # Hot-reload pid_map on each tick so new sweeps pick up new PIDs
        fresh = load_pid_map([td, "."])
        if fresh:
            _pid_map.update(fresh)

        latest_mtime, active_lbl = -1, None
        for lbl, fp in log_sources_.items():
            try:
                events = parse_log(fp)
                mtime = os.path.getmtime(fp)
            except (FileNotFoundError, OSError):
                events = []
                mtime = -1
            df = build_intervals(events)
            store[lbl] = df.to_dict("records")
            if mtime > latest_mtime:
                latest_mtime, active_lbl = mtime, lbl
        store["__active__"] = active_lbl
        return store

    # ----------------------------------------------------------------------- #
    # Callback: render sweep charts
    # ----------------------------------------------------------------------- #
    outputs = [
        Output("pct-histogram",  "figure"),
        Output("contention-bar", "figure"),
        Output("summary-cards",  "children"),
    ] + [Output({"type": "gantt", "index": lbl}, "figure") for lbl in labels]

    @callback(*outputs, Input("interval-store", "data"))
    def render(store):
        dfs = {
            lbl: pd.DataFrame(rows)
            for lbl, rows in store.items()
            if not lbl.startswith("__")
        }

        all_pct, contended = [], []
        for lbl, df in dfs.items():
            pct_df = pct_by_state(df)
            for _, row in pct_df.iterrows():
                all_pct.append({"run": lbl, "state": row["state"], "pct": row["pct"]})
            contended.append({"run": lbl, "pct_contended": pct_contended(df)})

        if all_pct:
            hist_df = pd.DataFrame(all_pct)
            hist_df["state"] = pd.Categorical(hist_df["state"], categories=STATES, ordered=True)
            fig_hist = px.bar(
                hist_df, x="state", y="pct", color="run",
                barmode="group",
                labels={"pct": "% time", "state": "state"},
                text_auto=".1f",
            )
            fig_hist.update_layout(
                **base_layout(),
                yaxis=dict(autorange=True, title="% time"),
                title="% of total interval time per lock state",
                uirevision="hist-zoom"
            )
            fig_hist.update_traces(textposition="outside")
        else:
            fig_hist = empty_fig("% histogram — waiting for data")

        if contended:
            cont_df = pd.DataFrame(contended)
            fig_cont = px.bar(
                cont_df, x="run", y="pct_contended",
                color="pct_contended",
                color_continuous_scale=["#2ec4b6", "#ff6b35", "#e63946"],
                range_color=[0, 100],
                text_auto=".1f",
                labels={"pct_contended": "% contended"},
            )
            fig_cont.update_layout(
                **base_layout(),
                yaxis=dict(autorange=True, title="% contended"),
                title="% time contended (SHARED + RESERVED + PENDING + EXCLUSIVE)",
                uirevision="cont-zoom"
            )
            fig_cont.update_traces(textposition="outside")
            fig_cont.update_coloraxes(showscale=False)
        else:
            fig_cont = empty_fig("contention — waiting for data")

        cards = []
        for lbl, df in dfs.items():
            pc = pct_contended(df)
            color = "#e63946" if pc > 50 else ("#ff6b35" if pc > 20 else "#2ec4b6")
            cards.append(html.Div([
                html.Div(lbl, style={"fontWeight": "bold", "marginBottom": "4px", "color": ACCENT}),
                html.Div(f"{pc:.1f}%", style={"fontSize": "1.6em", "color": color, "fontWeight": "bold"}),
                html.Div("contended", style={"fontSize": "0.75em"}),
                html.Div(f"intervals: {len(df)}", style={"fontSize": "0.75em", "marginTop": "4px"}),
                html.Div(f"fds: {df['fd'].nunique() if not df.empty else 0}", style={"fontSize": "0.75em"}),
            ], style={
                "backgroundColor": CARD_BG,
                "borderRadius": "8px",
                "padding": "10px 16px",
                "minWidth": "130px",
                "borderTop": f"3px solid {color}",
            }))

        origin = pd.Timestamp("2026-02-26")
        gantts = []
        for lbl in labels:
            df = dfs[lbl]
            if not df.empty:
                gdf = df.copy()
                gdf["label"] = gdf.apply(
                    lambda r: pid_label(r["pid"], r["fd"], _pid_map), axis=1
                )
                fig_g = px.timeline(
                    gdf,
                    x_start=gdf["start"].apply(lambda x: origin + pd.Timedelta(seconds=x)),
                    x_end=gdf["end"].apply(lambda x: origin + pd.Timedelta(seconds=x)),
                    y="label", color="state",
                    color_discrete_map=STATE_COLORS,
                    category_orders={"label": sorted(gdf["label"].unique())},
                )
                fig_g.update_layout(**base_layout(), xaxis_title="log line (pseudo-time)", uirevision="gantt-zoom")
            else:
                fig_g = empty_fig(f"{lbl} — waiting")
            gantts.append(fig_g)

        return fig_hist, fig_cont, cards, *gantts

    # ----------------------------------------------------------------------- #
    # Callback: highlight active gantt
    # ----------------------------------------------------------------------- #
    @callback(
        [Output({"type": "gantt-wrap", "index": lbl}, "style") for lbl in labels],
        Input("interval-store", "data"),
    )
    def highlight_active(store):
        active = store.get("__active__")
        styles = []
        for lbl in labels:
            if lbl == active:
                styles.append({
                    "marginBottom": "8px", "borderRadius": "8px", "padding": "4px",
                    "border": f"2px solid {ACCENT}",
                    "boxShadow": f"0 0 8px {ACCENT}88",
                    "backgroundColor": CARD_BG,
                })
            else:
                styles.append({
                    "marginBottom": "8px", "borderRadius": "8px", "padding": "4px",
                    "border": "2px solid transparent",
                })
        return styles

    # ----------------------------------------------------------------------- #
    # Callback: run all statements concurrently
    # ----------------------------------------------------------------------- #
    @callback(
        Output("adhoc-store",  "data",     allow_duplicate=True),
        Output("adhoc-status", "children", allow_duplicate=True),
        Input("btn-adhoc-run", "n_clicks"),
        State("adhoc-db",    "value"),
        State("adhoc-sql",   "value"),
        State("adhoc-delay", "value"),
        prevent_initial_call=True,
    )
    def adhoc_run(n_clicks, db_path, sql_block, stagger_ms):
        if not n_clicks or not db_path or not sql_block:
            return no_update, no_update

        db_abs    = str(Path(db_path).expanduser().resolve())
        stagger_s = max(0.0, float(stagger_ms or 0)) / 1000.0

        # Accept newlines or semicolons as statement separators
        raw = sql_block.replace("\n", ";")
        statements = [s.strip() for s in raw.split(";") if s.strip()]
        if not statements:
            return [], "no statements found"

        env = {**os.environ, "LD_PRELOAD": str(Path.cwd() / "libsqlite3.so")}

        results: list[pd.DataFrame | None] = [None] * len(statements)
        errors:  list[str | None]          = [None] * len(statements)
        global_start = time.monotonic()

        def run_one(idx: int, stmt: str) -> None:
            # Stagger: thread N sleeps N * stagger_s before it opens the DB.
            # Thread 0 starts immediately; thread 1 waits 1*stagger_s, etc.
            if stagger_s > 0 and idx > 0:
                time.sleep(stagger_s * idx)

            # Enforce rollback journal — no WAL, so writers need EXCLUSIVE lock
            runner = (
                "import sqlite3, sys\n"
                f"c = sqlite3.connect({db_abs!r})\n"
                "try:\n"
                "    c.execute('PRAGMA journal_mode=DELETE')\n"
                f"    list(c.execute({stmt!r}))\n"
                "    c.commit()\n"
                "except Exception as e:\n"
                "    print(e, file=sys.stderr)\n"
                "finally:\n"
                "    c.close()\n"
            )

            tmp = tempfile.NamedTemporaryFile(suffix=".log", delete=False)
            tmp.close()

            t0 = time.monotonic() - global_start
            try:
                subprocess.run(
                    [sys.executable, "-c", runner],
                    env=env,
                    stderr=open(tmp.name, "w"),
                    stdout=subprocess.DEVNULL,
                    timeout=60,
                )
                t1 = time.monotonic() - global_start
                events = parse_log_wallclock(tmp.name, t0, t1)
                df = build_intervals_wallclock(events)
                if not df.empty:
                    df["stmt"] = idx + 1
                results[idx] = df
            except subprocess.TimeoutExpired:
                errors[idx] = f"s{idx + 1}: timeout"
            except Exception as e:
                errors[idx] = f"s{idx + 1}: {e}"
            finally:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass

        # Launch all threads; stagger sleep is inside each thread
        threads = [
            threading.Thread(target=run_one, args=(i, s), daemon=True)
            for i, s in enumerate(statements)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        all_records: list[dict] = []
        for df in results:
            if df is not None and not df.empty:
                all_records.extend(df.to_dict("records"))

        err_msgs = [e for e in errors if e]
        elapsed  = time.monotonic() - global_start
        status = (
            f"{len(statements)} concurrent  |  {len(all_records)} intervals  "
            f"|  {elapsed:.2f}s wall"
            + (f"  |  stagger={int(stagger_ms)}ms" if stagger_ms else "")
            + (f"  |  {'; '.join(err_msgs)}" if err_msgs else "")
        )
        return all_records, status

    # ----------------------------------------------------------------------- #
    # Callback: reset adhoc
    # ----------------------------------------------------------------------- #
    @callback(
        Output("adhoc-store",  "data",     allow_duplicate=True),
        Output("adhoc-status", "children", allow_duplicate=True),
        Input("btn-adhoc-reset", "n_clicks"),
        prevent_initial_call=True,
    )
    def adhoc_reset(n_clicks):
        if not n_clicks:
            return no_update, no_update
        return [], ""

    # ----------------------------------------------------------------------- #
    # Callback: render adhoc gantt (real wall-clock x-axis)
    # ----------------------------------------------------------------------- #
    @callback(
        Output("adhoc-gantt", "figure"),
        Input("adhoc-store", "data"),
    )
    def adhoc_render(records):
        if not records:
            return empty_fig("enter SQL statements and click Run Concurrent")

        df = pd.DataFrame(records)
        origin = pd.Timestamp("2026-02-26")

        # x-axis is real elapsed seconds — overlapping bars = real contention
        df["label"] = df.apply(
            lambda r: f"s{r.get('stmt', '?')} pid={r['pid']} fd={r['fd']}", axis=1
        )
        fig = px.timeline(
            df,
            x_start=df["start"].apply(lambda x: origin + pd.Timedelta(seconds=x)),
            x_end=df["end"].apply(  lambda x: origin + pd.Timedelta(seconds=x)),
            y="label",
            color="state",
            color_discrete_map=STATE_COLORS,
            title="Concurrent lock state timeline — wall-clock (s)",
        )
        fig.update_layout(
            **base_layout(),
            xaxis_title="elapsed seconds",
            uirevision="fig-zoom"
        )
        return fig

    return app


# --------------------------------------------------------------------------- #
# Badge helper
# --------------------------------------------------------------------------- #

def _badge_style(state: str) -> dict:
    colors = {
        "run":  ("#2ec4b6", "#fff"),
        "ok":   ("#4caf50", "#fff"),
        "err":  ("#e63946", "#fff"),
        "warn": ("#ff6b35", "#fff"),
        "idle": (CARD_BG,   TEXT_COLOR),
    }
    bg, fg = colors.get(state, (CARD_BG, TEXT_COLOR))
    return {
        "padding": "3px 10px", "borderRadius": "12px",
        "fontSize": "0.8em",
        "backgroundColor": bg, "color": fg,
        "border": "1px solid #ccc",
    }


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SQLite lock state dashboard")
    parser.add_argument("sources", nargs="+", help="Log files or directories")
    parser.add_argument("--script", default=None,
                        help="Path to sweep script (e.g. run_sweep.fish)")
    parser.add_argument("--port", type=int, default=3070)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    logs = discover_logs(args.sources)
    if not logs:
        print("No .log files found")
        sys.exit(1)

    # Load PID map from trace dir(s) and CWD
    search_dirs = ["."] + args.sources
    pid_map = load_pid_map(search_dirs)

    print(f"Sources ({len(logs)}): {list(logs.keys())}")
    if pid_map:
        print(f"PID map: {pid_map}")
    else:
        print("No pid_map.json found; labels will show raw PIDs.")
    if args.script:
        print(f"Sweep script: {args.script}")
    else:
        print("No --script provided; Run Sweep button disabled.")

    app = create_app(logs, sweep_script=args.script, pid_map=pid_map)
    app.run(debug=False, host=args.host, port=args.port)