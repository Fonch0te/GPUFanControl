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

## Motherboard fans (CPU radiator, case intakes)

The daemon can also drive Super I/O fan headers (Nuvoton NCT67xx and
friends) through the kernel hwmon interface, keyed to CPU temperature,
GPU temperature, or the hotter of the two. Configure under `motherboard`
in `config/gpu-fan-control.json` or on the **Motherboard Fans** UI page:

```json
"motherboard": {
  "enabled": true,
  "chip": "nct6796",
  "fans": {
    "cpu_radiator": { "pwm": 2, "label": "CPU radiator", "source": "cpu",
                      "enabled": true, "curve": [ {"temp_c": 30, "fan_pct": 30}, ... ] },
    "intakes":      { "pwm": 1, "label": "Bottom 180mm", "source": "gpu",
                      "enabled": true, "curve": [ ... ] }
  }
}
```

- `chip` is the hwmon `name` (see `sensors`); hwmon indices change between boots so it is resolved by name.
- `pwm` is the header index: `pwmN` is driven, `fanN_input` is its tachometer.
- `source`: `cpu` (coretemp package), `gpu` (hottest GPU), or `max`.
- Headers not listed, or with `enabled: false`, stay on the BIOS curve. On
  shutdown every claimed header is handed back to the BIOS.
- If the source sensor cannot be read the fan runs at the top of its curve.
- The UI page lists every header with live RPM so you can work out which
  physical fan is on which header after rewiring.

Requires the daemon to run as root (see the system service above), which it
already does for NVML fan writes.

## Troubleshooting

**Fans stuck at 0% / "Unknown Error" from nvidia-settings**

On GeForce cards (e.g. RTX 3090) two independent things must both be correct
before fan control works. The installer checks both automatically; you can also
run them by hand:

```bash
# 1. Kernel module must be proprietary, not open (REQUIRES REBOOT)
sudo bin/fix-open-driver.sh

# 2. nvidia-settings must match the driver version (no reboot)
sudo bin/fix-nvidia-settings.sh
```

Both scripts are idempotent and safe to re-run — they detect whether a fix is
needed and do nothing if everything is already correct.

**Writes fail with "Operation not permitted for the current user"**

NVIDIA driver 520+ routes `nvidia-settings` fan writes through NVML, which
only root may call, so the per-user service can no longer move the fans.
Switch to the root system service (the UI stays per-user):

```bash
sudo bin/install-system-service.sh
```

See [docs/managing-fan-control-rtx-3090.md](docs/managing-fan-control-rtx-3090.md)
for the full RTX 3090 setup/maintenance guide, and
[docs/troubleshooting-fan-control-not-working.md](docs/troubleshooting-fan-control-not-working.md)
for the detailed diagnosis history.
