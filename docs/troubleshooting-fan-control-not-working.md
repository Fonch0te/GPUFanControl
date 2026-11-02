# Troubleshooting: Fan Control Not Working (Fans Stuck at 0%)

**Date:** 2026-04-27  
**Hardware:** 2× NVIDIA GeForce RTX 3090  
**OS:** Ubuntu 24.04, kernel 6.17.0-20-generic  
**Driver:** 580.126.09

---

## Symptom

The UI (http://localhost:8505) shows correct temperature readings and non-zero fan targets
(e.g. GPU 0: 34°C → target 32%, GPU 1: 46°C → target 46%), but both fans report 0% actual
speed and 0 RPM. The daemon shows no errors in its logs.

---

## Diagnosis

### Step 1 — Confirm fans are truly stopped

```bash
nvidia-smi --query-gpu=index,fan.speed,temperature.gpu --format=csv,noheader
# Output: 0, 0 %, 33   and   1, 0 %, 46
```

Both fans at 0% despite elevated temperatures.

### Step 2 — Confirm manual fan control is enabled

```bash
DISPLAY=:1 nvidia-settings -q "[gpu:0]/GPUFanControlState"
# Output: 1  (manual control is on)
```

`GPUFanControlState=1` is set, so the daemon's init succeeded.

### Step 3 — Try setting a fan speed manually

```bash
DISPLAY=:1 nvidia-settings -a "[fan:0]/GPUTargetFanSpeed=40"
# Output: ERROR: Error assigning value 40 to attribute 'GPUTargetFanSpeed'
#         (AgentAI:1[fan:0]) as specified in assignment
#         '[fan:0]/GPUTargetFanSpeed=40' (Unknown Error).
# Exit code: 0
```

The assignment fails with "Unknown Error". Critically, **nvidia-settings exits with
code 0 even on error**, so the daemon's `_run_setting` method (which only checks
`returncode != 0`) silently treats this as success.

### Step 4 — Confirm the kernel module is the open variant

```bash
cat /proc/driver/nvidia/version
# NVRM version: NVIDIA UNIX Open Kernel Module for x86_64  580.126.09 ...
```

**Root cause identified.** The open kernel modules (`NVIDIA UNIX Open Kernel Module`)
do not support `GPUTargetFanSpeed` writes on GeForce consumer GPUs. This is a known
driver limitation: open modules were designed for datacenter/professional GPUs
(A100, H100, etc.) and fan control via nvidia-settings is unsupported for GeForce.

### Why the open module was loaded

On Ubuntu 24.04, the `nvidia-driver-580` metapackage installs the open kernel modules
by default for Ampere (RTX 30xx) and later GPUs. The open modules ship via
`linux-modules-nvidia-580-open-6.17.0-20-generic`, which was installed and loaded.

The original `fix-open-driver.sh` only checked for a `nvidia-driver-*-open` package
(the older detection method), so it exited without doing anything — missing this case.

---

## Fix

Run the updated `fix-open-driver.sh` as root. It now detects both the old
(`nvidia-driver-*-open` metapackage) and new (Ubuntu 24.04 `linux-modules-nvidia-*-open-*`)
layouts:

```bash
sudo bin/fix-open-driver.sh
# Then reboot
```

The script will:
1. Install `linux-modules-nvidia-580-6.17.0-20-generic` (proprietary pre-built module)
2. Remove `linux-modules-nvidia-580-open-6.17.0-20-generic`
3. Run `update-initramfs -u`
4. Prompt for reboot

After reboot, verify the proprietary module is loaded:

```bash
cat /proc/driver/nvidia/version
# Should NOT contain "Open Kernel Module"

# Then confirm fan writes work:
DISPLAY=:1 nvidia-settings -a "[gpu:0]/GPUFanControlState=1"
DISPLAY=:1 nvidia-settings -a "[fan:0]/GPUTargetFanSpeed=50"
# Should print: Attribute 'GPUTargetFanSpeed' ... assigned value 50.
```

### Re-enable the daemon after reboot

The daemon service was disabled before the reboot to leave fans in auto mode
during the trading day. Re-enable it after rebooting:

```bash
systemctl --user enable --now gpu-fan-control
```

---

## Update 2026-06-02 — A second root cause: stale nvidia-settings

After the kernel module was switched to proprietary (running kernel
`6.17.0-29-generic`, DKMS-built proprietary module — `modinfo nvidia` reports
license `NVIDIA`, `/proc/driver/nvidia/version` no longer says "Open Kernel
Module"), **fan writes still failed with the identical "Unknown Error"** even
though Coolbits=28 was present and active in `xorg.conf`.

The remaining cause was a **version mismatch** between the userspace tool and
the driver:

```bash
nvidia-settings --version          # version 510.47.03   (Ubuntu noble/main)
nvidia-smi --query-gpu=driver_version --format=csv,noheader   # 580.126.09
```

Ubuntu's repo only ships `nvidia-settings` 510.x. Run against driver 580, the
old tool can still toggle `GPUFanControlState` (a stable, old attribute) but
fails every `GPUTargetFanSpeed` write — the exact symptom above. This is
**independent of** the open-vs-proprietary kernel module issue; both must be
correct.

### Fix

```bash
sudo bin/fix-nvidia-settings.sh        # no reboot required
```

It adds `ppa:graphics-drivers/ppa` and installs an `nvidia-settings` whose
major version matches (or exceeds) the driver. The installer now runs this as
step 6/10, and its final verify step performs a real fan-speed **write** test
(not just a query) so this class of failure is caught at install time.

> Lesson: the original diagnosis stopped at the kernel module. The query-based
> verification ("GPUFanControlState=1 succeeds") passed in both the broken and
> fixed states, masking the second cause. Always test an actual
> `GPUTargetFanSpeed` write.

See [managing-fan-control-rtx-3090.md](managing-fan-control-rtx-3090.md) for the
consolidated setup and maintenance guide.

---

## Additional Bugs Found

These bugs exist in the code regardless of the driver issue and should be fixed.

### Bug 1 — `_run_setting` silently swallows nvidia-settings errors

**File:** `src/nvidia_fan_writer.py`, method `_run_setting`

`nvidia-settings` returns exit code 0 even when an assignment fails (confirmed above).
The current check:

```python
if result.returncode != 0:
    logger.error(...)
    return False
```

…always logs "OK" even on error. The fix is to also scan stdout/stderr for the word
"Error":

```python
if result.returncode != 0 or "Error" in result.stderr or "Error" in result.stdout:
    logger.error(...)
    return False
```

### Bug 2 — Fan index mapping assumes 1 fan per card (written for A6000, breaks on RTX 3090)

**File:** `src/nvidia_fan_writer.py`, method `detect_index_mapping`

The comment reads: *"each A6000 has one fan per card"* and the mapping sets
`nv_fan: N` = `nv_gpu: N`. But RTX 3090 has **2 fans per card**, giving 4 fans total
across 2 GPUs:

```
nvidia-settings reports: 4 Fans on AgentAI:1
  [0] fan:0   (GPU 0, fan 1)
  [1] fan:1   (GPU 0, fan 2)
  [2] fan:2   (GPU 1, fan 1)
  [3] fan:3   (GPU 1, fan 2)
```

The current mapping sends speed commands only to `fan:0` (GPU 0) and `fan:1` (GPU 1),
missing `fan:1` on GPU 0 and `fan:3` on GPU 1. The fix is to query nvidia-settings for
the number of fans per GPU via the `GPUFanCount` attribute (or enumerate fans by
checking which GPU each fan belongs to) and issue a `set_fan_speed` call for every fan
on the target GPU.

---

## Timeline

| Time | Event |
|------|-------|
| Apr 10 | Streamlit UI started (PID 1507, port 8505) |
| Apr 23 | Daemon started (PID 3200016) |
| Apr 27 10:55 | Daemon restarted by systemd (PID 3606406) |
| Apr 27 11:47 | Daemon stopped and service disabled for diagnosis |
| Apr 27 11:47 | Both GPUs reset to auto fan control (GPUFanControlState=0) |
| Apr 27 | fix-open-driver.sh updated; awaiting reboot after trading day |
