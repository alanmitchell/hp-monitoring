"""A pretend device for tests and for development without hardware.

Analog channels follow a sine wave; binary channels switch between 0 and 1.
Per-channel settings: `mean` (default 20), `amplitude` (default 5) and
`cycle_seconds` (default 600, the length of one full cycle).
"""

import math
import time

from heatpump.sensors.device import Device


class FakeDevice(Device):
    def read(self) -> dict[str, float | None]:
        now = time.time()
        values = {}
        for ch in self.config.channels:
            phase = (now / ch.options.get("cycle_seconds", 600)) % 1.0
            if ch.kind == "binary":
                values[ch.name] = 1.0 if phase < 0.5 else 0.0
            else:
                mean = ch.options.get("mean", 20.0)
                amplitude = ch.options.get("amplitude", 5.0)
                values[ch.name] = mean + amplitude * math.sin(2 * math.pi * phase)
        return values
