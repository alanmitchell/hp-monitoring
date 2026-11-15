"""Energy Conservatory DG-700 pressure gauge, over its serial port.

One channel: the pressure in Pascals returned by the gauge's "read one pressure"
command. Device settings:

    port               serial port, e.g. /dev/serial/by-id/usb-Prolific_...-port0
    auto_zero_seconds  how often to re-zero the gauge (default 21600, i.e. 6 hours)

The gauge's own automatic zeroing is switched off when the port is opened, and
the driver zeroes it when the port is opened and then on its own schedule. A zero takes about 2.5 s, so
the reading that follows one is that much late.

Protocol: commands are single bytes. The gauge echoes each command byte plus
one, followed by any data. Pressures are 3 bytes in Microsoft Binary Format.
"""

import math
import time

from serial import Serial

from heatpump.sensors.config import DeviceConfig
from heatpump.sensors.device import Device

CMD_READ_PRESSURE = b"\x01"
CMD_FORCE_AUTO_ZERO = b"\x08"
CMD_DISABLE_AUTO_ZERO = b"\x09"

READ_TIMEOUT = 0.2  # seconds to wait for the gauge's reply
DISABLE_AUTO_ZERO_WAIT = 1.5  # seconds the gauge needs after each command
FORCE_AUTO_ZERO_WAIT = 2.5


def bytes_to_pascals(data: bytes) -> float:
    """Convert the gauge's 3 bytes (Microsoft Binary Format) to Pascals."""
    exponent, hi, lo = data
    if exponent == 0:
        return 0.0
    mantissa = ((hi & 0x7F) << 8) | lo
    value = math.ldexp(1 + mantissa / 2**15, exponent - 129)
    return -value if hi & 0x80 else value


class DG700(Device):
    def __init__(self, config: DeviceConfig):
        super().__init__(config)
        if len(config.channels) != 1:
            raise ValueError(f"Device '{config.name}': the dg700 driver needs exactly one channel")
        if "port" not in config.options:
            raise ValueError(f"Device '{config.name}': the dg700 driver needs a 'port' setting")
        self.port = config.options["port"]
        self.auto_zero_seconds = float(config.options.get("auto_zero_seconds", 6 * 3600))
        self.serial: Serial | None = None
        self.last_zero = 0.0

    def open(self) -> None:
        self.serial = Serial(self.port, timeout=READ_TIMEOUT, rtscts=False, dsrdtr=False)
        self._command(CMD_DISABLE_AUTO_ZERO)
        time.sleep(DISABLE_AUTO_ZERO_WAIT)
        self._auto_zero()

    def close(self) -> None:
        if self.serial is not None:
            self.serial.close()
            self.serial = None

    def read(self) -> dict[str, float | None]:
        if time.monotonic() - self.last_zero >= self.auto_zero_seconds:
            self._auto_zero()
        data = self._command(CMD_READ_PRESSURE, reply_length=3)
        return {self.config.channels[0].name: bytes_to_pascals(data)}

    def _auto_zero(self) -> None:
        self._command(CMD_FORCE_AUTO_ZERO)
        time.sleep(FORCE_AUTO_ZERO_WAIT)
        self.last_zero = time.monotonic()

    def _command(self, cmd: bytes, reply_length: int = 0) -> bytes:
        """Send a command; return its reply data. Raises OSError on a bad reply."""
        self.serial.reset_input_buffer()
        for b in cmd:  # the gauge wants the bytes one at a time
            self.serial.write(bytes([b]))
        reply = self.serial.read(len(cmd) + reply_length)
        if len(reply) < len(cmd) + reply_length:
            raise OSError(
                f"DG-700 sent {len(reply)} bytes, expected {len(cmd) + reply_length} "
                f"(command 0x{cmd.hex()})"
            )
        echo = bytes((b + 1) & 0xFF for b in cmd)
        if reply[: len(cmd)] != echo:
            raise OSError(f"DG-700 echoed 0x{reply[: len(cmd)].hex()}, expected 0x{echo.hex()}")
        return reply[len(cmd) :]
