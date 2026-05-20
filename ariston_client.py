"""Thread-safe wrapper around the async `ariston` library.

Owns a dedicated asyncio loop in a background thread so synchronous
callers (Streamlit reruns, the rule-controller thread) can invoke the
cloud API without each one creating/tearing down its own event loop.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from dataclasses import dataclass, asdict
from typing import Any, Optional

from ariston import Ariston
from ariston.base_device import AristonBaseDevice

_LOG = logging.getLogger(__name__)

# Ariston Net blocks the library's default User-Agent ("RestSharp/...") as a
# third-party client (see fustom/ariston-remotethermo-home-assistant-v3#362).
# Spoofing a real browser User-Agent has been confirmed by multiple users to
# stop the 429 lockouts for months at a time.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Mobile Safari/537.36"
)


def parse_retry_seconds(exc: BaseException) -> Optional[int]:
    """Pull the wait time out of an Ariston 429 ConnectionException.

    The body of the 429 is plain text like
        ``Requests are blocked for 66 seconds``
    Returns the integer seconds, or None if the exception isn't a 429 we recognise.
    """
    for arg in getattr(exc, "args", ()) or ():
        if isinstance(arg, (bytes, bytearray)):
            try:
                arg = arg.decode(errors="replace")
            except Exception:  # noqa: BLE001
                continue
        if isinstance(arg, str):
            m = re.search(r"blocked for (\d+)\s*seconds?", arg, re.IGNORECASE)
            if m:
                return int(m.group(1))
    return None


@dataclass
class DeviceSnapshot:
    name: Optional[str]
    gateway: Optional[str]
    serial_number: Optional[str]
    current_temperature: Optional[float]
    target_temperature: Optional[float]
    is_heating: Optional[bool]
    is_antileg: Optional[bool]
    is_on: Optional[bool]
    mode_text: Optional[str]
    mode_value: Optional[int]
    min_temp: Optional[float]
    max_temp: Optional[float]
    av_shw: Optional[int]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class AristonClient:
    """Synchronous facade over the async Ariston library."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._cloud: Optional[Ariston] = None
        self._device: Optional[AristonBaseDevice] = None
        self._lock = threading.Lock()
        self._start_loop()

    def _start_loop(self) -> None:
        ready = threading.Event()

        def runner() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=runner, daemon=True, name="ariston-loop")
        self._thread.start()
        ready.wait()

    def _run(self, coro):
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def connect(self, username: str, password: str,
                gateway: Optional[str] = None,
                user_agent: str = BROWSER_USER_AGENT) -> bool:
        """Connect, discover, and bind to a device.

        If `gateway` is provided, use it. Otherwise bind to the first discovered device.
        `user_agent` defaults to a browser string to avoid Ariston's third-party
        client blocks; pass a different value to override.
        Returns True if a device is bound.
        """
        async def _connect():
            cloud = Ariston()
            ok = await cloud.async_connect(username, password, user_agent=user_agent)
            if not ok:
                raise RuntimeError(
                    "Ariston login failed — wrong username or password."
                )
            devices = await cloud.async_discover()
            target_gw = gateway
            if target_gw:
                device = await cloud.async_hello(target_gw)
                if device is None and devices:
                    # Try a case-insensitive / serial-number fallback.
                    for d in devices:
                        if d.get("gw", "").lower() == target_gw.lower():
                            device = await cloud.async_hello(d["gw"])
                            break
                        if d.get("sn") == target_gw:
                            device = await cloud.async_hello(d["gw"])
                            break
            elif devices:
                device = await cloud.async_hello(devices[0]["gw"])
            else:
                device = None
            return cloud, device, devices

        with self._lock:
            cloud, device, devices = self._run(_connect())
            self._cloud = cloud
            self._device = device
            self._discovered = devices
            return device is not None

    @property
    def connected(self) -> bool:
        return self._device is not None

    @property
    def discovered(self) -> list[dict[str, Any]]:
        return getattr(self, "_discovered", [])

    def refresh(self) -> DeviceSnapshot:
        """Force-poll the cloud and return a snapshot of relevant values."""
        if not self.connected:
            raise RuntimeError("Not connected to Ariston cloud.")

        async def _refresh():
            assert self._device is not None
            await self._device.async_update_state()
            # Settings (max-setpoint, anti-legionella, …) are nice-to-have.
            try:
                await self._device.async_get_features()
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("async_get_features failed: %s", exc)
            return self._build_snapshot()

        with self._lock:
            return self._run(_refresh())

    def _build_snapshot(self) -> DeviceSnapshot:
        d = self._device
        assert d is not None
        return DeviceSnapshot(
            name=getattr(d, "name", None),
            gateway=getattr(d, "gateway", None),
            serial_number=getattr(d, "serial_number", None),
            current_temperature=getattr(d, "water_heater_current_temperature", None),
            target_temperature=getattr(d, "water_heater_target_temperature", None),
            is_heating=getattr(d, "is_heating", None),
            is_antileg=getattr(d, "is_antileg", None),
            is_on=getattr(d, "water_heater_power_value", None),
            mode_text=getattr(d, "water_heater_current_mode_text", None),
            mode_value=getattr(d, "water_heater_mode_value", None),
            min_temp=getattr(d, "water_heater_minimum_temperature", None),
            max_temp=getattr(d, "water_heater_maximum_temperature", None),
            av_shw=getattr(d, "av_shw_value", None),
        )

    def set_target_temperature(self, temperature: float) -> None:
        if not self.connected:
            raise RuntimeError("Not connected to Ariston cloud.")

        async def _set():
            assert self._device is not None
            await self._device.async_set_water_heater_temperature(float(temperature))

        with self._lock:
            self._run(_set())

    def set_operation_mode(self, mode_name: str) -> None:
        if not self.connected:
            raise RuntimeError("Not connected to Ariston cloud.")

        async def _set():
            assert self._device is not None
            await self._device.async_set_water_heater_operation_mode(mode_name)

        with self._lock:
            self._run(_set())

    def set_power(self, on: bool) -> None:
        if not self.connected:
            raise RuntimeError("Not connected to Ariston cloud.")

        async def _set():
            assert self._device is not None
            await self._device.async_set_power(bool(on))

        with self._lock:
            self._run(_set())

    def available_modes(self) -> list[str]:
        if not self.connected:
            return []
        return list(getattr(self._device, "water_heater_mode_operation_texts", []) or [])
