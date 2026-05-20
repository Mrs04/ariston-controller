"""Background controllers.

Two threads run independently and share the same `AristonClient`:

* `PollerThread`   – refreshes the device state every N seconds so the UI
                     and the rule controller see fresh data.
* `RuleThread`     – every M seconds evaluates the active rule and pushes
                     a new target temperature if it differs from the
                     currently programmed one (with a small dead-band so
                     we don't spam the API).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ariston_client import AristonClient, DeviceSnapshot, parse_retry_seconds
from rules import RuleSet

_LOG = logging.getLogger(__name__)


@dataclass
class LogEntry:
    when: datetime
    level: str
    message: str


class EventLog:
    """Thread-safe bounded log shared between controller threads and the UI."""

    def __init__(self, maxlen: int = 200) -> None:
        self._buf: deque[LogEntry] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, level: str, msg: str) -> None:
        with self._lock:
            self._buf.append(LogEntry(datetime.now(), level, msg))

    def info(self, msg: str) -> None:
        self.add("INFO", msg)

    def warn(self, msg: str) -> None:
        self.add("WARN", msg)

    def error(self, msg: str) -> None:
        self.add("ERROR", msg)

    def items(self) -> list[LogEntry]:
        with self._lock:
            return list(self._buf)


class PollerThread(threading.Thread):
    """Periodically refreshes device state and stores the latest snapshot."""

    def __init__(self, client: AristonClient, log: EventLog,
                 interval_seconds: float = 300.0) -> None:
        super().__init__(daemon=True, name="ariston-poller")
        self.client = client
        self.log = log
        self.interval = interval_seconds
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last: Optional[DeviceSnapshot] = None
        self._last_at: Optional[datetime] = None

    @property
    def last_snapshot(self) -> Optional[DeviceSnapshot]:
        with self._lock:
            return self._last

    @property
    def last_at(self) -> Optional[datetime]:
        with self._lock:
            return self._last_at

    def stop(self) -> None:
        self._stop.set()

    def set_interval(self, interval_seconds: float) -> None:
        self.interval = max(5.0, float(interval_seconds))

    def run(self) -> None:  # noqa: D401
        self.log.info(f"Poller started (every {self.interval:g}s).")
        while not self._stop.is_set():
            sleep_for = self.interval
            try:
                snap = self.client.refresh()
                with self._lock:
                    self._last = snap
                    self._last_at = datetime.now()
            except Exception as exc:  # noqa: BLE001
                retry = parse_retry_seconds(exc)
                if retry is not None:
                    sleep_for = max(self.interval, retry + 5)
                    self.log.warn(
                        f"Ariston rate-limited (429): blocked for {retry}s. "
                        f"Backing off {sleep_for:g}s."
                    )
                else:
                    self.log.error(f"Poll failed: {exc!r}")
            self._stop.wait(sleep_for)
        self.log.info("Poller stopped.")


class RuleThread(threading.Thread):
    """Applies the active rule to the device on a fixed cadence."""

    def __init__(self, client: AristonClient, poller: PollerThread,
                 ruleset_provider, log: EventLog,
                 interval_seconds: float = 300.0,
                 deadband: float = 0.5) -> None:
        super().__init__(daemon=True, name="ariston-rules")
        self.client = client
        self.poller = poller
        self.get_ruleset = ruleset_provider  # callable -> RuleSet (live)
        self.log = log
        self.interval = interval_seconds
        self.deadband = deadband
        self._stop = threading.Event()
        self._enabled = threading.Event()
        self._enabled.set()
        self._last_rule_name: Optional[str] = None
        self._last_target_applied: Optional[float] = None

    def stop(self) -> None:
        self._stop.set()

    def pause(self) -> None:
        self._enabled.clear()
        self.log.info("Rule controller paused.")

    def resume(self) -> None:
        self._enabled.set()
        self.log.info("Rule controller resumed.")

    @property
    def enabled(self) -> bool:
        return self._enabled.is_set()

    def set_interval(self, interval_seconds: float) -> None:
        self.interval = max(15.0, float(interval_seconds))

    def run(self) -> None:  # noqa: D401
        self.log.info(f"Rule controller started (every {self.interval:g}s).")
        while not self._stop.is_set():
            sleep_for = self.interval
            if self._enabled.is_set():
                try:
                    self._tick()
                except Exception as exc:  # noqa: BLE001
                    retry = parse_retry_seconds(exc)
                    if retry is not None:
                        sleep_for = max(self.interval, retry + 5)
                        self.log.warn(
                            f"Rule tick rate-limited: blocked for {retry}s. "
                            f"Backing off {sleep_for:g}s."
                        )
                    else:
                        self.log.error(f"Rule tick failed: {exc!r}")
            self._stop.wait(sleep_for)
        self.log.info("Rule controller stopped.")

    def _tick(self) -> None:
        snap = self.poller.last_snapshot
        if snap is None or snap.current_temperature is None:
            self.log.warn("No snapshot yet — skipping rule tick.")
            return

        ruleset: RuleSet = self.get_ruleset()
        rule = ruleset.active_rule()
        if rule is None:
            self.log.info("No active rule for current time.")
            return

        target = rule.compute_target(snap.current_temperature)
        current_target = snap.target_temperature or 0.0

        if self._last_rule_name != rule.name:
            self.log.info(
                f"Active rule → {rule.name} "
                f"(current={snap.current_temperature:.1f}°C, "
                f"target→{target:.0f}°C)."
            )
            self._last_rule_name = rule.name

        if abs(current_target - target) < self.deadband:
            return  # Already at the right setpoint.

        try:
            self.client.set_target_temperature(target)
            self._last_target_applied = target
            self.log.info(
                f"Set target {current_target:.0f} → {target:.0f}°C "
                f"(rule: {rule.name})."
            )
        except Exception as exc:  # noqa: BLE001
            self.log.error(f"set_target_temperature({target}) failed: {exc!r}")
