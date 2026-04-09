"""Live GPU status dashboard with daemon control."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import streamlit as st
from history import read_daemon_status

st.set_page_config(page_title="Dashboard", layout="wide")


def temp_color(temp_c: int) -> str:
    if temp_c < 0:
        return "gray"
    if temp_c < 50:
        return "green"
    if temp_c < 70:
        return "orange"
    if temp_c < 85:
        return "red"
    return "red"


def render_gpu_card(gpu: dict, col):
    """Render a single GPU status card."""
    with col:
        name = gpu.get("name", f"GPU {gpu['index']}")
        error = gpu.get("error")

        if error:
            st.error(f"**{name}** — Error: {error}")
            return

        temp = gpu["temperature_c"]
        fan = gpu["fan_speed_pct"]
        target = gpu["fan_target_pct"]
        power = gpu["power_draw_w"]
        util = gpu["utilization_pct"]
        mem_used = gpu["memory_used_mb"]
        mem_total = gpu["memory_total_mb"]

        color = temp_color(temp)
        st.markdown(f"### {name}")

        c1, c2 = st.columns(2)
        c1.metric("Temperature", f"{temp} C", delta=None)
        c2.metric("Fan Speed", f"{fan}%", delta=f"target: {target}%" if target >= 0 else None)

        c3, c4 = st.columns(2)
        c3.metric("Power", f"{power} W")
        c4.metric("Utilization", f"{util}%")

        mem_pct = round(mem_used / mem_total * 100, 1) if mem_total > 0 else 0
        st.progress(mem_pct / 100, text=f"Memory: {mem_used} / {mem_total} MB ({mem_pct}%)")

        # Color bar for temperature
        if temp < 50:
            st.success(f"Temp OK: {temp} C")
        elif temp < 70:
            st.warning(f"Temp Warm: {temp} C")
        elif temp < 85:
            st.error(f"Temp Hot: {temp} C")
        else:
            st.error(f"TEMP CRITICAL: {temp} C")


def main():
    st.title("GPU Dashboard")

    # Daemon control
    status = read_daemon_status()
    is_running = status.get("running", False)

    col_status, col_actions = st.columns([2, 3])

    with col_status:
        if is_running:
            uptime = status.get("uptime_seconds", 0)
            hours = int(uptime // 3600)
            minutes = int((uptime % 3600) // 60)
            st.success(f"Daemon: RUNNING (PID {status.get('pid', '?')}, uptime: {hours}h {minutes}m)")
        else:
            st.error("Daemon: STOPPED")

    with col_actions:
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("Start Daemon", disabled=is_running):
                import subprocess
                subprocess.run(
                    ["systemctl", "--user", "start", "gpu-fan-control"],
                    capture_output=True,
                )
                time.sleep(2)
                st.rerun()
        with c2:
            if st.button("Stop Daemon", disabled=not is_running):
                import subprocess
                subprocess.run(
                    ["systemctl", "--user", "stop", "gpu-fan-control"],
                    capture_output=True,
                )
                time.sleep(2)
                st.rerun()
        with c3:
            if st.button("Restart Daemon", disabled=not is_running):
                import subprocess
                subprocess.run(
                    ["systemctl", "--user", "restart", "gpu-fan-control"],
                    capture_output=True,
                )
                time.sleep(3)
                st.rerun()

    st.divider()

    # GPU cards
    gpu_states = status.get("gpu_states", [])

    if not gpu_states:
        st.info("No GPU data available. Is the daemon running?")
        return

    cols = st.columns(len(gpu_states))
    for gpu, col in zip(gpu_states, cols):
        render_gpu_card(gpu, col)

    # Auto-refresh
    st.caption(f"Last update: {time.strftime('%H:%M:%S')}")
    time.sleep(5)
    st.rerun()


main()
