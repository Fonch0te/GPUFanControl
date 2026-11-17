#!/usr/bin/env bash
# identify-headers.sh — find out which motherboard header drives which fan.
#
# For each header (default: every header the daemon is NOT driving) this
# pulses the fan: 100% for a few seconds, then 20%, then restores the
# header's original setting. Watch/listen to the fans while it runs and note
# which header number makes yours change speed.
#
# Usage:  sudo bin/identify-headers.sh            # all headers not in manual mode
#         sudo bin/identify-headers.sh 1 3 4      # only these headers
set -euo pipefail

CHIP="${CHIP:-nct6796}"
HOLD="${HOLD:-6}"

[[ $EUID -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }
/sbin/modprobe nct6775 2>/dev/null || true

DIR=""
for d in /sys/class/hwmon/hwmon*; do
    [[ "$(cat "$d/name" 2>/dev/null)" == "$CHIP" ]] && DIR="$d" && break
done
[[ -n "$DIR" ]] || { echo "hwmon chip '$CHIP' not found." >&2; exit 1; }

if [[ $# -gt 0 ]]; then
    HEADERS=("$@")
else
    HEADERS=()
    for f in "$DIR"/pwm[0-9]; do
        n="${f##*pwm}"
        # enable=1 means the daemon (or someone) is driving it manually: skip
        [[ "$(cat "${f}_enable")" == "1" ]] || HEADERS+=("$n")
    done
fi

echo "Chip $CHIP at $DIR — testing headers: ${HEADERS[*]}"
echo "Each header: 100% for ${HOLD}s, then 20% for ${HOLD}s, then restored."
echo

for n in "${HEADERS[@]}"; do
    pwm="$DIR/pwm$n"; en="${pwm}_enable"
    [[ -e "$pwm" ]] || { echo "pwm$n: no such header"; continue; }
    orig_en=$(cat "$en"); orig_pwm=$(cat "$pwm")
    restore() { echo "$orig_pwm" > "$pwm" 2>/dev/null || true; echo "$orig_en" > "$en" 2>/dev/null || true; }
    trap restore EXIT

    echo ">>> pwm$n  -> 100%"
    echo 1 > "$en"; echo 255 > "$pwm"; sleep "$HOLD"
    echo "    pwm$n  tach: $(cat "$DIR/fan${n}_input") rpm"
    echo ">>> pwm$n  -> 20%"
    echo 51 > "$pwm"; sleep "$HOLD"
    echo "    pwm$n  tach: $(cat "$DIR/fan${n}_input") rpm"
    restore; trap - EXIT
    echo "    pwm$n  restored (enable=$orig_en)"
    echo
    sleep 2
done
echo "Done. Tell the daemon which header it was on the Motherboard Fans page (Header pwmN)."
