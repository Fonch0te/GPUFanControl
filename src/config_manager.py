"""
Configuration management: load, validate, save, and hot-reload.

The config file is the shared contract between daemon and UI.
UI writes config changes; daemon detects mtime change and reloads.
"""

import json
import logging
import os
from pathlib import Path
from models import DaemonConfig, FanCurvePoint, GpuConfig

logger = logging.getLogger("gpu-fan-control.config")

DEFAULT_CONFIG_PATH = Path("/Projects/gpu-fan-control/config/gpu-fan-control.json")


class ConfigManager:
    def __init__(self, config_path: Path = DEFAULT_CONFIG_PATH):
        self._path = config_path
        self._last_mtime: float = 0
        self._config: DaemonConfig | None = None

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> DaemonConfig:
        """Load and validate config from JSON file."""
        with open(self._path) as f:
            raw = json.load(f)

        config = self._parse(raw)
        self._validate(config)
        self._last_mtime = os.path.getmtime(self._path)
        self._config = config
        logger.info(f"Config loaded from {self._path}")
        return config

    def has_changed(self) -> bool:
        """Check if config file has been modified since last load."""
        try:
            current_mtime = os.path.getmtime(self._path)
            return current_mtime > self._last_mtime
        except OSError:
            return False

    def reload_if_changed(self) -> tuple[DaemonConfig, bool]:
        """Reload config if file changed. Returns (config, did_reload)."""
        if self.has_changed():
            try:
                config = self.load()
                logger.info("Configuration reloaded successfully")
                return config, True
            except Exception as e:
                logger.error(f"Config reload failed, keeping current: {e}")
                return self._config, False
        return self._config, False

    def save(self, config: DaemonConfig):
        """Save config to JSON (used by UI). Atomic write via tmp + rename."""
        raw = self._serialize(config)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(raw, f, indent=2)
            f.write("\n")
        tmp_path.rename(self._path)
        self._last_mtime = os.path.getmtime(self._path)
        self._config = config

    def _validate(self, config: DaemonConfig):
        """Validate config. Raise ValueError on invalid."""
        if len(config.fan_curve) < 2:
            raise ValueError("Fan curve must have at least 2 points")

        for i in range(len(config.fan_curve) - 1):
            if config.fan_curve[i].temp_c >= config.fan_curve[i + 1].temp_c:
                raise ValueError("Fan curve temps must be strictly increasing")

        for pt in config.fan_curve:
            if not (0 <= pt.fan_pct <= 100):
                raise ValueError(f"Fan speed must be 0-100, got {pt.fan_pct}")
            if not (0 <= pt.temp_c <= 100):
                raise ValueError(f"Temp must be 0-100C, got {pt.temp_c}")

        if config.fan_curve[-1].fan_pct < 100:
            logger.warning("Fan curve max point is not 100%% — GPUs may throttle at high temps")

        if config.poll_interval_seconds < 1:
            raise ValueError("Poll interval must be >= 1 second")
        if config.hysteresis_celsius < 0:
            raise ValueError("Hysteresis must be >= 0")

    def _parse(self, raw: dict) -> DaemonConfig:
        """Parse raw JSON dict into DaemonConfig."""
        fan_curve = [
            FanCurvePoint(temp_c=pt["temp_c"], fan_pct=pt["fan_pct"])
            for pt in raw.get("fan_curve", [])
        ]

        gpus = {}
        for idx_str, gpu_raw in raw.get("gpus", {}).items():
            gpus[idx_str] = GpuConfig(
                enabled=gpu_raw.get("enabled", True),
                label=gpu_raw.get("label", f"GPU {idx_str}"),
                fan_index=gpu_raw.get("fan_index"),
            )

        profiles = {}
        for name, pts in raw.get("profiles", {}).items():
            profiles[name] = [
                FanCurvePoint(temp_c=pt["temp_c"], fan_pct=pt["fan_pct"])
                for pt in pts
            ]

        return DaemonConfig(
            version=raw.get("version", 1),
            poll_interval_seconds=raw.get("poll_interval_seconds", 5.0),
            hysteresis_celsius=raw.get("hysteresis_celsius", 3.0),
            display=raw.get("display", ":1"),
            log_level=raw.get("log_level", "INFO"),
            history_retention_minutes=raw.get("history_retention_minutes", 120),
            fan_curve=fan_curve,
            gpus=gpus,
            profiles=profiles,
        )

    def _serialize(self, config: DaemonConfig) -> dict:
        """Serialize DaemonConfig to JSON-compatible dict."""
        return {
            "version": config.version,
            "poll_interval_seconds": config.poll_interval_seconds,
            "hysteresis_celsius": config.hysteresis_celsius,
            "display": config.display,
            "log_level": config.log_level,
            "history_retention_minutes": config.history_retention_minutes,
            "fan_curve": [
                {"temp_c": pt.temp_c, "fan_pct": pt.fan_pct}
                for pt in config.fan_curve
            ],
            "gpus": {
                idx: {
                    "enabled": gpu.enabled,
                    "label": gpu.label,
                    **({"fan_index": gpu.fan_index} if gpu.fan_index is not None else {}),
                }
                for idx, gpu in config.gpus.items()
            },
            "profiles": {
                name: [{"temp_c": pt.temp_c, "fan_pct": pt.fan_pct} for pt in pts]
                for name, pts in config.profiles.items()
            },
        }
