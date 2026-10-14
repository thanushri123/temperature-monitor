"""Streamlit monitoring dashboard.  Run:  streamlit run app.py

All times are shown in IST. Storage is UTC epoch seconds.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from tempmon.config import Config
from tempmon.notifier import MemoryNotifier
from tempmon.runtime import build
from tempmon.simulator import Fleet, LiveSimulator, backfill
from tempmon.timeutil import IST, fmt_ist, ist_naive, now_ts

st.set_page_config(page_title="Temperature Monitor", page_icon="🌡️", layout="wide")

STATUS_ICON = {"OK": "🟢 OK", "ALERT": "🔴 ALERT", "OFFLINE": "⚪ OFFLINE"}


# --------------------------------------------------------------------------
# Runtime (one per server process; survives Streamlit reruns)
# --------------------------------------------------------------------------
@st.cache_resource
def get_runtime():
    feed = MemoryNotifier()
    proc = build(Config(), extra_notifiers=[feed])
    return {"proc": proc, "feed": feed, "sim": None, "fleet": None, "settings": None}


rt = get_runtime()
proc = rt["proc"]


def start_simulator(n_devices: int, interval_s: float, seed: int, backfill_hours: float):
    if rt["sim"] is not None:
        rt["sim"].stop()
        rt["sim"] = None
    fleet = Fleet(n_devices, seed)
    if backfill_hours > 0:
        backfill(proc, fleet, backfill_hours)
    sim = LiveSimulator(proc, fleet, interval_s)
    sim.start()
    rt.update(sim=sim, fleet=fleet, settings=(n_devices, interval_s, seed))


# --------------------------------------------------------------------------
# Sidebar
# --------------------------------------------------------------------------
with st.sidebar:
    st.title("🌡️ Temperature Monitor")
    st.caption(f"Now: **{fmt_ist(now_ts())}**")

    st.subheader("Alert rule")
    thr = st.slider("Threshold (°C) — alert when temp ≥", 20.0, 80.0,
                    float(proc.cfg.threshold_c), 0.5)
    if thr != proc.cfg.threshold_c:
        proc.cfg = dataclasses.replace(proc.cfg, threshold_c=thr)

    st.subheader("Device simulator")
    n_dev = st.number_input("Edge devices", 1, 500, 12)
    mode = st.radio("Cadence", ["Real-time (1 reading/min)", "Demo (1 reading / 5 s)"], index=1)
    interval = 60.0 if mode.startswith("Real") else 5.0
    hours = st.select_slider("Backfill history (hours)", [0, 1, 6, 24, 72, 168, 336], value=6)
    running = rt["sim"] is not None and rt["sim"].is_alive()
    c1, c2 = st.columns(2)
    if c1.button("▶ Start / Restart", use_container_width=True):
        with st.spinner("Generating device data…"):
            start_simulator(int(n_dev), interval, 42, hours)
        st.rerun()
    if c2.button("⏹ Stop", use_container_width=True, disabled=not running):
        rt["sim"].stop()
        st.rerun()
    st.caption("🟢 Simulator running" if running else "⚪ Simulator stopped")

    st.subheader("Refresh")
    refresh = st.select_slider("Auto-refresh (seconds)", [2, 5, 10, 30, 60], value=5)

    st.subheader("Data retention")
    st.caption(f"Keeping **{proc.cfg.retention_days} days**; purge runs hourly.")
    if st.button("Run retention purge now", use_container_width=True):
        st.toast(f"Purged: {proc.apply_retention()}")

# Auto-start once so the dashboard is never empty on first open.
if rt["sim"] is None and proc.store.counts()["readings"] == 0:
    with st.spinner("Starting simulator and generating 6 h of history…"):
        start_simulator(12, 5.0, 42, 6)
    st.rerun()


# --------------------------------------------------------------------------
# Live panel (auto-refreshing fragment)
# --------------------------------------------------------------------------
def device_chart(device_id: str, window_min: int) -> go.Figure:
    now = now_ts()
    df = proc.store.history(device_id, now - window_min * 60, now)
    fig = go.Figure()
    if not df.empty:
        df["t"] = ist_naive(df["ts"])
        fig.add_trace(go.Scatter(x=df["t"], y=df["temp_c"], mode="lines", name="Temperature",
                                 line=dict(color="#2b7de9", width=2)))
        b = df[df["breach"] == 1]
        fig.add_trace(go.Scatter(x=b["t"], y=b["temp_c"], mode="markers", name="Breach",
                                 marker=dict(color="#e5322d", size=8)))
    fig.add_hline(y=proc.cfg.threshold_c, line_dash="dash", line_color="#e5322d",
                  annotation_text=f"Threshold {proc.cfg.threshold_c:.1f}°C")
    fig.update_layout(height=360, margin=dict(l=10, r=10, t=30, b=10), xaxis_title="Time (IST)",
                      yaxis_title="°C", legend=dict(orientation="h", y=1.1))
    return fig


@st.fragment(run_every=refresh)
def live_panel():
    now = now_ts()
    status = proc.device_status(now)
    open_alerts = proc.store.alerts(only_open=True)
    counts = proc.store.counts()
    since24 = now - 86400

    online = int(status["online"].sum()) if not status.empty else 0
    total = len(status)
    k = st.columns(5)
    k[0].metric("Devices online", f"{online}/{total}")
    k[1].metric("Active alerts", len(open_alerts))
    k[2].metric("Readings stored", f"{counts['readings']:,}")
    k[3].metric("Rejected (24h)", len(proc.store.rejected(since24)))
    k[4].metric("Oldest data (IST)", fmt_ist(counts["oldest_ts"], with_date=True)[:17]
                if counts["oldest_ts"] else "-")

    if not open_alerts.empty:
        names = ", ".join(open_alerts["device_id"].head(6))
        st.error(f"🔥 {len(open_alerts)} active breach(es): {names}"
                 f" — threshold {proc.cfg.threshold_c:.1f}°C")

    left, right = st.columns([1, 1.4])
    with left:
        st.subheader("Fleet status")
        if status.empty:
            st.info("No devices yet.")
            selected = None
        else:
            view = status.assign(
                Status=status["status"].map(STATUS_ICON),
                Device=status["device_id"],
                **{"Last temp (°C)": status["last_temp_c"].round(1),
                   "Last seen (IST)": status["last_ts"].map(lambda t: fmt_ist(t, with_date=False))},
            )[["Device", "Status", "Last temp (°C)", "Last seen (IST)"]]
            order = {"🔴 ALERT": 0, "⚪ OFFLINE": 1, "🟢 OK": 2}
            view = view.sort_values("Status", key=lambda s: s.map(order))
            st.dataframe(view, hide_index=True, use_container_width=True, height=380)
            selected = st.selectbox("Inspect device", list(status["device_id"]),
                                    key="sel_device")
    with right:
        st.subheader("Live temperature")
        if selected:
            win = st.select_slider("Window", [15, 30, 60, 180, 360, 1440], value=60,
                                   format_func=lambda m: f"{m} min" if m < 60 else f"{m // 60} h",
                                   key="win")
            st.plotly_chart(device_chart(selected, win), use_container_width=True)

    st.subheader("Live alert feed")
    feed = list(reversed(rt["feed"].events[-15:]))
    if feed:
        from tempmon.notifier import format_event
        for e in feed:
            (st.error if e["type"] != "BREACH_RESOLVED" else st.success)(format_event(e))
    else:
        st.caption("No alerts since the dashboard started.")


tab_live, tab_alerts, tab_hist, tab_dq, tab_sys = st.tabs(
    ["📡 Live monitoring", "🚨 Alerts", "🗂️ History (14 days)", "🧹 Data quality", "⚙️ System"])

with tab_live:
    live_panel()

with tab_alerts:
    st.subheader("Alert history")
    a = proc.store.alerts(now_ts() - proc.cfg.retention_days * 86400)
    if a.empty:
        st.info("No alerts in the retention window.")
    else:
        a["Opened (IST)"] = a["opened_ts"].map(fmt_ist)
        a["Resolved (IST)"] = a["resolved_ts"].map(lambda t: fmt_ist(t) if pd.notna(t) else "— open —")
        a["Duration (min)"] = ((a["resolved_ts"].fillna(now_ts()) - a["opened_ts"]) / 60).round(0)
        a["State"] = a["resolved_ts"].map(lambda t: "🟢 Resolved" if pd.notna(t) else "🔴 Open")
        st.dataframe(
            a[["State", "device_id", "Opened (IST)", "Resolved (IST)", "Duration (min)",
               "peak_c", "breach_count", "threshold_c"]].rename(columns={
                "device_id": "Device", "peak_c": "Peak °C", "breach_count": "Breaching readings",
                "threshold_c": "Threshold °C"}),
            hide_index=True, use_container_width=True)

with tab_hist:
    st.subheader("Retrieve historical readings")
    now_i = datetime.fromtimestamp(now_ts(), IST)
    c = st.columns(5)
    devs = ["All devices"] + list(proc.store.devices()["device_id"]) if not proc.store.devices().empty else ["All devices"]
    dev = c[0].selectbox("Device", devs)
    d0 = c[1].date_input("From (IST)", (now_i - timedelta(days=1)).date(),
                         min_value=(now_i - timedelta(days=proc.cfg.retention_days)).date(),
                         max_value=now_i.date())
    t0 = c[2].time_input("From time", datetime.min.time())
    d1 = c[3].date_input("To (IST)", now_i.date(),
                         min_value=(now_i - timedelta(days=proc.cfg.retention_days)).date(),
                         max_value=now_i.date())
    t1 = c[4].time_input("To time", now_i.time().replace(microsecond=0))
    start = int(datetime.combine(d0, t0, IST).timestamp())
    end = int(datetime.combine(d1, t1, IST).timestamp())
    if start >= end:
        st.warning("'From' must be before 'To'.")
    else:
        h = proc.store.history(None if dev == "All devices" else dev, start, end)
        if h.empty:
            st.info("No readings in this range.")
        else:
            h["time_ist"] = ist_naive(h["ts"])
            st.caption(f"{len(h):,} readings · {int(h['breach'].sum()):,} at/above threshold")
            if dev != "All devices":
                fig = go.Figure(go.Scatter(x=h["time_ist"], y=h["temp_c"], mode="lines"))
                fig.add_hline(y=proc.cfg.threshold_c, line_dash="dash", line_color="#e5322d")
                fig.update_layout(height=320, xaxis_title="Time (IST)", yaxis_title="°C",
                                  margin=dict(l=10, r=10, t=10, b=10))
                st.plotly_chart(fig, use_container_width=True)
            else:  # fleet view: downsample to per-10-minute min/mean/max to stay fast
                g = h.set_index("time_ist")["temp_c"].resample("10min").agg(["min", "mean", "max"])
                fig = go.Figure()
                for col, color in (("max", "#e5322d"), ("mean", "#2b7de9"), ("min", "#7aa6e6")):
                    fig.add_trace(go.Scatter(x=g.index, y=g[col], name=col, line=dict(color=color)))
                fig.add_hline(y=proc.cfg.threshold_c, line_dash="dash", line_color="#e5322d")
                fig.update_layout(height=320, xaxis_title="Time (IST)", yaxis_title="°C (fleet)",
                                  margin=dict(l=10, r=10, t=10, b=10))
                st.plotly_chart(fig, use_container_width=True)
            out = h.assign(time_ist=h["time_ist"].dt.strftime("%Y-%m-%d %H:%M:%S"))[
                ["device_id", "time_ist", "temp_c", "breach"]]
            st.dataframe(out.tail(1000), hide_index=True, use_container_width=True, height=260)
            st.download_button("⬇ Download CSV (IST)", out.to_csv(index=False).encode(),
                               file_name="temperature_history_ist.csv", mime="text/csv")

with tab_dq:
    st.subheader("Rejected / invalid readings (quarantined, never alerted)")
    r = proc.store.rejected(now_ts() - proc.cfg.retention_days * 86400)
    if r.empty:
        st.success("No invalid readings.")
    else:
        st.bar_chart(r["code"].value_counts())
        r["Received (IST)"] = r["received_ts"].map(fmt_ist)
        st.dataframe(r[["Received (IST)", "code", "reason", "payload"]], hide_index=True,
                     use_container_width=True)

with tab_sys:
    cnt = proc.store.counts()
    st.subheader("System")
    st.json({
        "threshold_c": proc.cfg.threshold_c,
        "alert_condition": "temperature >= threshold",
        "retention_days": proc.cfg.retention_days,
        "renotify_minutes": proc.cfg.renotify_minutes,
        "offline_after_seconds": proc.cfg.offline_after_seconds,
        "valid_sensor_range_c": [proc.cfg.min_valid_c, proc.cfg.max_valid_c],
        "readings_stored": cnt["readings"],
        "oldest_reading_ist": fmt_ist(cnt["oldest_ts"]),
        "newest_reading_ist": fmt_ist(cnt["newest_ts"]),
        "timezone": "IST (UTC+05:30)",
    })
