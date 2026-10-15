"""Streamlit monitoring dashboard.  Run:  streamlit run app.py

All times are shown in IST. Storage is UTC epoch seconds.
Layout: KPI strip + breach banner always on top, then one tab ("page") per topic.
"""
from __future__ import annotations

import dataclasses
import io
import math
import struct
import wave
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from tempmon.config import Config
from tempmon.notifier import MemoryNotifier, format_event
from tempmon.runtime import build
from tempmon.simulator import Fleet, LiveSimulator, backfill
from tempmon.timeutil import IST, fmt_ist, ist_naive, now_ts, to_ist

st.set_page_config(page_title="Temperature Monitor", page_icon="🌡️", layout="wide")

STATUS_ICON = {"OK": "🟢 OK", "ALERT": "🔴 ALERT", "OFFLINE": "⚪ OFFLINE"}

# Hide the browser audio player; the beep still plays (autoplay) but nothing is shown.
st.markdown(
    "<style>audio{display:none !important;}"
    "[data-testid='stElementContainer']:has(audio){display:none !important;}</style>",
    unsafe_allow_html=True)


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


@st.cache_data
def beep_wav() -> bytes:
    """A short 880 Hz beep generated in memory (no audio file needed)."""
    rate, dur, freq = 22050, 0.35, 880
    n = int(rate * dur)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate)
                                  * min(1.0, i / 500, (n - i) / 2000)))
            for i in range(n)))
    return buf.getvalue()


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
    n_dev = st.number_input("Edge devices", 0, 500, 0)
    mode = st.radio("Cadence", ["Real-time (1 reading/min)", "Demo (1 reading / 5 s)"], index=1)
    interval = 60.0 if mode.startswith("Real") else 5.0
    hours = st.select_slider("Backfill history (hours)", [0, 1, 6, 24, 72, 168, 336], value=0)
    running = rt["sim"] is not None and rt["sim"].is_alive()
    c1, c2 = st.columns(2)
    if c1.button("▶ Start / Restart", use_container_width=True):
        if int(n_dev) < 1:
            st.warning("Set Edge devices to 1 or more, then click Start.")
        else:
            with st.spinner("Generating device data…"):
                start_simulator(int(n_dev), interval, 42, hours)
            st.rerun()
    if c2.button("⏹ Stop", use_container_width=True, disabled=not running):
        rt["sim"].stop()
        st.rerun()
    st.caption("🟢 Simulator running" if running else "⚪ Simulator stopped")

    st.subheader("Refresh and sound")
    refresh = st.select_slider("Auto-refresh (seconds)", [2, 5, 10, 30, 60], value=5)
    sound_on = st.toggle("🔔 Beep when a new alert opens", value=False)

    st.subheader("Data retention")
    st.caption(f"Keeping **{proc.cfg.retention_days} days**; purge runs hourly.")
    if st.button("Run retention purge now", use_container_width=True):
        st.toast(f"Purged: {proc.apply_retention()}")

    st.divider()
    confirm = st.checkbox("I want to delete ALL data")
    if st.button("Clear all data", use_container_width=True, disabled=not confirm):
        if rt["sim"] is not None:
            rt["sim"].stop()
            rt["sim"] = None
        proc.store.clear_all()
        rt["feed"].events.clear()
        st.rerun()


# --------------------------------------------------------------------------
# Top strip: KPIs + breach banner (always visible, auto-refreshing)
# --------------------------------------------------------------------------
@st.fragment(run_every=refresh)
def top_panel():
    now = now_ts()
    status = proc.device_status(now)
    open_alerts = proc.store.alerts(only_open=True)
    counts = proc.store.counts()

    online = int(status["online"].sum()) if not status.empty else 0
    k = st.columns(5)
    k[0].metric("Devices online", f"{online}/{len(status)}")
    k[1].metric("Active alerts", len(open_alerts))
    k[2].metric("Readings stored", f"{counts['readings']:,}")
    k[3].metric("Rejected (24h)", len(proc.store.rejected(now - 86400)))
    oldest = counts["oldest_ts"]
    if oldest is None:
        oldest_txt = "-"
    elif to_ist(oldest).date() == to_ist(now).date():
        oldest_txt = to_ist(oldest).strftime("%H:%M")      # today: time only
    else:
        oldest_txt = to_ist(oldest).strftime("%d %b")      # older: date only
    k[4].metric("Oldest data (IST)", oldest_txt, help=f"Oldest reading: {fmt_ist(oldest)}")

    if not open_alerts.empty:
        names = ", ".join(open_alerts["device_id"].head(6))
        st.error(f"🔥 {len(open_alerts)} active breach(es): {names}"
                 f" — threshold {proc.cfg.threshold_c:.1f}°C")

    # Beep once per newly opened alert (not for reminders or resolved messages)
    opened = sum(1 for e in rt["feed"].events if e["type"] == "BREACH_OPENED")
    seen = st.session_state.get("seen_opened")
    if seen is None or opened < seen:      # first load, or data was cleared
        st.session_state["seen_opened"] = opened
    elif opened > seen:
        st.session_state["seen_opened"] = opened
        if sound_on:
            st.audio(beep_wav(), format="audio/wav", autoplay=True)


top_panel()

(tab_fleet, tab_feed, tab_temp, tab_hist, tab_alerts,
 tab_dq, tab_manual, tab_sys) = st.tabs(
    ["📋 Fleet status", "🚨 Live alert feed", "📈 Live temperature", "🗂️ History (14 days)",
     "📜 Alert history", "🧹 Data quality", "✍️ Manual reading", "⚙️ System"])


# --------------------------------------------------------------------------
# Page 1: live alert feed
# --------------------------------------------------------------------------
@st.fragment(run_every=refresh)
def feed_panel():
    st.subheader("Live alert feed")
    feed = list(reversed(rt["feed"].events[-30:]))
    if not feed:
        st.info("No alerts since the dashboard started.")
    for e in feed:
        (st.success if e["type"] == "BREACH_RESOLVED" else st.error)(format_event(e))


with tab_feed:
    feed_panel()


# --------------------------------------------------------------------------
# Page 2: fleet status (large table)
# --------------------------------------------------------------------------
@st.fragment(run_every=refresh)
def fleet_panel():
    st.subheader("Fleet status")
    status = proc.device_status(now_ts())
    if status.empty:
        st.info("No devices yet. Start the simulator or send a manual reading.")
        return
    n = status["status"].value_counts()
    m = st.columns(3)
    m[0].metric("🟢 OK", int(n.get("OK", 0)))
    m[1].metric("🔴 In alert", int(n.get("ALERT", 0)))
    m[2].metric("⚪ Offline", int(n.get("OFFLINE", 0)))

    view = pd.DataFrame({
        "Device": status["device_id"],
        "Status": status["status"].map(STATUS_ICON),
        "Last temp (°C)": status["last_temp_c"].round(1),
        "Last seen (IST)": status["last_ts"].map(lambda t: fmt_ist(t, with_date=False)),
        "Seconds ago": status["age_s"].astype(int),
    })
    order = {"🔴 ALERT": 0, "⚪ OFFLINE": 1, "🟢 OK": 2}
    view = view.sort_values(["Status", "Device"], key=lambda s: s.map(order) if s.name == "Status" else s)

    def colour(v):
        return {"🔴 ALERT": "background-color:#5c1a1a;color:#ffffff",
                "⚪ OFFLINE": "background-color:#3a3f4a;color:#ffffff",
                "🟢 OK": "background-color:#14452f;color:#ffffff"}.get(v, "")

    styler = view.style
    styler = (getattr(styler, "map", None) or styler.applymap)(colour, subset=["Status"])
    st.dataframe(styler, hide_index=True, use_container_width=True,
                 height=min(760, 38 + 36 * len(view)))


with tab_fleet:
    fleet_panel()


# --------------------------------------------------------------------------
# Page 3: live temperature
# --------------------------------------------------------------------------
@st.fragment(run_every=refresh)
def temp_panel():
    st.subheader("Live temperature")
    devices = proc.store.devices()
    if devices.empty:
        st.info("No devices yet. Start the simulator or send a manual reading.")
        return
    c = st.columns([1, 2])
    device_id = c[0].selectbox("Device", list(devices["device_id"]), key="sel_device")
    win = c[1].select_slider("Window", [15, 30, 60, 180, 360, 1440], value=60,
                             format_func=lambda m: f"{m} min" if m < 60 else f"{m // 60} h",
                             key="win")
    now = now_ts()
    df = proc.store.history(device_id, now - win * 60, now)
    if df.empty:
        st.info("No readings for this device in the selected window.")
        return
    s = st.columns(4)
    s[0].metric("Latest (°C)", f"{df['temp_c'].iloc[-1]:.1f}")
    s[1].metric("Min (°C)", f"{df['temp_c'].min():.1f}")
    s[2].metric("Max (°C)", f"{df['temp_c'].max():.1f}")
    s[3].metric("Readings at/above threshold", int(df["breach"].sum()))

    df["t"] = ist_naive(df["ts"])
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df["t"], y=df["temp_c"], mode="lines", name="Temperature",
                             line=dict(color="#2b7de9", width=3)))
    b = df[df["breach"] == 1]
    fig.add_trace(go.Scatter(x=b["t"], y=b["temp_c"], mode="markers", name="Breach",
                             marker=dict(color="#e5322d", size=9)))
    fig.add_hline(y=proc.cfg.threshold_c, line_dash="dash", line_color="#e5322d",
                  annotation_text=f"Threshold {proc.cfg.threshold_c:.1f}°C")
    fig.update_layout(height=520, margin=dict(l=10, r=10, t=30, b=10),
                      xaxis_title="Time (IST)", yaxis_title="°C",
                      legend=dict(orientation="h", y=1.08))
    st.plotly_chart(fig, use_container_width=True)


with tab_temp:
    temp_panel()


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------
with tab_hist:
    st.subheader("Retrieve historical readings")
    now_i = datetime.fromtimestamp(now_ts(), IST)
    dev_df = proc.store.devices()
    devs = ["All devices"] + (list(dev_df["device_id"]) if not dev_df.empty else [])
    lo = (now_i - timedelta(days=proc.cfg.retention_days)).date()
    c = st.columns(5)
    dev = c[0].selectbox("Device", devs)
    d0 = c[1].date_input("From (IST)", (now_i - timedelta(days=1)).date(),
                         min_value=lo, max_value=now_i.date())
    t0 = c[2].time_input("From time", datetime.min.time())
    d1 = c[3].date_input("To (IST)", now_i.date(), min_value=lo, max_value=now_i.date())
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
            fig = go.Figure()
            if dev != "All devices":
                fig.add_trace(go.Scatter(x=h["time_ist"], y=h["temp_c"], mode="lines", name="°C"))
            else:  # fleet view: downsample to per-10-minute min/mean/max to stay fast
                g = h.set_index("time_ist")["temp_c"].resample("10min").agg(["min", "mean", "max"])
                for col, color in (("max", "#e5322d"), ("mean", "#2b7de9"), ("min", "#7aa6e6")):
                    fig.add_trace(go.Scatter(x=g.index, y=g[col], name=col, line=dict(color=color)))
            fig.add_hline(y=proc.cfg.threshold_c, line_dash="dash", line_color="#e5322d")
            fig.update_layout(height=380, xaxis_title="Time (IST)",
                              yaxis_title="°C" if dev != "All devices" else "°C (fleet)",
                              margin=dict(l=10, r=10, t=10, b=10))
            st.plotly_chart(fig, use_container_width=True)
            out = h.assign(time_ist=h["time_ist"].dt.strftime("%Y-%m-%d %H:%M:%S"))[
                ["device_id", "time_ist", "temp_c", "breach"]]
            st.dataframe(out.tail(1000), hide_index=True, use_container_width=True, height=300)
            st.download_button("⬇ Download CSV (IST)", out.to_csv(index=False).encode(),
                               file_name="temperature_history_ist.csv", mime="text/csv")


# --------------------------------------------------------------------------
# Alert history
# --------------------------------------------------------------------------
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
            hide_index=True, use_container_width=True, height=500)


# --------------------------------------------------------------------------
# Data quality
# --------------------------------------------------------------------------
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


# --------------------------------------------------------------------------
# Manual reading
# --------------------------------------------------------------------------
with tab_manual:
    st.subheader("Send a reading manually")
    st.caption("Goes through the same validation, threshold check and alert logic as simulated readings. "
               "Try ERR, a blank value or 999 in the temperature box to see invalid readings rejected. "
               "Tip: use a device ID the simulator does not use (like manual-01), otherwise its next "
               "simulated reading can close the alert within seconds.")
    now_m = datetime.fromtimestamp(now_ts(), IST)
    with st.form("manual_form"):
        f1, f2 = st.columns(2)
        dev_in = f1.text_input("Device ID", value="manual-01")
        temp_in = f2.text_input("Temperature (°C)", value=f"{proc.cfg.threshold_c + 2:.1f}",
                                help="Default is 2 °C above the threshold so the first click opens an alert")
        use_now = st.checkbox("Use current time (IST)", value=True)
        f3, f4 = st.columns(2)
        d_in = f3.date_input("Date (IST), used only if the box above is unticked", now_m.date())
        t_in = f4.time_input("Time (IST), used only if the box above is unticked",
                             now_m.time().replace(second=0, microsecond=0))
        submitted = st.form_submit_button("Send reading", type="primary")
    if submitted:
        ts = now_ts() if use_now else int(datetime.combine(d_in, t_in, IST).timestamp())
        res = proc.ingest({"device_id": dev_in, "timestamp": ts, "temperature_c": temp_in})
        st.session_state["manual_result"] = {"res": res, "ts": ts, "dev": dev_in.strip()}
        st.rerun()  # re-run so the top numbers, banner, feed and beep reflect this reading at once

    shown = st.session_state.pop("manual_result", None)
    if shown:
        res, ts, dev = shown["res"], shown["ts"], shown["dev"]
        kinds = [e["type"] for e in res.events]
        if res.status == "rejected":
            st.error(f"Rejected: {res.reason} (code: {res.code}). Saved on the Data quality page.")
        elif res.status == "duplicate":
            st.warning("Duplicate: this device already has a reading at that exact time. Ignored.")
        elif "BREACH_OPENED" in kinds:
            st.error(f"🔥 ALERT OPENED for {dev} at {fmt_ist(ts)}: at or above the "
                     f"{proc.cfg.threshold_c:.1f}°C threshold.")
        elif res.breach:
            st.warning(f"Stored. {dev} is at or above the {proc.cfg.threshold_c:.1f}°C threshold, but it "
                       "already has an open alert, so no new alert was opened.")
        elif "BREACH_RESOLVED" in kinds:
            st.success(f"Alert for {dev} resolved: back below the {proc.cfg.threshold_c:.1f}°C threshold.")
        else:
            st.success(f"Stored at {fmt_ist(ts)}. Below the {proc.cfg.threshold_c:.1f}°C threshold, "
                       f"so no alert. Send {proc.cfg.threshold_c:.1f} or higher to open one.")
        for e in res.events:
            st.info(format_event(e))
        sim_ids = ({d.device_id for d in rt["fleet"].devices}
                   if rt["fleet"] is not None and rt["sim"] is not None and rt["sim"].is_alive()
                   else set())
        if dev in sim_ids:
            st.warning(f"{dev} is also sent by the running simulator, so its next simulated reading "
                       "can close this alert within seconds. Use another device ID, or stop the simulator.")


# --------------------------------------------------------------------------
# System
# --------------------------------------------------------------------------
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