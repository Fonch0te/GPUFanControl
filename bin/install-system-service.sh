#!/usr/bin/env bash
# install-system-service.sh — run the fan daemon as a root system service
#
# NVIDIA driver >= 520 routes nvidia-settings fan writes through NVML, which
# only root may call. This script:
#   1. stops/disables the per-user unit (systemctl --user gpu-fan-control)
#   2. installs systemd/gpu-fan-control-system.service as
#      /etc/systemd/system/gpu-fan-control.service with real paths filled in
#   3. enables + starts it
#
# Usage:  sudo bin/install-system-service.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run with sudo." >&2
    exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
TARGET_USER="${SUDO_USER:-}"
if [[ -z "$TARGET_USER" || "$TARGET_USER" == root ]]; then
    echo "Run this via sudo from your normal login user (needed for DISPLAY/XAUTHORITY)." >&2
    exit 1
fi
TARGET_UID=$(id -u "$TARGET_USER")
TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
UNIT=/etc/systemd/system/gpu-fan-control.service

echo "[1/3] Stopping the per-user daemon (if present)..."
sudo -u "$TARGET_USER" XDG_RUNTIME_DIR="/run/user/$TARGET_UID" \
    systemctl --user disable --now gpu-fan-control.service 2>/dev/null || true

echo "[2/3] Installing $UNIT ..."
sed -e "s|@PROJECT_DIR@|$PROJECT_DIR|g" \
    -e "s|@UID@|$TARGET_UID|g" \
    -e "s|@HOME@|$TARGET_HOME|g" \
    "$PROJECT_DIR/systemd/gpu-fan-control-system.service" > "$UNIT"
mkdir -p "$TARGET_HOME/gpu-fan-control/data"
systemctl daemon-reload

echo "[3/3] Enabling and starting..."
systemctl enable --now gpu-fan-control.service
sleep 3
systemctl status gpu-fan-control.service --no-pager | head -12
echo ""
echo "Logs:  journalctl -u gpu-fan-control -f"
echo "Fans:  nvidia-smi --query-gpu=temperature.gpu,fan.speed --format=csv,noheader"
