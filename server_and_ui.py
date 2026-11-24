"""
Pit-wall station: receives telemetry frames from the Raspberry Pi hub and shows them on a
dark, F1-style dashboard (Flask + Dash + Plotly).

  POST /api/telemetry      one JSON frame (see telemetry_common.py)
  GET  /api/latest         the newest frame and the active alerts, as JSON
  GET  /                   the dashboard (refreshes every 0.5 s)

Run
  python server_and_ui.py                 wait for frames from car_sender.py
  python server_and_ui.py --demo          no Pi needed: built-in simulated car and driver
  python server_and_ui.py --demo --demo-lap 14 --speedup 3
"""
from __future__ import annotations

import argparse
import logging
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

import plotly.graph_objects as go
from dash import Dash, Input, Output, dcc, html
from flask import Flask, jsonify, request

from telemetry_common import DRIVER_FIELDS, WHEELS, AlertEngine, get_path, make_frame, to_float

HISTORY_S = 120.0          # seconds of trend shown on the charts
NEON = {"pink": "#ff2d75", "cyan": "#22d3ee", "green": "#34d399", "yellow": "#facc15",
        "orange": "#fb923c", "blue": "#60a5fa", "violet": "#a78bfa", "grid": "#1f2937", "text": "#e5e7eb",
        "muted": "#7c8796"}


# ----------------------------------------------------------------------------- state
class TelemetryStore:
    """Thread-safe: frames arrive on Flask request threads, the dashboard reads on others."""

    def __init__(self, maxlen: int = 2000):
        self.lock = threading.Lock()
        self.latest: Optional[Dict[str, Any]] = None
        self.received_at = 0.0
        self.history: Deque[Dict[str, Any]] = deque(maxlen=maxlen)
        self.alerts = AlertEngine()
        self.active: List = []
        self.count = 0
        self.rate_window: Deque[float] = deque(maxlen=40)

    def ingest(self, frame: Dict[str, Any]) -> None:
        now = time.time()
        with self.lock:
            self.latest = frame
            self.received_at = now
            self.count += 1
            self.rate_window.append(now)
            self.active = self.alerts.update(frame, now)
            d, c = frame.get("driver", {}), frame.get("car", {})
            row = {"t": now, "hr": d.get("hr_bpm"), "g": d.get("g_force"), "resp": d.get("resp_bpm"),
                   "speed": c.get("speed_kmh"), "g_lat": c.get("g_lat"), "g_long": c.get("g_long")}
            for w in WHEELS:
                row[f"tt_{w}"] = get_path(frame, f"car.tyres.{w}.temp_c")
            self.history.append(row)

    def snapshot(self):
        with self.lock:
            now = time.time()
            hist = [r for r in self.history if now - r["t"] <= HISTORY_S]
            rate = 0.0
            if len(self.rate_window) > 1 and now - self.rate_window[-1] < 2:
                rate = (len(self.rate_window) - 1) / max(1e-6, self.rate_window[-1] - self.rate_window[0])
            return self.latest, now - self.received_at if self.latest else None, hist, list(self.active), \
                list(self.alerts.log[-12:]), rate


STORE = TelemetryStore()


# ----------------------------------------------------------------------------- helpers
def fmt(v: Any, nd: int = 0, unit: str = "") -> str:
    f = to_float(v)
    if f is None:
        return "--"
    return f"{f:.{nd}f}{unit}"


def tyre_color(t: Optional[float]) -> str:
    if t is None:
        return "#374151"
    if t < 80:
        return NEON["blue"]
    if t < 105:
        return NEON["green"]
    if t < 120:
        return NEON["yellow"]
    return NEON["pink"]


def tag(kind: str) -> html.Span:
    k = (kind or "sim").lower()
    return html.Span("SENSOR" if k == "sensor" else "SIM", className=f"tag tag-{'sensor' if k == 'sensor' else 'sim'}")


def tyre_map(frame: Optional[Dict[str, Any]]) -> go.Figure:
    """Top view of the car: body outline + four tyres coloured by temperature."""
    fig = go.Figure()
    body = [(0, 2.2), (0.35, 2.0), (0.42, 1.1), (0.6, 0.6), (0.6, -1.6), (0.48, -2.1), (0.2, -2.25),
            (-0.2, -2.25), (-0.48, -2.1), (-0.6, -1.6), (-0.6, 0.6), (-0.42, 1.1), (-0.35, 2.0), (0, 2.2)]
    fig.add_trace(go.Scatter(x=[p[0] for p in body], y=[p[1] for p in body], mode="lines", fill="toself",
                             line=dict(color=NEON["cyan"], width=1.5), fillcolor="rgba(34,211,238,0.06)",
                             hoverinfo="skip"))
    # front and rear wings
    for y0, y1, x in ((2.25, 2.45, 1.15), (-2.55, -2.3, 0.95)):
        fig.add_shape(type="rect", x0=-x, x1=x, y0=y0, y1=y1, line=dict(color=NEON["cyan"], width=1.2),
                      fillcolor="rgba(34,211,238,0.10)")
    fig.add_shape(type="circle", x0=-0.22, x1=0.22, y0=-0.15, y1=0.35, line=dict(color=NEON["violet"], width=1.2))
    pos = {"FL": (-1.0, 1.35), "FR": (1.0, 1.35), "RL": (-1.05, -1.45), "RR": (1.05, -1.45)}
    for w, (x, y) in pos.items():
        t = to_float(get_path(frame, f"car.tyres.{w}.temp_c")) if frame else None
        p = to_float(get_path(frame, f"car.tyres.{w}.pressure_psi")) if frame else None
        wear = to_float(get_path(frame, f"car.tyres.{w}.wear_pct")) if frame else None
        col = tyre_color(t)
        low = p is not None and p < 18.0
        fig.add_shape(type="rect", x0=x - 0.26, x1=x + 0.26, y0=y - 0.48, y1=y + 0.48,
                      line=dict(color=NEON["pink"] if low else col, width=4 if low else 2),
                      fillcolor=col, opacity=0.85)
        side = -1 if x < 0 else 1
        fig.add_annotation(x=x + side * 0.42, y=y + 0.25, xanchor="right" if side < 0 else "left",
                           text=f"<b>{w}</b>", showarrow=False, font=dict(color=NEON["muted"], size=11))
        fig.add_annotation(x=x + side * 0.42, y=y - 0.02, xanchor="right" if side < 0 else "left",
                           text=f"<b>{fmt(t, 0, '°C')}</b>", showarrow=False, font=dict(color=col, size=15))
        fig.add_annotation(x=x + side * 0.42, y=y - 0.30, xanchor="right" if side < 0 else "left",
                           text=f"{fmt(p, 1, ' psi')} · {fmt(wear, 0, '%')}", showarrow=False,
                           font=dict(color=NEON["pink"] if low else NEON["text"], size=11))
    fig.update_layout(margin=dict(l=0, r=0, t=0, b=0), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      showlegend=False, height=330,
                      xaxis=dict(visible=False, range=[-2.4, 2.4], fixedrange=True),
                      yaxis=dict(visible=False, range=[-2.7, 2.6], scaleanchor="x", fixedrange=True))
    return fig


def trend(hist: List[Dict[str, Any]]) -> go.Figure:
    now = time.time()
    x = [r["t"] - now for r in hist]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=x, y=[r["hr"] for r in hist], name="Heart rate [bpm]", mode="lines",
                             line=dict(color=NEON["blue"], width=2), connectgaps=False))
    fig.add_trace(go.Scatter(x=x, y=[r["g"] for r in hist], name="G-force [g]", mode="lines", yaxis="y2",
                             line=dict(color=NEON["orange"], width=1.6), connectgaps=False))
    fig.update_layout(
        margin=dict(l=48, r=48, t=8, b=36), height=250, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=NEON["text"], size=11), legend=dict(orientation="h", y=-0.22, x=0.5, xanchor="center"),
        xaxis=dict(title="seconds", range=[-HISTORY_S, 0], gridcolor=NEON["grid"], zeroline=False),
        yaxis=dict(title="Heart rate", range=[40, 190], gridcolor=NEON["grid"], zeroline=False),
        yaxis2=dict(title="G-force", overlaying="y", side="right", range=[0, 7], dtick=1, showgrid=False, zeroline=False),
        uirevision="keep")
    return fig


def tyre_trend(hist: List[Dict[str, Any]]) -> go.Figure:
    now = time.time()
    x = [r["t"] - now for r in hist]
    fig = go.Figure()
    for w, c in zip(WHEELS, (NEON["cyan"], NEON["pink"], NEON["green"], NEON["yellow"])):
        fig.add_trace(go.Scatter(x=x, y=[r[f"tt_{w}"] for r in hist], name=w, mode="lines", line=dict(color=c, width=1.6)))
    fig.add_hline(y=120, line=dict(color=NEON["pink"], width=1, dash="dot"))
    fig.update_layout(
        margin=dict(l=48, r=12, t=8, b=36), height=200, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=NEON["text"], size=11), legend=dict(orientation="h", y=-0.3, x=0.5, xanchor="center"),
        xaxis=dict(title="seconds", range=[-HISTORY_S, 0], gridcolor=NEON["grid"], zeroline=False),
        yaxis=dict(title="Tyre °C", range=[60, 145], gridcolor=NEON["grid"], zeroline=False), uirevision="keep")
    return fig


def vital(label: str, value: str, unit: str, kind: str, color: str, alert: bool = False) -> html.Div:
    return html.Div(className="vital" + (" vital-alert" if alert else ""), children=[
        html.Div(className="vital-head", children=[html.Span(label), tag(kind)]),
        html.Div(className="vital-value", style={"color": color}, children=[value, html.Span(unit, className="unit")]),
    ])


def bar(label: str, pct: Optional[float], color: str, text: str) -> html.Div:
    p = 0.0 if pct is None else max(0.0, min(100.0, pct))
    return html.Div(className="bar-row", children=[
        html.Span(label, className="bar-label"),
        html.Div(className="bar-track", children=html.Div(className="bar-fill", style={"width": f"{p:.0f}%", "background": color})),
        html.Span(text, className="bar-text"),
    ])


# ----------------------------------------------------------------------------- app
def build_app() -> Dash:
    server = Flask(__name__)

    @server.post("/api/telemetry")
    def ingest():
        frame = request.get_json(force=True, silent=True)
        if not isinstance(frame, dict) or "car" not in frame or "driver" not in frame:
            return jsonify(ok=False, error="expected a telemetry frame"), 400
        STORE.ingest(frame)
        return jsonify(ok=True, seq=frame.get("seq"))

    @server.get("/api/latest")
    def latest():
        frame, age, _h, active, _log, rate = STORE.snapshot()
        return jsonify(frame=frame, age_s=age, rate_hz=rate,
                       alerts=[{"level": a.level, "text": a.text} for a in active])

    app = Dash(__name__, server=server, title="F1 // Live Telemetry", update_title=None)
    app.layout = html.Div(className="page", children=[
        html.Div(className="header", children=[
            html.Div([html.Div("F1 // LIVE TELEMETRY", className="title"),
                      html.Div("ENGINEER CONSOLE", className="subtitle")]),
            html.Div(id="session", className="session"),
        ]),
        html.Div(className="grid", children=[
            html.Div(className="col", children=[
                html.Div(className="panel panel-pink", children=[
                    html.Div(className="panel-title", children=["CAR STATUS — TYRES", html.Span(id="car-src")]),
                    html.Div(className="tyres", children=[
                        dcc.Graph(id="tyre-map", config={"displayModeBar": False, "staticPlot": True}, className="tyre-map"),
                        html.Div(id="tyre-side", className="tyre-side"),
                    ]),
                ]),
                html.Div(className="panel panel-yellow", children=[
                    html.Div("CAR STATUS — BRAKES & DYNAMICS", className="panel-title"),
                    html.Div(id="brakes"),
                ]),
                html.Div(className="panel panel-cyan", children=[
                    html.Div("TYRE TEMPERATURE // LAST 2 MIN", className="panel-title"),
                    dcc.Graph(id="tyre-trend", config={"displayModeBar": False}),
                ]),
            ]),
            html.Div(className="col", children=[
                html.Div(className="panel panel-green", children=[
                    html.Div(className="panel-title", children=["DRIVER VITALS", html.Span(id="driver-src")]),
                    html.Div(id="vitals", className="vitals"),
                ]),
                html.Div(className="panel panel-cyan", children=[
                    html.Div("LOAD // G-FORCE & HEART RATE", className="panel-title"),
                    dcc.Graph(id="trend", config={"displayModeBar": False}),
                ]),
                html.Div(className="panel panel-pink alerts", children=[
                    html.Div("ALERT STREAM", className="panel-title"),
                    html.Div(id="alerts"),
                ]),
            ]),
        ]),
        dcc.Interval(id="tick", interval=500),
    ])

    @app.callback(
        Output("session", "children"), Output("tyre-map", "figure"), Output("tyre-side", "children"),
        Output("brakes", "children"), Output("tyre-trend", "figure"), Output("vitals", "children"),
        Output("trend", "figure"), Output("alerts", "children"), Output("car-src", "children"),
        Output("driver-src", "children"),
        Input("tick", "n_intervals"))
    def refresh(_n):
        frame, age, hist, active, log_, rate = STORE.snapshot()
        live = frame is not None and age is not None and age < 3
        s = (frame or {}).get("session", {})
        mode = s.get("mode", "—") if live else "NO DATA"
        session = [
            html.Div(className="badge-wrap", children=[html.Div("SESSION", className="micro"),
                                                       html.Div(mode, className=f"badge badge-{mode.lower().replace(' ', '')}")]),
            html.Div(className="sess-text", children=[
                html.Div(f"CAR {s.get('car', '--')}", className="car-no"),
                html.Div(f"DRIVER: {s.get('driver', '--')}", className="micro"),
            ]),
            html.Div(className="sess-text", children=[
                html.Div(f"LAP {s.get('lap', '--')}/{s.get('stint_laps', '--')}", className="car-no"),
                html.Div(f"{rate:.1f} Hz · frame {fmt(age, 1, ' s')} old" if frame else "waiting for the hub…", className="micro"),
            ]),
        ]
        car = (frame or {}).get("car", {})
        drv = (frame or {}).get("driver", {})
        src = (frame or {}).get("sources", {})
        fields = src.get("fields", {})

        side = []
        for w in WHEELS:
            wear = to_float(get_path(frame, f"car.tyres.{w}.wear_pct")) if frame else None
            colr = NEON["green"] if (wear or 0) < 60 else NEON["yellow"] if (wear or 0) < 75 else NEON["orange"] if (wear or 0) < 90 else NEON["pink"]
            side.append(bar(w, wear, colr, fmt(wear, 0, "% used")))
        side.insert(0, html.Div(className="micro", children=["TYRE WEAR ", tag(fields.get("tyres", "sim"))]))
        side += [html.Div(className="legend", children=[
            html.Span("● <80", style={"color": NEON["blue"]}), html.Span("● 80–105", style={"color": NEON["green"]}),
            html.Span("● 105–120", style={"color": NEON["yellow"]}), html.Span("● >120 °C", style={"color": NEON["pink"]})])]

        b = car.get("brakes", {})
        brakes = html.Div(className="brakes", children=[
            html.Div(className="stat", children=[html.Div(["BRAKE PRESSURE ", tag(fields.get("brakes", "sim"))], className="micro"),
                                                 html.Div([f"F {fmt(b.get('front_bar'))}", html.Span(" | ", className="sep"),
                                                           f"R {fmt(b.get('rear_bar'))}", html.Span(" bar", className="unit")],
                                                          className="big cyan")]),
            html.Div(className="stat", children=[html.Div("DISC TEMP", className="micro"),
                                                 html.Div([f"F {fmt(b.get('disc_front_c'))}", html.Span(" | ", className="sep"),
                                                           f"R {fmt(b.get('disc_rear_c'))}", html.Span(" °C", className="unit")],
                                                          className="big orange")]),
            html.Div(className="stat", children=[html.Div("SPEED", className="micro"),
                                                 html.Div([fmt(car.get("speed_kmh")), html.Span(" km/h", className="unit")], className="big")]),
            html.Div(className="stat", children=[html.Div("G LAT / LONG", className="micro"),
                                                 html.Div([fmt(car.get("g_lat"), 1), html.Span(" / ", className="sep"),
                                                           fmt(car.get("g_long"), 1), html.Span(" g", className="unit")], className="big violet")]),
        ])

        act_keys = {a.key for a in active}
        dage = to_float(drv.get("age_s"))
        vit = [
            vital("HEART RATE", fmt(drv.get("hr_bpm")), " bpm", fields.get("hr_bpm"), NEON["green"], "hr_high" in act_keys),
            vital("RESPIRATION", fmt(drv.get("resp_bpm")), " br/min", fields.get("resp_bpm"), NEON["pink"], "resp_high" in act_keys),
            vital("SpO₂", fmt(drv.get("spo2_pct")), " %", fields.get("spo2_pct"), NEON["cyan"], "spo2_low" in act_keys),
            vital("SKIN TEMP", fmt(drv.get("skin_temp_c"), 1), " °C", fields.get("skin_temp_c"), NEON["yellow"], "skin_hot" in act_keys),
            vital("PERSPIRATION", fmt(drv.get("perspiration_pct"), 0), " %", fields.get("perspiration_pct"), NEON["violet"]),
            vital("G-FORCE", fmt(drv.get("g_force"), 2), " g", fields.get("g_force"), NEON["orange"], "g_high" in act_keys),
        ]
        dsrc = src.get("driver", "--")
        driver_src = html.Span(f"via {dsrc} · {fmt(dage, 0, ' s')} old", className="micro right")
        car_src = html.Span(f"source: {src.get('car', '--')}", className="micro right")

        if active:
            alerts = [html.Div(className=f"alert alert-{a.level.lower()}",
                               children=[html.Span("⚠ " if a.level == "WARN" else "⛔ "), a.text,
                                         html.Span(time.strftime(" %H:%M:%S", time.localtime(a.since)), className="alert-time")])
                      for a in active[:8]]
        else:
            alerts = [html.Div("All parameters within limits.", className="alert alert-ok")]

        return (session, tyre_map(frame), side, brakes, tyre_trend(hist), vit, trend(hist), alerts,
                car_src, driver_src)

    return app


# ----------------------------------------------------------------------------- built-in demo
def run_demo(rate: float, speedup: float, start_lap: float, faults: bool, driver: str) -> None:
    from mock_generator import LAP_M, MockCar
    car = MockCar(seed=7)
    while car.lap + car.s / LAP_M < start_lap:          # fast-forward without recording
        car.step(1.0)
    schedule = [(25, "hr_spike", "FR"), (45, "overheat", "FR"), (70, "puncture", "RL")] if faults else []
    t0 = last = time.monotonic()
    seq = 0
    while True:
        now = time.monotonic()
        car.step((now - last) * speedup)
        last = now
        while schedule and now - t0 >= schedule[0][0]:
            _, kind, wheel = schedule.pop(0)
            car.inject(kind, wheel, duration=25, amount=0.12 if kind == "puncture" else 0.0)
        seq += 1
        session = car.session_dict("DEMO")
        session["driver"] = driver
        fields = {k: "sim" for k in DRIVER_FIELDS}
        fields.update({"tyres": "sim", "brakes": "sim", "speed": "sim"})
        STORE.ingest(make_frame(seq, session, car.car_dict(), car.driver_dict(),
                                {"driver": "mock", "car": "mock", "fields": fields}))
        time.sleep(1.0 / rate)


def main() -> None:
    ap = argparse.ArgumentParser(description="Pit-wall telemetry dashboard")
    ap.add_argument("--host", default="0.0.0.0", help="0.0.0.0 so the Pi can reach it over Wi-Fi")
    ap.add_argument("--port", type=int, default=8050)
    ap.add_argument("--demo", action="store_true", help="feed the dashboard from the built-in simulator")
    ap.add_argument("--demo-lap", type=float, default=1.0, help="start the demo at this lap")
    ap.add_argument("--speedup", type=float, default=1.0, help="demo simulation speed")
    ap.add_argument("--no-faults", action="store_true", help="demo without the scripted faults")
    ap.add_argument("--driver-name", default="DRIVER")
    a = ap.parse_args()
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    if a.demo:
        threading.Thread(target=run_demo, args=(2.0, a.speedup, a.demo_lap, not a.no_faults, a.driver_name),
                         daemon=True).start()
    app = build_app()
    print(f"Dashboard on http://127.0.0.1:{a.port}  (frames: POST /api/telemetry)")
    app.run(host=a.host, port=a.port, debug=False)


if __name__ == "__main__":
    main()
