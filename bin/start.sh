#!/usr/bin/env bash
# Start the GPU fan control daemon
systemctl --user start gpu-fan-control
sleep 2
systemctl --user status gpu-fan-control --no-pager
