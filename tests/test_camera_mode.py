"""Capture interval by heat pump mode."""

from datetime import UTC, datetime, timedelta

import pytest

from heatpump.camera import mode
from heatpump.common import db

MODES = {
    "defrost": {"when": "defrost_valve == 1", "interval": 10},
    "heating": {"when": "compressor_kw > 0.5", "interval": 120},
    "off": {"interval": 900},
}


def test_defaults_without_a_camera_section():
    cfg = mode.parse({})
    assert (cfg.interval, cfg.modes) == (60, [])
    reader = mode.ModeReader(cfg)
    assert reader.current() is None and not reader.uses_readings


def test_first_matching_mode_wins():
    cfg = mode.parse({"interval": 30, "modes": MODES})
    assert cfg.channels == {"defrost_valve", "compressor_kw"}

    def chosen(valve, kw):
        m = mode.choose(cfg.modes, {"defrost_valve": valve, "compressor_kw": kw})
        return m.name, m.interval

    assert chosen(1, 3.0) == ("defrost", 10)
    assert chosen(0, 3.0) == ("heating", 120)
    assert chosen(0, 0.0) == ("off", 900)


def test_formula_that_cannot_be_evaluated_gives_unknown():
    cfg = mode.parse({"modes": {"a": {"when": "1 / power > 2", "interval": 5}}})
    assert mode.choose(cfg.modes, {"power": 0.0}) is None


@pytest.mark.parametrize(
    "section",
    [
        {"interval": 0},
        {"speed": 3},
        {"modes": {"a": {"when": "x > 1"}}},                       # no interval
        {"modes": {"a": {"when": "x >", "interval": 5}}},           # not a formula
        {"modes": {"a": {"when": "__import__('os')", "interval": 5}}},
        {"modes": {"a": {"when": "x.real > 1", "interval": 5}}},
        {"modes": {"a": {"when": "sqrt(x, 2) > 1", "interval": 5}}},
        {"modes": {"a": {"when": "x > 1", "interval": 5, "colour": 1}}},
        {"modes": {"off": {"interval": 900}, "a": {"when": "x > 1", "interval": 5}}},
    ],
)
def test_config_mistakes_are_reported(section):
    with pytest.raises(ValueError, match="camera"):
        mode.parse(section)


def test_load_uses_defaults_without_a_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HP_CONFIG", str(tmp_path / "missing.toml"))
    assert mode.load().modes == []
    (tmp_path / "config.toml").write_text('[camera.modes.off]\ninterval = "often"\n')
    monkeypatch.setenv("HP_CONFIG", str(tmp_path / "config.toml"))
    with pytest.raises(SystemExit):
        mode.load()


def add_reading(con, sensor_id, value, age_seconds, quality=0):
    ts = datetime.now(UTC) - timedelta(seconds=age_seconds)
    con.execute(
        f"INSERT INTO hp.readings VALUES (TIMESTAMPTZ '{ts.isoformat()}', {sensor_id}, "
        f"{'NULL' if value is None else value}, {quality})"
    )


def test_mode_follows_the_latest_readings(server, capsys):
    con = db.connect()
    con.execute(
        "INSERT INTO hp.sensors (sensor_id, name, kind) VALUES "
        "(1, 'defrost_valve', 'binary'), (2, 'compressor_kw', 'analog')"
    )
    reader = mode.ModeReader(mode.parse({"modes": MODES}))
    assert reader.uses_readings

    assert reader.current() is None  # no readings at all
    add_reading(con, 1, 0, 30)
    add_reading(con, 2, 0.0, 300)  # too old
    assert reader.current() is None
    assert capsys.readouterr().out.count("mode unknown") == 1  # said once, not twice

    add_reading(con, 2, 0.0, 20)
    assert reader.current().name == "off"
    assert "can be read again" in capsys.readouterr().out
    add_reading(con, 2, 3.2, 10)
    assert reader.current().name == "heating"
    add_reading(con, 1, 1, 5)
    assert reader.current().name == "defrost"

    add_reading(con, 1, None, 1, quality=2)  # latest read of the valve failed
    assert reader.current() is None


def test_mode_is_unknown_while_the_database_is_down(db_env, capsys):
    reader = mode.ModeReader(mode.parse({"modes": MODES}))
    assert reader.current() is None
    assert "database is unavailable" in capsys.readouterr().out
