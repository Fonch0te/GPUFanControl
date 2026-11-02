#!/usr/bin/env bash
# GPU Fan Control - Install Script
# Creates venv, installs deps, sets up systemd user services
#
# Supports NVIDIA GeForce (consumer) and Quadro/RTX Pro (workstation) GPUs.
# Two GeForce-specific issues are auto-detected and fixed so fan control works:
#   1. Open kernel modules (nvidia-driver-*-open / linux-modules-nvidia-*-open)
#      do not support fan speed writes -> switched to proprietary
#      (see bin/fix-open-driver.sh; requires a reboot).
#   2. Ubuntu ships a stale nvidia-settings (510.x) that fails GPUTargetFanSpeed
#      writes against newer drivers -> upgraded to match the driver
#      (see bin/fix-nvidia-settings.sh; no reboot needed).
set -euo pipefail

# Use the directory of the script's parent as the project root
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$PROJECT_DIR/venv"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
DATA_DIR="$PROJECT_DIR/data"

echo "=== GPU Fan Control Installer ==="
echo ""

# 1. Create directories
echo "[1/10] Creating directories..."
mkdir -p "$DATA_DIR"
mkdir -p "$PROJECT_DIR/config"

# 2. Create virtual environment
echo "[2/10] Creating Python virtual environment..."
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

# 3. Install dependencies
echo "[3/10] Installing dependencies..."
pip install --upgrade pip -q
pip install -r "$PROJECT_DIR/requirements.txt" -q
echo "  Dependencies installed"

# 4. Copy default config if needed
if [ ! -f "$PROJECT_DIR/config/gpu-fan-control.json" ]; then
    cp "$PROJECT_DIR/config/default.json" "$PROJECT_DIR/config/gpu-fan-control.json"
    echo "[4/10] Created default configuration"
else
    echo "[4/10] Configuration already exists, skipping"
fi

# 5. Check for open driver + GeForce fan control issue (kernel module)
echo "[5/10] Checking NVIDIA kernel module (open vs proprietary)..."
NEEDS_REBOOT=false
if sudo "$PROJECT_DIR/bin/fix-open-driver.sh" --yes; then
    echo "  Driver was switched from open to proprietary."
    NEEDS_REBOOT=true
else
    echo "  Driver check passed (no switch needed)."
fi

# 6. Ensure nvidia-settings matches the driver (userspace fan-write fix)
echo "[6/10] Checking nvidia-settings version vs driver..."
if sudo "$PROJECT_DIR/bin/fix-nvidia-settings.sh" --yes; then
    echo "  nvidia-settings matches the driver."
else
    echo "  WARNING: nvidia-settings could not be matched to the driver."
    echo "           Fan speed writes may fail with 'Unknown Error'."
fi

# 7. Configure Coolbits for manual fan control
echo "[7/10] Configuring Coolbits for manual fan control..."
sudo nvidia-xconfig --cool-bits=28 --allow-empty-initial-configuration
echo "  Coolbits=28 written to /etc/X11/xorg.conf via nvidia-xconfig"

# 8. Detect XAUTHORITY
echo "[8/10] Detecting X11 environment..."
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

# 9. Install systemd user services
echo "[9/10] Installing systemd services..."
mkdir -p "$SYSTEMD_USER_DIR"

# Copy service files (systemd %h expands to $HOME automatically)
cp "$PROJECT_DIR/systemd/gpu-fan-control.service" "$SYSTEMD_USER_DIR/"
cp "$PROJECT_DIR/systemd/gpu-fan-control-ui.service" "$SYSTEMD_USER_DIR/"

# Fix paths in service files to use the actual project directory
sed -i "s|%h/gpu-fan-control/|$PROJECT_DIR/|g" "$SYSTEMD_USER_DIR/gpu-fan-control.service"
sed -i "s|%h/gpu-fan-control/|$PROJECT_DIR/|g" "$SYSTEMD_USER_DIR/gpu-fan-control-ui.service"

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

# 10. Verify setup
echo "[10/10] Verifying setup..."

# Test pynvml
if "$VENV_DIR/bin/python" -c "import pynvml; pynvml.nvmlInit(); count=pynvml.nvmlDeviceGetCount(); pynvml.nvmlShutdown(); print(f'  pynvml OK: {count} GPUs')" 2>/dev/null; then
    :
else
    echo "  WARNING: pynvml verification failed"
fi

# Test nvidia-settings can reach the GPU
if DISPLAY=:1 nvidia-settings -q "[gpu:0]/GPUFanControlState" >/dev/null 2>&1; then
    echo "  nvidia-settings: OK"

    # Definitive test: a real fan-speed WRITE must succeed. The query/state
    # check above passes even when writes fail with "Unknown Error", so write
    # the fan's current speed back to itself (a harmless no-op) and look for
    # an error in the output (nvidia-settings exits 0 even on failure).
    CUR=$(DISPLAY=:1 nvidia-settings -t -q "[fan:0]/GPUCurrentFanSpeed" 2>/dev/null || echo 30)
    CUR=${CUR:-30}
    WRITE_OUT=$(DISPLAY=:1 nvidia-settings \
        -a "[gpu:0]/GPUFanControlState=1" \
        -a "[fan:0]/GPUTargetFanSpeed=$CUR" 2>&1 || true)
    DISPLAY=:1 nvidia-settings -a "[gpu:0]/GPUFanControlState=0" >/dev/null 2>&1 || true
    if echo "$WRITE_OUT" | grep -qiE 'error|unknown'; then
        echo "  WARNING: fan-speed WRITE failed — fan control will not work:"
        echo "    $(echo "$WRITE_OUT" | grep -iE 'error|unknown' | head -1)"
        echo "    Check the kernel module (bin/fix-open-driver.sh) and"
        echo "    nvidia-settings version (bin/fix-nvidia-settings.sh)."
    else
        echo "  fan-speed write: OK"
    fi
else
    echo "  WARNING: nvidia-settings cannot access GPU (check DISPLAY)"
fi

echo ""
echo "=== Installation Complete ==="
echo ""
if [[ "$NEEDS_REBOOT" == true ]]; then
    echo "*** REBOOT REQUIRED ***"
    echo "The NVIDIA driver was switched from open to proprietary."
    echo "Fan control will not work until you reboot."
    echo ""
fi
echo "Commands:"
echo "  Start daemon:   systemctl --user start gpu-fan-control"
echo "  Stop daemon:    systemctl --user stop gpu-fan-control"
echo "  Daemon logs:    journalctl --user -u gpu-fan-control -f"
echo "  Start UI:       systemctl --user start gpu-fan-control-ui"
echo "  UI URL:         http://localhost:8505"
echo ""
echo "Quick test (manual):"
echo "  $VENV_DIR/bin/python $PROJECT_DIR/src/daemon.py"
