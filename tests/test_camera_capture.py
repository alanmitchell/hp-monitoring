import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
capture = pytest.importorskip("heatpump.camera.capture")


@pytest.fixture
def anchorage(monkeypatch):
    monkeypatch.setenv("TZ", "America/Anchorage")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def test_relpath_uses_local_date_and_time(anchorage):
    # 05:32:15 UTC on the 14th is 20:32:15 on the 13th in Alaska (UTC-9)
    assert capture.image_relpath(utc(2026, 11, 14, 5, 32, 15)) == Path(
        "coil/2026-11-13/20-32-15.jpg"
    )


def test_relpath_marks_repeated_hour_when_dst_ends(anchorage):
    # Alaska falls back at 02:00 on 2026-11-01, so 01:30 local happens twice
    first = capture.image_relpath(utc(2026, 11, 1, 9, 30, 0))
    second = capture.image_relpath(utc(2026, 11, 1, 10, 30, 0))
    assert first == Path("coil/2026-11-01/01-30-00.jpg")
    assert second == Path("coil/2026-11-01/01-30-00b.jpg")


def test_save_frame_writes_jpg_under_image_root(anchorage, tmp_path, monkeypatch):
    monkeypatch.setenv("HP_DATA_DIR", str(tmp_path))
    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    relpath = capture.save_frame(frame, utc(2026, 11, 14, 5, 32, 15))
    assert relpath == Path("coil/2026-11-13/20-32-15.jpg")
    assert (tmp_path / "images" / relpath).stat().st_size > 0
