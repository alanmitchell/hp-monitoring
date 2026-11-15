"""The driver interface, and the thread that runs one device.

A driver is a subclass of `Device` with a `read()` method. There are two styles:

* Polled (the default): the thread calls `read()` every `period` seconds, on
  wall-clock boundaries, so devices with the same period are read at the same
  moments.
* Streaming (`streaming = True`): the device sends data on its own schedule.
  `read()` blocks until the next set of values arrives and the thread calls it
  again straight away.

Either way `read()` must never block forever: use timeouts and raise an exception
when one expires. Any exception makes the thread store a failed reading, close
the device, wait, and open it again.
"""

import math
import threading
import time
from collections.abc import Callable

from heatpump.common.timeutil import utc_now
from heatpump.sensors.config import DeviceConfig
from heatpump.sensors.filter import QUALITY_FAILED, QUALITY_OK, ChangeFilter, Reading

MAX_RETRY_WAIT = 30.0  # seconds; the wait doubles after each failure up to this


class Device:
    """Base class for drivers. `self.config` holds the device's settings and channels."""

    streaming = False

    def __init__(self, config: DeviceConfig):
        self.config = config

    def open(self) -> None:
        """Connect to the hardware."""

    def close(self) -> None:
        """Disconnect. Must be safe to call when not open."""

    def read(self) -> dict[str, float | None]:
        """Return raw values keyed by channel name. None means that channel failed."""
        raise NotImplementedError


class DeviceThread(threading.Thread):
    """Reads one device in a loop and passes the readings worth storing to `emit`."""

    def __init__(
        self, device: Device, emit: Callable[[list[Reading]], None], stop: threading.Event
    ):
        super().__init__(name=device.config.name, daemon=True)
        self.device = device
        self.emit = emit
        self.stop_event = stop
        self.last_alive = time.monotonic()  # watched by the watchdog in main
        self.period = device.config.period or 0.0
        self.filters = {
            ch.name: ChangeFilter(ch.name, ch.deadband, ch.max_interval)
            for ch in device.config.channels
        }
        if not device.streaming and not device.config.period:
            raise ValueError(f"Device '{device.config.name}' needs a 'period' setting")

    def run(self) -> None:
        name = self.device.config.name
        is_open = False
        failures = 0
        while not self.stop_event.is_set():
            self.last_alive = time.monotonic()
            try:
                if not is_open:
                    self.device.open()
                    is_open = True
                if not self.device.streaming and self._wait_for_tick():
                    break
                poll_time = utc_now()
                values = self.device.read()
                # A stream's values arrived just now; a poll's were requested at poll_time
                self._handle(utc_now() if self.device.streaming else poll_time, values)
                if failures:
                    print(f"{name}: recovered")
                failures = 0
            except Exception as e:  # noqa: BLE001 - any driver error means reconnect
                failures += 1
                print(f"{name}: {type(e).__name__}: {e}")
                self._handle(utc_now(), {ch: None for ch in self.filters})
                self._close()
                is_open = False
                self.stop_event.wait(min(MAX_RETRY_WAIT, 2.0 ** (failures - 1)))
        self._close()

    def _wait_for_tick(self) -> bool:
        """Sleep until the next multiple of `period`. Returns True if told to stop."""
        period = self.device.config.period
        now = time.time()
        next_tick = (math.floor(now / period) + 1) * period
        return self.stop_event.wait(next_tick - now)

    def _handle(self, ts, values: dict[str, float | None]) -> None:
        samples = []
        for ch, value in values.items():
            if ch not in self.filters:
                continue  # the driver returned something the config does not list
            failed = value is None or math.isnan(value)
            samples.append(
                (ch, None, QUALITY_FAILED) if failed else (ch, float(value), QUALITY_OK)
            )
        force = self.device.config.report_together and any(
            self.filters[ch].changed(value, quality) for ch, value, quality in samples
        )
        readings = []
        for ch, value, quality in samples:
            readings.extend(self.filters[ch].feed(ts, value, quality, force=force))
        if readings:
            self.emit(readings)

    def _close(self) -> None:
        try:
            self.device.close()
        except Exception as e:  # noqa: BLE001 - any driver error means reconnect
            print(f"{self.device.config.name}: error while closing: {e}")
