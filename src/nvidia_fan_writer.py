"""
Set GPU fan speeds via nvidia-settings subprocess.

Uses nvidia-settings CLI with Coolbits=28 (already enabled in xorg.conf).
Requires DISPLAY environment variable for X11 context.
"""

import logging
import os
import subprocess

logger = logging.getLogger("gpu-fan-control.writer")


class NvidiaFanWriter:
    def __init__(self, display: str = ":1"):
        self._display = display
        self._env = {**os.environ, "DISPLAY": display}

    def detect_index_mapping(self, gpu_count: int) -> dict[int, dict]:
        """
        Auto-detect pynvml-to-nvidia-settings index mapping via PCI bus IDs.

        pynvml and nvidia-settings can enumerate GPUs in different orders.
        This matches them by PCI bus ID, then assumes fan:N = nvidia-settings gpu:N
        (each A6000 has one fan per card).

        Returns: {pynvml_gpu_index: {"nv_gpu": N, "nv_fan": N, "pci_bus": "XX"}}
        Falls back to identity mapping on failure.
        """
        # Step 1: Get PCI bus IDs from nvidia-smi (same ordering as pynvml)
        nvml_buses: dict[int, int] = {}  # {pynvml_idx: pci_bus_decimal}
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=index,pci.bus_id", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    parts = line.split(",")
                    idx = int(parts[0].strip())
                    # PCI bus ID like "00000000:2C:00.0" — extract bus number
                    pci_full = parts[1].strip()
                    bus_hex = pci_full.split(":")[1]  # "2C"
                    nvml_buses[idx] = int(bus_hex, 16)
                    logger.info(f"pynvml gpu:{idx} -> PCI bus 0x{bus_hex} ({int(bus_hex, 16)})")
        except Exception as e:
            logger.warning(f"Failed to query nvidia-smi PCI buses: {e}")

        # Step 2: Get PCI bus IDs from nvidia-settings
        nvsettings_buses: dict[int, int] = {}  # {nv_settings_idx: pci_bus_decimal}
        for nv_idx in range(gpu_count):
            try:
                result = subprocess.run(
                    ["nvidia-settings", "-t", "-q", f"[gpu:{nv_idx}]/PCIBus"],
                    env=self._env,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    bus_dec = int(result.stdout.strip())
                    nvsettings_buses[nv_idx] = bus_dec
                    logger.info(f"nvidia-settings gpu:{nv_idx} -> PCI bus {bus_dec}")
            except Exception as e:
                logger.warning(f"Failed to query nvidia-settings gpu:{nv_idx} PCIBus: {e}")

        # Step 3: Match by PCI bus — build pynvml_idx -> nv_settings_idx mapping
        if nvml_buses and nvsettings_buses and len(nvml_buses) == len(nvsettings_buses):
            # Invert nvsettings: {pci_bus: nv_idx}
            bus_to_nv = {bus: nv_idx for nv_idx, bus in nvsettings_buses.items()}

            mapping: dict[int, dict] = {}
            for pynvml_idx, bus in nvml_buses.items():
                if bus in bus_to_nv:
                    nv_idx = bus_to_nv[bus]
                    mapping[pynvml_idx] = {
                        "nv_gpu": nv_idx,
                        "nv_fan": nv_idx,  # fan:N = gpu:N (1 fan per card)
                        "pci_bus": f"0x{bus:02X}",
                    }
                    logger.info(
                        f"Mapped: pynvml gpu:{pynvml_idx} -> "
                        f"nvidia-settings gpu:{nv_idx}/fan:{nv_idx} "
                        f"(PCI bus 0x{bus:02X})"
                    )

            if len(mapping) == gpu_count:
                return mapping

            logger.warning(f"PCI bus matching incomplete: {len(mapping)}/{gpu_count} GPUs matched")

        # Fallback: identity mapping
        logger.warning(
            "Could not auto-detect index mapping, using identity "
            "(pynvml gpu:N -> nvidia-settings gpu:N/fan:N)"
        )
        return {
            i: {"nv_gpu": i, "nv_fan": i, "pci_bus": "unknown"}
            for i in range(gpu_count)
        }

    def enable_manual_control(self, nv_gpu_index: int) -> bool:
        """Enable manual fan control for a GPU (nvidia-settings index). Returns success."""
        return self._run_setting(f"[gpu:{nv_gpu_index}]/GPUFanControlState=1")

    def set_fan_speed(self, nv_fan_index: int, speed_pct: int) -> bool:
        """Set target fan speed (clamped 0-100). Uses nvidia-settings fan index."""
        speed_pct = max(0, min(100, speed_pct))
        return self._run_setting(f"[fan:{nv_fan_index}]/GPUTargetFanSpeed={speed_pct}")

    def disable_manual_control(self, nv_gpu_index: int) -> bool:
        """Reset to auto fan control (nvidia-settings index). Used on graceful shutdown."""
        return self._run_setting(f"[gpu:{nv_gpu_index}]/GPUFanControlState=0")

    def _run_setting(self, assignment: str) -> bool:
        """Execute nvidia-settings -a <assignment>. Returns success."""
        cmd = ["nvidia-settings", "-a", assignment]
        try:
            result = subprocess.run(
                cmd,
                env=self._env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                logger.error(f"nvidia-settings failed [{assignment}]: {result.stderr.strip()}")
                return False
            logger.debug(f"nvidia-settings OK: {assignment}")
            return True
        except subprocess.TimeoutExpired:
            logger.error(f"nvidia-settings timed out: {assignment}")
            return False
        except Exception as e:
            logger.error(f"nvidia-settings error: {e}")
            return False
