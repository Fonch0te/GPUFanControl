#!/usr/bin/env bash
# GPU Fan Control - Install Script
# Creates venv, installs deps, sets up systemd user services
set -euo pipefail

PROJECT_DIR="$HOME/gpu-fan-control"
VENV_DIR="$PROJECT_DIR/venv"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
DATA_DIR="$PROJECT_DIR/data"

echo "=== GPU Fan Control Installer ==="
echo ""

# 1. Create directories
echo "[1/7] Creating directories..."
mkdir -p "$DATA_DIR"
mkdir -p "$PROJECT_DIR/config"

# 2. Create virtual environment
echo "[2/7] Creating Python virtual environment..."
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

# 3. Install dependencies
echo "[3/7] Installing dependencies..."
pip install --upgrade pip -q
pip install -r "$PROJECT_DIR/requirements.txt" -q
echo "  Dependencies installed"

# 4. Copy default config if needed
if [ ! -f "$PROJECT_DIR/config/gpu-fan-control.json" ]; then
    cp "$PROJECT_DIR/config/default.json" "$PROJECT_DIR/config/gpu-fan-control.json"
    echo "[4/7] Created default configuration"
else
    echo "[4/7] Configuration already exists, skipping"
fi

# 5. Detect XAUTHORITY
echo "[5/7] Detecting X11 environment..."
XAUTH_PATH=""
if [ -f "/run/user/$(id -u)/gdm/Xauthority" ]; then
    XAUTH_PATH="/run/user/$(id -u)/gdm/Xauthority"
elif [ -f "$HOME/.Xauthority" ]; then
    XAUTH_PATH="$HOME/.Xauthority"
fi

if [ -n "$XAUTH_PATH" ]; then
    echo "  XAUTHORITY: $XAUTH_PATH"
else
    echo "  WARNING: Could not detect XAUTHORITY"
fi

# 6. Install systemd user services
echo "[6/7] Installing systemd services..."
mkdir -p "$SYSTEMD_USER_DIR"

# Copy service files (systemd %h expands to $HOME automatically)
cp "$PROJECT_DIR/systemd/gpu-fan-control.service" "$SYSTEMD_USER_DIR/"
cp "$PROJECT_DIR/systemd/gpu-fan-control-ui.service" "$SYSTEMD_USER_DIR/"

# Add XAUTHORITY if detected
if [ -n "$XAUTH_PATH" ]; then
    # Append XAUTHORITY to the daemon service
    if ! grep -q "XAUTHORITY" "$SYSTEMD_USER_DIR/gpu-fan-control.service"; then
        sed -i "/^Environment=DISPLAY/a Environment=XAUTHORITY=$XAUTH_PATH" \
            "$SYSTEMD_USER_DIR/gpu-fan-control.service"
    fi
fi

# Enable lingering (services run without active session)
if ! loginctl enable-linger "$(whoami)" 2>/dev/null; then
    echo "  WARNING: Could not enable linger. Run manually with sudo:"
    echo "    sudo loginctl enable-linger $(whoami)"
    echo "  Without linger, the fan daemon won't start until you log in."
fi

# Reload and enable
systemctl --user daemon-reload
systemctl --user enable gpu-fan-control.service
echo "  Daemon service enabled"

# 7. Verify setup
echo "[7/7] Verifying setup..."

# Test pynvml
if "$VENV_DIR/bin/python" -c "import pynvml; pynvml.nvmlInit(); count=pynvml.nvmlDeviceGetCount(); pynvml.nvmlShutdown(); print(f'  pynvml OK: {count} GPUs')" 2>/dev/null; then
    :
else
    echo "  WARNING: pynvml verification failed"
fi

# Test nvidia-settings
if DISPLAY=:1 nvidia-settings -q "[gpu:0]/GPUFanControlState" >/dev/null 2>&1; then
    echo "  nvidia-settings: OK"
else
    echo "  WARNING: nvidia-settings cannot access GPU (check DISPLAY)"
fi

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Commands:"
echo "  Start daemon:   systemctl --user start gpu-fan-control"
echo "  Stop daemon:    systemctl --user stop gpu-fan-control"
echo "  Daemon logs:    journalctl --user -u gpu-fan-control -f"
echo "  Start UI:       systemctl --user start gpu-fan-control-ui"
echo "  UI URL:         http://localhost:8502"
echo ""
echo "Quick test (manual):"
echo "  $VENV_DIR/bin/python $PROJECT_DIR/src/daemon.py"
