"""Data models for GPU Fan Control daemon and UI."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class FanCurvePoint:
    """Single point on a fan curve: temperature -> fan speed mapping."""
    temp_c: int
    fan_pct: int


@dataclass
class GpuConfig:
    """Per-GPU configuration."""
    enabled: bool = True
    label: str = ""
    fan_index: Optional[int] = None  # nvidia-settings fan index; None = auto-detect


@dataclass
class MoboFanConfig:
    """One motherboard fan header driven by the daemon."""
    pwm: int                       # hwmon pwmN index (fanN_input is its tach)
    label: str = ""
    enabled: bool = True
    source: str = "cpu"            # cpu | gpu | max (hotter of the two)
    curve: list[FanCurvePoint] = field(default_factory=list)


@dataclass
class MotherboardConfig:
    """Motherboard (Super I/O) fan control section."""
    enabled: bool = False
    chip: str = "nct6796"          # hwmon `name` attribute to resolve by
    fans: dict[str, MoboFanConfig] = field(default_factory=dict)


@dataclass
class DaemonConfig:
    """Complete daemon configuration loaded from JSON."""
    version: int = 1
    poll_interval_seconds: float = 5.0
    hysteresis_celsius: float = 3.0
    display: str = ":1"
    log_level: str = "INFO"
    history_retention_minutes: int = 120
    fan_curve: list[FanCurvePoint] = field(default_factory=list)
    gpus: dict[str, GpuConfig] = field(default_factory=dict)
    profiles: dict[str, list[FanCurvePoint]] = field(default_factory=dict)
    motherboard: MotherboardConfig = field(default_factory=MotherboardConfig)


@dataclass
class MoboFanState:
    """Snapshot of one daemon-controlled motherboard fan header."""
    name: str
    label: str
    pwm: int
    source: str
    source_temp_c: int
    duty_pct: int
    rpm: int
    target_pct: int
    timestamp: float = field(default_factory=time.time)
    error: Optional[str] = None


@dataclass
class GpuState:
    """Snapshot of a single GPU's state at a point in time."""
    index: int
    temperature_c: int
    fan_speed_pct: int
    fan_target_pct: int
    power_draw_w: float
    utilization_pct: int
    memory_used_mb: int
    memory_total_mb: int
    name: str
    timestamp: float = field(default_factory=time.time)
    error: Optional[str] = None


@dataclass
class HistoryEntry:
    """Single history record for one GPU at one point in time."""
    timestamp: float
    gpu_index: int
    temperature_c: int
    fan_speed_pct: int
    fan_target_pct: int
    power_draw_w: float
    utilization_pct: int
