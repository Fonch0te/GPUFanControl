#!/usr/bin/env bash
# Follow daemon logs
journalctl --user -u gpu-fan-control -f --no-pager
