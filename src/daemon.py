#!/usr/bin/env python3
"""
GPU Fan Control Daemon.

Polls GPU temperatures via pynvml, applies fan curve via nvidia-settings.
Optionally drives motherboard fan headers (hwmon/sysfs) from CPU and/or GPU
temperature. Runs as a root systemd service with auto-restart.

Lifecycle:
  1. Load config
  2. Initialize pynvml
  3. Enable manual fan control on enabled GPUs
  4. Poll loop: read temps -> compute targets -> set fans -> record history
  5. On SIGTERM/SIGINT: reset fans to auto, shutdown cleanly

Usage:
  python daemon.py [--config /path/to/config.json]
"""

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from config_manager import ConfigManager, DEFAULT_CONFIG_PATH
from fan_controller import FanController
from gpu_monitor import GpuMonitor
from history import HistoryBuffer, write_daemon_status, write_stopped_status
from hwmon import CpuTempReader, HwmonFanWriter
from models import MoboFanState
from nvidia_fan_writer import NvidiaFanWriter

logger = logging.getLogger("gpu-fan-control")


class FanControlDaemon:
    def __init__(self, config_path: Path):
        self._config_mgr = ConfigManager(config_path)
        self._monitor = GpuMonitor()
        self._writer: NvidiaFanWriter | None = None
        self._controller: FanController | None = None
        self._history: HistoryBuffer | None = None
        self._running = False
        self._start_time = 0.0
        self._config = None
        self._gpu_count = 0
        # Motherboard fan control (optional)
        self._mobo_writer: HwmonFanWriter | None = None
        self._cpu_temp: CpuTempReader | None = None
        self._mobo_controllers: dict[str, FanController] = {}

    def run(self):
        """Main entry point. Blocks until shutdown signal."""
        self._setup_signals()

        try:
            self._config = self._config_mgr.load()
        except Exception as e:
            logger.critical(f"Failed to load config: {e}")
            sys.exit(1)

        self._setup_logging(self._config.log_level)
        logger.info(f"Config loaded from {self._config_mgr.path}")

        try:
            gpu_count = self._monitor.initialize()
        except Exception as e:
            logger.critical(f"Failed to initialize NVML: {e}")
            sys.exit(1)

        self._gpu_count = gpu_count
        logger.info(f"Discovered {gpu_count} GPUs")

        self._writer = NvidiaFanWriter(display=self._config.display)
        self._controller = FanController(
            self._config.fan_curve, self._config.hysteresis_celsius
        )
        self._history = HistoryBuffer(self._config.history_retention_minutes)

        # Build index mapping and enable manual fan control, retrying if X11
        # isn't ready yet (common at boot — display-manager.service is "started"
        # before the X server actually accepts connections).
        self._idx_map: dict[int, dict] = {}
        self._initialize_fan_control(gpu_count)
        self._initialize_mobo_control()

        self._running = True
        self._start_time = time.time()
        logger.info("Daemon started, entering control loop")

        try:
            self._control_loop()
        finally:
            self._shutdown()

    def _initialize_fan_control(self, gpu_count: int):
        """Detect GPU index mapping and enable manual fan control.

        Retries with backoff when nvidia-settings can't reach X11 (boot race).
        Gives up after ~5 minutes and enters the control loop anyway — the drift
        detector will keep retrying set_fan_speed each poll cycle.
        """
        max_attempts = 10
        backoff_seconds = 5

        for attempt in range(1, max_attempts + 1):
            # Step 1: detect PCI bus mapping
            auto_map = self._writer.detect_index_mapping(gpu_count)

            self._idx_map.clear()
            for idx in range(gpu_count):
                entry = auto_map.get(idx, {"nv_gpu": idx, "nv_fan": idx, "pci_bus": "unknown"})
                gpu_cfg = self._config.gpus.get(str(idx))
                if gpu_cfg is not None and gpu_cfg.fan_index is not None:
                    entry = {**entry, "nv_fan": gpu_cfg.fan_index}
                self._idx_map[idx] = entry

            # Step 2: enable manual fan control on each enabled GPU
            all_ok = True
            for idx in range(gpu_count):
                gpu_cfg = self._config.gpus.get(str(idx))
                enabled = gpu_cfg.enabled if gpu_cfg is not None else True
                if enabled:
                    nv_gpu = self._idx_map[idx]["nv_gpu"]
                    if not self._writer.enable_manual_control(nv_gpu):
                        all_ok = False

            if all_ok:
                for idx, entry in self._idx_map.items():
                    src = f"fan_index from config" if self._config.gpus.get(str(idx)) and self._config.gpus[str(idx)].fan_index is not None else f"auto-detected via PCI bus {entry['pci_bus']}"
                    logger.info(f"GPU {idx}: nv_gpu={entry['nv_gpu']}, nv_fan={entry['nv_fan']} ({src})")
                logger.info(f"Fan control initialized successfully (attempt {attempt})")
                return

            wait = backoff_seconds * attempt
            logger.warning(
                f"nvidia-settings not ready (attempt {attempt}/{max_attempts}), "
                f"retrying in {wait}s..."
            )
            time.sleep(wait)

        logger.error(
            f"Fan control initialization failed after {max_attempts} attempts. "
            f"Entering control loop anyway — drift detection will retry fan writes."
        )

    # ------------------------------------------------------------------
    # Motherboard fans
    # ------------------------------------------------------------------

    def _initialize_mobo_control(self):
        """Claim configured motherboard headers. Safe to call again on reload."""
        self._release_mobo_control()

        mobo = self._config.motherboard
        if not mobo.enabled or not mobo.fans:
            return

        self._cpu_temp = CpuTempReader()
        self._mobo_writer = HwmonFanWriter(mobo.chip)
        if not self._mobo_writer.available:
            logger.error("Motherboard fan control disabled: hwmon chip not found")
            self._mobo_writer = None
            return

        for name, fan in mobo.fans.items():
            if not fan.enabled:
                continue
            if self._mobo_writer.claim(fan.pwm):
                self._mobo_controllers[name] = FanController(
                    fan.curve, self._config.hysteresis_celsius
                )
                logger.info(
                    f"Mobo fan '{name}' ({fan.label}): pwm{fan.pwm}, source={fan.source}"
                )
            else:
                logger.error(f"Mobo fan '{name}': could not claim pwm{fan.pwm}, skipping")

    def _release_mobo_control(self):
        if self._mobo_writer:
            self._mobo_writer.release_all()
        self._mobo_writer = None
        self._mobo_controllers.clear()

    def _apply_mobo_control(self, gpu_states) -> tuple[list[MoboFanState], int | None]:
        """Compute and write motherboard fan duties. Returns (states, cpu_temp)."""
        mobo = self._config.motherboard
        if not mobo.enabled or self._mobo_writer is None:
            return [], None

        cpu_temp = self._cpu_temp.read() if self._cpu_temp else None
        gpu_temps = [g.temperature_c for g in gpu_states if g.error is None and g.temperature_c >= 0]
        gpu_temp = max(gpu_temps) if gpu_temps else None

        states: list[MoboFanState] = []
        for name, fan in mobo.fans.items():
            controller = self._mobo_controllers.get(name)
            if controller is None:
                continue

            if fan.source == "cpu":
                src_temp = cpu_temp
            elif fan.source == "gpu":
                src_temp = gpu_temp
            else:  # max
                candidates = [t for t in (cpu_temp, gpu_temp) if t is not None]
                src_temp = max(candidates) if candidates else None

            duty, rpm = self._mobo_writer.read(fan.pwm)

            if src_temp is None:
                # Failsafe: no usable sensor -> run the top of the curve.
                target = max(pt.fan_pct for pt in fan.curve)
                changed = duty != target
                logger.warning(f"Mobo fan '{name}': no {fan.source} temperature, failsafe {target}%")
            else:
                target, changed = controller.compute_fan_speed(0, src_temp)

            drifted = not changed and duty >= 0 and abs(duty - target) > 3
            error = None
            if changed or drifted:
                if self._mobo_writer.set_duty(fan.pwm, target):
                    reason = "drift correction" if drifted else "curve"
                    logger.info(
                        f"Mobo '{name}': {fan.source} {src_temp}C -> pwm{fan.pwm} {target}% "
                        f"(actual was {duty}%, {rpm} rpm, {reason})"
                    )
                else:
                    error = "write failed"
                    logger.error(f"Mobo '{name}': failed to set pwm{fan.pwm} to {target}%")

            states.append(MoboFanState(
                name=name,
                label=fan.label,
                pwm=fan.pwm,
                source=fan.source,
                source_temp_c=src_temp if src_temp is not None else -1,
                duty_pct=duty,
                rpm=rpm,
                target_pct=target,
                error=error,
            ))
        return states, cpu_temp

    def _control_loop(self):
        """Main poll loop."""
        while self._running:
            loop_start = time.time()

            # Check for config hot-reload
            new_config, reloaded = self._config_mgr.reload_if_changed()
            if reloaded and new_config:
                self._config = new_config
                self._controller.update_curve(self._config.fan_curve)
                self._controller.hysteresis = self._config.hysteresis_celsius
                self._initialize_mobo_control()
                logger.info("Fan curve updated from config reload")

            # Read all GPU states
            states = self._monitor.read_all()

            # Apply fan control per GPU
            for state in states:
                idx_str = str(state.index)
                gpu_cfg = self._config.gpus.get(idx_str)

                if gpu_cfg and not gpu_cfg.enabled:
                    continue

                if state.error:
                    continue

                target_pct, changed = self._controller.compute_fan_speed(
                    state.index, state.temperature_c
                )
                state.fan_target_pct = target_pct

                # Drift detection: re-apply if actual fan speed diverges
                # from target, even when hysteresis suppressed the update.
                # Catches external resets (driver power state, other processes).
                drifted = (
                    not changed
                    and state.fan_speed_pct >= 0
                    and abs(state.fan_speed_pct - target_pct) > 3
                )

                if changed or drifted:
                    nv_fan = self._idx_map.get(
                        state.index, {"nv_fan": state.index}
                    )["nv_fan"]
                    success = self._writer.set_fan_speed(nv_fan, target_pct)
                    if success:
                        reason = "drift correction" if drifted else "curve"
                        logger.info(
                            f"GPU {state.index}: {state.temperature_c}C -> nv fan:{nv_fan} {target_pct}% "
                            f"(actual was {state.fan_speed_pct}%, {reason})"
                        )
                    else:
                        logger.error(
                            f"GPU {state.index}: failed to set nv fan:{nv_fan} to {target_pct}%"
                        )

            # Motherboard fans (CPU radiator, case intakes, ...)
            try:
                mobo_states, cpu_temp = self._apply_mobo_control(states)
            except Exception as e:
                logger.error(f"Motherboard fan control error: {e}")
                mobo_states, cpu_temp = [], None

            # Record history and write status
            self._history.record(states)
            if mobo_states:
                self._history.record_mobo(mobo_states, cpu_temp)
            try:
                write_daemon_status(
                    states, str(self._config_mgr.path), self._start_time,
                    mobo_states=mobo_states, cpu_temp_c=cpu_temp,
                )
            except Exception as e:
                logger.warning(f"Failed to write status: {e}")

            # Sleep remainder of poll interval
            elapsed = time.time() - loop_start
            sleep_time = max(0, self._config.poll_interval_seconds - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)

    def _shutdown(self):
        """Graceful shutdown: reset fans to auto mode."""
        logger.info("Shutting down, resetting fans to auto mode...")

        if self._writer and self._config:
            for idx in range(self._gpu_count):
                gpu_cfg = self._config.gpus.get(str(idx))
                enabled = gpu_cfg.enabled if gpu_cfg is not None else True
                if enabled:
                    nv_gpu = self._idx_map.get(idx, {"nv_gpu": idx})["nv_gpu"]
                    self._writer.disable_manual_control(nv_gpu)
                    logger.info(f"GPU {idx} (nv gpu:{nv_gpu}): fan control reset to auto")

        if self._mobo_writer:
            logger.info("Restoring motherboard fan headers to BIOS control...")
            self._release_mobo_control()

        if self._history:
            self._history.flush()

        self._monitor.shutdown()

        try:
            write_stopped_status()
        except Exception:
            pass

        logger.info("Daemon stopped cleanly")

    def _setup_signals(self):
        signal.signal(signal.SIGTERM, self._handle_stop)
        signal.signal(signal.SIGINT, self._handle_stop)

    def _handle_stop(self, signum, frame):
        logger.info(f"Received signal {signum}, initiating shutdown")
        self._running = False

    def _setup_logging(self, level: str):
        log_level = getattr(logging, level.upper(), logging.INFO)
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )


def main():
    parser = argparse.ArgumentParser(description="GPU Fan Control Daemon")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to configuration file",
    )
    args = parser.parse_args()

    daemon = FanControlDaemon(args.config)
    daemon.run()


if __name__ == "__main__":
    main()
