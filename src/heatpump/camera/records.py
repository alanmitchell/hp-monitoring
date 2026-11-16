"""Image file naming and the rows of the `images` table.

Used by `hp-camera` (a row per captured image) and by `hp index-images` (rows for
JPGs already on disk). OpenCV is not imported here, so indexing works without the
camera extra.

Files are stored under the image root as <camera>/<local date>/<local time>.jpg,
for example ``coil/2026-11-13/20-32-15.jpg``. The file name carries the capture
time to the second, so the database row can always be rebuilt from the file.
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from heatpump.common import db

CAMERA = "coil"
# camera folder name -> camera_id in the images table
CAMERA_IDS = {"coil": 1}

ROWS_PER_INSERT = 500

_NAME_RE = re.compile(r"(\d\d)-(\d\d)-(\d\d)(b?)\.jpg")


def image_relpath(ts: datetime, camera: str = CAMERA) -> Path:
    """Path of an image captured at `ts` (timezone-aware), relative to the image root.

    Folder and file names use local time so people can browse them. During the
    hour that repeats when daylight saving time ends, files from the second pass
    through the hour get a 'b' suffix (01-30-00b.jpg) so they do not collide
    with the first pass.
    """
    local = datetime.fromtimestamp(ts.timestamp())  # naive local time; fold=1 on the repeat
    suffix = "b" if local.fold else ""
    return Path(camera) / f"{local:%Y-%m-%d}" / f"{local:%H-%M-%S}{suffix}.jpg"


def ts_from_relpath(relpath: Path) -> datetime | None:
    """The reverse of `image_relpath`: UTC capture time from a path, or None if the
    path does not follow the naming scheme.

    Must run in the time zone the images were captured in.
    """
    if len(relpath.parts) != 3 or relpath.parts[0] not in CAMERA_IDS:
        return None
    match = _NAME_RE.fullmatch(relpath.parts[2])
    if not match:
        return None
    hour, minute, second, repeat = match.groups()
    try:
        day = datetime.strptime(relpath.parts[1], "%Y-%m-%d")
        local = day.replace(
            hour=int(hour), minute=int(minute), second=int(second), fold=1 if repeat else 0
        )
    except ValueError:
        return None
    return local.astimezone(UTC)


def jpeg_size(path: Path) -> tuple[int, int] | None:
    """(width, height) read from a JPG's header, without decoding the image."""
    with path.open("rb") as f:
        if f.read(2) != b"\xff\xd8":
            return None
        while True:
            # Each segment is FF, a marker byte, then a 2-byte length that includes itself
            byte = f.read(1)
            while byte == b"\xff":
                byte = f.read(1)
            if not byte:
                return None
            marker = byte[0]
            if marker == 0x01 or 0xD0 <= marker <= 0xD8:
                continue  # markers that have no length
            length = int.from_bytes(f.read(2), "big")
            if length < 2:
                return None
            # Start-of-frame segments hold the size; C4, C8 and CC are other things
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                data = f.read(5)
                if len(data) < 5:
                    return None
                return int.from_bytes(data[3:5], "big"), int.from_bytes(data[1:3], "big")
            f.seek(length - 2, 1)


def image_row(
    ts: datetime, relpath: Path, size: tuple[int, int] | None, capture_ctx: str | None = None
) -> dict:
    """One `images` row as a plain dict (also the spool file's line format).

    `capture_ctx` is the heat pump mode `hp-camera` believed in when it took the image.
    """
    width, height = size or (None, None)
    return {
        "ts": ts.isoformat(),
        "camera_id": CAMERA_IDS[relpath.parts[0]],
        "path": relpath.as_posix(),
        "width": width,
        "height": height,
        "capture_ctx": capture_ctx,
    }


def insert_images(con: duckdb.DuckDBPyConnection, rows: list[dict]) -> int:
    """Insert rows into hp.images, skipping paths that are already there.

    Returns the number inserted. Skipping known paths makes it safe to insert the
    same rows twice, so indexing can be re-run and a spool file can be replayed.
    """
    if not rows:
        return 0
    # Quack: parameters do not work on hp.* tables, and hp.images cannot be read
    # and inserted into in one statement. So find the new rows in local temp tables.
    con.execute(
        "CREATE OR REPLACE TEMP TABLE new_images (ts TIMESTAMPTZ, camera_id USMALLINT, "
        "path VARCHAR, width USMALLINT, height USMALLINT, capture_ctx VARCHAR)"
    )
    con.executemany(
        "INSERT INTO new_images VALUES (?, ?, ?, ?, ?, ?)",
        [
            [datetime.fromisoformat(r["ts"]), r["camera_id"], r["path"], r["width"], r["height"],
             r.get("capture_ctx")]
            for r in rows
        ],
    )
    times = [datetime.fromisoformat(r["ts"]) for r in rows]
    con.execute(
        "CREATE OR REPLACE TEMP TABLE known_images AS SELECT path FROM hp.images "
        f"WHERE ts BETWEEN {_sql(min(times))} AND {_sql(max(times))}"
    )
    new = con.execute(
        "SELECT DISTINCT ON (path) ts, camera_id, path, width, height, capture_ctx FROM new_images "
        "WHERE path NOT IN (SELECT path FROM known_images) ORDER BY ts"
    ).fetchall()
    # Quack again: INSERT ... SELECT from a local table fails on hp.images, because
    # the image_id default is a sequence the client cannot see ("Sequence with name
    # image_id_seq does not exist"). An INSERT with the values written out works,
    # and the server then assigns the image_id.
    for i in range(0, len(new), ROWS_PER_INSERT):
        values = ", ".join(
            "(" + ", ".join(_sql(v) for v in r) + ")" for r in new[i : i + ROWS_PER_INSERT]
        )
        con.execute("INSERT INTO hp.images (ts, camera_id, path, width, height, capture_ctx) "
            f"VALUES {values}")
    return len(new)


def _sql(value) -> str:
    """A Python value written as an SQL literal."""
    if value is None:
        return "NULL"
    if isinstance(value, datetime):
        return f"TIMESTAMPTZ '{value.isoformat()}'"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return str(int(value))


class ImageSpool:
    """Gets image rows into the database without losing any.

    Each row is appended to a file on disk first, then the file is loaded into
    `hp-db` and deleted. While the database is down the file just grows, and it
    is loaded once the database is back.
    """

    def __init__(self, path: Path):
        self.path = path
        self._con: duckdb.DuckDBPyConnection | None = None
        self._db_ok = True  # so the "database down" message prints once per outage
        path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, row: dict) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps(row) + "\n")

    def load(self) -> None:
        """Try to move the spooled rows into the database."""
        rows = self._read()
        if not rows:
            self.path.unlink(missing_ok=True)
            return
        try:
            if self._con is None:
                self._con = db.connect()
            insert_images(self._con, rows)
            self.path.unlink()
            if not self._db_ok:
                print(f"Database is back; {len(rows)} spooled image records loaded")
            self._db_ok = True
        except duckdb.Error as e:
            if self._db_ok:
                print(f"Database unavailable, image records are kept in {self.path}: {e}")
            self._db_ok = False
            self.close()

    def close(self) -> None:
        if self._con is not None:
            try:
                self._con.close()
            except duckdb.Error:
                pass
            self._con = None

    def _read(self) -> list[dict]:
        try:
            lines = self.path.read_text().splitlines()
        except FileNotFoundError:
            return []
        rows = []
        for line in lines:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # a line cut short by a crash; `hp index-images` can recover it
        return rows
