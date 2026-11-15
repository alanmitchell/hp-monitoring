"""Database schema (DDL) and the migration mechanism.

`MIGRATIONS[n - 1]` is the SQL that takes a database from schema version n - 1 to
version n. To change the schema, append a new entry; never edit an entry that has
already been applied to a real database. `hp-db` calls `migrate()` at startup.
"""

import duckdb

V1 = """
-- Catalog of measurement channels
CREATE TABLE sensors (
    sensor_id    USMALLINT PRIMARY KEY,
    name         VARCHAR UNIQUE NOT NULL,   -- 'supply_temp', 'return_temp', 'power_kw', ...
    kind         VARCHAR NOT NULL,          -- 'analog' | 'binary'
    unit         VARCHAR,
    location     VARCHAR,
    description  VARCHAR
);

-- Calibration versions per sensor (supply/return pair calibrated together)
CREATE TABLE calibrations (
    sensor_id    USMALLINT NOT NULL,
    valid_from   TIMESTAMPTZ NOT NULL,
    valid_to     TIMESTAMPTZ,               -- NULL = current
    offset_c     DOUBLE DEFAULT 0,
    gain         DOUBLE DEFAULT 1,
    pair_group   VARCHAR,                   -- ties supply/return sensors calibrated together
    notes        VARCHAR,
    PRIMARY KEY (sensor_id, valid_from)
);

-- Raw readings (on/off stored as 0/1). No index: rows arrive in time order,
-- so DuckDB's zonemaps already make time-range filters fast.
CREATE TABLE readings (
    ts          TIMESTAMPTZ NOT NULL,       -- UTC
    sensor_id   USMALLINT NOT NULL,
    value       DOUBLE,
    quality     UTINYINT DEFAULT 0          -- 0 ok, 1 suspect, 2 missing/failed read
);

-- One row per JPG
CREATE SEQUENCE image_id_seq;
CREATE TABLE images (
    image_id     BIGINT PRIMARY KEY DEFAULT nextval('image_id_seq'),
    ts           TIMESTAMPTZ NOT NULL,      -- capture time, UTC
    camera_id    USMALLINT NOT NULL,
    path         VARCHAR NOT NULL,          -- relative to the configured image root
    width        USMALLINT,
    height       USMALLINT,
    capture_ctx  VARCHAR                    -- 'defrost' | 'running' | 'idle'
);

-- Frost measures, separate so several methods can coexist and be recomputed
CREATE TABLE image_metrics (
    image_id        BIGINT NOT NULL,
    metric          VARCHAR NOT NULL,       -- 'frost_fraction', 'edge_density', ...
    value           DOUBLE,
    method_version  VARCHAR NOT NULL,
    computed_at     TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (image_id, metric, method_version)
);

-- Derived: operating mode intervals
CREATE TABLE mode_intervals (
    cycle_id   BIGINT,                      -- groups a heating run with its defrost
    mode       VARCHAR NOT NULL,            -- 'heating' | 'defrost' | 'off' | ...
    start_ts   TIMESTAMPTZ NOT NULL,
    end_ts     TIMESTAMPTZ                  -- NULL while in progress
);

-- Derived: downsampled data for long time ranges in the web UI
CREATE TABLE readings_1min (
    bucket_ts   TIMESTAMPTZ NOT NULL,
    sensor_id   USMALLINT NOT NULL,
    n           UINTEGER,
    avg_value   DOUBLE,
    min_value   DOUBLE,
    max_value   DOUBLE,
    PRIMARY KEY (bucket_ts, sensor_id)
);

-- Readings with the calibration that was valid at the time applied
CREATE VIEW readings_cal AS
SELECT r.ts, r.sensor_id, s.name,
       CASE WHEN s.kind = 'analog'
            THEN coalesce(c.gain, 1) * r.value + coalesce(c.offset_c, 0)
            ELSE r.value END AS value,
       r.quality
FROM readings r
JOIN sensors s USING (sensor_id)
LEFT JOIN calibrations c
  ON c.sensor_id = r.sensor_id
 AND r.ts >= c.valid_from
 AND (c.valid_to IS NULL OR r.ts < c.valid_to);
"""

MIGRATIONS = [V1]
SCHEMA_VERSION = len(MIGRATIONS)


def current_version(con: duckdb.DuckDBPyConnection) -> int:
    """Schema version of the database; 0 for a new, empty database."""
    con.execute(
        "CREATE TABLE IF NOT EXISTS schema_info ("
        "version INTEGER NOT NULL, applied_at TIMESTAMPTZ DEFAULT now())"
    )
    return con.execute("SELECT coalesce(max(version), 0) FROM schema_info").fetchone()[0]


def migrate(con: duckdb.DuckDBPyConnection) -> int:
    """Bring the database up to SCHEMA_VERSION. Returns the version it started at."""
    start = current_version(con)
    if start > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database is at schema version {start}, but this code only knows "
            f"version {SCHEMA_VERSION}. Update the code before running it."
        )
    for version in range(start + 1, SCHEMA_VERSION + 1):
        # Each step is all-or-nothing, so a failed migration leaves the old schema intact
        con.execute("BEGIN")
        try:
            con.execute(MIGRATIONS[version - 1])
            con.execute("INSERT INTO schema_info (version) VALUES (?)", [version])
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    return start
