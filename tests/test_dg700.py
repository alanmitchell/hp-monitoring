"""DG-700 driver, tested against a simulated gauge."""

from typing import ClassVar

import pytest

pytest.importorskip("serial")

from heatpump.sensors.config import ChannelConfig, DeviceConfig
from heatpump.sensors.devices import dg700, make_device


class FakeGauge:
    """Stands in for serial.Serial: echoes each command byte + 1, then any data."""

    pressure_bytes = bytes([135, 0x48, 0x00])  # 100.0 Pa
    echo_offset = 1
    instances: ClassVar[list] = []

    def __init__(self, port, **kwargs):
        self.port, self.sent, self.pending, self.closed = port, [], b"", False
        FakeGauge.instances.append(self)

    def reset_input_buffer(self):
        self.pending = b""

    def write(self, data):
        self.sent.append(data)
        self.pending += bytes([(data[0] + self.echo_offset) & 0xFF])
        if data == dg700.CMD_READ_PRESSURE:
            self.pending += self.pressure_bytes

    def read(self, n):
        out, self.pending = self.pending[:n], self.pending[n:]
        return out

    def close(self):
        self.closed = True


@pytest.fixture
def gauge(monkeypatch):
    monkeypatch.setattr(dg700, "Serial", FakeGauge)
    monkeypatch.setattr(dg700, "DISABLE_AUTO_ZERO_WAIT", 0)
    monkeypatch.setattr(dg700, "FORCE_AUTO_ZERO_WAIT", 0)
    monkeypatch.setattr(FakeGauge, "instances", [])
    cfg = DeviceConfig("dg", "dg700", period=10, channels=[ChannelConfig("coil_pressure")],
                       options={"port": "/dev/fake", "auto_zero_seconds": 1000})
    return make_device(cfg)


@pytest.mark.parametrize("data, pascals", [
    ([0, 0, 0], 0.0),
    ([129, 0x00, 0x00], 1.0),
    ([129, 0x80, 0x00], -1.0),
    ([135, 0x48, 0x00], 100.0),
    ([127, 0x40, 0x00], 0.375),
    ([133, 0xC8, 0x00], -25.0),
])
def test_bytes_to_pascals(data, pascals):
    assert dg700.bytes_to_pascals(bytes(data)) == pascals


def test_open_disables_auto_zero_then_zeroes_and_reads(gauge):
    gauge.open()
    assert gauge.read() == {"coil_pressure": 100.0}
    port = FakeGauge.instances[0]
    assert port.port == "/dev/fake"
    assert port.sent == [b"\x09", b"\x08", b"\x01"]
    gauge.close()
    assert port.closed


def test_gauge_is_rezeroed_when_due(gauge):
    gauge.open()
    gauge.read()
    gauge.last_zero -= 1001  # pretend the last zero was long ago
    gauge.read()
    gauge.read()
    assert FakeGauge.instances[0].sent[2:] == [b"\x01", b"\x08", b"\x01", b"\x01"]


def test_short_reply_is_an_error(gauge, monkeypatch):
    gauge.open()
    monkeypatch.setattr(FakeGauge, "pressure_bytes", b"\x87\x48")  # one byte missing
    with pytest.raises(OSError, match="expected 4"):
        gauge.read()


def test_wrong_echo_is_an_error(gauge, monkeypatch):
    monkeypatch.setattr(FakeGauge, "echo_offset", 0)
    with pytest.raises(OSError, match="echoed"):
        gauge.open()


def test_config_mistakes_are_rejected():
    one = [ChannelConfig("p")]
    with pytest.raises(ValueError, match="port"):
        make_device(DeviceConfig("dg", "dg700", period=10, channels=one))
    with pytest.raises(ValueError, match="one channel"):
        make_device(DeviceConfig("dg", "dg700", period=10, options={"port": "x"},
                                 channels=[ChannelConfig("a"), ChannelConfig("b")]))
