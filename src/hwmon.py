"""
Motherboard fan control via the Linux hwmon sysfs interface.

Targets Super I/O fan controllers exposed by the kernel (nct6775 family:
nct6796 etc.). Each header N has:

  pwmN           duty 0-255 (writable when pwmN_enable == 1)
  pwmN_enable    0 = full speed, 1 = manual, 2+ = firmware/"smart fan" curve
  pwmN_mode      0 = DC voltage, 1 = PWM signal
  fanN_input     tachometer RPM (0 if nothing connected or stalled)

Writing pwm files requires root. The daemon runs as a root system service;
the UI only reads (sysfs attributes are world-readable).

On shutdown the original pwmN_enable / pwmN values are restored so the BIOS
curve takes over again, mirroring what NvidiaFanWriter does for the GPUs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("gpu-fan-control.hwmon")

HWMON_ROOT = Path("/sys/class/hwmon")


def find_hwmon(name: str) -> Optional[Path]:
    """Locate the hwmon directory whose `name` attribute matches.

    hwmon indices are not stable across boots, so always resolve by name.
    """
    if not HWMON_ROOT.exists():
        return None
    for d in sorted(HWMON_ROOT.iterdir()):
        try:
            if (d / "name").read_text().strip() == name:
                return d
        except OSError:
            continue
    return None


def _read_int(path: Path) -> Optional[int]:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _write_int(path: Path, value: int):
    path.write_text(f"{int(value)}\n")


def pct_to_pwm(pct: int) -> int:
    pct = max(0, min(100, int(pct)))
    return round(pct * 255 / 100)


def pwm_to_pct(raw: int) -> int:
    return round(max(0, min(255, raw)) * 100 / 255)


class CpuTempReader:
    """Reads the CPU package temperature from coretemp (or k10temp on AMD)."""

    CANDIDATES = ("coretemp", "k10temp", "zenpower")

    def __init__(self):
        self._path: Optional[Path] = None
        self._package_input: Optional[Path] = None
        self._core_inputs: list[Path] = []
        self._resolve()

    def _resolve(self):
        for name in self.CANDIDATES:
            d = find_hwmon(name)
            if d is not None:
                self._path = d
                break
        if self._path is None:
            logger.warning("No CPU temperature sensor found (coretemp/k10temp)")
            return

        for label_file in sorted(self._path.glob("temp*_label")):
            try:
                label = label_file.read_text().strip()
            except OSError:
                continue
            input_file = Path(str(label_file).replace("_label", "_input"))
            if label.startswith("Package") or label in ("Tctl", "Tdie"):
                self._package_input = input_file
            elif label.startswith("Core"):
                self._core_inputs.append(input_file)

        if self._package_input is None and not self._core_inputs:
            # Some drivers expose temp1_input with no label
            fallback = self._path / "temp1_input"
            if fallback.exists():
                self._package_input = fallback

        logger.info(
            f"CPU temp source: {self._path.name} "
            f"({'package' if self._package_input else f'max of {len(self._core_inputs)} cores'})"
        )

    @property
    def available(self) -> bool:
        return self._package_input is not None or bool(self._core_inputs)

    def read(self) -> Optional[int]:
        """Return CPU temperature in whole degrees C, or None on failure."""
        if self._package_input is not None:
            raw = _read_int(self._package_input)
            if raw is not None:
                return round(raw / 1000)
        readings = [r for r in (_read_int(p) for p in self._core_inputs) if r is not None]
        if readings:
            return round(max(readings) / 1000)
        return None


class HwmonFanWriter:
    """Manual control of PWM fan headers on one hwmon chip."""

    def __init__(self, chip_name: str):
        self._chip_name = chip_name
        self._path: Optional[Path] = find_hwmon(chip_name)
        # pwm index -> (original enable, original pwm) for restore on release
        self._claimed: dict[int, tuple[Optional[int], Optional[int]]] = {}
        if self._path is None:
            logger.error(f"hwmon chip '{chip_name}' not found under {HWMON_ROOT}")
        else:
            logger.info(f"hwmon chip '{chip_name}' at {self._path.resolve()}")

    @property
    def available(self) -> bool:
        return self._path is not None

    def _pwm_file(self, pwm: int, suffix: str = "") -> Path:
        return self._path / f"pwm{pwm}{suffix}"

    def claim(self, pwm: int) -> bool:
        """Switch a header to manual mode, remembering its previous state."""
        if self._path is None:
            return False
        enable_file = self._pwm_file(pwm, "_enable")
        if not enable_file.exists():
            logger.error(f"pwm{pwm}: header does not exist on {self._chip_name}")
            return False
        if pwm in self._claimed:
            return True
        orig_enable = _read_int(enable_file)
        orig_pwm = _read_int(self._pwm_file(pwm))
        try:
            _write_int(enable_file, 1)
        except OSError as e:
            logger.error(f"pwm{pwm}: cannot enable manual mode: {e}")
            return False
        self._claimed[pwm] = (orig_enable, orig_pwm)
        logger.info(
            f"pwm{pwm}: manual control enabled "
            f"(was enable={orig_enable}, duty={pwm_to_pct(orig_pwm) if orig_pwm is not None else '?'}%)"
        )
        return True

    def set_duty(self, pwm: int, pct: int) -> bool:
        """Write a duty cycle (0-100 %). Returns success."""
        if self._path is None:
            return False
        try:
            # Some drivers silently ignore pwm writes unless enable == 1.
            if _read_int(self._pwm_file(pwm, "_enable")) != 1:
                _write_int(self._pwm_file(pwm, "_enable"), 1)
            _write_int(self._pwm_file(pwm), pct_to_pwm(pct))
            return True
        except OSError as e:
            logger.error(f"pwm{pwm}: write failed: {e}")
            return False

    def read(self, pwm: int) -> tuple[int, int]:
        """Return (duty_pct, rpm); -1 for either if unreadable."""
        if self._path is None:
            return -1, -1
        raw = _read_int(self._pwm_file(pwm))
        rpm = _read_int(self._path / f"fan{pwm}_input")
        return (pwm_to_pct(raw) if raw is not None else -1), (rpm if rpm is not None else -1)

    def release(self, pwm: int):
        """Restore the header's original mode (typically the BIOS curve)."""
        state = self._claimed.pop(pwm, None)
        if state is None or self._path is None:
            return
        orig_enable, orig_pwm = state
        try:
            if orig_pwm is not None:
                _write_int(self._pwm_file(pwm), orig_pwm)
            if orig_enable is not None:
                _write_int(self._pwm_file(pwm, "_enable"), orig_enable)
            logger.info(f"pwm{pwm}: restored enable={orig_enable}")
        except OSError as e:
            logger.error(f"pwm{pwm}: restore failed: {e}")

    def release_all(self):
        for pwm in list(self._claimed):
            self.release(pwm)


def read_all_headers(chip_name: str) -> list[dict]:
    """Snapshot every pwm/fan header on a chip (read-only; used by the UI).

    Returns [{pwm, duty_pct, rpm, enable, mode}] sorted by header index.
    """
    d = find_hwmon(chip_name)
    if d is None:
        return []
    out = []
    for pwm_file in sorted(d.glob("pwm[0-9]"), key=lambda p: int(p.name[3:])):
        n = int(pwm_file.name[3:])
        raw = _read_int(pwm_file)
        out.append({
            "pwm": n,
            "duty_pct": pwm_to_pct(raw) if raw is not None else -1,
            "rpm": _read_int(d / f"fan{n}_input") or 0,
            "enable": _read_int(d / f"pwm{n}_enable"),
            "mode": _read_int(d / f"pwm{n}_mode"),
        })
    return out
