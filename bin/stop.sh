#!/usr/bin/env bash
# Stop the GPU fan control daemon (fans reset to auto)
systemctl --user stop gpu-fan-control
echo "Daemon stopped. Fans reset to auto mode."
