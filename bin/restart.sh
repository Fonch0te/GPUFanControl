#!/usr/bin/env bash
# Restart the GPU fan control daemon
systemctl --user restart gpu-fan-control
sleep 2
systemctl --user status gpu-fan-control --no-pager
