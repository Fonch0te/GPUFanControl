"""
Ring buffer for GPU telemetry history with disk persistence.

Daemon writes status.json every poll cycle and history.json every 30s.
UI reads these files independently — no IPC needed.
All disk writes are atomic (tmp + rename).
"""

import json
import logging
import os
import time
from collections import deque
from dataclasses import asdict
from pathlib import Path
from models import GpuState, HistoryEntry, MoboFanState

logger = logging.getLogger("gpu-fan-control.history")

# Shared with the UI. Overridable so the daemon can run as root (NVML fan
# writes require root on driver >= 520) while the UI reads as the login user.
DATA_DIR = Path(os.environ.get("GPU_FAN_CONTROL_DATA_DIR") or (Path.home() / "gpu-fan-control" / "data"))
HISTORY_PATH = DATA_DIR / "history.json"
STATUS_PATH = DATA_DIR / "status.json"
MOBO_HISTORY_PATH = DATA_DIR / "mobo_history.json"


class HistoryBuffer:
    def __init__(self, retention_minutes: int = 120, flush_interval: int = 30):
        self._retention = retention_minutes * 60
        self._flush_interval = flush_interval
        self._buffer: deque[HistoryEntry] = deque()
        self._mobo_buffer: deque[dict] = deque()
        self._last_flush: float = 0

    def record(self, states: list[GpuState]):
        """Add current GPU states to history."""
        now = time.time()
        for s in states:
            if s.error is None:
                self._buffer.append(HistoryEntry(
                    timestamp=now,
                    gpu_index=s.index,
                    temperature_c=s.temperature_c,
                    fan_speed_pct=s.fan_speed_pct,
                    fan_target_pct=s.fan_target_pct,
                    power_draw_w=s.power_draw_w,
                    utilization_pct=s.utilization_pct,
                ))
        self._prune()

        if now - self._last_flush >= self._flush_interval:
            self.flush()
            self._last_flush = now

    def record_mobo(self, states: list[MoboFanState], cpu_temp_c: int | None):
        """Add motherboard fan states (and CPU temp) to history."""
        now = time.time()
        for s in states:
            if s.error is None:
                self._mobo_buffer.append({
                    "timestamp": now,
                    "name": s.name,
                    "source_temp_c": s.source_temp_c,
                    "cpu_temp_c": cpu_temp_c if cpu_temp_c is not None else -1,
                    "duty_pct": s.duty_pct,
                    "rpm": s.rpm,
                    "target_pct": s.target_pct,
                })
        self._prune()

    def flush(self):
        """Write history to disk atomically."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = [asdict(e) for e in self._buffer]
        _atomic_write(HISTORY_PATH, data)
        if self._mobo_buffer:
            _atomic_write(MOBO_HISTORY_PATH, list(self._mobo_buffer))
        logger.debug(f"History flushed: {len(data)} GPU entries, {len(self._mobo_buffer)} mobo entries")

    def _prune(self):
        """Remove entries older than retention period."""
        cutoff = time.time() - self._retention
        while self._buffer and self._buffer[0].timestamp < cutoff:
            self._buffer.popleft()
        while self._mobo_buffer and self._mobo_buffer[0]["timestamp"] < cutoff:
            self._mobo_buffer.popleft()

    @staticmethod
    def load_mobo_from_disk() -> list[dict]:
        """Load motherboard fan history (used by UI)."""
        if not MOBO_HISTORY_PATH.exists():
            return []
        try:
            with open(MOBO_HISTORY_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []

    @staticmethod
    def load_from_disk() -> list[HistoryEntry]:
        """Load history from disk (used by UI)."""
        if not HISTORY_PATH.exists():
            return []
        try:
            with open(HISTORY_PATH) as f:
                data = json.load(f)
            return [HistoryEntry(**e) for e in data]
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            logger.warning(f"Failed to load history: {e}")
            return []


def write_daemon_status(
    gpu_states: list[GpuState],
    config_path: str,
    start_time: float,
    mobo_states: list[MoboFanState] | None = None,
    cpu_temp_c: int | None = None,
):
    """Write daemon status JSON for UI consumption."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    status = {
        "running": True,
        "pid": os.getpid(),
        "uptime_seconds": round(time.time() - start_time, 1),
        "gpu_states": [asdict(s) for s in gpu_states],
        "mobo_states": [asdict(s) for s in (mobo_states or [])],
        "cpu_temperature_c": cpu_temp_c if cpu_temp_c is not None else -1,
        "config_path": config_path,
        "timestamp": time.time(),
    }
    _atomic_write(STATUS_PATH, status)


def write_stopped_status():
    """Write stopped status on clean shutdown."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(STATUS_PATH, {
        "running": False,
        "pid": 0,
        "timestamp": time.time(),
    })


def read_daemon_status() -> dict:
    """Read daemon status from disk (used by UI)."""
    if not STATUS_PATH.exists():
        return {"running": False}
    try:
        with open(STATUS_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"running": False}


def _atomic_write(path: Path, data):
    """Write JSON atomically via tmp + rename."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f)
    tmp.rename(path)
