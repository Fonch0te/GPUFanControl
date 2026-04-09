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
from models import GpuState, HistoryEntry

logger = logging.getLogger("gpu-fan-control.history")

DATA_DIR = Path.home() / "gpu-fan-control" / "data"
HISTORY_PATH = DATA_DIR / "history.json"
STATUS_PATH = DATA_DIR / "status.json"


class HistoryBuffer:
    def __init__(self, retention_minutes: int = 120, flush_interval: int = 30):
        self._retention = retention_minutes * 60
        self._flush_interval = flush_interval
        self._buffer: deque[HistoryEntry] = deque()
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

    def flush(self):
        """Write history to disk atomically."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = [asdict(e) for e in self._buffer]
        _atomic_write(HISTORY_PATH, data)
        logger.debug(f"History flushed: {len(data)} entries")

    def _prune(self):
        """Remove entries older than retention period."""
        cutoff = time.time() - self._retention
        while self._buffer and self._buffer[0].timestamp < cutoff:
            self._buffer.popleft()

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
):
    """Write daemon status JSON for UI consumption."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    status = {
        "running": True,
        "pid": os.getpid(),
        "uptime_seconds": round(time.time() - start_time, 1),
        "gpu_states": [asdict(s) for s in gpu_states],
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
