"""Turns raw readings into engineering values, using expressions from the config.

A channel's `transform` setting is a formula such as ``"1 - x"`` or
``"thermistor(x, therm_supply, r_fixed=10000, a=..., b=..., c=..., scale='rankine')"``.

* ``x`` is the channel's own raw reading.
* Any other name is the (already transformed) value of another channel on the
  same device, or one of the FUNCTIONS below.
* A channel whose formula does not use ``x`` is computed purely from other channels.

Formulas are checked when the program starts. While running, a formula that
cannot produce a number (an input failed, division by zero, a thermistor reading
out of range) gives None, which is stored as a failed reading.
"""

import ast
import math
from graphlib import CycleError, TopologicalSorter

from heatpump.sensors.config import ChannelConfig

RANKINE_OFFSET = 459.67


def thermistor(
    v: float,
    v_supply: float,
    *,
    r_fixed: float,
    a: float,
    b: float,
    c: float,
    scale: str,
    thermistor_leg: str = "ground",
) -> float:
    """Temperature in degrees F of a thermistor in a voltage divider.

    v               voltage at the junction of the thermistor and the fixed resistor
    v_supply        voltage across the whole divider
    r_fixed         the fixed resistor, in ohms
    a, b, c         Steinhart-Hart coefficients: 1/T = a + b*ln(R) + c*ln(R)^3
    scale           'rankine' or 'kelvin': the absolute scale T comes out in
    thermistor_leg  'ground' (default) if the thermistor is the leg connected to
                    ground, 'supply' if it is the leg connected to the supply
    """
    if scale not in ("rankine", "kelvin"):
        raise TypeError("thermistor: scale must be 'rankine' or 'kelvin'")
    if thermistor_leg not in ("ground", "supply"):
        raise TypeError("thermistor: thermistor_leg must be 'ground' or 'supply'")
    # Within 0.5% of either rail means a shorted or disconnected thermistor
    if not 0.005 * v_supply < v < 0.995 * v_supply:
        raise ValueError("thermistor voltage is outside the divider's usable range")
    if thermistor_leg == "ground":
        r = r_fixed * v / (v_supply - v)
    else:
        r = r_fixed * (v_supply - v) / v
    ln_r = math.log(r)
    absolute = 1.0 / (a + b * ln_r + c * ln_r**3)
    if scale == "kelvin":
        absolute *= 1.8
    return absolute - RANKINE_OFFSET


FUNCTIONS = {
    "thermistor": thermistor,
    "abs": abs, "min": min, "max": max, "round": round,
    "sqrt": math.sqrt, "exp": math.exp, "log": math.log, "log10": math.log10,
    "sin": math.sin, "cos": math.cos, "tan": math.tan, "atan": math.atan,
}  # fmt: skip

# Only plain arithmetic, comparisons and calls to FUNCTIONS are allowed in a formula
ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp,
    ast.Call, ast.keyword, ast.Name, ast.Load, ast.Constant,
    ast.operator, ast.unaryop, ast.boolop, ast.cmpop,
)  # fmt: skip


class Transforms:
    """The formulas for one device's channels."""

    def __init__(self, channels: list[ChannelConfig]):
        names = {ch.name for ch in channels}
        self._code = {}
        self._inputs: dict[str, set[str]] = {}  # channel -> other channels it uses
        self._uses_x: dict[str, bool] = {}
        for ch in channels:
            if ch.transform is None:
                self._inputs[ch.name], self._uses_x[ch.name] = set(), True
                continue
            used = self._check(ch.name, ch.transform, names)
            self._code[ch.name] = compile(ch.transform, f"<transform of {ch.name}>", "eval")
            self._inputs[ch.name] = used & names
            self._uses_x[ch.name] = "x" in used
        try:
            self._order = list(TopologicalSorter(self._inputs).static_order())
        except CycleError as e:
            raise ValueError(f"Transforms refer to each other in a circle: {e.args[1]}") from None
        # Trial run, to catch mistakes such as a misspelled or missing argument
        trial = self.apply(dict.fromkeys(names, 1.0), strict=True)
        assert set(trial) == names

    @staticmethod
    def _check(channel: str, formula: str, channel_names: set[str]) -> set[str]:
        """Validate a formula; return the names it uses."""
        where = f"Channel '{channel}': transform"
        try:
            tree = ast.parse(formula, mode="eval")
        except SyntaxError as e:
            raise ValueError(f"{where} is not a valid formula: {e.msg}") from None
        used = set()
        for node in ast.walk(tree):
            if not isinstance(node, ALLOWED_NODES):
                raise ValueError(  # noqa: TRY004 - a config mistake, reported as such
                    f"{where} uses something not allowed ({type(node).__name__})"
                )
            if isinstance(node, ast.Call):
                if not (isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS):
                    raise ValueError(f"{where} calls an unknown function")
            elif isinstance(node, ast.Name):
                if node.id not in FUNCTIONS and node.id != "x" and node.id not in channel_names:
                    raise ValueError(
                        f"{where} uses '{node.id}', which is not a channel on this device "
                        "or a known function"
                    )
                if node.id == channel:
                    raise ValueError(f"{where} refers to itself; use x for its own reading")
                used.add(node.id)
        return used

    def apply(self, raw: dict[str, float | None], strict: bool = False) -> dict[str, float | None]:
        """Raw readings keyed by channel name -> engineering values.

        Channels that need a raw reading (`x`) and did not get one this time are
        left out of the result.
        """
        values: dict[str, float | None] = {}
        for name in self._order:
            if self._uses_x[name] and name not in raw:
                continue
            x = raw.get(name)
            if name not in self._code:
                values[name] = x
                continue
            inputs = {n: values.get(n) for n in self._inputs[name]}
            if (self._uses_x[name] and _missing(x)) or any(_missing(v) for v in inputs.values()):
                values[name] = None
                continue
            try:
                scope = {**FUNCTIONS, **inputs, "x": x}
                result = float(eval(self._code[name], {"__builtins__": {}}, scope))
                values[name] = result if math.isfinite(result) else None
            except (ArithmeticError, ValueError):
                values[name] = None  # e.g. log of a negative number, sensor out of range
            except Exception as e:
                # A mistake in the formula itself, not in the data
                if strict:
                    raise ValueError(f"Channel '{name}': transform failed: {e}") from None
                raise
        return values


def _missing(value: float | None) -> bool:
    return value is None or math.isnan(value)
