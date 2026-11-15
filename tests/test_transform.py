"""Transform formulas and the thermistor helper."""

import pytest

from heatpump.sensors.config import ChannelConfig, parse
from heatpump.sensors.transform import Transforms, thermistor

# Typical 10 kOhm thermistor (25 C = 77 F at 10 kOhm), in kelvin and in Rankine form
KELVIN = {"a": 1.129241e-3, "b": 2.341077e-4, "c": 8.775468e-8}
RANKINE = {k: v / 1.8 for k, v in KELVIN.items()}


def ch(name, transform=None, **kw):
    return ChannelConfig(name, transform=transform, **kw)


def test_thermistor_rankine_coefficients_give_degrees_f():
    # Half the supply voltage means the thermistor equals the 10 kOhm fixed resistor
    assert thermistor(1.024, 2.048, r_fixed=10000, scale="rankine", **RANKINE) == pytest.approx(77.0, abs=0.05)
    assert thermistor(1.024, 2.048, r_fixed=10000, scale="kelvin", **KELVIN) == pytest.approx(77.0, abs=0.05)


def test_thermistor_divider_value_and_leg():
    # Thermistor (10 k) on the ground leg under a 20 k resistor: v = Vs / 3
    assert thermistor(2.048 / 3, 2.048, r_fixed=20000, scale="rankine", **RANKINE) == pytest.approx(77.0, abs=0.05)
    # Same parts, thermistor on the supply leg: v = Vs * 2 / 3
    assert thermistor(2.048 * 2 / 3, 2.048, r_fixed=20000, scale="rankine",
                      thermistor_leg="supply", **RANKINE) == pytest.approx(77.0, abs=0.05)
    # Colder means more resistance, so a higher voltage on the ground leg
    assert thermistor(1.5, 2.048, r_fixed=10000, scale="rankine", **RANKINE) < 77.0


@pytest.mark.parametrize("v", [0.0, 0.001, 2.047, 2.048, 2.2])
def test_thermistor_shorted_or_open_is_an_error(v):
    with pytest.raises(ValueError):
        thermistor(v, 2.048, r_fixed=10000, scale="rankine", **RANKINE)


def test_transforms_apply_in_dependency_order():
    formula = "thermistor(x, vref, r_fixed=10000, a={a!r}, b={b!r}, c={c!r}, scale='rankine')".format(
        **RANKINE
    )
    t = Transforms([
        ch("delta", "temp - 32"),  # no x: computed from another channel
        ch("temp", formula),
        ch("vref", store=False),
        ch("valve", "1 - x", kind="binary"),
        ch("plain"),
    ])
    out = t.apply({"temp": 1.024, "vref": 2.048, "valve": 1.0, "plain": 5.0})
    assert out["temp"] == pytest.approx(77.0, abs=0.05)
    assert out["delta"] == pytest.approx(45.0, abs=0.05)
    assert (out["valve"], out["plain"], out["vref"]) == (0.0, 5.0, 2.048)


def test_bad_data_gives_none_not_an_exception():
    t = Transforms([ch("a", "100 / x"), ch("b", "sqrt(x)"), ch("c", "a + 1"), ch("d", "x * 2")])
    out = t.apply({"a": 0.0, "b": -1.0, "d": None})
    assert out == {"a": None, "b": None, "c": None, "d": None}
    assert "d" not in t.apply({"a": 1.0, "b": 1.0})  # not read this time: left out


@pytest.mark.parametrize("formula, message", [
    ("x +", "not a valid formula"),
    ("x * gain", "not a channel"),
    ("open('/etc/passwd')", "unknown function"),
    ("x.real", "not allowed"),
    ("me + 1", "refers to itself"),
    ("thermistor(x, 2.0, r_fixed=10000)", "transform failed"),  # coefficients missing
    ("thermistor(x, 2.0, r_fixed=1e4, a=1, b=1, c=1, scale='celsius')", "transform failed"),
])
def test_formula_mistakes_are_caught_at_startup(formula, message):
    with pytest.raises(ValueError, match=message):
        Transforms([ch("me", formula)])


def test_circular_references_are_rejected():
    with pytest.raises(ValueError, match="circle"):
        Transforms([ch("a", "b + 1"), ch("b", "a + 1")])


def test_transform_and_store_come_from_the_config():
    cfg = parse({"sensors": {"devices": {"d": {"driver": "fake", "period": 1, "channels": {
        "vref": {"store": False}, "t": {"transform": "x * 2"}}}}}})
    vref, t = cfg.devices[0].channels
    assert (vref.store, vref.transform, t.store, t.transform) == (False, None, True, "x * 2")
    assert t.options == {} and vref.options == {}
