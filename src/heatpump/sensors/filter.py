"""Decides which samples are worth storing.

A sample is stored when it differs from the last stored value by more than the
deadband, or when `max_interval` seconds have passed since the last stored one.
With no deadband every sample is stored.

When a change is stored after some skipped samples, the last skipped sample is
stored too. Without it, a graph would draw a slow ramp from the old value to the
new one, when the value really sat still and then jumped.
"""

from datetime import datetime
from typing import NamedTuple

QUALITY_OK = 0
QUALITY_SUSPECT = 1
QUALITY_FAILED = 2  # the read failed; value is NULL


class Reading(NamedTuple):
    """One row for the `readings` table (the sensor is identified by name here)."""

    ts: datetime
    name: str
    value: float | None
    quality: int = QUALITY_OK


class ChangeFilter:
    """Filter for one channel."""

    def __init__(self, name: str, deadband: float | None, max_interval: float):
        self.name = name
        self.deadband = deadband
        self.max_interval = max_interval
        self._stored: Reading | None = None  # the last reading that was stored
        self._skipped: Reading | None = None  # the newest reading that was not

    def changed(self, value: float | None, quality: int = QUALITY_OK) -> bool:
        """Would this sample count as a change from the last stored one?"""
        last = self._stored
        if last is None or self.deadband is None:
            return True
        if quality != last.quality or (value is None) != (last.value is None):
            return True
        return value is not None and abs(value - last.value) > self.deadband

    def feed(
        self, ts: datetime, value: float | None, quality: int = QUALITY_OK, force: bool = False
    ) -> list[Reading]:
        """Take one sample; return the readings to store (none, one or two).

        `force` treats the sample as a change, used to keep paired channels together.
        """
        sample = Reading(ts, self.name, value, quality)
        out = []
        if force or self.changed(value, quality):
            if self._skipped is not None:
                out.append(self._skipped)
            out.append(sample)
        elif (ts - self._stored.ts).total_seconds() >= self.max_interval:
            out.append(sample)
        else:
            self._skipped = sample
            return []
        self._stored = sample
        self._skipped = None
        return out
