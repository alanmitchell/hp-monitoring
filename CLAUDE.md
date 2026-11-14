# Heat Pump Defrost Monitoring: Project Brief for Claude Code

Handoff from a claude.ai chat session (2026-11-13). It records decisions made, the proposed structure and schema, open issues, and how sure we are about each point. Treat items marked **[decided]** as settled. Treat items marked **[proposed]** as starting points to challenge. Items marked **[verify]** were not checked against current documentation, so confirm them before building on them.

Confidence labels (High / Medium / Low) are my own estimates, not measurements.

---

## 1. Project goal

Log data from an air-to-water heat pump to study its defrost behavior:

- **Sensor data**: power, temperatures (including metering-grade supply/return water ΔT), equipment on/off status.
- **Coil images**: periodic JPGs of frost on the outdoor unit's evaporator coil.
- **Analysis and presentation**: graph sensor data, view the images that line up in time with graph points, and compute a quantitative frost measure per image.

Owner: Alan Mitchell, self-employed Python web developer (FastAPI, Django). Intermediate Python skill. Uses `uv` on Linux. Prefers dependencies that install as Python packages.

## 2. Decisions already made [decided]

| Topic | Decision |
|---|---|
| Language | Python for all collection and analysis code |
| Database | DuckDB for all non-image data |
| Images | JPG files on disk, one database row per image (filename, timestamp, optional frost measure) |
| Hardware | Ubuntu Server (26.04) on a mini-PC with an Intel N150 CPU; collection and web server run on this one machine |
| Processes | Separate processes for camera capture and for other sensors |
| Remote access | Small web server on the mini-PC serves data views to other machines on Alan's Tailscale network |
| Multi-process DB access | DuckDB's Quack remote protocol (DuckDB 2.0 LTS, released 2026-10-21, reportedly has a "polished" Quack) |
| Sensor calibration | Strap-on sensors on 3/4" copper under insulation, calibrated as a pair; recalibrate on any sensor replacement |

Sampling rates (from Alan):

- Sensor data: up to 1 s intervals during defrost, slower during normal operation.
- Images: about every 10 s during defrost, every couple of minutes while the compressor runs, every 15-30 min when idle.

## 3. Project structure [proposed]

**One repository, one installable package, several console-script entry points.** Confidence: High (~85%). The collectors, web app and analysis code all share the schema, config, time handling and calibration logic. Splitting them into separate projects would mean versioning and syncing that shared code, which is real cost for a one-person project.

```
heatpump/
  pyproject.toml            # uv-managed; console scripts + optional dependency groups
  uv.lock                   # pin DuckDB exactly
  src/heatpump/
    common/                 # config, schema.py (DDL + schema version), Quack client helper,
                            #   UTC time helper, logging setup, spool-to-disk fallback
    dbserver/               # hp-db: opens the .duckdb file and runs quack_serve
    sensors/                # hp-sensors: driver interface, real drivers, fake driver, collector loop
    camera/                 # hp-camera: capture loop, file naming, image-record creation
    analysis/               # queries, ΔT and defrost-cycle detection, frost metrics, plotting
    web/                    # hp-web: thin FastAPI layer (JSON endpoints, image serving, static JS)
  config/                   # example config only; real config and calibration data stay out of git
  deploy/                   # systemd unit files
  tests/
```

Console scripts: `hp-db`, `hp-sensors`, `hp-camera`, `hp-web`, plus a CLI for ad-hoc analysis and migrations.

Structural rules:

1. **Collectors stay dumb.** They capture and persist raw data with minimal logic. Derived values (ΔT, defrost detection, frost metrics, calibrated values) are computed in `analysis/` from raw data, so they can be re-run after a calibration change.
2. **Logic lives in `analysis/`, not `web/`.** The web app and notebooks call the same functions.
3. **Optional dependency groups** (`[sensors]`, `[camera]`, `[web]`, `[analysis]`) so a laptop can install only `[analysis]` and attach to the mini-PC over Tailscale.
4. **Sensor drivers sit behind a small interface**, with a fake driver for tests and development without hardware.
5. **One time helper.** All timestamps are UTC (`TIMESTAMPTZ`), produced by one shared function. Image-to-sensor alignment is the key join, so clock consistency matters more than any other schema detail. Run `chrony` or `systemd-timesyncd` on the mini-PC.
6. **Image paths in the database are relative** to one configured image root, so the archive can be moved without rewriting rows.
7. **Pin the DuckDB version exactly** and use the same version on every machine that connects over Quack. Mixed-version behavior over Quack was not found in documentation. Do not assume it works.

## 4. Process layout [proposed]

Run as systemd units on the mini-PC, each with its own log file and restart policy:

1. `hp-db`: owns the `.duckdb` file, runs `quack_serve`, does nothing else.
2. `hp-sensors`: reads sensors, batches inserts into `hp-db` over localhost.
3. `hp-camera`: saves the JPG, then inserts the image row.
4. `hp-web`: FastAPI, a Quack client, serves views to the tailnet.

Why a dedicated `hp-db` process rather than letting the sensor process own the file: sensor drivers and camera code are the most likely to hang or crash. Restarting an owner process would drop every other connection. **Confidence in this choice: Medium (~60%).** The alternative (sensor process writes locally and also serves Quack) avoids HTTP on the hot path and has one fewer process. Expected load is about 20 rows/s batched plus one image row per 10 s, so I expect localhost HTTP overhead to be negligible, but I have not measured it.

**Required safeguard:** every collector must spool to a local file (JSONL or Parquet) if an insert fails, and replay on reconnect, so a database outage does not lose data. [High confidence this is needed.]

**Fallback if Quack proves flaky** (any of these means the schema below still works unchanged):

- Logger writes time-partitioned Parquet files and analysis queries them directly.
- Analysis opens a periodically copied snapshot of the `.duckdb` file.
- Collectors append to raw files and a separate ingest step loads DuckDB.

## 5. Proposed DuckDB schema [proposed]

Design notes:

- **Readings are in long format** (one row per sensor sample), not one wide column per sensor. Sensors have different sample rates, so a wide table would be mostly NULLs. At ~20 channels and 1 Hz this is about 1.7M rows/day, small for DuckDB.
- **Store raw values**, apply calibration in a view, so recalibration never rewrites data.
- Insert in time order so DuckDB's zonemaps make time-range filters fast. Do not add an index on `readings`.
- The sensor count (10-30) and camera count (1-2) are my assumptions, not facts from Alan.

```sql
-- Schema version, for migrations
CREATE TABLE schema_info (
    version     INTEGER NOT NULL,
    applied_at  TIMESTAMPTZ DEFAULT now()
);

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
    offset_c     DOUBLE DEFAULT 0,          -- or generic coefficients; choose when the method is known
    gain         DOUBLE DEFAULT 1,
    pair_group   VARCHAR,                   -- ties supply/return sensors calibrated together
    notes        VARCHAR,
    PRIMARY KEY (sensor_id, valid_from)
);

-- Raw readings (on/off stored as 0/1)
CREATE TABLE readings (
    ts          TIMESTAMPTZ NOT NULL,       -- UTC
    sensor_id   USMALLINT NOT NULL,
    value       DOUBLE,
    quality     UTINYINT DEFAULT 0          -- 0 ok, 1 suspect, 2 missing/failed read, ...
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
    capture_ctx  VARCHAR                    -- 'defrost' | 'running' | 'idle' (what the scheduler believed)
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

-- Derived: operating mode intervals, the key table for alignment
CREATE TABLE mode_intervals (
    cycle_id   BIGINT,                      -- groups a heating run with its defrost
    mode       VARCHAR NOT NULL,            -- 'heating' | 'defrost' | 'off' | ...
    start_ts   TIMESTAMPTZ NOT NULL,
    end_ts     TIMESTAMPTZ                  -- NULL while in progress
);

-- Derived: downsampled data so the web UI does not ship raw 1 Hz data for long ranges
CREATE TABLE readings_1min (
    bucket_ts   TIMESTAMPTZ NOT NULL,
    sensor_id   USMALLINT NOT NULL,
    n           UINTEGER,
    avg_value   DOUBLE,
    min_value   DOUBLE,
    max_value   DOUBLE,
    PRIMARY KEY (bucket_ts, sensor_id)
);

-- Calibrated view (example; extend once the calibration form is settled)
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
```

Schema open points:

- **On/off status**: logging it as 0/1 in `readings` is simplest. Logging only on change is cheaper but needs forward-fill logic. Pick one convention. Suggested default: log as 0/1 at the sensor's normal rate, and derive `mode_intervals` from it.
- **`mode_intervals` derivation** depends on which signals exist (reversing valve, compressor power, coil temperature). I cannot say which derivation will be reliable until the real sensor set and some defrost data exist. Confidence the table is useful: High. Confidence in any particular derivation rule: Low.
- **Primary keys in DuckDB** add index overhead on insert. They are used only on the small tables here. If `images` insert rate is ever a problem (it should not be at one row per 10 s), drop the constraint.
- **Calibration form** (offset and gain vs. polynomial) is undecided. Alan's pair calibration process determines it.

### Aligning images with the graph

Nearest sensor reading at or before each image:

```sql
SELECT i.path, i.ts, r.value AS coil_temp
FROM images i
ASOF JOIN readings_cal r
  ON r.name = 'coil_temp' AND i.ts >= r.ts;
```

Viewer flow: query readings for the graphed window, query `images` for the same window, and on hover fetch the nearest image with `ORDER BY abs(epoch(ts) - epoch(?)) LIMIT 1`. **[verify]** The `ASOF JOIN` syntax above is from memory of DuckDB 1.x and was not re-tested on 2.0.

## 6. Quack usage [verify]

The pattern discussed:

```python
# hp-db process: the only process that opens the file
con = duckdb.connect("heatpump.duckdb")
con.execute("CALL quack_serve('quack:localhost', token='...')")

# client (collector, web app, notebook)
con = duckdb.connect()
con.execute("ATTACH 'quack:localhost' AS hp (TOKEN '...', DISABLE_SSL true)")
con.sql("SELECT ... FROM hp.readings WHERE ts BETWEEN ... ")
# hp.query('...') ships the whole query to the server so only results return
```

**Treat this snippet as unverified.** It came from the *beta* Quack documentation (DuckDB 1.5.3 era). DuckDB 2.0 changed Quack and added a `CONNECT` statement. First task in Claude Code: read the current 2.0 Quack docs and correct the syntax, port, token handling and TLS defaults.

Known uncertainties:

- Sustained write throughput and several concurrent writer clients under real load have no independent reports I could find. The load here is light, so I expect it to work (Medium confidence). Test with a few days of real logging before relying on it.
- Behavior when `hp-db` restarts (client reconnect, in-flight batches) is unverified. The spool-and-replay safeguard covers this.
- Tailscale encrypts traffic, so plain HTTP between tailnet machines is a defensible choice. This is a judgment call, not a verified security conclusion. Keep the Quack token regardless.

## 7. Packages

Install through `uv`; group per entry point.

| Group | Packages | Notes |
|---|---|---|
| core | `duckdb` (pin exact 2.0.x), `pydantic` + `pydantic-settings`, `pyarrow` | Config and schema validation; Arrow for DuckDB interchange and Parquet spooling |
| sensors | hardware-specific (unknown) | See open question on sensor hardware below |
| camera | `opencv-python-headless`, `pillow` | Headless build avoids GUI dependencies on a server. Camera type is unknown (USB/UVC vs. network/RTSP) |
| analysis | `polars` or `pandas`, `numpy`, `scipy`, `plotly`, `scikit-image` (optional) | Choose polars or pandas; Alan's experience level and existing habits should decide. DuckDB works with both |
| web | `fastapi`, `uvicorn`, `jinja2` (optional) | Serve JSON endpoints and images; use `plotly.js` or `uPlot` client-side |
| dev | `pytest`, `ruff`, `mypy` (optional) | Fake sensor driver enables tests without hardware |

Presentation choices for the web UI:

- Plotly is the lowest-effort route to interactive time-series graphs with a hover/click hook. uPlot is much lighter and faster for dense 1 Hz data but needs more hand-written JS. **Suggested default: Plotly first; switch only if long ranges are slow.**
- Downsample on the server using `readings_1min` so the browser never receives raw 1 Hz data for multi-day ranges.
- Serve images through FastAPI (`FileResponse`) from the image root. Consider thumbnails if browsing many images becomes slow.

Frost metric options, simplest first: grayscale thresholding of the coil region, edge density, or color/brightness statistics within a fixed ROI. A neural-network estimator is possible but unnecessary to start. Simple OpenCV metrics should run fine on the N150 (**not benchmarked**). A fixed camera position and fixed ROI will matter more than the algorithm. Plan for lighting variation (day/night, snow) as the main source of error, and consider IR illumination for night images.

## 8. Open questions for Alan

1. What sensor hardware and interfaces? (1-Wire/DS18B20, I2C ADC, USB DAQ, Modbus power meter, GPIO inputs...) This determines the `sensors/` driver code and any additional packages.
2. What camera, and how is it connected? Is it fixed-position with lighting at night?
3. How many sensor channels and cameras, really? (I assumed 10-30 and 1-2.)
4. Expected retention and image volume: roughly how much disk, and when are old JPGs pruned?
5. Does the web UI need to show live data during a defrost cycle, or is after-the-fact review enough? (Live data raises the stakes for Quack reliability.)
6. How is the web server secured beyond Tailscale membership? Tailscale ACLs, or an application token?

## 9. Suggested first steps in Claude Code

1. Read current DuckDB 2.0 Quack docs; fix section 6 to match.
2. `uv init` the package with the layout in section 3; create `common/schema.py` with the DDL above and a schema-version migration mechanism.
3. Build `hp-db`, a fake sensor driver, and a collector that writes through Quack. Test with spool-and-replay by killing `hp-db` mid-run.
4. Add `hp-camera` with a fake or file-based image source; insert image rows.
5. Build `analysis/` queries and the `ASOF JOIN` alignment; check them against synthetic data.
6. Add `hp-web` with one graph and a hover-to-image view.
7. Add real sensor drivers once the hardware questions are answered.
8. Run a multi-day soak test on the mini-PC before relying on the setup.
