"""Per-GPU power (watt) limits."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import streamlit as st
from config_manager import ConfigManager, DEFAULT_CONFIG_PATH
from history import read_daemon_status
from models import GpuConfig

st.set_page_config(page_title="Power Limits", layout="wide")


def main():
    st.title("GPU Power Limits")
    st.caption(
        "Caps the board power of each GPU. The driver forgets this on every reboot; "
        "the daemon re-applies it at startup and keeps it enforced."
    )

    try:
        mgr = ConfigManager(DEFAULT_CONFIG_PATH)
        config = mgr.load()
    except Exception as e:
        st.error(f"Failed to load config: {e}")
        return

    status = read_daemon_status()
    if not status.get("running", False):
        st.warning("Daemon is not running. Limits are saved but will not be applied until it starts.")

    gpus = [g for g in status.get("gpu_states", []) if not g.get("error")]
    if not gpus:
        st.info("No GPU data available. Is the daemon running?")
        return

    if st.button("Refresh readings"):
        st.rerun()

    choices: dict[str, int | None] = {}
    cols = st.columns(len(gpus))
    for gpu, col in zip(gpus, cols):
        idx = str(gpu["index"])
        with col:
            st.markdown(f"### GPU {idx} — {gpu.get('name', '')}")
            cur = gpu.get("power_limit_w", -1)
            lo = gpu.get("power_limit_min_w", -1)
            hi = gpu.get("power_limit_max_w", -1)
            default = gpu.get("power_limit_default_w", -1)

            if cur < 0 or lo < 0 or hi <= lo:
                st.info("This card does not expose an adjustable power limit.")
                continue

            a, b = st.columns(2)
            a.metric("Drawing now", f"{gpu['power_draw_w']} W")
            b.metric("Enforced limit", f"{cur} W", delta=f"default {default} W", delta_color="off")

            cfg = config.gpus.get(idx)
            saved = cfg.power_limit_w if cfg is not None else None

            enabled = st.toggle(
                "Limit power", value=saved is not None, key=f"pl_on_{idx}",
                help="Off = use the card's default limit.",
            )
            watts = st.slider(
                "Limit (W)", min_value=int(lo), max_value=int(hi),
                value=int(min(hi, max(lo, saved if saved is not None else default))),
                step=5, disabled=not enabled, key=f"pl_w_{idx}",
            )
            pct = round(watts / default * 100) if default > 0 else 100
            st.caption(f"Allowed range {lo}-{hi} W. {watts} W is {pct}% of the default.")
            choices[idx] = int(watts) if enabled else None

    st.divider()
    if st.button("Apply & Save", type="primary"):
        try:
            for idx, watts in choices.items():
                if idx not in config.gpus:
                    config.gpus[idx] = GpuConfig(enabled=True, label=f"GPU {idx}")
                config.gpus[idx].power_limit_w = watts
            mgr._validate(config)
            mgr.save(config)
            st.success("Saved. The daemon applies it within one poll interval; press Refresh readings to confirm.")
        except Exception as e:
            st.error(f"Not saved: {e}")

    st.caption(
        "Rule of thumb for an RTX 3090: 280-300 W keeps roughly 95% of the performance "
        "with clearly less heat. Below about 250 W the slowdown becomes noticeable."
    )


main()
