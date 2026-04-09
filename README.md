# GPU Fan Control

Custom fan curve daemon + Streamlit UI for 3x NVIDIA RTX A6000 GPUs.

The NVIDIA control panel doesn't work for these cards, so this daemon manages fan speeds based on a configurable temperature-to-speed curve with hysteresis.

## Quick Start

```bash
# First time setup
./install.sh

# Start daemon
bin/start.sh

# Start UI
bin/ui-start.sh
# Open http://localhost:8505
```

## Commands

```
bin/start.sh        Start fan control daemon
bin/stop.sh         Stop daemon (fans reset to auto)
bin/restart.sh      Restart daemon
bin/status.sh       Daemon + GPU status
bin/logs.sh         Follow daemon logs
bin/ui-start.sh     Start Streamlit UI (port 8505)
bin/ui-open.sh      Open browser to UI
bin/help.sh         List all commands
```

## Architecture

- **Daemon** (`src/daemon.py`): Polls temps via pynvml, sets fans via nvidia-settings
- **UI** (`ui/`): Streamlit dashboard, fan curve editor, history charts
- **Communication**: File-based (status.json, history.json, config file)
- **Service**: systemd user service with auto-restart on failure

### Fan Control Flow

1. Read GPU temperatures (pynvml — no X11 needed)
2. Interpolate fan curve to get target speed
3. Apply hysteresis (skip if temp change < 3°C)
4. Set fan speed (nvidia-settings with DISPLAY=:1)
5. Record to history buffer
6. Repeat every 5 seconds

### Safety

- Hardware thermal protection at 93°C regardless of software
- Default curve hits 100% fan at 90°C
- Graceful shutdown resets fans to auto mode
- Daemon crash → systemd restarts in 10s

## Configuration

Edit via the UI (Fan Curve page) or directly:

```
config/gpu-fan-control.json    Active config (hot-reloaded by daemon)
config/default.json            Factory defaults
```

### Fan Curve Profiles

- **Default**: Balanced (30°C→30%, 90°C→100%)
- **Quiet**: Lower fan speeds, higher temps allowed
- **Aggressive**: Higher fan speeds, cooler operation

## Requirements

- NVIDIA driver with Coolbits enabled in xorg.conf
- Python 3.11+
- nvidia-settings CLI
- X11 display (DISPLAY=:1)
