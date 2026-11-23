"""The derived `frost` sensor: estimated frost on the evaporator coil, 0 to 10.

While the evaporator fan runs, the pressure across the coil tells how blocked it
is: every 15 seconds a score is stored from the average `coil_pressure` over the
15 seconds centred on that time. Readings below 20 Pa (fan not running) are left
out of the average, and a 15 second span with no reading of 20 Pa or more gets
no pressure score.

While the fan is off, each coil image gets a score from `frost_image.py`, stored
with the image's timestamp.

Scores are ordinary rows of `readings` under the sensor name `frost`, so they
can be graphed like any other channel. `compute()` works on any time range and
is used both by `hp frost-backfill` (history) and by `hp-frost` (ongoing).
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

from heatpump.analysis.queries import _query, _ts
from heatpump.common.paths import image_root

SENSOR = "frost"
PRESSURE_SENSOR = "coil_pressure"

FAN_ON_PA = 20.0  # the fan is running at this pressure or more
ZERO_PA = 23.0  # frost score 0 at this pressure or less
FULL_PA = 80.0  # frost score 10 at this pressure or more
PERIOD = 15  # seconds between pressure scores, and the length of the averaging span
# An image is scored unless the newest pressure reading before it shows the fan
# running. A reading older than this says nothing (the pressure sensor is down).
MAX_PRESSURE_AGE = timedelta(seconds=60)
CAMERA_ID = 1

# The frost-from-image function was fitted on data from this time on, and
# `hp frost-backfill` starts here by default (1:42 PM Alaska Standard Time)
HISTORY_START = datetime(2026, 11, 16, 22, 42, tzinfo=UTC)


def pressure_to_frost(pa: float) -> float:
    """Frost score from the coil pressure with the fan running."""
    return min(10.0, max(0.0, (pa - ZERO_PA) / (FULL_PA - ZERO_PA) * 10.0))


def sensor_id(con: duckdb.DuckDBPyConnection, name: str) -> int | None:
    row = con.execute(f"SELECT sensor_id FROM hp.sensors WHERE name = '{name}'").fetchone()
    return row[0] if row else None


def register_sensor(con: duckdb.DuckDBPyConnection) -> int:
    """sensor_id of the `frost` sensor, adding it to the `sensors` table if needed."""
    found = sensor_id(con, SENSOR)
    if found is not None:
        return found
    new_id = con.execute("SELECT coalesce(max(sensor_id), 0) + 1 FROM hp.sensors").fetchone()[0]
    con.execute(
        "INSERT INTO hp.sensors (sensor_id, name, kind, unit, location, description) VALUES "
        f"({new_id}, '{SENSOR}', 'analog', 'frost 0-10', 'outdoor unit', "
        "'Estimated frost on the evaporator coil: from coil_pressure while the fan runs, "
        "from the coil image otherwise')"
    )
    print(f"Registered new sensor: {SENSOR}")
    return new_id


def pressure_scores(
    con: duckdb.DuckDBPyConnection, start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    """(time, score) for each 15 s clock time in [start, end) at which the fan ran."""
    pid = sensor_id(con, PRESSURE_SENSOR)
    if pid is None:
        return []
    half = timedelta(seconds=PERIOD / 2)
    # Each reading belongs to the clock time whose span, [t - 7.5 s, t + 7.5 s), holds it
    rows = _query(
        con,
        f"SELECT CAST(floor((epoch(ts) + {PERIOD / 2}) / {PERIOD}) AS BIGINT) * {PERIOD} AS t, "
        f"avg(value) FROM readings WHERE sensor_id = {pid} AND quality = 0 "
        f"AND value >= {FAN_ON_PA} AND ts >= {_ts(start - half)} AND ts < {_ts(end + half)} "
        "GROUP BY t ORDER BY t",
    )
    result = []
    for t, pa in rows:
        ts = datetime.fromtimestamp(t, UTC)
        if start <= ts < end:
            result.append((ts, round(pressure_to_frost(pa), 2)))
    return result


def fan_off_images(
    con: duckdb.DuckDBPyConnection, start: datetime, end: datetime
) -> list[tuple[datetime, str]]:
    """(time, path) of the coil images in [start, end) taken while the fan was not
    running, or while its state was unknown because there was no recent pressure reading."""
    pid = sensor_id(con, PRESSURE_SENSOR)
    rows = _query(
        con,
        "SELECT i.ts, i.path, p.ts, p.value FROM "
        f"(SELECT ts, path FROM images WHERE camera_id = {CAMERA_ID} "
        f" AND ts >= {_ts(start)} AND ts < {_ts(end)}) i "
        "ASOF LEFT JOIN "
        f"(SELECT ts, value FROM readings WHERE sensor_id = {pid if pid is not None else -1} "
        f" AND quality = 0 AND ts >= {_ts(start - MAX_PRESSURE_AGE)} AND ts < {_ts(end)}) p "
        "ON i.ts >= p.ts ORDER BY i.ts",
    )
    return [
        (ts, path)
        for ts, path, p_ts, pa in rows
        if not (p_ts is not None and ts - p_ts <= MAX_PRESSURE_AGE and pa >= FAN_ON_PA)
    ]


def compute(
    con: duckdb.DuckDBPyConnection, start: datetime, end: datetime, root: Path | None = None
) -> list[tuple[datetime, float]]:
    """All frost scores with a time in [start, end), in time order. Nothing is stored.

    The data for the range must be complete: `end` should be at least 7.5 s plus
    the collectors' write delay in the past.
    """
    from heatpump.analysis.frost_image import image_to_frost  # needs OpenCV

    root = root or image_root()
    scores = pressure_scores(con, start, end)
    for ts, path in fan_off_images(con, start, end):
        score = image_to_frost(root / path)
        if score is None:
            print(f"No frost score for {path}: the image is missing or unreadable")
            continue
        scores.append((ts, round(score, 2)))
    scores.sort()
    return scores


def store(con: duckdb.DuckDBPyConnection, scores: list[tuple[datetime, float]]) -> None:
    """Append frost scores to `readings`."""
    if not scores:
        return
    fid = register_sensor(con)
    # Quack: parameters do not work on hp.* tables, so go through a local temp table
    con.execute("CREATE OR REPLACE TEMP TABLE frost_buf (ts TIMESTAMPTZ, value DOUBLE)")
    con.executemany("INSERT INTO frost_buf VALUES (?, ?)", [list(s) for s in scores])
    con.execute(
        f"INSERT INTO hp.readings (ts, sensor_id, value, quality) "
        f"SELECT ts, {fid}, value, 0 FROM frost_buf ORDER BY ts"
    )


def last_score_time(con: duckdb.DuckDBPyConnection) -> datetime | None:
    """Time of the newest stored frost score, or None if there are none."""
    fid = sensor_id(con, SENSOR)
    if fid is None:
        return None
    return _query(con, f"SELECT max(ts) FROM readings WHERE sensor_id = {fid}")[0][0]


def backfill(
    con: duckdb.DuckDBPyConnection,
    start: datetime,
    end: datetime,
    root: Path | None = None,
    chunk: timedelta = timedelta(hours=6),
) -> int:
    """Replace the frost scores in [start, end) with freshly computed ones.

    Works through the range a chunk at a time, deleting a chunk's old scores just
    before storing its new ones. Returns the number of scores stored.
    """
    fid = register_sensor(con)
    count = 0
    a = start
    while a < end:
        b = min(a + chunk, end)
        scores = compute(con, a, b, root)
        con.execute(
            f"DELETE FROM hp.readings WHERE sensor_id = {fid} "
            f"AND ts >= {_ts(a)} AND ts < {_ts(b)}"
        )
        store(con, scores)
        count += len(scores)
        print(f"{a.astimezone():%Y-%m-%d %H:%M} to {b.astimezone():%Y-%m-%d %H:%M}: "
              f"{len(scores)} scores")
        a = b
    return count

