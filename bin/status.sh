#!/usr/bin/env bash
# Show daemon status and current GPU temps
echo "=== Daemon Status ==="
systemctl --user status gpu-fan-control --no-pager 2>/dev/null || echo "Daemon not running"
echo ""
echo "=== GPU Status ==="
nvidia-smi --query-gpu=index,name,temperature.gpu,fan.speed,power.draw --format=csv,noheader
