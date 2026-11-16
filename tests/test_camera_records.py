"""Image naming, the images-table rows, the spool and `hp index-images`."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import start_hp_db

from heatpump import cli
from heatpump.camera import records
from heatpump.common import db


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def row(ts, size=(640, 480)):
    return records.image_row(ts, records.image_relpath(ts), size)


def stored(con):
    return con.execute(
        "SELECT image_id, ts, camera_id, path, width, height FROM hp.images ORDER BY ts"
    ).fetchall()


@pytest.mark.parametrize(
    "ts",
    [
        utc(2026, 11, 14, 5, 32, 15),
        utc(2026, 11, 1, 9, 30, 0),   # first pass through the repeated hour
        utc(2026, 11, 1, 10, 30, 0),  # second pass ('b' suffix)
        utc(2026, 7, 4, 20, 0, 59),   # daylight saving time
    ],
)
def test_path_gives_back_the_capture_time(anchorage, ts):
    assert records.ts_from_relpath(records.image_relpath(ts)) == ts


@pytest.mark.parametrize(
    "path",
    ["coil/2026-11-13/notes.jpg", "coil/2026-13-45/10-00-00.jpg", "coil/2026-11-13/25-00-00.jpg",
     "other/2026-11-13/10-00-00.jpg", "coil/10-00-00.jpg"],
)
def test_other_paths_are_not_images(path):
    assert records.ts_from_relpath(Path(path)) is None


def test_jpeg_size(tmp_path):
    np = pytest.importorskip("numpy")
    cv2 = pytest.importorskip("cv2")
    path = tmp_path / "a.jpg"
    cv2.imwrite(str(path), np.zeros((48, 64, 3), dtype=np.uint8))
    assert records.jpeg_size(path) == (64, 48)
    path.write_bytes(b"not a jpg")
    assert records.jpeg_size(path) is None


def test_insert_images_skips_paths_already_stored(anchorage, server, monkeypatch):
    monkeypatch.setattr(records, "ROWS_PER_INSERT", 1)  # several INSERT statements
    con = db.connect()
    a, b, c = (row(utc(2026, 11, 14, 5, 32, s)) for s in (0, 10, 20))
    assert records.insert_images(con, [a, b]) == 2
    assert records.insert_images(con, [b, c, c]) == 1
    rows = stored(con)
    assert [r[3] for r in rows] == [
        "coil/2026-11-13/20-32-00.jpg", "coil/2026-11-13/20-32-10.jpg", "coil/2026-11-13/20-32-20.jpg",
    ]
    assert len({r[0] for r in rows}) == 3  # each got its own image_id
    assert rows[0][1:3] == (utc(2026, 11, 14, 5, 32, 0), 1)
    assert rows[0][4:] == (640, 480)


def test_spool_keeps_rows_while_the_database_is_down(anchorage, db_env, tmp_path, capsys):
    spool = records.ImageSpool(tmp_path / "spool" / "images.jsonl")
    spool.add(row(utc(2026, 11, 14, 5, 32, 0)))
    spool.load()  # no hp-db running
    spool.add(row(utc(2026, 11, 14, 5, 33, 0)))
    spool.load()
    assert len(spool.path.read_text().splitlines()) == 2
    assert capsys.readouterr().out.count("Database unavailable") == 1

    with spool.path.open("a") as f:
        f.write('{"ts": "2026-11-1')  # a line cut short by a crash
    proc = start_hp_db()
    try:
        spool.load()
        assert not spool.path.exists()
        assert len(stored(db.connect())) == 2
        assert "Database is back" in capsys.readouterr().out
    finally:
        proc.kill()
        proc.wait()


def test_index_images_adds_missing_rows_once(anchorage, server, capsys):
    np = pytest.importorskip("numpy")
    cv2 = pytest.importorskip("cv2")
    root = db.db_path().parent / "images"
    times = [utc(2026, 11, 14, 5, 32, 0), utc(2026, 11, 14, 5, 33, 0), utc(2026, 11, 1, 10, 30, 0)]
    for ts in times:
        path = root / records.image_relpath(ts)
        path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(path), np.zeros((48, 64, 3), dtype=np.uint8))
    (root / "coil" / "2026-11-13" / "notes.jpg").write_bytes(b"x")

    con = db.connect()
    records.insert_images(con, [row(times[0], (64, 48))])  # hp-camera already recorded this one

    cli.index_images()
    assert "2 added, 1 already in the database, 1 skipped" in capsys.readouterr().out
    rows = stored(con)
    assert [r[1] for r in rows] == sorted(times)
    assert all(r[4:] == (64, 48) for r in rows)

    cli.index_images()
    assert "0 added, 3 already in the database" in capsys.readouterr().out
    assert len(stored(con)) == 3
