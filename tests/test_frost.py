"""The derived frost sensor: analysis/frost.py, frost_image.py and hp-frost, against a real hp-db."""

from datetime import UTC, datetime, timedelta

import cv2
import numpy as np
import pytest

from heatpump.analysis import frost, frost_image
from heatpump.camera import records
from heatpump.common import db
from heatpump.frost import main as frost_main

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)  # a 15 s clock time


def at(seconds):
    return T0 + timedelta(seconds=seconds)


@pytest.fixture
def con(server):
    con = db.connect()
    con.execute(
        "INSERT INTO hp.sensors (sensor_id, name, kind, unit) VALUES (5, 'coil_pressure', 'analog', 'Pa')"
    )
    return con


def add_pressure(con, points):
    values = ", ".join(f"(TIMESTAMPTZ '{at(s).isoformat()}', 5, {v}, 0)" for s, v in points)
    con.execute(f"INSERT INTO hp.readings VALUES {values}")


def add_image(con, root, seconds, bgr):
    """Save a one-colour JPG and record it. Returns its path relative to `root`."""
    relpath = records.image_relpath(at(seconds))
    (root / relpath).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(root / relpath), np.full((480, 640, 3), bgr, np.uint8))
    records.insert_images(con, [records.image_row(at(seconds), relpath, (640, 480))])
    return relpath


BLUE = (200, 90, 30)  # like the clean coil
WHITE = (235, 235, 235)  # like heavy frost


def stored(con):
    fid = frost.sensor_id(con, "frost")
    return con.execute(
        f"SELECT ts, value FROM hp.readings WHERE sensor_id = {fid} ORDER BY ts"
    ).fetchall()


def test_pressure_to_frost():
    assert frost.pressure_to_frost(20) == 0
    assert frost.pressure_to_frost(23) == 0
    assert frost.pressure_to_frost(51.5) == pytest.approx(5)
    assert frost.pressure_to_frost(80) == 10
    assert frost.pressure_to_frost(150) == 10


def test_pressure_scores_average_the_centred_15_seconds(con):
    add_pressure(con, [
        (-7.5, 40), (0, 50), (7.4, 60),  # the span of T0: [-7.5, 7.5)
        (7.5, 30), (10, 5),              # T0 + 15: the 5 Pa reading is left out
        (25, 12), (35, 19.9),            # T0 + 30: fan off, no score
        (50, 200),                       # T0 + 45
    ])
    scores = frost.pressure_scores(con, at(0), at(60))
    assert scores == [
        (at(0), round(frost.pressure_to_frost(50), 2)),
        (at(15), round(frost.pressure_to_frost(30), 2)),
        (at(45), 10.0),
    ]
    # The range is [start, end): a clock time equal to `end` belongs to the next range
    assert [t for t, _ in frost.pressure_scores(con, at(15), at(45))] == [at(15)]


def test_images_are_scored_only_while_the_fan_is_off(con, tmp_path):
    root = tmp_path / "images"
    add_pressure(con, [(0, 45), (20, 10), (30, 10)])
    add_image(con, root, 5, WHITE)     # fan running: no image score
    add_image(con, root, 25, WHITE)    # fan off
    add_image(con, root, 300, BLUE)    # no pressure reading for minutes: scored anyway
    assert [t for t, _ in frost.fan_off_images(con, at(0), at(600))] == [at(25), at(300)]

    scores = dict(frost.compute(con, at(0), at(600), root))
    assert set(scores) == {at(0), at(25), at(300)}
    assert scores[at(25)] > 7  # white
    assert scores[at(300)] < 1  # blue


def test_images_are_scored_when_there_is_no_pressure_sensor(server, tmp_path):
    con = db.connect()
    root = tmp_path / "images"
    add_image(con, root, 5, BLUE)
    assert [t for t, _ in frost.compute(con, at(0), at(60), root)] == [at(5)]


def test_a_missing_image_file_is_skipped(con, tmp_path):
    root = tmp_path / "images"
    relpath = add_image(con, root, 5, BLUE)
    (root / relpath).unlink()
    assert frost.compute(con, at(0), at(60), root) == []


def test_backfill_replaces_scores_and_registers_the_sensor(con, tmp_path):
    root = tmp_path / "images"
    add_pressure(con, [(0, 45), (15, 45), (30, 10)])
    add_image(con, root, 40, BLUE)
    assert frost.last_score_time(con) is None
    assert frost.backfill(con, at(0), at(60), root, chunk=timedelta(seconds=30)) == 3
    first = stored(con)
    assert [t for t, _ in first] == [at(0), at(15), at(40)]
    assert frost.backfill(con, at(0), at(60), root) == 3  # again: no duplicates
    assert stored(con) == first
    assert frost.last_score_time(con) == at(40)
    # Pressure readings are untouched, and frost shows up as a sensor
    assert con.execute("SELECT count(*) FROM hp.readings WHERE sensor_id = 5").fetchone()[0] == 3
    name, kind = con.execute(
        "SELECT name, kind FROM hp.sensors WHERE sensor_id = 6"
    ).fetchone()
    assert (name, kind) == ("frost", "analog")


def test_hp_frost_continues_after_the_last_score(con, tmp_path, monkeypatch):
    monkeypatch.setenv("HP_DATA_DIR", str(tmp_path))  # images under tmp_path/images
    root = tmp_path / "images"
    add_pressure(con, [(0, 45), (15, 45), (30, 45), (45, 10)])
    add_image(con, root, 50, WHITE)
    done = frost_main.catch_up(con, at(0), at(20))
    assert done == at(20)
    assert [t for t, _ in stored(con)] == [at(0), at(15)]
    # A restart picks up just after the newest score and does not repeat it
    start = frost_main.start_time(con)
    assert at(15) < start < at(16)
    frost_main.catch_up(con, start, at(60))
    assert [t for t, _ in stored(con)] == [at(0), at(15), at(30), at(50)]


def test_blue_scores_low_and_white_scores_high():
    def score(bgr):
        return frost_image.score_features(
            frost_image.image_features(np.full((480, 640, 3), bgr, np.uint8))
        )

    assert 0 <= score(BLUE) < 1
    assert 7 < score(WHITE) <= 10


def test_unreadable_image_gives_no_score(tmp_path):
    (tmp_path / "bad.jpg").write_bytes(b"not a jpg")
    assert frost_image.image_to_frost(tmp_path / "bad.jpg") is None
    assert frost_image.image_to_frost(tmp_path / "missing.jpg") is None
