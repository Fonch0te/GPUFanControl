#!/usr/bin/env bash
# fix-nvidia-settings.sh — Ensure the nvidia-settings CLI matches the running driver
#
# Problem (seen on RTX 3090 + Ubuntu 24.04):
#   Ubuntu's main/universe repo ships nvidia-settings 510.x regardless of which
#   NVIDIA driver is installed. Run against driver 580.x, this stale
#   nvidia-settings can still toggle GPUFanControlState (an old, stable
#   attribute) but FAILS every GPUTargetFanSpeed write with:
#
#       ERROR: Error assigning value N to attribute 'GPUTargetFanSpeed'
#              ... (Unknown Error).
#
#   nvidia-settings exits 0 even on this failure, so the daemon thinks it
#   succeeded while the fans stay at 0%.
#
#   This is a SEPARATE issue from the open-vs-proprietary kernel module problem
#   handled by fix-open-driver.sh. BOTH must be correct for fan control to work:
#     1. Proprietary kernel module loaded   (fix-open-driver.sh)
#     2. nvidia-settings major >= driver     (this script)
#
# Fix:
#   Install a matching nvidia-settings from ppa:graphics-drivers/ppa, whose
#   nvidia-settings tracks the current driver branches (>= the installed driver).
#   This is a userspace package only — NO reboot required.
#
# Detection:
#   driver_major   = major version from /proc/driver/nvidia/version (or nvidia-smi)
#   settings_major = major version from `nvidia-settings --version`
#     settings_major >= driver_major  -> nothing to do (exit 0)
#     settings_major <  driver_major  -> add PPA, install nvidia-settings, re-check
#
# Exit codes:
#   0  nvidia-settings matches the driver (already, or after a successful fix)
#   1  could not make nvidia-settings match the driver (caller should warn)
#
# Usage:
#   sudo ./fix-nvidia-settings.sh         # interactive (prompts before acting)
#   sudo ./fix-nvidia-settings.sh --yes   # non-interactive (used by installer)

set -euo pipefail

AUTO_CONFIRM=false
if [[ "${1:-}" == "--yes" || "${1:-}" == "-y" ]]; then
    AUTO_CONFIRM=true
fi

PPA="ppa:graphics-drivers/ppa"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log()  { echo "[fix-nvidia-settings] $*"; }
warn() { echo "[fix-nvidia-settings] WARNING: $*" >&2; }
fail() { echo "[fix-nvidia-settings] ERROR: $*" >&2; exit 1; }

require_root() {
    if [[ $EUID -ne 0 ]]; then
        fail "This script must be run as root (sudo)."
    fi
}

# Major version of the running NVIDIA driver (e.g. "580").
driver_major() {
    local ver=""
    if [[ -f /proc/driver/nvidia/version ]]; then
        ver=$(grep -oP '\d+\.\d+\.\d+' /proc/driver/nvidia/version | head -1)
    fi
    if [[ -z "$ver" ]] && command -v nvidia-smi &>/dev/null; then
        ver=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)
    fi
    [[ -n "$ver" ]] && echo "${ver%%.*}"
}

# Major version of the installed nvidia-settings CLI (e.g. "510").
settings_major() {
    if ! command -v nvidia-settings &>/dev/null; then
        return 0  # empty -> treated as needing install
    fi
    nvidia-settings --version 2>/dev/null \
        | grep -oP 'version \K\d+' | head -1
}

# ---------------------------------------------------------------------------
# Fix: install matching nvidia-settings from the graphics-drivers PPA
# ---------------------------------------------------------------------------

install_matching_settings() {
    local drv="$1"

    log ""
    log "=== nvidia-settings upgrade required ==="
    log "  Driver major:          $drv"
    log "  nvidia-settings major: ${2:-<not installed>}"
    log ""
    log "This will:"
    log "  1. Ensure $PPA is enabled"
    log "  2. apt-get update"
    log "  3. Install/upgrade nvidia-settings to match the driver"
    log "  (userspace only — no reboot required)"
    log ""

    if [[ "$AUTO_CONFIRM" != true ]]; then
        read -rp "[fix-nvidia-settings] Proceed? [y/N] " answer
        if [[ ! "$answer" =~ ^[Yy] ]]; then
            log "Aborted by user."
            exit 0
        fi
    fi

    # add-apt-repository lives in software-properties-common
    if ! command -v add-apt-repository &>/dev/null; then
        log "Installing software-properties-common (for add-apt-repository) ..."
        apt-get update -qq
        apt-get install -y software-properties-common
    fi

    # Enable the PPA if not already present
    if ! grep -rq "graphics-drivers" /etc/apt/sources.list /etc/apt/sources.list.d/ 2>/dev/null; then
        log "Adding $PPA ..."
        add-apt-repository -y "$PPA"
    else
        log "$PPA already enabled."
    fi

    apt-get update -qq

    log "Installing nvidia-settings (matching driver $drv) ..."
    apt-get install -y nvidia-settings
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    require_root

    log "Checking nvidia-settings vs driver compatibility..."

    local drv set
    drv=$(driver_major || true)
    set=$(settings_major || true)

    if [[ -z "$drv" ]]; then
        warn "Could not determine the NVIDIA driver version. Is the driver loaded?"
        log "Skipping nvidia-settings check."
        exit 0
    fi

    log "  Driver major:          $drv"
    log "  nvidia-settings major: ${set:-<not installed>}"

    if [[ -n "$set" && "$set" -ge "$drv" ]]; then
        log "nvidia-settings ($set) >= driver ($drv). No action needed."
        exit 0
    fi

    install_matching_settings "$drv" "$set"

    # Re-check after install
    local new
    new=$(settings_major || true)
    if [[ -n "$new" && "$new" -ge "$drv" ]]; then
        log ""
        log "=== nvidia-settings is now version $new (driver $drv) ==="
        log "Fan control writes should now succeed. Verify with:"
        log "  DISPLAY=:1 nvidia-settings -a '[gpu:0]/GPUFanControlState=1' -a '[fan:0]/GPUTargetFanSpeed=50'"
        exit 0
    fi

    warn "nvidia-settings is still version ${new:-unknown} after the upgrade attempt."
    warn "The graphics-drivers PPA may not yet carry a build >= $drv."
    exit 1
}

main "$@"
