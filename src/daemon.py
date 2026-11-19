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
from collections import deque
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
        # name -> deque of (timestamp, raw temp) for rolling-average smoothing
        self._mobo_temp_hist: dict[str, deque] = {}
        self._mobo_next_retry = 0.0
        # GPUs whose power limit this daemon has changed from the driver default
        self._power_limited: set[int] = set()
        # GPU fan control needs a logged-in X session. Until one exists (boot,
        # logout) writes fail; the control loop keeps retrying initialization.
        self._idx_map: dict[int, dict] = {}
        self._fan_ready = False
        self._fan_next_retry = 0.0
        self._fan_fail_streak = 0
        self._drift_streak: dict[int, int] = {}
        self._fan_last_reinit = 0.0

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
        self._initialize_fan_control(gpu_count)
        self._initialize_mobo_control()
        self._apply_power_limits()

        self._running = True
        self._start_time = time.time()
        logger.info("Daemon started, entering control loop")

        try:
            self._control_loop()
        finally:
            self._shutdown()

    FAN_RETRY_SECONDS = 30
    FAN_REINIT_MIN_INTERVAL = 300

    def _initialize_fan_control(self, gpu_count: int):
        """Startup: a few quick attempts, then hand over to the control loop.

        nvidia-settings needs a running X session. At boot that only exists
        once someone has logged in, which can be minutes or hours later, so
        startup must not block on it: motherboard fans and power limits do not
        need X and should come up immediately. The control loop calls
        _try_initialize_fan_control() every FAN_RETRY_SECONDS until it works.
        """
        for attempt in range(1, 4):
            if self._try_initialize_fan_control(gpu_count):
                return
            time.sleep(5)
        logger.warning(
            "GPU fan control not available yet (no X session on "
            f"{self._config.display}?). GPUs stay on their firmware fan curve; "
            f"retrying every {self.FAN_RETRY_SECONDS}s."
        )

    def _try_initialize_fan_control(self, gpu_count: int) -> bool:
        """One attempt: detect the GPU/fan mapping and enable manual control."""
        self._fan_next_retry = time.time() + self.FAN_RETRY_SECONDS
        auto_map = self._writer.detect_index_mapping(gpu_count)

        idx_map: dict[int, dict] = {}
        for idx in range(gpu_count):
            entry = auto_map.get(idx, {"nv_gpu": idx, "nv_fan": idx, "nv_fans": [idx], "pci_bus": "unknown"})
            gpu_cfg = self._config.gpus.get(str(idx))
            if gpu_cfg is not None and gpu_cfg.fan_index is not None:
                entry = {**entry, "nv_fan": gpu_cfg.fan_index, "nv_fans": [gpu_cfg.fan_index]}
            idx_map[idx] = entry

        all_ok = True
        for idx in range(gpu_count):
            gpu_cfg = self._config.gpus.get(str(idx))
            enabled = gpu_cfg.enabled if gpu_cfg is not None else True
            if enabled and not self._writer.enable_manual_control(idx_map[idx]["nv_gpu"]):
                all_ok = False

        if not all_ok:
            self._fan_ready = False
            return False

        self._idx_map = idx_map
        for idx, entry in idx_map.items():
            gpu_cfg = self._config.gpus.get(str(idx))
            src = (
                "fan_index from config"
                if gpu_cfg is not None and gpu_cfg.fan_index is not None
                else f"auto-detected via PCI bus {entry['pci_bus']}"
            )
            logger.info(f"GPU {idx}: nv_gpu={entry['nv_gpu']}, nv_fans={entry.get('nv_fans', [entry['nv_fan']])} ({src})")
        logger.info("GPU fan control initialized")
        self._fan_ready = True
        self._fan_fail_streak = 0
        self._drift_streak.clear()
        # Forget remembered targets so every fan is written on the next poll
        self._controller = FanController(
            self._config.fan_curve, self._config.hysteresis_celsius
        )
        return True

    # ------------------------------------------------------------------
    # GPU power limits
    # ------------------------------------------------------------------

    def _apply_power_limits(self, states=None):
        """Make each GPU's power cap match the config.

        The driver forgets the limit on every reboot (and on some resets), so
        this runs at startup, on config reload, and whenever a poll shows the
        enforced limit drifting from the configured one. A GPU with no
        power_limit_w is put back on the driver default if we changed it earlier.
        """
        for idx in range(self._gpu_count):
            gpu_cfg = self._config.gpus.get(str(idx))
            wanted = gpu_cfg.power_limit_w if gpu_cfg is not None else None

            if states is not None:
                st = next((s for s in states if s.index == idx), None)
                limits = {
                    "power_limit_w": st.power_limit_w,
                    "power_limit_default_w": st.power_limit_default_w,
                    "power_limit_min_w": st.power_limit_min_w,
                    "power_limit_max_w": st.power_limit_max_w,
                } if st is not None and st.error is None else None
            else:
                limits = self._monitor.read_power_limits(idx)
            if not limits or limits["power_limit_w"] < 0:
                continue  # card does not expose a power limit

            if wanted is None:
                if idx not in self._power_limited:
                    continue  # never touched it; leave whatever is set
                target = limits["power_limit_default_w"]
            else:
                target = max(limits["power_limit_min_w"],
                             min(limits["power_limit_max_w"], int(wanted)))

            if limits["power_limit_w"] == target:
                if wanted is None:
                    self._power_limited.discard(idx)
                continue

            ok, applied = self._monitor.set_power_limit(idx, target)
            if ok:
                note = "" if wanted is None or applied == wanted else f" (requested {wanted} W, clamped)"
                what = "restored to default" if wanted is None else "set"
                logger.info(
                    f"GPU {idx}: power limit {what}: {limits['power_limit_w']} W -> {applied} W{note}"
                )
                if wanted is None:
                    self._power_limited.discard(idx)
                else:
                    self._power_limited.add(idx)

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
        self._mobo_temp_hist.clear()

    def _apply_mobo_control(self, gpu_states) -> tuple[list[MoboFanState], int | None]:
        """Compute and write motherboard fan duties. Returns (states, cpu_temp)."""
        mobo = self._config.motherboard
        if not mobo.enabled:
            return [], None
        if self._mobo_writer is None:
            # Chip was missing at startup (e.g. nct6775 module not loaded yet).
            # Retry once a minute instead of staying off until the next restart.
            now = time.time()
            if now < self._mobo_next_retry:
                return [], None
            self._mobo_next_retry = now + 60
            self._initialize_mobo_control()
            if self._mobo_writer is None:
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

            # Rolling average: the package sensor spikes 15-20C on short bursts
            # while a radiator's coolant moves over minutes. Averaging the input
            # stops the fans hunting without slowing the response to real load.
            raw_temp = src_temp
            if src_temp is not None and fan.smoothing_seconds > 0:
                now = time.time()
                hist = self._mobo_temp_hist.setdefault(name, deque())
                hist.append((now, src_temp))
                cutoff = now - fan.smoothing_seconds
                while hist and hist[0][0] < cutoff:
                    hist.popleft()
                src_temp = round(sum(t for _, t in hist) / len(hist))

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
                    raw_note = f" (raw {raw_temp}C)" if raw_temp != src_temp else ""
                    logger.info(
                        f"Mobo '{name}': {fan.source} {src_temp}C{raw_note} -> pwm{fan.pwm} {target}% "
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
                raw_temp_c=raw_temp if raw_temp is not None else -1,
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

            # Power caps: apply config changes and re-assert after driver resets
            try:
                self._apply_power_limits(states)
            except Exception as e:
                logger.error(f"Power limit error: {e}")

            # GPU fans need an X session; keep trying until one is there
            if not self._fan_ready and time.time() >= self._fan_next_retry:
                self._try_initialize_fan_control(self._gpu_count)

            # Apply fan control per GPU
            for state in states:
                idx_str = str(state.index)
                gpu_cfg = self._config.gpus.get(idx_str)

                if gpu_cfg and not gpu_cfg.enabled:
                    continue

                if state.error:
                    continue

                if not self._fan_ready:
                    # Card is on its firmware curve; report it as such
                    state.fan_target_pct = state.fan_speed_pct
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
                    entry = self._idx_map.get(state.index, {"nv_fan": state.index})
                    # A card can have several fan controllers (RTX 3090: two)
                    nv_fans = entry.get("nv_fans") or [entry["nv_fan"]]
                    success = all(
                        [self._writer.set_fan_speed(f, target_pct) for f in nv_fans]
                    )
                    nv_fan = ",".join(str(f) for f in nv_fans)
                    if success:
                        reason = "drift correction" if drifted else "curve"
                        logger.info(
                            f"GPU {state.index}: {state.temperature_c}C -> nv fan:{nv_fan} {target_pct}% "
                            f"(actual was {state.fan_speed_pct}%, {reason})"
                        )
                        self._fan_fail_streak = 0
                    else:
                        logger.error(
                            f"GPU {state.index}: failed to set nv fan:{nv_fan} to {target_pct}%"
                        )
                        self._fan_fail_streak += 1

                # A fan that never reaches its target means manual control was
                # lost (X restarted, logout/login) or the mapping is wrong.
                self._drift_streak[state.index] = (
                    self._drift_streak.get(state.index, 0) + 1 if drifted else 0
                )

            if self._fan_ready:
                stuck = [i for i, n in self._drift_streak.items() if n >= 6]
                lost = self._fan_fail_streak >= 3
                if lost or (stuck and time.time() - self._fan_last_reinit > self.FAN_REINIT_MIN_INTERVAL):
                    why = "writes failing" if lost else f"GPU {stuck} not following its target"
                    logger.warning(f"GPU fan control lost ({why}); re-initializing")
                    self._fan_last_reinit = time.time()
                    self._fan_ready = False
                    self._fan_next_retry = 0.0

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
