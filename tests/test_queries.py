"""analysis/queries.py against a real hp-db."""

from datetime import UTC, datetime, timedelta

import pytest

from heatpump.analysis import queries
from heatpump.camera import records
from heatpump.common import db

T0 = datetime(2026, 11, 10, tzinfo=UTC)


def at(seconds):
    return T0 + timedelta(seconds=seconds)


def ms(seconds):
    return round(at(seconds).timestamp() * 1000)


@pytest.fixture
def con(server):
    con = db.connect()
    con.execute(
        "INSERT INTO hp.sensors (sensor_id, name, kind, unit) VALUES "
        "(1, 'supply_temp', 'analog', 'degF'), (2, 'valve', 'binary', NULL)"
    )
    return con


def add(con, sensor_id, points):
    """points: (seconds after T0, value) pairs; a value of None is a failed reading."""
    values = ", ".join(
        f"(TIMESTAMPTZ '{at(s).isoformat()}', {sensor_id}, "
        f"{'NULL' if v is None else v}, {2 if v is None else 0})"
        for s, v in points
    )
    con.execute(f"INSERT INTO hp.readings VALUES {values}")


def test_sensors_and_latest(con):
    assert [s["name"] for s in queries.sensors(con)] == ["supply_temp", "valve"]
    assert queries.sensors(con)[0]["unit"] == "degF"
    add(con, 1, [(0, 100.0), (60, 101.0)])
    add(con, 2, [(-2 * 86400, 1)])  # more than a day old
    assert queries.latest(con, at(120)) == [
        {"sensor_id": 1, "ts": ms(60), "value": 101.0, "quality": 0}
    ]


def test_few_readings_come_back_as_stored_with_one_either_side(con):
    add(con, 1, [(-500, 1.0), (-20, 2.0), (10, 3.0), (20, None), (30, 4.0), (110, 5.0), (150, 6.0)])
    series = queries.readings(con, [1, 2], at(0), at(100))
    s = series[1]
    assert s.bucket_seconds is None and s.low is None
    # -500 is left out: only the last one before the range. 110 is the first after it.
    assert s.ts == [ms(-20), ms(10), ms(20), ms(30), ms(110)]
    assert s.value == [2.0, 3.0, None, 4.0, 5.0]
    assert series[2].ts == []  # a channel with no data


def test_a_long_silence_breaks_the_line(con):
    add(con, 1, [(0, 1.0), (60, 1.0), (1000, 2.0), (1060, 2.0)])
    s = queries.readings(con, [1], at(0), at(2000))[1]
    assert s.ts == [ms(0), ms(60), ms(60) + 1, ms(1000), ms(1060)]
    assert s.value == [1.0, 1.0, None, 2.0, 2.0]


def test_many_readings_are_averaged_by_time(con):
    # 0 until 90 s, 10 until 200 s, 20 after that; then nothing until 5 at 500 s
    add(con, 1, [(0, 0), (30, 0), (60, 0), (90, 10), (120, 10), (200, 20), (500, 5)])
    s = queries.readings(con, [1], at(0), at(600), max_points=5, max_gap=100)[1]
    assert s.bucket_seconds == 120
    assert s.ts == [ms(60), ms(180), ms(300), ms(420), ms(540)]
    assert s.value == pytest.approx([
        2.5,            # 0 for 90 s, 10 for 30 s
        40 / 3,         # 10 for 80 s, 20 for 40 s
        20,             # 20 held for max_gap (until 300 s), the rest of the bucket is empty
        None,           # no data: breaks the line
        5,              # from 500 s to the end
    ])
    assert s.low == [0, 10, 20, None, 5]
    assert s.high == [10, 20, 20, None, 5]


def test_value_before_the_range_is_carried_in(con):
    add(con, 1, [(-30, 7)] + [(s, 9) for s in range(60, 120, 5)])
    s = queries.readings(con, [1], at(0), at(120), max_points=4)[1]
    assert s.bucket_seconds == 30
    assert s.value == pytest.approx([7, 7, 9, 9])


def test_dense_and_sparse_channels_in_one_request(con):
    add(con, 1, [(s, s) for s in range(0, 600, 2)])
    add(con, 2, [(100, 1), (400, 0)])
    series = queries.readings(con, [2, 1], at(0), at(600), max_points=100)
    assert series[1].bucket_seconds == 10 and len(series[1].ts) == 60
    assert series[1].value[0] == pytest.approx(4)  # 0, 2, 4, 6, 8, each held 2 s
    assert series[2].bucket_seconds is None and series[2].value == [1, 0]


def test_calibration_is_applied(con):
    con.execute(
        "INSERT INTO hp.calibrations (sensor_id, valid_from, offset_c) "
        f"VALUES (1, TIMESTAMPTZ '{at(50).isoformat()}', 0.5)"
    )
    add(con, 1, [(0, 10.0), (60, 10.0)])
    assert queries.readings(con, [1], at(0), at(100))[1].value == [10.0, 10.5]


def test_bucket_width():
    hour = timedelta(hours=1)
    assert queries.bucket_width(T0, T0 + hour, 2000) == 2
    assert queries.bucket_width(T0, T0 + 24 * hour, 2000) == 60
    assert queries.bucket_width(T0, T0 + 7 * 24 * hour, 2000) == 600
    assert queries.bucket_width(T0, T0 + 3 * 365 * 24 * hour, 100) == 11 * 86400


def test_times_must_have_a_time_zone(con):
    with pytest.raises(ValueError, match="timezone"):
        queries.images(con, datetime(2026, 11, 10), at(10))


def test_images(con):
    rows = [
        records.image_row(at(s), records.image_relpath(at(s)), (64, 48), mode)
        for s, mode in [(0, "off"), (60, "defrost"), (120, None)]
    ]
    records.insert_images(con, rows)
    found = queries.images(con, at(30), at(121))
    assert [(i["ts"], i["mode"]) for i in found] == [(ms(60), "defrost"), (ms(120), None)]
    assert queries.image_path(con, found[0]["image_id"]) == rows[1]["path"]
    assert queries.image_path(con, 999_999) is None
