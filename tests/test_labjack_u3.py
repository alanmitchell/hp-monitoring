"""LabJack U3 driver, tested against a simulated U3."""

import threading
from typing import ClassVar

import pytest

pytest.importorskip("u3")

from heatpump.common.timeutil import utc_now
from heatpump.sensors.config import ChannelConfig, DeviceConfig
from heatpump.sensors.device import DeviceThread
from heatpump.sensors.devices import labjack_u3, make_device


class FakeU3:
    volts: ClassVar[dict] = {0: 1.024, 3: 2.048, 9: 0.5}
    digital: ClassVar[dict] = {4: 1, 10: 0, 17: 1}
    instances: ClassVar[list] = []

    def __init__(self, **kwargs):
        self.kwargs, self.calls, self.closed = kwargs, [], False
        FakeU3.instances.append(self)

    def configIO(self, FIOAnalog=None, EIOAnalog=None):
        self.config = (FIOAnalog, EIOAnalog)

    def getAIN(self, n, longSettle=False):
        self.calls.append(("ain", n, longSettle))
        return self.volts[n]

    def getDIState(self, n):
        self.calls.append(("di", n))
        return self.digital[n]

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def fake_u3(monkeypatch):
    monkeypatch.setattr(labjack_u3.u3, "U3", FakeU3)
    monkeypatch.setattr(FakeU3, "instances", [])


def device(channels, **options):
    return make_device(DeviceConfig("lj", "labjack_u3", period=1, channels=channels, options=options))


def chan(name, pin=None, transform=None, **options):
    if pin:
        options["pin"] = pin
    return ChannelConfig(name, transform=transform, options=options)


def test_pin_names():
    assert labjack_u3.parse_pin("AIN0") == ("analog", 0)
    assert labjack_u3.parse_pin("ain15") == ("analog", 15)
    assert labjack_u3.parse_pin("FIO4") == ("digital", 4)
    assert labjack_u3.parse_pin("EIO2") == ("digital", 10)
    assert labjack_u3.parse_pin("CIO1") == ("digital", 17)
    for bad in ("AIN16", "FIO8", "CIO4", "DAC0", "4"):
        with pytest.raises(ValueError):
            labjack_u3.parse_pin(bad)


def test_reads_analog_and_digital_pins():
    dev = device([chan("a", "AIN0"), chan("b", "AIN9"), chan("sw", "FIO4"),
                  chan("e", "EIO2"), chan("c", "CIO1")])
    dev.open()
    port = FakeU3.instances[0]
    assert port.config == (0b00000001, 0b00000010)  # AIN0 and AIN9 (EIO1) set to analog
    assert dev.read() == {"a": 1.024, "b": 0.5, "sw": 1.0, "e": 0.0, "c": 1.0}
    dev.close()
    assert port.closed


def test_samples_long_settle_and_shared_pin():
    settings = {"samples": 4, "long_settle": True}
    dev = device([chan("a", "AIN0", **settings), chan("a_raw", "AIN0", **settings)])
    dev.open()
    assert dev.read() == {"a": pytest.approx(1.024), "a_raw": pytest.approx(1.024)}
    assert FakeU3.instances[0].calls == [("ain", 0, True)] * 4  # the shared pin is read once


def test_serial_number_selects_the_unit():
    dev = device([chan("a", "AIN0")], serial_number=320012345)
    dev.open()
    assert FakeU3.instances[0].kwargs == {"firstFound": False, "serial": 320012345}


@pytest.mark.parametrize("channels, message", [
    ([chan("a", "AIN4"), chan("b", "FIO4")], "already used as analog"),
    ([chan("a", "XYZ1")], "not a pin name"),
    ([chan("a")], "needs a 'pin' or a 'transform'"),
    ([chan("a", "AIN0", samples=0)], "samples"),
])
def test_config_mistakes_are_rejected(channels, message):
    with pytest.raises(ValueError, match=message):
        device(channels)


def test_thermistor_channel_end_to_end():
    """Supply and thermistor voltages from the U3, through the transform, to degrees F."""
    formula = ("thermistor(x, therm_supply, r_fixed=10000, a=6.273561e-4, b=1.300598e-4, "
               "c=4.875260e-8, scale='rankine')")
    dev = device([
        chan("therm_supply", "AIN3"),
        chan("suction_temp", "AIN0", transform=formula),
        chan("defrost_valve", "FIO4", transform="1 - x"),
        chan("superheat", transform="suction_temp - 40"),
    ])
    dev.config.channels[0].store = False
    out, stop = [], threading.Event()
    thread = DeviceThread(dev, out.extend, stop)
    dev.open()
    thread._handle(utc_now(), dev.read())
    values = {r.name: r.value for r in out}
    assert set(values) == {"suction_temp", "defrost_valve", "superheat"}  # therm_supply not stored
    assert values["suction_temp"] == pytest.approx(77.0, abs=0.05)
    assert values["superheat"] == pytest.approx(37.0, abs=0.05)
    assert values["defrost_valve"] == 0.0
