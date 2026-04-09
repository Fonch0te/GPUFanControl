"""GPU Fan Control - Streamlit main app."""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import streamlit as st

st.set_page_config(
    page_title="GPU Fan Control",
    page_icon=":thermometer:",
    layout="wide",
)

st.title("GPU Fan Control Panel")
st.caption("3x RTX A6000 — Fan curve management and temperature monitoring")
st.markdown("Use the sidebar to navigate between Dashboard, Fan Curve Editor, and History.")
