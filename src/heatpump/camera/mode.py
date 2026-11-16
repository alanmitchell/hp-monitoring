"""Decides how often `hp-camera` takes an image, from the heat pump's current mode.

The `[camera]` section of the config file lists modes, each with a capture
interval and a `when` formula over the latest sensor readings, for example::

    [camera.modes.defrost]
    when = "defrost_valve == 1"
    interval = 10

Modes are tried in the order written and the first whose formula is true wins.
A mode without `when` matches whenever no earlier one did. With no modes, or
when the mode cannot be worked out (database down, a needed reading missing or
too old), the mode is unknown and the default interval is used.
"""

import ast
from dataclasses import dataclass, field

import duckdb

from heatpump.common import db
from heatpump.common.config import config_path, load_config
from heatpump.sensors.transform import ALLOWED_NODES, FUNCTIONS


@dataclass
class Mode:
    name: str
    interval: float                # seconds between images in this mode
    when: str | None = None        # formula; None = matches if no earlier mode did
    channels: frozenset[str] = frozenset()  # sensor channels the formula reads

    def __post_init__(self):
        self._code = compile(self.when, f"<when of mode {self.name}>", "eval") if self.when else None

    def matches(self, values: dict[str, float]) -> bool:
        if self._code is None:
            return True
        return bool(eval(self._code, {"__builtins__": {}}, {**FUNCTIONS, **values}))


@dataclass
class CameraConfig:
    interval: float = 60           # used while the mode is unknown
    mode_check_seconds: float = 5  # how often the mode is re-checked between images
    max_reading_age: float = 180   # a reading older than this is treated as missing
    modes: list[Mode] = field(default_factory=list)

    @property
    def channels(self) -> set[str]:
        return set().union(*(m.channels for m in self.modes))


def parse(section: dict) -> CameraConfig:
    """The `[camera]` section of the config file -> CameraConfig. Raises ValueError."""
    section = dict(section)
    modes_in = section.pop("modes", {})
    cfg = CameraConfig()
    for key, value in section.items():
        if key not in ("interval", "mode_check_seconds", "max_reading_age"):
            raise ValueError(f"[camera]: unknown setting '{key}'")
        setattr(cfg, key, _seconds(f"[camera] {key}", value))

    for name, settings in modes_in.items():
        where = f"[camera.modes.{name}]"
        unknown = set(settings) - {"when", "interval"}
        if unknown:
            raise ValueError(f"{where}: unknown setting '{min(unknown)}'")
        if "interval" not in settings:
            raise ValueError(f"{where}: interval is required")
        if cfg.modes and cfg.modes[-1].when is None:
            raise ValueError(
                f"{where} can never be chosen: [camera.modes.{cfg.modes[-1].name}] comes "
                "before it and has no 'when', so it always matches. Put it last."
            )
        when = settings.get("when")
        channels = _check_formula(where, when) if when is not None else frozenset()
        mode = Mode(name, _seconds(f"{where} interval", settings["interval"]), when, channels)
        try:  # trial run, to catch mistakes such as a wrong function argument
            mode.matches(dict.fromkeys(channels, 1.0))
        except ArithmeticError, ValueError:
            pass
        except Exception as e:  # noqa: BLE001 - any other error is a mistake in the formula
            raise ValueError(f"{where}: 'when' failed: {e}") from None
        cfg.modes.append(mode)
    return cfg


def load() -> CameraConfig:
    """Camera settings from the config file; the defaults if there is no file."""
    if not config_path().exists():
        return CameraConfig()
    try:
        return parse(load_config().get("camera", {}))
    except ValueError as e:
        raise SystemExit(f"Config file {config_path()}: {e}") from None


def _seconds(what: str, value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{what} must be a number of seconds greater than 0")
    return float(value)


def _check_formula(where: str, formula) -> frozenset[str]:
    """Validate a `when` formula; return the sensor channels it reads."""
    if not isinstance(formula, str):
        raise ValueError(f"{where}: 'when' must be a formula in quotes")  # noqa: TRY004
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"{where}: 'when' is not a valid formula: {e.msg}") from None
    channels = set()
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_NODES):
            raise ValueError(  # noqa: TRY004 - a config mistake, reported as such
                f"{where}: 'when' uses something not allowed ({type(node).__name__})"
            )
        if isinstance(node, ast.Call):
            if not (isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS):
                raise ValueError(f"{where}: 'when' calls an unknown function")
        elif isinstance(node, ast.Name) and node.id not in FUNCTIONS:
            channels.add(node.id)
    return frozenset(channels)


def choose(modes: list[Mode], values: dict[str, float]) -> Mode | None:
    """The first mode whose formula is true for these readings. None = unknown."""
    try:
        for mode in modes:
            if mode.matches(values):
                return mode
    except ArithmeticError, ValueError:
        pass  # e.g. division by zero in a formula
    return None


class ModeReader:
    """Works out the current mode from the latest sensor readings in the database."""

    def __init__(self, cfg: CameraConfig):
        self.cfg = cfg
        self.channels = sorted(cfg.channels)
        self._con: duckdb.DuckDBPyConnection | None = None
        self._problem: str | None = None  # so each problem is printed once, not every check

    @property
    def uses_readings(self) -> bool:
        """False if the mode never changes, so there is nothing to keep checking."""
        return bool(self.channels)

    def current(self) -> Mode | None:
        if not self.channels:
            return choose(self.cfg.modes, {})
        try:
            values = self._latest()
        except duckdb.Error as e:
            self._report(f"the database is unavailable ({e})")
            self.close()
            return None
        missing = [name for name in self.channels if name not in values]
        if missing:
            self._report(f"no good reading in the last {self.cfg.max_reading_age:g} s for "
                         + ", ".join(missing))
            return None
        self._report(None)
        return choose(self.cfg.modes, values)

    def close(self) -> None:
        if self._con is not None:
            try:
                self._con.close()
            except duckdb.Error:
                pass
            self._con = None

    def _latest(self) -> dict[str, float]:
        """Latest good reading of each needed channel, if recent enough."""
        if self._con is None:
            self._con = db.connect()
        # No ? parameters on hp.* tables, so the names are written into the SQL
        names = ", ".join("'" + name.replace("'", "''") + "'" for name in self.channels)
        rows = self._con.execute(
            "SELECT s.name, r.value, r.quality FROM hp.readings r "
            "JOIN hp.sensors s USING (sensor_id) "
            f"WHERE r.ts > now() - to_seconds({self.cfg.max_reading_age!r}) "
            f"AND s.name IN ({names}) "
            "QUALIFY row_number() OVER (PARTITION BY r.sensor_id ORDER BY r.ts DESC) = 1"
        ).fetchall()
        return {name: value for name, value, quality in rows if quality == 0 and value is not None}

    def _report(self, problem: str | None) -> None:
        if problem and self._problem is None:
            print(f"Heat pump mode unknown, using the default interval: {problem}")
        elif problem is None and self._problem is not None:
            print("Heat pump mode can be read again")
        self._problem = problem
