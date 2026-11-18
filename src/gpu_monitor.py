"""
pynvml wrapper for GPU telemetry reads.

No X11 dependency — talks directly to NVIDIA kernel driver.
Per-GPU error handling: a failure on one GPU never crashes the daemon.
"""

import logging
import pynvml
from models import GpuState

logger = logging.getLogger("gpu-fan-control.monitor")


class GpuMonitor:
    def __init__(self):
        self._handles: list = []
        self._gpu_count: int = 0
        self._initialized: bool = False

    def initialize(self) -> int:
        """Init NVML, discover GPUs, return count."""
        pynvml.nvmlInit()
        self._gpu_count = pynvml.nvmlDeviceGetCount()
        self._handles = [
            pynvml.nvmlDeviceGetHandleByIndex(i)
            for i in range(self._gpu_count)
        ]
        self._initialized = True
        logger.info(f"NVML initialized: {self._gpu_count} GPUs discovered")
        return self._gpu_count

    def read_gpu(self, index: int) -> GpuState:
        """Read telemetry for one GPU. Returns GpuState with error field on failure."""
        try:
            handle = self._handles[index]
            temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            fan = pynvml.nvmlDeviceGetFanSpeed(handle)
            power = pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            name = pynvml.nvmlDeviceGetName(handle)
            limits = self.read_power_limits(index)

            return GpuState(
                index=index,
                temperature_c=temp,
                fan_speed_pct=fan,
                fan_target_pct=-1,
                power_draw_w=round(power, 1),
                utilization_pct=util.gpu,
                memory_used_mb=mem.used // (1024 * 1024),
                memory_total_mb=mem.total // (1024 * 1024),
                name=name if isinstance(name, str) else name.decode(),
                **limits,
            )
        except Exception as e:
            logger.warning(f"GPU {index} read error: {e}")
            return GpuState(
                index=index,
                temperature_c=-1,
                fan_speed_pct=-1,
                fan_target_pct=-1,
                power_draw_w=0,
                utilization_pct=0,
                memory_used_mb=0,
                memory_total_mb=0,
                name=f"GPU {index}",
                error=str(e),
            )

    def read_power_limits(self, index: int) -> dict:
        """Current/default/min/max board power limit in watts (-1 if unsupported)."""
        out = {
            "power_limit_w": -1, "power_limit_default_w": -1,
            "power_limit_min_w": -1, "power_limit_max_w": -1,
        }
        try:
            handle = self._handles[index]
            out["power_limit_w"] = round(pynvml.nvmlDeviceGetPowerManagementLimit(handle) / 1000)
            out["power_limit_default_w"] = round(
                pynvml.nvmlDeviceGetPowerManagementDefaultLimit(handle) / 1000
            )
            lo, hi = pynvml.nvmlDeviceGetPowerManagementLimitConstraints(handle)
            out["power_limit_min_w"] = round(lo / 1000)
            out["power_limit_max_w"] = round(hi / 1000)
        except Exception as e:
            logger.debug(f"GPU {index}: power limit not readable: {e}")
        return out

    def set_power_limit(self, index: int, watts: int) -> tuple[bool, int]:
        """Set the board power limit (needs root). Clamped to what the card allows.

        Returns (success, watts_actually_requested).
        """
        try:
            handle = self._handles[index]
            lo, hi = pynvml.nvmlDeviceGetPowerManagementLimitConstraints(handle)
            mw = max(lo, min(hi, int(watts) * 1000))
            pynvml.nvmlDeviceSetPowerManagementLimit(handle, mw)
            return True, round(mw / 1000)
        except Exception as e:
            logger.error(f"GPU {index}: failed to set power limit to {watts} W: {e}")
            return False, int(watts)

    def read_all(self) -> list[GpuState]:
        """Read all GPUs. Never raises; errors are per-GPU."""
        return [self.read_gpu(i) for i in range(self._gpu_count)]

    def shutdown(self):
        """Shutdown NVML cleanly."""
        if self._initialized:
            pynvml.nvmlShutdown()
            self._initialized = False
            logger.info("NVML shutdown")
