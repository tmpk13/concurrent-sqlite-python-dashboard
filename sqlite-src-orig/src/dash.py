#!/usr/bin/env python3
"""
SQLite lock state dashboard.

Parses stderr lock traces from custom os_unix.c:
  LOCK pid=123 fd=4: NO_LOCK -> SHARED
  UNLOCK pid=123 fd=4: SHARED -> NO_LOCK

Usage:
  python dashboard.py lock_trace.log [more_logs...]
  python dashboard.py traces/       # directory of .log files
"""
import json
import re, sys, os
from collections import defaultdict
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from dash import Dash, html, dcc, callback, Input, Output

# -- Palette (sunset theme) --
STATE_COLORS = {
    "UNLOCKED":  "#4a4a4a",   # dark grey
    "SHARED":    "#2ec4b6",   # cyan/mint
    "RESERVED":  "#ff6b35",   # orange
    "PENDING":   "#e63946",   # coral
    "EXCLUSIVE": "#9b5de5",   # magenta
}
STATES = list(STATE_COLORS.keys())
BG_COLOR = "#faf8f5"  # slightly off-white
TEXT_COLOR = "#3a3a3a"  # dark grey

# -- PID map --
def load_pid_map(search_dirs: list[str]) -> dict[int, str]:
    """Load pid_map.json from the first directory that contains it."""
    for d in search_dirs:
        p = Path(d) / "pid_map.json"
        if p.is_file():
            try:
                raw = json.loads(p.read_text())
                return {int(k): v for k, v in raw.items()}
            except (json.JSONDecodeError, ValueError):
                pass
    return {}


def pid_label(pid: int, fd: int, pid_map: dict[int, str]) -> str:
    name = pid_map.get(pid)
    if name:
        return f"{name} (pid={pid} fd={fd})"
    return f"pid={pid} fd={fd}"


# -- Parser --
LINE_RE = re.compile(
    r"(LOCK|UNLOCK)\s+pid=(\d+)\s+fd=(-?\d+):\s+(\w+)\s*->\s*(\w+)"
)

def normalize_state(s):
    return "UNLOCKED" if s == "NO_LOCK" else s

def parse_log(filepath):
    """Returns list of dicts: {ts_line, pid, fd, from_state, to_state}"""
    events = []
    with open(filepath) as f:
        for i, line in enumerate(f):
            m = LINE_RE.search(line)
            if not m:
                continue
            events.append({
                "line": i,
                "op": m.group(1),
                "pid": int(m.group(2)),
                "fd": int(m.group(3)),
                "from": normalize_state(m.group(4)),
                "to": normalize_state(m.group(5)),
            })
    return events

def build_intervals(events):
    """Convert transition events into (fd, state, start_line, end_line) intervals."""
    current = {}
    intervals = []

    for ev in events:
        key = (ev["pid"], ev["fd"])
        if key in current:
            st, start = current[key]
            intervals.append({
                "pid": ev["pid"],
                "fd": ev["fd"],
                "state": st,
                "start": start,
                "end": ev["line"],
                "duration": ev["line"] - start,
            })
        current[key] = (ev["to"], ev["line"])

    return pd.DataFrame(intervals) if intervals else pd.DataFrame(
        columns=["pid", "fd", "state", "start", "end", "duration"]
    )

def state_histogram(df):
    """Total lines spent in each state."""
    if df.empty:
        return pd.DataFrame({"state": STATES, "duration": [0]*5})
    agg = df.groupby("state")["duration"].sum().reset_index()
    for s in STATES:
        if s not in agg["state"].values:
            agg = pd.concat([agg, pd.DataFrame([{"state": s, "duration": 0}])])
    agg["state"] = pd.Categorical(agg["state"], categories=STATES, ordered=True)
    return agg.sort_values("state")

# -- Load all log files --
def discover_logs(paths):
    """Given CLI args, return {label: filepath}"""
    logs = {}
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            for f in sorted(pp.glob("*.log")):
                logs[f.stem] = str(f)
        elif pp.is_file():
            logs[pp.stem] = str(pp)
    return logs

# -- App --
def create_app(log_files, pid_map=None, search_dirs=None):
    _pid_map = pid_map or {}
    _search_dirs = search_dirs or ["."]

    datasets = {}
    for label, fp in log_files.items():
        events = parse_log(fp)
        df = build_intervals(events)
        datasets[label] = df

    app = Dash(__name__, requests_pathname_prefix="/")
    app.layout = html.Div(style={"backgroundColor": BG_COLOR, "color": TEXT_COLOR,
                                  "fontFamily": "monospace", "padding": "20px"}, children=[
        html.H2("SQLite Lock State Dashboard", style={"color": TEXT_COLOR}),

        html.Label("Log file:"),
        dcc.Dropdown(
            id="log-select",
            options=[{"label": k, "value": k} for k in datasets],
            value=list(datasets.keys())[0] if datasets else None,
            style={"width": "400px", "marginBottom": "20px"},
        ),

        html.Div(id="stats", style={"marginBottom": "10px"}),

        html.Div([
            html.Div([
                html.H4("Time in State (histogram)"),
                dcc.Graph(id="histogram"),
            ], style={"width": "48%", "display": "inline-block", "verticalAlign": "top"}),
            html.Div([
                html.H4("Lock State Gantt (per fd)"),
                dcc.Graph(id="gantt"),
            ], style={"width": "48%", "display": "inline-block", "verticalAlign": "top"}),
        ]),

        html.Div([
            html.H4("State distribution per fd"),
            dcc.Graph(id="per-fd-bar"),
        ]),
    ])

    @callback(
        Output("histogram", "figure"),
        Output("gantt", "figure"),
        Output("per-fd-bar", "figure"),
        Output("stats", "children"),
        Input("log-select", "value"),
    )
    def update(label):
        # Re-read pid_map.json each render so new sweep PIDs appear
        fresh = load_pid_map(_search_dirs)
        if fresh:
            _pid_map.update(fresh)

        if label is None or label not in datasets:
            empty = go.Figure()
            return empty, empty, empty, ""

        df = datasets[label]
        template_args = dict(
            paper_bgcolor=BG_COLOR,
            plot_bgcolor=BG_COLOR,
            font_color=TEXT_COLOR,
        )

        # Histogram
        hist_df = state_histogram(df)
        fig_hist = px.bar(
            hist_df, x="state", y="duration",
            color="state",
            color_discrete_map=STATE_COLORS,
            title="Total lines in each lock state",
        )
        fig_hist.update_layout(**template_args, showlegend=False)

        # Gantt
        if not df.empty:
            gantt_df = df.copy()
            gantt_df["fd_label"] = gantt_df.apply(
                lambda r: pid_label(r["pid"], r["fd"], _pid_map), axis=1
            )
            fig_gantt = px.timeline(
                gantt_df,
                x_start=gantt_df["start"].apply(lambda x: pd.Timestamp("2024-01-01") + pd.Timedelta(seconds=x)),
                x_end=gantt_df["end"].apply(lambda x: pd.Timestamp("2024-01-01") + pd.Timedelta(seconds=x)),
                y="fd_label",
                color="state",
                color_discrete_map=STATE_COLORS,
                title="Lock hold intervals (x=log line as pseudo-time)",
            )
            fig_gantt.update_layout(**template_args, xaxis_title="log line (pseudo-time)")
        else:
            fig_gantt = go.Figure()
            fig_gantt.update_layout(**template_args)

        # Per-fd stacked bar
        if not df.empty:
            pf = df.copy()
            pf["fd_label"] = pf.apply(
                lambda r: pid_label(r["pid"], r["fd"], _pid_map), axis=1
            )
            pf = pf.groupby(["fd_label", "state"])["duration"].sum().reset_index()
            pf["state"] = pd.Categorical(pf["state"], categories=STATES, ordered=True)
            fig_pf = px.bar(
                pf, x="fd_label", y="duration", color="state",
                color_discrete_map=STATE_COLORS,
                barmode="stack",
                title="State distribution per fd",
            )
            fig_pf.update_layout(**template_args)
        else:
            fig_pf = go.Figure()
            fig_pf.update_layout(**template_args)

        n_events = len(df)
        stats_text = f"Events: {n_events} | FDs: {df['fd'].nunique() if not df.empty else 0}"

        return fig_hist, fig_gantt, fig_pf, stats_text

    return app

# -- Main --
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python dashboard.py <logfile_or_dir> [...]")
        sys.exit(1)

    logs = discover_logs(sys.argv[1:])
    if not logs:
        print("No .log files found")
        sys.exit(1)

    search_dirs = [".", *sys.argv[1:]]
    pid_map = load_pid_map(search_dirs)

    print(f"Loaded {len(logs)} log(s): {list(logs.keys())}")
    if pid_map:
        print(f"PID map: {pid_map}")

    app = create_app(logs, pid_map=pid_map, search_dirs=search_dirs)
    app.run(debug=False, host="0.0.0.0", port=3070)