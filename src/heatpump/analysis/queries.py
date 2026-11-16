"""Read-only queries used by the web app and by notebooks.

Every function takes a connection from `heatpump.common.db.connect()`. Each
query is sent whole to `hp-db` with ``hp.query('...')``, so the work is done by
the server and only the result comes back. Values are written into the SQL text
(never user-supplied strings), because ``?`` parameters do not work over Quack.

Times go in as timezone-aware datetimes and come out as epoch milliseconds
(UTC), which is what a browser wants and avoids converting every row.

How readings are stored matters here (see `sensors/filter.py`): a channel is
stored only when it changes, plus a heartbeat every `max_interval` seconds. So
readings are irregular, each value holds until the next one, and a gap longer
than the heartbeat means the collector was not running.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import duckdb

DEFAULT_MAX_POINTS = 2000
# Readings further apart than this (seconds) are a gap in the data, not a held value.
# Must be longer than the longest `max_interval` in the sensors config.
DEFAULT_MAX_GAP = 300.0
LATEST_LOOKBACK = timedelta(days=1)

# Bucket widths in seconds: 1 s to 1 day, all of which divide a day evenly
BUCKET_WIDTHS = [
    1, 2, 5, 10, 15, 30,
    60, 120, 300, 600, 900, 1800,
    3600, 7200, 10800, 21600, 43200, 86400,
]  # fmt: skip


@dataclass
class Series:
    """One channel's data for a time range, ready to plot.

    `bucket_seconds` is None when `value` holds the stored readings themselves.
    Otherwise each point is one time bucket: `ts` is the middle of the bucket,
    `value` the time-weighted average, and `low`/`high` the smallest and largest
    reading in it. A None in `value` breaks the line (failed reading or a gap).
    """

    sensor_id: int
    bucket_seconds: int | None = None
    ts: list[int] = field(default_factory=list)  # epoch milliseconds, UTC
    value: list[float | None] = field(default_factory=list)
    low: list[float | None] | None = None
    high: list[float | None] | None = None


def sensors(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """The channel catalog, in sensor_id order."""
    columns = ["sensor_id", "name", "kind", "unit", "location", "description"]
    rows = _query(con, f"SELECT {', '.join(columns)} FROM sensors ORDER BY sensor_id")
    return [dict(zip(columns, row, strict=True)) for row in rows]


def latest(con: duckdb.DuckDBPyConnection, now: datetime) -> list[dict]:
    """The newest reading of each channel that has one in the day before `now`."""
    rows = _query(
        con,
        "SELECT sensor_id, epoch_ms(ts), value, quality FROM readings_cal "
        f"WHERE ts > {_ts(now - LATEST_LOOKBACK)} AND ts <= {_ts(now)} "
        "QUALIFY row_number() OVER (PARTITION BY sensor_id ORDER BY ts DESC) = 1 "
        "ORDER BY sensor_id",
    )
    return [dict(zip(["sensor_id", "ts", "value", "quality"], row, strict=True)) for row in rows]


def bucket_width(start: datetime, end: datetime, max_points: int) -> int:
    """The smallest standard bucket (seconds) that covers the range in `max_points`."""
    needed = (end - start).total_seconds() / max_points
    for width in BUCKET_WIDTHS:
        if width >= needed:
            return width
    return math.ceil(needed / 86400) * 86400


def readings(
    con: duckdb.DuckDBPyConnection,
    sensor_ids: list[int],
    start: datetime,
    end: datetime,
    max_points: int = DEFAULT_MAX_POINTS,
    max_gap: float = DEFAULT_MAX_GAP,
) -> dict[int, Series]:
    """Calibrated readings of the given channels for start <= time < end.

    A channel with at most `max_points` stored readings in the range comes back
    as stored. A channel with more is averaged into buckets, so that no channel
    returns more than about `max_points` points. Asking for a shorter range
    therefore gives finer detail, down to the stored readings.
    """
    ids = sorted({int(i) for i in sensor_ids})
    result = {i: Series(i) for i in ids}
    if not ids or end <= start:
        return result
    where_ids = f"sensor_id IN ({', '.join(map(str, ids))})"

    counts = dict(
        _query(
            con,
            f"SELECT sensor_id, count(*) FROM readings WHERE {where_ids} "
            f"AND ts >= {_ts(start)} AND ts < {_ts(end)} GROUP BY sensor_id",
        )
    )
    dense = [i for i in ids if counts.get(i, 0) > max_points]
    sparse = [i for i in ids if i not in dense]
    if sparse:
        _stored(con, result, sparse, start, end, max_gap)
    if dense:
        _bucketed(con, result, dense, start, end, bucket_width(start, end, max_points), max_gap)
    return result


def _stored(con, result, ids, start, end, max_gap) -> None:
    """Fill in the stored readings, plus the one just before and the one just
    after the range, so a line can be drawn to both edges of a graph."""
    gap = timedelta(seconds=max_gap)
    source = f"FROM readings_cal WHERE sensor_id IN ({', '.join(map(str, ids))})"
    select = "SELECT sensor_id, epoch_ms(ts) AS ms, value"
    edge = "QUALIFY row_number() OVER (PARTITION BY sensor_id ORDER BY ts {}) = 1"
    rows = _query(
        con,
        f"{select} {source} AND ts >= {_ts(start)} AND ts < {_ts(end)} "
        f"UNION ALL ({select} {source} AND ts >= {_ts(start - gap)} AND ts < {_ts(start)} "
        f"{edge.format('DESC')}) "
        f"UNION ALL ({select} {source} AND ts >= {_ts(end)} AND ts < {_ts(end + gap)} "
        f"{edge.format('ASC')}) "
        "ORDER BY sensor_id, ms",
    )
    gap_ms = max_gap * 1000
    for sensor_id, ms, value in rows:
        series = result[sensor_id]
        if series.ts and ms - series.ts[-1] > gap_ms and series.value[-1] is not None:
            # The collector was down: break the line just after the last reading
            series.ts.append(series.ts[-1] + 1)
            series.value.append(None)
        series.ts.append(ms)
        series.value.append(value)


def _bucketed(con, result, ids, start, end, width, max_gap) -> None:
    """Fill in time-weighted averages per bucket.

    Each reading holds from its own time until the next reading (or for
    `max_gap` seconds, if the next one is later than that). That span is cut at
    the bucket edges, and each piece is weighted by its length. Buckets start on
    whole multiples of `width`, so they do not move when the range changes.
    """
    s0 = math.floor(start.timestamp() / width) * width
    e0 = math.ceil(end.timestamp() / width) * width
    lookback = _ts(start - timedelta(seconds=max_gap + width))
    piece_len = f"least(b, {s0} + (i + 1) * {width}) - greatest(a, {s0} + i * {width})"
    rows = _query(
        con,
        f"""
        WITH r AS (
            SELECT sensor_id, epoch(ts) AS t, value,
                   lead(epoch(ts)) OVER (PARTITION BY sensor_id ORDER BY ts) AS next_t
            FROM readings_cal
            WHERE sensor_id IN ({", ".join(map(str, ids))})
              AND ts >= {lookback} AND ts < to_timestamp({e0})
        ), span AS (
            SELECT sensor_id, value, greatest(t, {s0}) AS a,
                   least(coalesce(next_t, {e0}), {e0}, t + {max_gap!r}, epoch(now())) AS b
            FROM r WHERE value IS NOT NULL
        ), piece AS (
            SELECT sensor_id, value, a, b,
                   unnest(range(floor((a - {s0}) / {width})::BIGINT,
                                floor((b - {s0}) / {width})::BIGINT + 1)) AS i
            FROM span WHERE b > a
        )
        SELECT sensor_id, i, sum(value * ({piece_len})) / sum({piece_len}),
               min(value), max(value)
        FROM piece WHERE {piece_len} > 0
        GROUP BY sensor_id, i ORDER BY sensor_id, i
        """,
    )
    for sensor_id in ids:
        series = result[sensor_id]
        series.bucket_seconds, series.low, series.high = width, [], []
    last_i: dict[int, int] = {}
    for sensor_id, i, avg, low, high in rows:
        series = result[sensor_id]
        if sensor_id in last_i and i > last_i[sensor_id] + 1:
            # One or more empty buckets: break the line in the first of them
            _add(series, s0, width, last_i[sensor_id] + 1, None, None, None)
        _add(series, s0, width, i, avg, low, high)
        last_i[sensor_id] = i


def _add(series: Series, s0: int, width: int, i: int, avg, low, high) -> None:
    series.ts.append(round((s0 + (i + 0.5) * width) * 1000))
    series.value.append(avg)
    series.low.append(low)
    series.high.append(high)


def images(
    con: duckdb.DuckDBPyConnection, start: datetime, end: datetime, max_images: int | None = None
) -> list[dict]:
    """Images captured in start <= time < end, oldest first.

    If the range holds more than `max_images`, every 2nd, 3rd, ... image is
    returned instead, so the answer stays about that size and still spans the range.
    """
    step = "1" if max_images is None else f"greatest(ceil(total / {int(max_images)})::BIGINT, 1)"
    rows = _query(
        con,
        "SELECT image_id, epoch_ms(ts), capture_ctx FROM ("
        "SELECT *, row_number() OVER (ORDER BY ts, image_id) AS n, count(*) OVER () AS total "
        f"FROM images WHERE ts >= {_ts(start)} AND ts < {_ts(end)}) "
        f"WHERE (n - 1) % {step} = 0 ORDER BY ts, image_id",
    )
    return [dict(zip(["image_id", "ts", "mode"], row, strict=True)) for row in rows]


def image_path(con: duckdb.DuckDBPyConnection, image_id: int) -> str | None:
    """An image's path relative to the image root, or None if there is no such image."""
    rows = _query(con, f"SELECT path FROM images WHERE image_id = {int(image_id)}")
    return rows[0][0] if rows else None


def _query(con: duckdb.DuckDBPyConnection, sql: str) -> list[tuple]:
    """Run a query on the server and fetch the result."""
    return con.execute("FROM hp.query('" + sql.replace("'", "''") + "')").fetchall()


def _ts(value: datetime) -> str:
    """A timezone-aware datetime as an SQL literal."""
    if value.tzinfo is None:
        raise ValueError("times must be timezone-aware")
    return f"TIMESTAMPTZ '{value.isoformat()}'"
