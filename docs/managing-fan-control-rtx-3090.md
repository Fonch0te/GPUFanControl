# Managing Fan Control on NVIDIA RTX 3090 (GeForce)

A practical guide for getting and keeping fan control working on consumer
GeForce GPUs (validated on **2× RTX 3090, Ubuntu 24.04, driver 580**).

GeForce cards are not officially supported by NVIDIA for fan control via
`nvidia-settings`, so a few things must line up. This project scripts all of
them; this doc explains what they do and how to manage them over time.

---

## The three requirements

Fan control through `nvidia-settings` only works when **all three** are true:

| # | Requirement | Why | Fixed by | Reboot? |
|---|-------------|-----|----------|---------|
| 1 | **Proprietary** kernel module loaded (not the *open* module) | The open kernel modules silently reject `GPUTargetFanSpeed` writes on GeForce cards | `bin/fix-open-driver.sh` | **Yes** |
| 2 | `nvidia-settings` major version **>=** driver major version | Ubuntu ships `nvidia-settings` 510.x no matter the driver; 510 vs driver 580 fails every `GPUTargetFanSpeed` write with "Unknown Error" | `bin/fix-nvidia-settings.sh` | No |
| 3 | **Coolbits** enabled in `xorg.conf` | Without `Coolbits` the driver won't allow manual fan control at all | installer runs `nvidia-xconfig --cool-bits=28` | X restart |

> The tricky part: requirements 1 and 2 produce the **same** symptom
> (fans at 0%, `GPUTargetFanSpeed` → "Unknown Error"). Fixing only the kernel
> module is not enough — the stale `nvidia-settings` will keep the failure
> alive until it is also upgraded. That is exactly what happened on this box.

---

## One-shot setup (new machine)

```bash
./install.sh        # runs all checks/fixes below, in order
```

The installer (`bin/install_fan_control.sh`) performs, among other steps:

- **[5/10]** `fix-open-driver.sh --yes` — switch open → proprietary kernel module
- **[6/10]** `fix-nvidia-settings.sh --yes` — match `nvidia-settings` to the driver
- **[7/10]** `nvidia-xconfig --cool-bits=28` — enable Coolbits
- **[10/10]** a real fan-speed **write** test (not just a query) to confirm it works

If step 5 switched the kernel module, the installer prints `*** REBOOT
REQUIRED ***`. Reboot, then re-run `./install.sh` (it is idempotent) or just
verify manually (below).

---

## Running the fixes individually

Both fix scripts are **idempotent and self-detecting** — safe to run anytime.

```bash
# Kernel module: open -> proprietary  (reboot afterwards)
sudo bin/fix-open-driver.sh

# nvidia-settings: upgrade to match driver  (no reboot)
sudo bin/fix-nvidia-settings.sh
```

`fix-nvidia-settings.sh` adds `ppa:graphics-drivers/ppa` (whose
`nvidia-settings` tracks current driver branches) and installs the matching
`nvidia-settings`. It exits without changes if `nvidia-settings` already
matches the driver.

---

## Verifying fan control works

```bash
# 1. Confirm the PROPRIETARY module is loaded (should print nothing)
grep "Open Kernel Module" /proc/driver/nvidia/version
modinfo nvidia | grep license      # should say: NVIDIA  (open = "Dual MIT/GPL")

# 2. Confirm nvidia-settings matches the driver
nvidia-settings --version | grep version    # major must be >= ...
nvidia-smi --query-gpu=driver_version --format=csv,noheader   # ... this

# 3. Confirm Coolbits is set
grep -i coolbits /etc/X11/xorg.conf          # Option "Coolbits" "28"

# 4. The definitive test — a real fan WRITE must succeed
DISPLAY=:1 nvidia-settings \
  -a "[gpu:0]/GPUFanControlState=1" \
  -a "[fan:0]/GPUTargetFanSpeed=50"
# Good:  Attribute 'GPUTargetFanSpeed' ... assigned value 50.
# Bad:   ERROR: ... (Unknown Error)   <- a requirement above is still unmet

# Reset to auto when done testing
DISPLAY=:1 nvidia-settings -a "[gpu:0]/GPUFanControlState=0"
```

> Note: `nvidia-settings` exits with code **0 even when a write fails**, so
> always check the *text* output for "Error", not just the exit code.

The RTX 3090 has **2 fans per card** → 4 fans total across 2 GPUs
(`fan:0`,`fan:1` on GPU 0; `fan:2`,`fan:3` on GPU 1). A write test on `fan:0`
alone is enough to prove the mechanism; the daemon drives all of them.

---

## Maintenance: surviving kernel & driver updates

This is the part that bites GeForce users repeatedly.

- **Kernel updates** rebuild the NVIDIA module for the new kernel. If the
  *open* module package (`linux-modules-nvidia-*-open-*`) is still installed,
  a new kernel can boot back into the open module and fan control breaks
  again. After a kernel update, re-run the verify steps; if the open module
  came back, run `sudo bin/fix-open-driver.sh` and reboot. (This box uses a
  DKMS proprietary build for the running kernel; leftover `*-open-*` packages
  for *old* kernels are harmless but can be removed.)

- **Driver updates** (e.g. 580 → 585) may once again outrun the distro's
  `nvidia-settings`. Re-run `sudo bin/fix-nvidia-settings.sh` — it will pull a
  matching `nvidia-settings` from the PPA. No reboot needed.

- **Quick health check after any update:**
  ```bash
  ./install.sh        # idempotent; re-checks all three requirements + write test
  ```
  or just step 4 of the verify section above.

---

## Re-enabling the daemon

The fan daemon runs as a systemd **user** service:

```bash
systemctl --user enable --now gpu-fan-control      # start + enable on boot
systemctl --user status gpu-fan-control            # check
journalctl --user -u gpu-fan-control -f            # logs
```

If the service description still reads "RTX A6000" or you see an
`Unknown key name 'StartLimitIntervalSec'` warning, the systemd unit predates
this hardware — re-run `./install.sh` to refresh the installed unit files.

---

## Background

For the full diagnosis history (how the open-module and nvidia-settings issues
were each found), see
[troubleshooting-fan-control-not-working.md](troubleshooting-fan-control-not-working.md).
