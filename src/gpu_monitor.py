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

    def read_all(self) -> list[GpuState]:
        """Read all GPUs. Never raises; errors are per-GPU."""
        return [self.read_gpu(i) for i in range(self._gpu_count)]

    def shutdown(self):
        """Shutdown NVML cleanly."""
        if self._initialized:
            pynvml.nvmlShutdown()
            self._initialized = False
            logger.info("NVML shutdown")
