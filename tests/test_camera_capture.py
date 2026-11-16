from datetime import UTC, datetime
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
capture = pytest.importorskip("heatpump.camera.capture")


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


def test_main_saves_the_image_and_records_it(anchorage, server, tmp_path, monkeypatch):
    from heatpump.common import db

    monkeypatch.setenv("RTSP_URL", "rtsp://camera.invalid/stream")
    monkeypatch.setenv("HP_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setattr(capture, "capture_frame", lambda url: np.zeros((48, 64, 3), dtype=np.uint8))

    def stop_after_first_image(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(capture.time, "sleep", stop_after_first_image)
    capture.main()

    rows = db.connect().execute("SELECT ts, camera_id, path, width, height FROM hp.images").fetchall()
    assert len(rows) == 1
    ts, camera_id, path, width, height = rows[0]
    assert (camera_id, width, height) == (1, 64, 48)
    assert ts.microsecond == 0
    assert Path(path) == capture.image_relpath(ts)  # the row and the file name agree
    assert (db.db_path().parent / "images" / path).stat().st_size > 0
    assert not (db.db_path().parent / "spool" / "camera" / "images.jsonl").exists()


def test_main_needs_the_database_token(monkeypatch):
    monkeypatch.setenv("RTSP_URL", "rtsp://camera.invalid/stream")
    monkeypatch.delenv("HP_DB_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        capture.main()


def test_main_follows_the_mode(anchorage, server, tmp_path, monkeypatch, capsys):
    """Fake clock: the heat pump is off, then defrosts from t=100 to t=130."""
    from heatpump.camera import mode
    from heatpump.common import db

    config = tmp_path / "config.toml"
    config.write_text(
        '[camera]\nmode_check_seconds = 5\n'
        '[camera.modes.defrost]\nwhen = "valve == 1"\ninterval = 10\n'
        '[camera.modes.off]\ninterval = 60\n'
    )
    monkeypatch.setenv("HP_CONFIG", str(config))
    monkeypatch.setenv("RTSP_URL", "rtsp://camera.invalid/stream")

    clock = [0.0]
    taken = []
    modes = mode.load().modes

    def fake_sleep(seconds):
        clock[0] += seconds
        if clock[0] >= 180:
            raise KeyboardInterrupt

    def fake_frame(url):
        taken.append(clock[0])
        return np.zeros((48, 64, 3), dtype=np.uint8)

    monkeypatch.setattr(capture.time, "sleep", fake_sleep)
    monkeypatch.setattr(capture.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(capture, "capture_frame", fake_frame)
    monkeypatch.setattr(
        mode.ModeReader, "current",
        lambda self: mode.choose(modes, {"valve": 1 if 100 <= clock[0] < 130 else 0}),
    )
    # A new file name for every image, though the real clock barely moves
    monkeypatch.setattr(
        capture, "image_relpath", lambda ts: Path(f"coil/2026-11-13/00-00-{len(taken):02d}.jpg")
    )
    capture.main()

    # Every 60 s when off; at once when defrost starts, then every 10 s; at once when it ends
    assert taken == [0, 60, 100, 110, 120, 130]
    out = capsys.readouterr().out
    assert out.count("Heat pump mode: defrost") == 1 and out.count("Heat pump mode: off") == 2
    ctx = db.connect().execute("SELECT capture_ctx FROM hp.images ORDER BY path").fetchall()
    assert [c[0] for c in ctx] == ["off", "off", "defrost", "defrost", "defrost", "off"]
