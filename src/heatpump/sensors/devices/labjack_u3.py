"""LabJack U3 data acquisition device (written for the U3-LV).

Returns raw values: volts for analog pins, 0 or 1 for digital pins. Use each
channel's `transform` setting to turn those into engineering values.

Channel settings:

    pin          which terminal to read:
                   AIN0 - AIN15   analog input, 0 to 2.44 V
                                  (AIN0-7 are the FIO0-7 terminals, AIN8-15 are EIO0-7)
                   FIO0 - FIO7, EIO0 - EIO7, CIO0 - CIO3   digital input
                 Leave `pin` out for a channel computed only from other channels.
    samples      analog only: average this many readings (default 1)
    long_settle  analog only: true gives the input longer to settle, for
                 sources with a high resistance (default false)

Device settings:

    serial_number  which U3 to open, if more than one is attached (default: the first found)

Several channels may share a pin, for example to store both a raw voltage and
a transformed value. A terminal cannot be both analog and digital.

Needs LabJack's Exodriver system library; see the README.
"""

import re

import u3

from heatpump.sensors.config import DeviceConfig
from heatpump.sensors.device import Device

DIGITAL_BASE = {"FIO": 0, "EIO": 8, "CIO": 16}  # the U3's numbering of its digital I/O
DIGITAL_COUNT = {"FIO": 8, "EIO": 8, "CIO": 4}


def parse_pin(pin: str) -> tuple[str, int]:
    """'AIN3' -> ('analog', 3); 'EIO2' -> ('digital', 10). The number is the U3's I/O number."""
    m = re.fullmatch(r"(AIN|FIO|EIO|CIO)(\d+)", str(pin).upper())
    if not m:
        raise ValueError(f"'{pin}' is not a pin name such as AIN0, FIO4, EIO2 or CIO1")
    prefix, n = m.group(1), int(m.group(2))
    if prefix == "AIN":
        if n > 15:
            raise ValueError(f"'{pin}': analog inputs are AIN0 to AIN15")
        return "analog", n
    if n >= DIGITAL_COUNT[prefix]:
        raise ValueError(f"'{pin}': there are only {DIGITAL_COUNT[prefix]} {prefix} terminals")
    return "digital", DIGITAL_BASE[prefix] + n


class LabJackU3(Device):
    def __init__(self, config: DeviceConfig):
        super().__init__(config)
        self.serial_number = config.options.get("serial_number")
        self.device: u3.U3 | None = None
        self.pins: dict[str, tuple] = {}  # channel name -> (type, I/O number, samples, long_settle)
        use: dict[int, str] = {}  # I/O number -> 'analog' | 'digital'
        for ch in config.channels:
            where = f"Device '{config.name}', channel '{ch.name}'"
            if "pin" not in ch.options:
                if ch.transform is None:
                    raise ValueError(f"{where}: needs a 'pin' or a 'transform'")
                continue
            try:
                pin_type, number = parse_pin(ch.options["pin"])
            except ValueError as e:
                raise ValueError(f"{where}: {e}") from None
            if use.setdefault(number, pin_type) != pin_type:
                raise ValueError(f"{where}: that terminal is already used as {use[number]}")
            samples = int(ch.options.get("samples", 1))
            if samples < 1:
                raise ValueError(f"{where}: samples must be 1 or more")
            self.pins[ch.name] = (pin_type, number, samples, bool(ch.options.get("long_settle", False)))
        # Bit n set = terminal n is an analog input. Low byte is FIO, high byte is EIO.
        self.analog_mask = sum(1 << n for n, pin_type in use.items() if pin_type == "analog")

    def open(self) -> None:
        if self.serial_number is None:
            self.device = u3.U3()
        else:
            self.device = u3.U3(firstFound=False, serial=int(self.serial_number))
        self.device.configIO(FIOAnalog=self.analog_mask & 0xFF, EIOAnalog=self.analog_mask >> 8)

    def close(self) -> None:
        if self.device is not None:
            self.device.close()
            self.device = None

    def read(self) -> dict[str, float | None]:
        values = {}
        seen: dict[tuple, float] = {}  # channels sharing a pin get the identical reading
        for name, pin in self.pins.items():
            if pin not in seen:
                pin_type, number, samples, long_settle = pin
                if pin_type == "analog":
                    total = sum(
                        self.device.getAIN(number, longSettle=long_settle) for _ in range(samples)
                    )
                    seen[pin] = total / samples
                else:
                    seen[pin] = float(self.device.getDIState(number))
            values[name] = seen[pin]
        return values
