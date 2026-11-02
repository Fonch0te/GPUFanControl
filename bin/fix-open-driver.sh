#!/usr/bin/env bash
# fix-open-driver.sh — Switch from NVIDIA open kernel modules to proprietary
#
# The open kernel modules do not support fan speed writes via nvidia-settings
# on GeForce (consumer) GPUs. GPUTargetFanSpeed assignments return "Unknown
# Error" even with Coolbits enabled.
#
# This script detects the condition via two methods:
#   Method A: nvidia-driver-*-open metapackage is installed (older layout)
#   Method B: linux-modules-nvidia-*-open-* kernel package is installed AND
#             /proc/driver/nvidia/version confirms "Open Kernel Module" is
#             running (Ubuntu 24.04+ layout where the -open module ships
#             separately from the driver metapackage)
#
# In both cases the fix installs the matching proprietary kernel module
# package for the running kernel and removes the open-module package.
#
# Conditions checked:
#   1. Open kernel module is detected (package or /proc check)
#   2. At least one GeForce GPU is present
#   3. A GPUTargetFanSpeed write actually fails (definitive test)
#
# If all three are true, the script offers to switch. If any check fails
# the script exits cleanly — no changes are made.
#
# Usage:
#   sudo ./fix-open-driver.sh           # interactive (prompts before acting)
#   sudo ./fix-open-driver.sh --yes     # non-interactive (auto-confirm)

set -euo pipefail

AUTO_CONFIRM=false
if [[ "${1:-}" == "--yes" || "${1:-}" == "-y" ]]; then
    AUTO_CONFIRM=true
fi

# Globals populated by detect_open_driver
OPEN_PKG=""
DRIVER_VERSION=""
PROPRIETARY_PKG=""
OPEN_MODULE_PKG=""   # kernel-specific open module package to remove
DETECTION_METHOD=""  # "metapackage" or "kernel-module"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log()  { echo "[fix-open-driver] $*"; }
warn() { echo "[fix-open-driver] WARNING: $*" >&2; }
fail() { echo "[fix-open-driver] ERROR: $*" >&2; exit 1; }

require_root() {
    if [[ $EUID -ne 0 ]]; then
        fail "This script must be run as root (sudo)."
    fi
}

# ---------------------------------------------------------------------------
# Step 1: Detect open driver (two methods)
# ---------------------------------------------------------------------------

detect_open_driver() {
    # Method A: nvidia-driver-*-open metapackage (older Ubuntu layout)
    local meta_pkg
    meta_pkg=$(dpkg -l 2>/dev/null | awk '/^ii.*nvidia-driver-[0-9]+-open / {print $2}' | head -1)

    if [[ -n "$meta_pkg" ]]; then
        OPEN_PKG="$meta_pkg"
        DRIVER_VERSION=$(echo "$meta_pkg" | grep -oP '\d+' | head -1)
        PROPRIETARY_PKG="nvidia-driver-${DRIVER_VERSION}"
        DETECTION_METHOD="metapackage"
        log "Detected open driver metapackage: $OPEN_PKG (version $DRIVER_VERSION)"
        return 0
    fi

    # Method B: /proc confirms open module is running + kernel module package
    # Ubuntu 24.04 ships open modules via linux-modules-nvidia-*-open-* while
    # the driver metapackage (nvidia-driver-580) has no -open suffix.
    if [[ ! -f /proc/driver/nvidia/version ]]; then
        log "NVIDIA driver not loaded. No action needed."
        return 1
    fi

    if ! grep -q "Open Kernel Module" /proc/driver/nvidia/version; then
        log "Proprietary NVIDIA kernel module is already in use. No action needed."
        return 1
    fi

    # Extract major version from running module (e.g. "580.126.09" -> "580")
    local full_ver
    full_ver=$(grep -oP '\d+\.\d+\.\d+' /proc/driver/nvidia/version | head -1)
    DRIVER_VERSION=$(echo "$full_ver" | cut -d. -f1)

    local kernel
    kernel=$(uname -r)

    # Find the installed open kernel module package for the running kernel
    OPEN_MODULE_PKG=$(dpkg -l 2>/dev/null \
        | awk "/^ii.*linux-modules-nvidia-${DRIVER_VERSION}-open-${kernel} / {print \$2}" \
        | head -1)

    if [[ -z "$OPEN_MODULE_PKG" ]]; then
        # Package name may differ slightly; fall back to a broader search
        OPEN_MODULE_PKG=$(dpkg -l 2>/dev/null \
            | awk "/^ii.*linux-modules-nvidia-[0-9]+-open-${kernel} / {print \$2}" \
            | head -1)
    fi

    PROPRIETARY_PKG="linux-modules-nvidia-${DRIVER_VERSION}-${kernel}"
    OPEN_PKG="${OPEN_MODULE_PKG:-linux-modules-nvidia-${DRIVER_VERSION}-open-${kernel}}"
    DETECTION_METHOD="kernel-module"

    log "Open NVIDIA kernel module detected: version $full_ver, kernel $kernel"
    log "  Open package:        ${OPEN_MODULE_PKG:-<not found via dpkg>}"
    log "  Proprietary target:  $PROPRIETARY_PKG"
    return 0
}

# ---------------------------------------------------------------------------
# Step 2: Check for GeForce (consumer) GPUs
# ---------------------------------------------------------------------------

detect_geforce() {
    if ! command -v nvidia-smi &>/dev/null; then
        warn "nvidia-smi not found."
        return 1
    fi

    local gpu_names
    gpu_names=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null || true)

    if [[ -z "$gpu_names" ]]; then
        warn "Could not query GPU names."
        return 1
    fi

    # Check if any GPU is a GeForce card
    if echo "$gpu_names" | grep -qi "GeForce"; then
        local geforce_list
        geforce_list=$(echo "$gpu_names" | grep -i "GeForce" | sort -u | paste -sd ", ")
        log "GeForce GPU(s) detected: $geforce_list"
        return 0
    fi

    log "No GeForce GPUs found ($(echo "$gpu_names" | paste -sd ", "))."
    log "Professional/datacenter GPUs may work with the open driver. No action needed."
    return 1
}

# ---------------------------------------------------------------------------
# Step 3: Test fan speed write
# ---------------------------------------------------------------------------

test_fan_write() {
    if ! command -v nvidia-settings &>/dev/null; then
        warn "nvidia-settings not found."
        return 0  # can't test, assume broken
    fi

    # Find a working DISPLAY
    local display=""
    for d in ":1" ":0" ":2"; do
        if DISPLAY="$d" nvidia-settings -t -q "[gpu:0]/GPUFanControlState" &>/dev/null; then
            display="$d"
            break
        fi
    done

    if [[ -z "$display" ]]; then
        warn "Could not find a working X display for nvidia-settings."
        log "Cannot perform fan write test — assuming fan control is broken."
        return 0  # assume broken if we can't even connect
    fi

    # Enable manual control first
    DISPLAY="$display" nvidia-settings -a "[gpu:0]/GPUFanControlState=1" &>/dev/null || true

    # Read current speed so we can write it back (no-op change)
    local current
    current=$(DISPLAY="$display" nvidia-settings -t -q "[fan:0]/GPUCurrentFanSpeed" 2>/dev/null || echo "30")
    current=${current:-30}

    # Try to write the current speed — this should be a harmless no-op
    local result
    result=$(DISPLAY="$display" nvidia-settings -a "[fan:0]/GPUTargetFanSpeed=$current" 2>&1 || true)

    if echo "$result" | grep -qi "error\|unknown"; then
        log "Fan speed write FAILED: $result"
        return 0  # broken — needs fix
    fi

    log "Fan speed write succeeded. Fan control is working correctly."
    log "No driver change needed."
    return 1  # working — no fix needed
}

# ---------------------------------------------------------------------------
# Step 4: Switch driver
# ---------------------------------------------------------------------------

switch_driver() {
    log ""
    log "=== Driver Switch Required ==="
    log "  Current:  $OPEN_PKG (open kernel modules)"
    log "  Target:   $PROPRIETARY_PKG (proprietary kernel modules)"
    log ""
    log "This will:"
    log "  1. Install $PROPRIETARY_PKG"
    if [[ -n "$OPEN_MODULE_PKG" ]]; then
        log "  2. Remove $OPEN_MODULE_PKG"
        log "  3. Update initramfs"
        log "  4. Require a REBOOT to take effect"
    else
        log "  2. Same driver version ($DRIVER_VERSION), same CUDA compatibility"
        log "  3. Require a REBOOT to take effect"
    fi
    log ""

    # Check that the proprietary package is available
    if ! apt-cache show "$PROPRIETARY_PKG" &>/dev/null; then
        fail "Package $PROPRIETARY_PKG not found in apt repositories. Run 'sudo apt update' and retry."
    fi

    if [[ "$AUTO_CONFIRM" != true ]]; then
        read -rp "[fix-open-driver] Proceed with driver switch? [y/N] " answer
        if [[ ! "$answer" =~ ^[Yy] ]]; then
            log "Aborted by user."
            exit 0
        fi
    fi

    apt-get update -qq

    log "Installing $PROPRIETARY_PKG ..."
    apt-get install -y "$PROPRIETARY_PKG"

    if [[ -n "$OPEN_MODULE_PKG" ]]; then
        log "Removing open module package $OPEN_MODULE_PKG ..."
        apt-get remove -y "$OPEN_MODULE_PKG" || warn "Could not remove $OPEN_MODULE_PKG — continuing anyway."
        log "Updating initramfs ..."
        update-initramfs -u
    fi

    log ""
    log "=== Driver switch complete ==="
    log "REBOOT REQUIRED for the proprietary kernel modules to load."
    log "After reboot, verify with:"
    log "  grep -v 'Open' /proc/driver/nvidia/version"
    log "  nvidia-settings -a '[fan:0]/GPUTargetFanSpeed=50'"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    require_root

    log "Checking NVIDIA driver and fan control compatibility..."
    log ""

    # All three checks must pass to trigger the switch
    if ! detect_open_driver; then
        exit 0
    fi

    if ! detect_geforce; then
        exit 0
    fi

    if ! test_fan_write; then
        exit 0
    fi

    switch_driver
}

main "$@"
