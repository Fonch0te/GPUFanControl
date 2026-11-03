"""Motherboard fan headers: live view, curve editing, header identification."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from config_manager import ConfigManager, DEFAULT_CONFIG_PATH
from history import read_daemon_status
from hwmon import read_all_headers
from models import FanCurvePoint, MoboFanConfig

st.set_page_config(page_title="Motherboard Fans", layout="wide")

SOURCES = {
    "cpu": "CPU package temp",
    "gpu": "Hottest GPU temp",
    "max": "Hotter of CPU / GPU",
}


def load_config():
    mgr = ConfigManager(DEFAULT_CONFIG_PATH)
    return mgr.load(), mgr


def plot_curve(points: list[dict], marker_temp: int | None, title: str) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=[p["temp_c"] for p in points],
        y=[p["fan_pct"] for p in points],
        mode="lines+markers",
        name="Curve",
        line=dict(color="#1f77b4", width=3),
        marker=dict(size=10),
    ))
    fig.add_hrect(y0=0, y1=100, x0=0, x1=50, fillcolor="green", opacity=0.05, layer="below")
    fig.add_hrect(y0=0, y1=100, x0=50, x1=70, fillcolor="orange", opacity=0.05, layer="below")
    fig.add_hrect(y0=0, y1=100, x0=70, x1=85, fillcolor="red", opacity=0.05, layer="below")
    fig.add_hrect(y0=0, y1=100, x0=85, x1=100, fillcolor="darkred", opacity=0.08, layer="below")
    if marker_temp is not None and marker_temp > 0:
        fig.add_vline(
            x=marker_temp, line_dash="dash", line_color="#2ca02c",
            annotation_text=f"now: {marker_temp}C", annotation_position="top",
        )
    fig.update_layout(
        title=title,
        xaxis_title="Source temperature (C)",
        yaxis_title="Fan duty (%)",
        xaxis=dict(range=[20, 100], dtick=10),
        yaxis=dict(range=[0, 105], dtick=10),
        height=380,
        margin=dict(l=50, r=20, t=50, b=50),
    )
    return fig


def render_live(status: dict, chip: str):
    """Live state of daemon-controlled fans plus every raw header on the chip."""
    cpu_temp = status.get("cpu_temperature_c", -1)
    mobo_states = status.get("mobo_states", [])

    c1, c2 = st.columns([1, 3])
    c1.metric("CPU package", f"{cpu_temp} C" if cpu_temp >= 0 else "n/a")

    with c2:
        if mobo_states:
            cols = st.columns(len(mobo_states))
            for s, col in zip(mobo_states, cols):
                with col:
                    st.markdown(f"**{s['label']}**  \npwm{s['pwm']} · source: {s['source']}")
                    a, b = st.columns(2)
                    a.metric("Duty", f"{s['duty_pct']}%", delta=f"target {s['target_pct']}%")
                    b.metric("RPM", s["rpm"] if s["rpm"] >= 0 else "n/a")
                    if s.get("error"):
                        st.error(s["error"])
        else:
            st.info("Daemon is not reporting motherboard fans (section disabled or daemon stopped).")

    with st.expander("All headers on the controller (for identifying which fan is where)", expanded=False):
        headers = read_all_headers(chip)
        if not headers:
            st.warning(f"hwmon chip '{chip}' not found.")
        else:
            mode_names = {0: "DC", 1: "PWM"}
            enable_names = {0: "full speed", 1: "manual (daemon)", 5: "BIOS smart fan"}
            df = pd.DataFrame([
                {
                    "header": f"pwm{h['pwm']}",
                    "rpm": h["rpm"],
                    "duty %": h["duty_pct"],
                    "control": enable_names.get(h["enable"], f"mode {h['enable']}"),
                    "signal": mode_names.get(h["mode"], "?"),
                }
                for h in headers
            ])
            st.dataframe(df, hide_index=True, use_container_width=True)
            st.caption("Headers showing 0 rpm have no fan connected (or a fan whose tach is not on this header).")


def render_editor(config, mgr):
    mobo = config.motherboard
    st.subheader("Configuration")

    enabled = st.toggle(
        "Enable motherboard fan control",
        value=mobo.enabled,
        help="When off, every header returns to the BIOS curve.",
    )

    fan_names = list(mobo.fans.keys())
    col_sel, col_new = st.columns([3, 1])
    with col_sel:
        selected = st.selectbox("Fan", fan_names, index=0 if fan_names else None)
    with col_new:
        st.write("")
        st.write("")
        if st.button("Add fan"):
            n = 1
            while f"fan_{n}" in mobo.fans:
                n += 1
            mobo.fans[f"fan_{n}"] = MoboFanConfig(
                pwm=1, label=f"Fan {n}", enabled=False, source="cpu",
                curve=[FanCurvePoint(30, 30), FanCurvePoint(60, 60), FanCurvePoint(85, 100)],
            )
            mgr.save(config)
            st.rerun()

    if not selected:
        st.info("No fans configured. Click 'Add fan'.")
        return enabled

    fan = mobo.fans[selected]
    state_key = f"mobo_curve_{selected}"
    if state_key not in st.session_state:
        st.session_state[state_key] = [{"temp_c": p.temp_c, "fan_pct": p.fan_pct} for p in fan.curve]

    c1, c2, c3, c4 = st.columns([3, 1, 2, 1])
    label = c1.text_input("Label", value=fan.label)
    pwm = c2.number_input("Header (pwmN)", min_value=1, max_value=16, value=int(fan.pwm), step=1)
    source = c3.selectbox(
        "Temperature source", list(SOURCES.keys()),
        index=list(SOURCES.keys()).index(fan.source),
        format_func=lambda k: SOURCES[k],
    )
    fan_enabled = c4.toggle("Enabled", value=fan.enabled)

    col_chart, col_table = st.columns([3, 2])
    with col_table:
        st.markdown("**Curve points**")
        df = pd.DataFrame(st.session_state[state_key])
        edited = st.data_editor(
            df,
            column_config={
                "temp_c": st.column_config.NumberColumn("Temp (C)", min_value=0, max_value=100, step=1),
                "fan_pct": st.column_config.NumberColumn("Duty (%)", min_value=0, max_value=100, step=1),
            },
            num_rows="dynamic",
            use_container_width=True,
            key=f"editor_{selected}",
        )
        if edited is not None and not edited.empty:
            st.session_state[state_key] = edited.sort_values("temp_c").to_dict("records")

    with col_chart:
        status = read_daemon_status()
        live = next((s for s in status.get("mobo_states", []) if s["name"] == selected), None)
        marker = live["source_temp_c"] if live else status.get("cpu_temperature_c")
        st.plotly_chart(
            plot_curve(st.session_state[state_key], marker, f"{label} curve"),
            use_container_width=True,
        )

    st.caption(
        "Water-cooler tip: the radiator reacts to coolant temperature, which lags the CPU "
        "sensor by a minute or more. Keep the curve gentle in the 40-65C range and rely on "
        "hysteresis (set on the Fan Curve page) to stop the fans hunting."
    )

    col_save, col_del = st.columns([1, 1])
    with col_save:
        if st.button("Apply & Save", type="primary"):
            try:
                fan.label = label
                fan.pwm = int(pwm)
                fan.source = source
                fan.enabled = fan_enabled
                fan.curve = [
                    FanCurvePoint(temp_c=int(p["temp_c"]), fan_pct=int(p["fan_pct"]))
                    for p in st.session_state[state_key]
                ]
                mobo.enabled = enabled
                mgr._validate(config)
                mgr.save(config)
                st.success("Saved. The daemon reloads within one poll interval.")
            except Exception as e:
                st.error(f"Not saved: {e}")
    with col_del:
        if st.button("Remove this fan"):
            del mobo.fans[selected]
            st.session_state.pop(state_key, None)
            mgr.save(config)
            st.rerun()
    return enabled


def main():
    st.title("Motherboard Fans")

    try:
        config, mgr = load_config()
    except Exception as e:
        st.error(f"Failed to load config: {e}")
        return

    status = read_daemon_status()
    if not status.get("running", False):
        st.warning("Daemon is not running.")

    render_live(status, config.motherboard.chip)
    st.divider()
    render_editor(config, mgr)
    st.caption(f"Last update: {time.strftime('%H:%M:%S')}")


main()
