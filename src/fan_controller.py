"""
Fan curve interpolation and hysteresis logic.

Linear interpolation between defined curve points.
Hysteresis prevents fan oscillation at curve boundaries —
speed only changes if temp moves >= hysteresis_celsius from
the temperature at which the current speed was set.
"""

import logging
from models import FanCurvePoint

logger = logging.getLogger("gpu-fan-control.controller")


class FanController:
    def __init__(self, curve: list[FanCurvePoint], hysteresis_c: float = 3.0):
        self._curve = sorted(curve, key=lambda p: p.temp_c)
        self._hysteresis = hysteresis_c
        # Per-GPU state: {gpu_index: (last_decision_temp, last_fan_pct)}
        self._state: dict[int, tuple[float, int]] = {}

    @property
    def hysteresis(self) -> float:
        return self._hysteresis

    @hysteresis.setter
    def hysteresis(self, value: float):
        self._hysteresis = value

    def update_curve(self, curve: list[FanCurvePoint]):
        """Hot-reload fan curve. Resets hysteresis state."""
        self._curve = sorted(curve, key=lambda p: p.temp_c)
        self._state.clear()
        logger.info("Fan curve updated, hysteresis state reset")

    def compute_fan_speed(self, gpu_index: int, current_temp_c: int) -> tuple[int, bool]:
        """
        Compute target fan speed for a GPU.
        Returns (target_fan_pct, changed).
        changed=False means hysteresis suppressed the update.
        """
        target = self._interpolate(current_temp_c)

        if gpu_index in self._state:
            last_temp, last_pct = self._state[gpu_index]
            if abs(current_temp_c - last_temp) < self._hysteresis:
                # Keep reference pinned at last decision temp, don't slide
                return last_pct, False

        self._state[gpu_index] = (current_temp_c, target)
        return target, True

    def _interpolate(self, temp_c: int) -> int:
        """Linear interpolation on the fan curve."""
        if not self._curve:
            return 50  # Safe fallback

        if temp_c <= self._curve[0].temp_c:
            return self._curve[0].fan_pct

        if temp_c >= self._curve[-1].temp_c:
            return self._curve[-1].fan_pct

        for i in range(len(self._curve) - 1):
            lo = self._curve[i]
            hi = self._curve[i + 1]
            if lo.temp_c <= temp_c <= hi.temp_c:
                ratio = (temp_c - lo.temp_c) / (hi.temp_c - lo.temp_c)
                return round(lo.fan_pct + ratio * (hi.fan_pct - lo.fan_pct))

        return self._curve[-1].fan_pct
