"""Interactive fan curve editor with profiles."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from config_manager import ConfigManager, DEFAULT_CONFIG_PATH
from history import read_daemon_status
from models import FanCurvePoint

st.set_page_config(page_title="Fan Curve", layout="wide")


def load_config():
    mgr = ConfigManager(DEFAULT_CONFIG_PATH)
    return mgr.load(), mgr


def plot_fan_curve(curve_points: list[dict], gpu_temps: list[dict] = None):
    """Plot the fan curve with optional current GPU temp markers."""
    temps = [p["temp_c"] for p in curve_points]
    speeds = [p["fan_pct"] for p in curve_points]

    fig = go.Figure()

    # Fan curve line
    fig.add_trace(go.Scatter(
        x=temps,
        y=speeds,
        mode="lines+markers",
        name="Fan Curve",
        line=dict(color="#1f77b4", width=3),
        marker=dict(size=10),
    ))

    # Thermal zone bands
    fig.add_hrect(y0=0, y1=100, x0=0, x1=50, fillcolor="green", opacity=0.05, layer="below")
    fig.add_hrect(y0=0, y1=100, x0=50, x1=70, fillcolor="orange", opacity=0.05, layer="below")
    fig.add_hrect(y0=0, y1=100, x0=70, x1=85, fillcolor="red", opacity=0.05, layer="below")
    fig.add_hrect(y0=0, y1=100, x0=85, x1=100, fillcolor="darkred", opacity=0.08, layer="below")

    # Current GPU temps as vertical lines
    colors = ["#2ca02c", "#ff7f0e", "#9467bd"]
    if gpu_temps:
        for i, gpu in enumerate(gpu_temps):
            if gpu.get("error") is None and gpu.get("temperature_c", -1) > 0:
                color = colors[i % len(colors)]
                fig.add_vline(
                    x=gpu["temperature_c"],
                    line_dash="dash",
                    line_color=color,
                    annotation_text=f"GPU {gpu['index']}: {gpu['temperature_c']}C",
                    annotation_position="top",
                )

    fig.update_layout(
        title="Fan Curve",
        xaxis_title="Temperature (C)",
        yaxis_title="Fan Speed (%)",
        xaxis=dict(range=[20, 100], dtick=10),
        yaxis=dict(range=[0, 105], dtick=10),
        height=450,
        margin=dict(l=50, r=20, t=50, b=50),
    )

    return fig


def main():
    st.title("Fan Curve Editor")

    try:
        config, mgr = load_config()
    except Exception as e:
        st.error(f"Failed to load config: {e}")
        return

    # Profile selector
    st.subheader("Profiles")
    profile_names = list(config.profiles.keys())

    col_profile, col_load = st.columns([3, 1])
    with col_profile:
        selected_profile = st.selectbox(
            "Load profile",
            profile_names,
            index=0 if profile_names else None,
        )
    with col_load:
        st.write("")  # spacing
        st.write("")
        load_profile = st.button("Load Profile")

    if load_profile and selected_profile:
        profile_points = config.profiles[selected_profile]
        st.session_state["curve_data"] = [
            {"temp_c": p.temp_c, "fan_pct": p.fan_pct} for p in profile_points
        ]
        st.rerun()

    st.divider()

    # Initialize curve data from config if not in session state
    if "curve_data" not in st.session_state:
        st.session_state["curve_data"] = [
            {"temp_c": p.temp_c, "fan_pct": p.fan_pct} for p in config.fan_curve
        ]

    # Layout: chart + table side by side
    col_chart, col_table = st.columns([3, 2])

    with col_table:
        st.subheader("Curve Points")
        df = pd.DataFrame(st.session_state["curve_data"])
        edited_df = st.data_editor(
            df,
            column_config={
                "temp_c": st.column_config.NumberColumn(
                    "Temp (C)", min_value=0, max_value=100, step=1
                ),
                "fan_pct": st.column_config.NumberColumn(
                    "Fan (%)", min_value=0, max_value=100, step=1
                ),
            },
            num_rows="dynamic",
            use_container_width=True,
            key="curve_editor",
        )

        # Update session state from editor
        if edited_df is not None and not edited_df.empty:
            new_data = edited_df.sort_values("temp_c").to_dict("records")
            st.session_state["curve_data"] = new_data

    with col_chart:
        # Get current GPU temps for overlay
        status = read_daemon_status()
        gpu_temps = status.get("gpu_states", [])

        fig = plot_fan_curve(st.session_state["curve_data"], gpu_temps)
        st.plotly_chart(fig, use_container_width=True)

    st.divider()

    # Settings
    st.subheader("Settings")
    col_hyst, col_poll = st.columns(2)
    with col_hyst:
        hysteresis = st.slider(
            "Hysteresis (C)",
            min_value=0.0,
            max_value=10.0,
            value=float(config.hysteresis_celsius),
            step=0.5,
            help="Temp must change by this amount before fan speed adjusts. Prevents oscillation.",
        )
    with col_poll:
        poll_interval = st.slider(
            "Poll Interval (seconds)",
            min_value=1.0,
            max_value=30.0,
            value=float(config.poll_interval_seconds),
            step=1.0,
            help="How often the daemon checks GPU temperatures.",
        )

    # Save buttons
    st.divider()
    col_save, col_reset = st.columns(2)

    with col_save:
        if st.button("Apply & Save", type="primary"):
            try:
                curve_data = st.session_state["curve_data"]
                new_curve = [
                    FanCurvePoint(temp_c=int(p["temp_c"]), fan_pct=int(p["fan_pct"]))
                    for p in curve_data
                ]
                config.fan_curve = new_curve
                config.hysteresis_celsius = hysteresis
                config.poll_interval_seconds = poll_interval
                mgr.save(config)
                st.success("Configuration saved. Daemon will reload automatically.")
            except Exception as e:
                st.error(f"Failed to save: {e}")

    with col_reset:
        if st.button("Reset to Default"):
            default_mgr = ConfigManager(
                Path(__file__).parent.parent.parent / "config" / "default.json"
            )
            try:
                default_config = default_mgr.load()
                st.session_state["curve_data"] = [
                    {"temp_c": p.temp_c, "fan_pct": p.fan_pct}
                    for p in default_config.fan_curve
                ]
                st.rerun()
            except Exception as e:
                st.error(f"Failed to load defaults: {e}")


main()
