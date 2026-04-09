"""Temperature, fan speed, and power history charts."""

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from history import HistoryBuffer

st.set_page_config(page_title="History", layout="wide")

GPU_COLORS = {0: "#2ca02c", 1: "#ff7f0e", 2: "#9467bd"}


def load_history_df() -> pd.DataFrame:
    entries = HistoryBuffer.load_from_disk()
    if not entries:
        return pd.DataFrame()

    data = [
        {
            "timestamp": e.timestamp,
            "datetime": datetime.fromtimestamp(e.timestamp),
            "gpu_index": e.gpu_index,
            "temperature_c": e.temperature_c,
            "fan_speed_pct": e.fan_speed_pct,
            "fan_target_pct": e.fan_target_pct,
            "power_draw_w": e.power_draw_w,
            "utilization_pct": e.utilization_pct,
        }
        for e in entries
    ]
    return pd.DataFrame(data)


def make_temp_chart(df: pd.DataFrame) -> go.Figure:
    fig = go.Figure()

    for gpu_idx in sorted(df["gpu_index"].unique()):
        gpu_df = df[df["gpu_index"] == gpu_idx]
        fig.add_trace(go.Scatter(
            x=gpu_df["datetime"],
            y=gpu_df["temperature_c"],
            mode="lines",
            name=f"GPU {gpu_idx}",
            line=dict(color=GPU_COLORS.get(gpu_idx, "#333"), width=2),
        ))

    # Thermal zone bands
    fig.add_hline(y=50, line_dash="dot", line_color="green", opacity=0.3)
    fig.add_hline(y=70, line_dash="dot", line_color="orange", opacity=0.3)
    fig.add_hline(y=85, line_dash="dot", line_color="red", opacity=0.3)

    fig.update_layout(
        title="Temperature Over Time",
        xaxis_title="Time",
        yaxis_title="Temperature (C)",
        yaxis=dict(range=[20, 100]),
        height=350,
        margin=dict(l=50, r=20, t=50, b=50),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def make_fan_chart(df: pd.DataFrame) -> go.Figure:
    fig = go.Figure()

    for gpu_idx in sorted(df["gpu_index"].unique()):
        gpu_df = df[df["gpu_index"] == gpu_idx]

        # Actual fan speed
        fig.add_trace(go.Scatter(
            x=gpu_df["datetime"],
            y=gpu_df["fan_speed_pct"],
            mode="lines",
            name=f"GPU {gpu_idx} actual",
            line=dict(color=GPU_COLORS.get(gpu_idx, "#333"), width=2),
        ))

        # Target fan speed
        fig.add_trace(go.Scatter(
            x=gpu_df["datetime"],
            y=gpu_df["fan_target_pct"],
            mode="lines",
            name=f"GPU {gpu_idx} target",
            line=dict(color=GPU_COLORS.get(gpu_idx, "#333"), width=1, dash="dot"),
            showlegend=False,
        ))

    fig.update_layout(
        title="Fan Speed Over Time",
        xaxis_title="Time",
        yaxis_title="Fan Speed (%)",
        yaxis=dict(range=[0, 105]),
        height=350,
        margin=dict(l=50, r=20, t=50, b=50),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def make_power_chart(df: pd.DataFrame) -> go.Figure:
    fig = go.Figure()

    for gpu_idx in sorted(df["gpu_index"].unique()):
        gpu_df = df[df["gpu_index"] == gpu_idx]
        fig.add_trace(go.Scatter(
            x=gpu_df["datetime"],
            y=gpu_df["power_draw_w"],
            mode="lines",
            name=f"GPU {gpu_idx}",
            line=dict(color=GPU_COLORS.get(gpu_idx, "#333"), width=2),
        ))

    fig.update_layout(
        title="Power Draw Over Time",
        xaxis_title="Time",
        yaxis_title="Power (W)",
        height=350,
        margin=dict(l=50, r=20, t=50, b=50),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def main():
    st.title("Temperature & Fan History")

    # Time range selector
    time_ranges = {"Last 30 min": 30, "Last 1 hour": 60, "Last 2 hours": 120}
    selected = st.radio("Time Range", list(time_ranges.keys()), horizontal=True, index=1)
    minutes = time_ranges[selected]

    # Load and filter data
    df = load_history_df()

    if df.empty:
        st.info("No history data available. Start the daemon to begin collecting data.")
        return

    cutoff = time.time() - (minutes * 60)
    df = df[df["timestamp"] >= cutoff]

    if df.empty:
        st.info(f"No data in the last {minutes} minutes.")
        return

    st.caption(f"Showing {len(df)} data points from {len(df['gpu_index'].unique())} GPUs")

    # Charts
    st.plotly_chart(make_temp_chart(df), use_container_width=True)
    st.plotly_chart(make_fan_chart(df), use_container_width=True)

    with st.expander("Power Draw", expanded=False):
        st.plotly_chart(make_power_chart(df), use_container_width=True)

    # Auto-refresh
    st.caption(f"Last update: {time.strftime('%H:%M:%S')}")
    time.sleep(10)
    st.rerun()


main()
