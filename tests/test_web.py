"""The web API against a real hp-db."""

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from heatpump.camera import records
from heatpump.common import db
from heatpump.web import app as web_app
from heatpump.web import main as web_main
from heatpump.web.app import create_app

T0 = datetime(2026, 11, 10, tzinfo=UTC)


def at(seconds):
    return T0 + timedelta(seconds=seconds)


def iso(seconds):
    return at(seconds).isoformat().replace("+00:00", "Z")


def ms(seconds):
    return round(at(seconds).timestamp() * 1000)


def add(con, sensor_id, points, t0=T0):
    values = ", ".join(
        f"(TIMESTAMPTZ '{(t0 + timedelta(seconds=s)).isoformat()}', {sensor_id}, {v}, 0)"
        for s, v in points
    )
    con.execute(f"INSERT INTO hp.readings VALUES {values}")


@pytest.fixture
def con(server):
    con = db.connect()
    con.execute(
        "INSERT INTO hp.sensors (sensor_id, name, kind, unit) VALUES "
        "(1, 'supply_temp', 'analog', 'degF'), (2, 'valve', 'binary', NULL)"
    )
    return con


@pytest.fixture
def client(con):
    return TestClient(create_app())


def test_sensors(client):
    body = client.get("/api/sensors").json()
    assert [(s["name"], s["kind"], s["unit"]) for s in body] == [
        ("supply_temp", "analog", "degF"), ("valve", "binary", None),
    ]


def test_latest_lists_every_channel(client, con):
    now = datetime.now(UTC)
    add(con, 1, [(-120, 98.5), (-60, 99.5)], t0=now)
    body = client.get("/api/latest").json()
    assert [(s["name"], s["value"]) for s in body] == [("supply_temp", 99.5), ("valve", None)]
    assert abs(body[0]["ts"] / 1000 - (now.timestamp() - 60)) < 1
    assert body[1]["ts"] is None


def test_readings_stored_and_bucketed(client, con):
    add(con, 1, [(s, s) for s in range(0, 600, 2)])
    add(con, 2, [(100, 1), (400, 0)])
    params = {"sensors": "valve, supply_temp", "start": iso(0), "end": iso(600), "max_points": 100}
    body = client.get("/api/readings", params=params).json()
    assert (body["start"], body["end"]) == (ms(0), ms(600))
    valve, temp = body["series"]  # in the order asked for
    assert valve == {
        "name": "valve", "unit": None, "kind": "binary", "bucket_seconds": None,
        "ts": [ms(100), ms(400)], "value": [1, 0], "low": None, "high": None,
    }
    assert (temp["name"], temp["bucket_seconds"], len(temp["ts"])) == ("supply_temp", 10, 60)
    assert temp["value"][0] == pytest.approx(4) and (temp["low"][0], temp["high"][0]) == (0, 8)

    # Asking for a shorter range gives the stored readings
    params = {"sensors": "supply_temp", "start": iso(100), "end": iso(200), "max_points": 100}
    zoomed = client.get("/api/readings", params=params).json()["series"][0]
    assert zoomed["bucket_seconds"] is None and zoomed["value"][:3] == [98, 100, 102]


def test_readings_accepts_other_time_zones(client, con):
    add(con, 1, [(0, 5.0)])
    params = {"sensors": "supply_temp", "start": "2026-11-09T15:00:00-09:00", "end": iso(60)}
    assert client.get("/api/readings", params=params).json()["series"][0]["value"] == [5.0]


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({"sensors": "nope", "start": iso(0), "end": iso(60)}, 404),
        ({"sensors": "valve", "start": iso(60), "end": iso(0)}, 422),            # backwards
        ({"sensors": "valve", "start": "2026-11-10T00:00:00", "end": iso(60)}, 422),  # no zone
        ({"sensors": "valve", "start": "yesterday", "end": iso(60)}, 422),
        ({"sensors": "valve", "start": iso(0), "end": iso(60), "max_points": 1}, 422),
        ({"start": iso(0), "end": iso(60)}, 422),                                # no sensors
        ({"sensors": "valve'; DROP TABLE readings; --", "start": iso(0), "end": iso(60)}, 404),
    ],
)
def test_bad_requests_are_refused(client, params, status):
    response = client.get("/api/readings", params=params)
    assert response.status_code == status, response.text


def test_images_and_image_file(client, con, anchorage):
    root = db.db_path().parent / "images"
    rows = []
    for s, mode in [(0, "off"), (60, "defrost")]:
        relpath = records.image_relpath(at(s))
        (root / relpath).parent.mkdir(parents=True, exist_ok=True)
        (root / relpath).write_bytes(b"jpg-" + mode.encode())
        rows.append(records.image_row(at(s), relpath, (64, 48), mode))
    # One row whose file is gone, and one pointing outside the image root
    rows.append(records.image_row(at(120), records.image_relpath(at(120)), None))
    rows.append({**rows[0], "ts": at(180).isoformat(), "path": "coil/../../heatpump.duckdb"})
    records.insert_images(con, rows)

    listed = client.get("/api/images", params={"start": iso(0), "end": iso(600)}).json()
    assert [(i["ts"], i["mode"]) for i in listed] == [
        (ms(0), "off"), (ms(60), "defrost"), (ms(120), None), (ms(180), "off"),
    ]
    response = client.get(f"/images/{listed[1]['image_id']}")
    assert response.content == b"jpg-defrost"
    assert response.headers["content-type"] == "image/jpeg"
    assert "immutable" in response.headers["cache-control"]

    assert client.get(f"/images/{listed[2]['image_id']}").status_code == 404  # file gone
    assert client.get(f"/images/{listed[3]['image_id']}").status_code == 404  # outside the root
    assert client.get("/images/999999").status_code == 404
    assert client.get("/images/abc").status_code == 422


def test_thumbnail_is_made_once_and_kept(client, con, anchorage):
    from PIL import Image

    root = db.db_path().parent / "images"
    rows = []
    for s, size in [(0, (2560, 1440)), (60, (640, 360))]:
        relpath = records.image_relpath(at(s))
        (root / relpath).parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, (40, 90, 160)).save(root / relpath, "JPEG")
        rows.append(records.image_row(at(s), relpath, size))
    # A file that is not an image
    bad = records.image_relpath(at(120))
    (root / bad).write_bytes(b"not a jpg")
    rows.append(records.image_row(at(120), bad, None))
    records.insert_images(con, rows)
    big, small, broken = (
        i["image_id"] for i in client.get("/api/images", params={"start": iso(0), "end": iso(600)}).json()
    )

    response = client.get(f"/thumbs/{big}")
    assert response.headers["content-type"] == "image/jpeg"
    assert "immutable" in response.headers["cache-control"]
    kept = db.db_path().parent / "thumbs" / "840" / rows[0]["path"]
    with Image.open(kept) as thumb:
        assert thumb.size == (840, 473)
    assert response.content == kept.read_bytes()
    assert len(response.content) < (root / rows[0]["path"]).stat().st_size
    assert list(kept.parent.iterdir()) == [kept]  # no temporary file left behind

    # The second request is answered from the kept file
    kept.write_bytes(b"kept")
    assert client.get(f"/thumbs/{big}").content == b"kept"
    # A small image is sent as it is; the full size is still available
    assert client.get(f"/thumbs/{small}").content == (root / rows[1]["path"]).read_bytes()
    assert client.get(f"/images/{big}").content == (root / rows[0]["path"]).read_bytes()
    assert client.get(f"/thumbs/{broken}").status_code == 404
    assert client.get("/thumbs/999999").status_code == 404


def test_images_are_rotated(con, anchorage):
    from io import BytesIO

    from PIL import Image

    root = db.db_path().parent / "images"
    rows = []
    for s, size in [(0, (2560, 1440)), (60, (640, 360))]:
        relpath = records.image_relpath(at(s))
        (root / relpath).parent.mkdir(parents=True, exist_ok=True)
        picture = Image.new("RGB", size, (40, 90, 160))
        picture.paste((250, 250, 250), (0, 0, size[0] // 2, size[1] // 2))  # top left is white
        picture.save(root / relpath, "JPEG")
        rows.append(records.image_row(at(s), relpath, size))
    bad = records.image_relpath(at(120))
    (root / bad).write_bytes(b"not a jpg")
    rows.append(records.image_row(at(120), bad, None))
    records.insert_images(con, rows)
    span = {"start": iso(0), "end": iso(600)}

    def is_white(image, x, y):
        return min(image.convert("RGB").getpixel((x, y))) > 200

    client = TestClient(create_app(image_rotate=90))
    assert client.get("/api/settings").json() == {"image_rotate": 90}
    big, small, broken = (i["image_id"] for i in client.get("/api/images", params=span).json())

    # The thumbnail is turned, 840 wide, and kept in a folder of its own
    response = client.get(f"/thumbs/{big}?r=90")
    kept = db.db_path().parent / "thumbs" / "840-r90" / rows[0]["path"]
    assert response.content == kept.read_bytes()
    with Image.open(kept) as thumb:
        assert thumb.size == (840, 1493)
        # Clockwise: what was top left is now top right
        assert is_white(thumb, 800, 40) and not is_white(thumb, 40, 40)
    # A small image is turned too, and not made bigger
    with Image.open(BytesIO(client.get(f"/thumbs/{small}").content)) as thumb:
        assert thumb.size == (360, 640)

    # Full size: turned for each request, the camera's file left alone
    original = (root / rows[0]["path"]).read_bytes()
    response = client.get(f"/images/{big}?r=90")
    assert response.headers["content-type"] == "image/jpeg"
    assert "immutable" in response.headers["cache-control"]
    with Image.open(BytesIO(response.content)) as full:
        assert full.size == (1440, 2560)
        assert is_white(full, 1400, 40) and not is_white(full, 40, 40)
    assert (root / rows[0]["path"]).read_bytes() == original
    assert client.get(f"/images/{broken}").status_code == 404
    assert client.get(f"/thumbs/{broken}").status_code == 404

    for degrees, size, white in [(180, (2560, 1440), (2500, 1400)), (270, (1440, 2560), (40, 2500))]:
        turned = TestClient(create_app(image_rotate=degrees))
        with Image.open(BytesIO(turned.get(f"/images/{big}").content)) as full:
            assert full.size == size and is_white(full, *white)

    # Not rotated: the file as it is
    plain = TestClient(create_app())
    assert plain.get("/api/settings").json() == {"image_rotate": 0}
    assert plain.get(f"/images/{big}").content == original
    with pytest.raises(ValueError):
        create_app(image_rotate=45)


def test_long_image_ranges_are_thinned(client, con, monkeypatch):
    rows = [records.image_row(T0 + timedelta(seconds=10 * i), records.image_relpath(
        T0 + timedelta(seconds=10 * i)), None) for i in range(25)]
    records.insert_images(con, rows)
    monkeypatch.setattr(web_app, "MAX_IMAGES", 10)
    listed = client.get("/api/images", params={"start": iso(0), "end": iso(600)}).json()
    assert [(i["ts"] - listed[0]["ts"]) // 1000 for i in listed] == list(range(0, 250, 30))
    # A range within the limit is returned whole
    assert len(client.get("/api/images", params={"start": iso(0), "end": iso(95)}).json()) == 10


def test_public_safeguards(client):
    response = client.get("/api/sensors")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-robots-tag"].startswith("noindex")
    assert "Disallow: /" in client.get("/robots.txt").text
    too_many = {"sensors": "a", "start": iso(0), "end": iso(60), "max_points": 5001}
    assert client.get("/api/readings", params=too_many).status_code == 422


def test_database_down_gives_503(db_env):
    response = TestClient(create_app()).get("/api/sensors")
    assert response.status_code == 503
    # No details of the failure: the page may be on the public internet
    assert response.json() == {"detail": "The database is unavailable"}


def test_app_reconnects_after_hp_db_restarts(server, db_env):
    from conftest import start_hp_db

    client = TestClient(create_app())
    assert client.get("/api/sensors").json() == []
    server.kill()
    server.wait()
    assert client.get("/api/sensors").status_code == 503
    restarted = start_hp_db()
    try:
        assert client.get("/api/sensors").json() == []
    finally:
        restarted.kill()
        restarted.wait()


def test_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HP_CONFIG", str(tmp_path / "missing.toml"))
    assert web_main.load_settings() == {
        "host": "127.0.0.1", "port": 8000, "max_gap_seconds": 300, "image_rotate": 0,
    }
    config = tmp_path / "config.toml"
    monkeypatch.setenv("HP_CONFIG", str(config))
    config.write_text("[web]\nport = 8080\n")
    assert web_main.load_settings()["port"] == 8080
    config.write_text("[web]\nimage_rotate = 90\n")
    assert web_main.load_settings()["image_rotate"] == 90
    for bad in (
        'port = "eighty"', "colour = 1", "max_gap_seconds = 0",
        "image_rotate = 45", 'image_rotate = "90"', "image_rotate = 90.0", "image_rotate = false",
    ):
        config.write_text(f"[web]\n{bad}\n")
        with pytest.raises(SystemExit):
            web_main.load_settings()


def test_page_and_its_files(db_env):
    client = TestClient(create_app())
    page = client.get("/")
    assert page.status_code == 200 and "<title>Heat pump monitoring</title>" in page.text
    for path in ("static/app.js", "static/app.css", "static/plotly.min.js"):
        assert f'"{path}"' in page.text
        assert client.get(f"/{path}").status_code == 200
    assert client.get("/static/plotly.min.js").text.lstrip().startswith("/**\n* plotly.js")
    assert client.get("/static/../app.py").status_code == 404
