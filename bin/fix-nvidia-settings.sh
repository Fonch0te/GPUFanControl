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
# Fix (two sources, tried in order):
#   1. ppa:graphics-drivers/ppa — may carry a newer nvidia-settings.
#   2. NVIDIA's CUDA apt repo (developer.download.nvidia.com) — ships an
#      nvidia-settings + libxnvctrl0 .deb for EVERY driver release. The exact
#      driver version is downloaded, SHA256-verified against the repo index,
#      and installed with apt. (The PPA alone was not enough on Ubuntu 24.04 +
#      driver 580: it still resolved to nvidia-settings 510.)
#   Both are userspace packages only — NO reboot required.
#
# Detection:
#   driver_major   = major version from /proc/driver/nvidia/version (or nvidia-smi)
#   settings_major = major version from `nvidia-settings --version`
#     settings_major >= driver_major  -> nothing to do (exit 0)
#     settings_major <  driver_major  -> PPA, re-check, NVIDIA repo, re-check
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

# Full version of the running NVIDIA driver (e.g. "580.173.02").
driver_full() {
    local ver=""
    if [[ -f /proc/driver/nvidia/version ]]; then
        ver=$(grep -oP '\d+\.\d+\.\d+' /proc/driver/nvidia/version | head -1)
    fi
    if [[ -z "$ver" ]] && command -v nvidia-smi &>/dev/null; then
        ver=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)
    fi
    [[ -n "$ver" ]] && echo "$ver"
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

    # Re-check after the PPA install
    local new
    new=$(settings_major || true)
    if [[ -n "$new" && "$new" -ge "$drv" ]]; then
        report_success "$new" "$drv"
    fi

    # The PPA did not carry a build >= driver (common on newer drivers:
    # e.g. Ubuntu 24.04 + driver 580 still gets nvidia-settings 510).
    # Fall back to NVIDIA's own CUDA repo, which ships an nvidia-settings
    # for every driver release.
    warn "nvidia-settings is still version ${new:-unknown} after the PPA attempt."
    install_from_nvidia_repo "$drv"

    new=$(settings_major || true)
    if [[ -n "$new" && "$new" -ge "$drv" ]]; then
        report_success "$new" "$drv"
    fi

    warn "nvidia-settings is still version ${new:-unknown} after all attempts."
    exit 1
}

report_success() {
    log ""
    log "=== nvidia-settings is now version $1 (driver $2) ==="
    log "Fan control writes should now succeed. Verify with:"
    log "  DISPLAY=:1 nvidia-settings -a '[gpu:0]/GPUFanControlState=1' -a '[fan:0]/GPUTargetFanSpeed=50'"
    exit 0
}

# ---------------------------------------------------------------------------
# Fallback: download the exact-match .debs from NVIDIA's CUDA repo
# ---------------------------------------------------------------------------

# Returns the "Filename" and "SHA256" fields for one package/version stanza
# in a Debian Packages index.
pkg_fields() {
    local index="$1" pkg="$2" ver="$3"
    awk -v pk="$pkg" -v v="$ver" '
        BEGIN { RS=""; FS="\n" }
        {
            okp=0; okv=0; fn=""; sha=""
            for (i=1; i<=NF; i++) {
                if ($i == "Package: " pk) okp=1
                if ($i == "Version: " v)  okv=1
                if ($i ~ /^Filename: /) fn=substr($i, 11)
                if ($i ~ /^SHA256: /)   sha=substr($i, 9)
            }
            if (okp && okv) { print fn, sha; exit }
        }' "$index"
}

install_from_nvidia_repo() {
    local drv_major="$1"
    local drv_full
    drv_full=$(driver_full || true)

    # ubuntu2404, ubuntu2204, ...
    local rel
    rel=$(. /etc/os-release 2>/dev/null && echo "${ID}${VERSION_ID//./}")
    if [[ -z "$rel" || "$rel" != ubuntu* ]]; then
        warn "NVIDIA repo fallback only supports Ubuntu (got '${rel:-unknown}')."
        return 1
    fi
    local repo="https://developer.download.nvidia.com/compute/cuda/repos/${rel}/x86_64"

    log ""
    log "=== Trying NVIDIA's CUDA repo for a matching nvidia-settings ==="
    log "  $repo"

    local tmp
    tmp=$(mktemp -d)
    chmod 755 "$tmp"   # apt's _apt user must be able to read the .debs

    if ! curl -sfL "$repo/Packages" -o "$tmp/Packages"; then
        warn "Could not download the package index from NVIDIA's repo."
        return 1
    fi

    # Prefer the exact driver version, else the newest build of the same major.
    local versions ver
    versions=$(awk '/^Package: nvidia-settings$/{p=1; next} /^Package: /{p=0} p && /^Version: /{print $2}' "$tmp/Packages")
    ver=$(echo "$versions" | grep -E "^${drv_full}-" | head -1 || true)
    if [[ -z "$ver" ]]; then
        ver=$(echo "$versions" | grep -E "^${drv_major}\." | sort -V | tail -1 || true)
    fi
    if [[ -z "$ver" ]]; then
        warn "NVIDIA's repo has no nvidia-settings build for driver $drv_major."
        return 1
    fi
    log "  Selected nvidia-settings $ver"

    # Download libxnvctrl0 + nvidia-settings and verify SHA256 against the index.
    local pkg fn sha
    for pkg in libxnvctrl0 nvidia-settings; do
        read -r fn sha < <(pkg_fields "$tmp/Packages" "$pkg" "$ver")
        if [[ -z "$fn" || -z "$sha" ]]; then
            warn "Could not locate $pkg $ver in the index."
            return 1
        fi
        log "  Downloading ${fn#./} ..."
        if ! curl -sfL "$repo/${fn#./}" -o "$tmp/$pkg.deb"; then
            warn "Download failed for $pkg."
            return 1
        fi
        if ! echo "$sha  $tmp/$pkg.deb" | sha256sum -c --quiet; then
            warn "SHA256 mismatch for $pkg — refusing to install."
            return 1
        fi
    done
    log "  Checksums verified."

    # apt resolves remaining dependencies (pkg-config, screen-resolution-extra)
    log "Installing nvidia-settings $ver ..."
    apt-get install -y "$tmp/libxnvctrl0.deb" "$tmp/nvidia-settings.deb"
}

main "$@"
