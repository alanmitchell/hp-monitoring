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

Viewer flow: query readings for the graphed window, query `images` for the same window, and on hover fetch the nearest image with `ORDER BY abs(epoch(ts) - epoch(?)) LIMIT 1`. The `ASOF JOIN` above was run on DuckDB 2.0.0 on 2026-11-15, both against `hp.*` tables and through `hp.query(...)`, and works as written.

## 6. Quack usage [verified on DuckDB 2.0.0, 2026-11-14]

Checked against the 2.0 docs (duckdb.org/docs/2.0/quack/) and by running it.
Implemented in `dbserver/main.py` and `common/db.py`.

```python
# hp-db process: the only process that opens the file
con = duckdb.connect("heatpump.duckdb")
con.execute("CALL quack_serve('quack:localhost', token = '...')")  # returns at once
...
con.execute("CALL quack_stop('quack:localhost')")

# client (collector, web app, notebook): heatpump.common.db.connect()
con = duckdb.connect()
con.execute("CREATE SECRET (TYPE quack, TOKEN '...', SCOPE 'quack:localhost')")
con.execute("ATTACH 'quack:localhost' AS hp (TYPE quack)")
con.sql("SELECT ... FROM hp.readings WHERE ts BETWEEN ... ")
con.sql("FROM hp.query('SELECT ...')")   # ships the whole query to the server
```

Facts:

- Default port 9494. `quack:localhost` binds to localhost only and uses plain
  HTTP. Non-local URIs need `allow_other_hostname = true` on the server and
  default to HTTPS with a self-signed certificate (`disable_ssl`, `ssl_cert_file`,
  `ssl_key_file` on the server; `DISABLE_SSL`, `SSL_FINGERPRINT` on ATTACH). The
  docs recommend a TLS reverse proxy over exposing Quack directly. hp-db
  currently serves localhost only; laptop access over Tailscale is not built yet.
- The extension autoinstalls on first use (needs internet once per machine).
- `?` parameters fail in statements touching `hp.*` tables. Insert via a local
  temp table: `INSERT INTO hp.readings SELECT * FROM buf`.
- One statement cannot scan an `hp.*` table and insert into another `hp.*` table
  ("Multiple streaming scans ... not currently supported"). Local sources
  (temp tables, `read_json` of a local file) inserted into `hp.*` work.
- `INSERT INTO hp.images SELECT ... FROM <local table>` fails with "Sequence
  with name image_id_seq does not exist", even when `image_id` is supplied,
  because of the column's sequence default. `INSERT ... VALUES (literals)` works
  and the server assigns the id (`camera/records.py` does this).
  `hp.query('SELECT nextval(...)')` also works.
- Server and client in one Python process hang. Use separate processes.
- `FROM hp.query('SELECT ...')` runs window functions, `QUALIFY`, `ASOF JOIN`,
  the `readings_cal` view and `INSERT ... SELECT` on the server. Checked
  2026-11-15. `analysis/queries.py` sends every query this way.
- Cursors of one client connection cannot run queries at the same time from
  several threads: all but one fail with "INTERRUPT Error: superseded by a new
  query". Use a lock around the connection, or one connection per thread.
- `CONNECT hp` exists, but Python's `con.sql()` fails while connected. Use ATTACH.
- `hp-db` runs `CHECKPOINT` every hour (`HP_DB_CHECKPOINT_SECONDS`; Alan asked
  for this 2026-11-16). It works with Quack clients connected. A copy of the
  `.duckdb` file alone, made while `hp-db` runs, opens and holds the data up to
  the last checkpoint (tested). A copy made during a checkpoint is not tested
  and may be inconsistent.
- Fetching TIMESTAMPTZ into Python needs `pytz` (now a core dependency).
- Client reconnect is off by default (`quack_enable_reconnects`). Untested here.
- Client/server version compatibility is still undocumented. Keep the exact pin.

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

1. ~~What sensor hardware and interfaces?~~ Answered 2026-11-14; see section 10.
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


## 10. Sensors package plan [decided 2026-11-14]

Agreed with Alan. Step 1 is built and tested; drivers follow.

### Hardware (all USB on the mini-PC)

| Device | Connection | How it is read | Fastest rate |
|---|---|---|---|
| LabJack U3 | USB | polled; several analog and digital channels | 1 s |
| Energy Conservatory DG-700 pressure gauge | serial | polled | 10 s |
| Heat pump controller | serial, own USB adapter | Modbus; probably **listen-only** on the controller-to-display link (not yet investigated) | 1 s |
| WattNode power meter | serial, own USB adapter | Modbus, mini-PC is master | 1 s |
| Adafruit CircuitPython board, 2 RTDs | serial | board streams supply/return pairs, both on one line | 1 s |

### Design

- **One `hp-sensors` process, one thread per port/device, one writer thread.**
  Threads because the work is blocking I/O and one process means one Quack
  connection, one spool and one systemd unit. `--only DEVICE` runs a subset, so
  a troublesome device can be moved to its own process later.
- **Driver interface** (`sensors/device.py`): subclass `Device`, implement
  `read() -> {channel_name: raw value}`. Polled drivers are called every
  `period` seconds on wall-clock boundaries. Streaming drivers
  (`streaming = True`) block in `read()` until data arrives. `read()` must
  always time out; any exception stores a `quality = 2` row, closes the device
  and reopens it with backoff (1 s doubling to 30 s).
- **Change filter** (`sensors/filter.py`), per channel: optional `deadband`
  plus `max_interval`. No deadband means store every sample. On a change the
  last skipped sample is stored too. `report_together = true` on a device stores
  all its channels whenever one triggers (for the supply/return RTD pair, so
  the ΔT error is not doubled). Sampling is at a fixed fast rate; the filter
  replaces "fast during defrost, slow otherwise". Filtering is on raw values.
- **Consequences for analysis**: readings are irregular, so averages and
  `readings_1min` must be time-weighted. A gap longer than `max_interval` means
  the collector was down. On/off channels are 0/1 with change-plus-heartbeat,
  which settles the on/off open point in section 5.
- **Writer** (`sensors/writer.py`): spool-first. Each batch is appended to a
  JSONL file under `~/hp-data/spool/`, then spool files are loaded with
  `read_json` and deleted. Known limit: a crash between insert and delete
  duplicates that file's rows.
- **Watchdog**: every thread updates `last_alive`; if one is stale for
  `watchdog_seconds` the process exits and systemd restarts it.
- **Config**: `[sensors]` in `config/config.toml` (plain `tomllib` and
  dataclasses, no pydantic). Channel names are unique across devices and are
  registered in the `sensors` table automatically.
- **Serial ports** must be named by `/dev/serial/by-id/...`, never `ttyUSB0`.
- **Modbus**: `modbus_master.py` for the WattNode; a separate hand-written
  `modbus_listen.py` for the controller (pair each request with its response,
  find frames by CRC, never transmit), sharing decoding code. Before the
  listener, write a capture tool to discover serial settings and registers.
  Log the WattNode's cumulative energy register so energy totals stay exact.

- **Transforms** (`sensors/transform.py`, decided 2026-11-15): a channel's
  `transform` is a formula applied between the driver and the change filter, so
  **engineering values are stored, not raw values** (Alan's preference; this
  overrides rule 1 in section 3 for sensor conversions; calibration corrections
  still belong in the database). `x` is the channel's raw reading; other names
  are channels on the same device or whitelisted functions. `store = false`
  channels are inputs only. Formulas are validated at startup. Raw values can be
  kept by adding an untransformed channel on the same pin. Cross-device
  derivations belong in `analysis/`.
- **`thermistor()` helper**: returns degrees F. Alan enters Steinhart-Hart
  coefficients per channel and his produce degrees Rankine (`scale='rankine'`).
  Thermistor is the ground leg of the divider by default; `r_fixed` per channel.
  The dividers are powered by an LM4040 2.048 V reference, read on its own
  analog channel, which is inside the U3-LV's 0 to 2.44 V input range.
- **`config_history` table** (schema version 2): `hp-sensors` stores the config
  file text whenever it changes, since stored values depend on it.
- **LabJack is a U3-LV** (`devices/labjack_u3.py`): generic driver; each channel
  names a `pin` (AIN0-15 analog, FIO/EIO/CIO digital). No pulse inputs: digital
  values hold for over a second, so polling is enough. Needs the Exodriver
  system library (not a Python package).

### Build order

1. ~~Framework, fake device, change filter, writer with spool-and-replay~~ (done)
2. ~~DG-700 driver~~ (written from Alan's sample script and tested against a simulated gauge; not yet run on the real gauge)
3. RTD stream driver; Modbus master driver for the WattNode
4. ~~LabJack U3 driver~~ (tested against a simulated U3; not yet run on the real device)
5. Heat pump controller: capture tool, then listener driver


## 11. hp-web plan [decided 2026-11-15]

Agreed with Alan. Steps 1 to 3 are built and tested; the page (step 4) is written but unchecked in a browser.

### Decisions

- **No live view for now.** After-the-fact review is enough (answers question 5
  in section 8).
- **Access**: `hp-web` binds to localhost. Originally `tailscale serve` (tailnet
  only); on 2026-11-16 Alan asked for it to be public, so **Tailscale Funnel**
  publishes it to the internet. Still no application login (answers question 6
  in section 8). Because it is public: the app is read-only, answers are capped
  (5,000 points per channel, 5,000 image records, thinned evenly), 503 errors
  carry no details, and headers plus `robots.txt` ask not to be indexed. There
  is no rate limit.
- **Plotly** for the graph, with no JS build step. Check whether the `plotly`
  Python package ships `plotly.min.js`, so it can be served from there.
- **Downsampling at query time**, not from `readings_1min`. Each request names a
  time range and a point budget (about 2,000 per series). The server picks the
  bucket size and returns time-weighted average, min and max per bucket; short
  ranges return the stored rows.
- **Zoom re-fetches.** On a Plotly zoom or pan the page requests the new visible
  range with the same budget, so resolution follows the view down to the stored
  data. Requests are debounced, the old trace stays until new data arrives, the
  fetch covers a margin beyond the view, and min/max are drawn as a band so
  short spikes show when zoomed out.
- **Image panel**: the page holds the image list (id, ts) for the window and
  finds the nearest image in the browser. Hover shows it, click pins it, arrow
  keys step. Images are served by `image_id`, with the path looked up in the
  database.
- **Queries live in `analysis/queries.py`**; `web/` only parses parameters and
  returns JSON. SQL is built from validated values (datetimes, integer ids)
  because `?` parameters fail on `hp.*`.
- API times are UTC; the page shows the browser's local time.

### Build order

1. ~~`hp-camera` writes `images` rows through a spool; `hp index-images`
   backfills JPGs already on disk~~ (done; `camera/records.py`, `cli.py`.
   Rows are deduplicated by path. Not yet run on the mini-PC.)
2. ~~`analysis/queries.py`: sensors, readings (bucketed), latest values, images~~
   (done and tested against a real `hp-db`. `readings()` returns stored rows
   for a channel with at most `max_points` rows in the range, else buckets of a
   standard width, 1 s to 1 day, aligned to multiples of the width. A value
   holds until the next reading, for at most `max_gap` seconds (default 300;
   must exceed every channel's `max_interval`); longer silences break the line.
   Times come back as epoch milliseconds. Six channels over 3 days of 1 Hz
   data took 0.13 s on the development machine, not the N150.)
3. ~~Web API: `/api/sensors`, `/api/latest`, `/api/readings`, `/api/images`,
   `/images/{image_id}`~~ (done; `web/app.py`, `web/main.py`. Sensors are
   named in requests, times are ISO 8601 with a zone. One Quack connection
   behind a lock, retried once on a new connection after an error; 503 while
   `hp-db` is down. Optional `[web]` config: host, port, `max_gap_seconds`.
   Tested with FastAPI's TestClient against a real `hp-db`, and run once with
   uvicorn. Tests need `httpx2`, not `httpx`.)
4. The page: graph, then image panel. Written (`web/static/index.html`,
   `app.css`, `app.js`; plotly.js 4.1.1 served from the `plotly` package), and
   the server side is tested. Claude has never seen the page: there was no
   browser or JS engine in the build session. Alan opened a demo on 2026-11-15
   and the server log shows the page loaded, fetched readings and images, and
   re-fetched after a range change, but how it looks and behaves (hover across
   panels, double-click zoom out, local time) is unconfirmed. Design notes: one panel per unit sharing the time
   axis, never two y scales in one panel; times go into Plotly as epoch
   milliseconds, which Plotly itself shows in the browser's local time (an
   earlier version also shifted them and so was off by the UTC offset; fixed
   2026-11-16, not yet confirmed by Alan); each fetch covers
   the view plus 15% each side; channel color follows its place in the catalog.
5. ~~`deploy/hp-web.service`, README, Tailscale Funnel setup~~ (written
   2026-11-16; `deploy/README.md`. Not yet installed on the mini-PC, and the
   Funnel commands were checked against `tailscale funnel --help` (1.102.3)
   only, not run.)
6. Later: defrost shading from `mode_intervals`, frost-metric trace.
   ~~Thumbnails~~ (built 2026-11-16 after Alan found images took 3-5 s through
   Funnel: `web/thumbs.py`, `GET /thumbs/{image_id}`, 840 px wide, made with
   Pillow on first request and kept under `thumbs/840/` in the data directory.
   The page shows the thumbnail; "Open full size" links to `/images/{id}`.
   Nothing prunes the thumbnail folder.)
   ~~Image rotation~~ (built 2026-11-19: `[web] image_rotate` = 0, 90, 180 or
   270 degrees clockwise. Display only; files on disk and the database are
   unchanged. Thumbnails are rotated when made and kept under
   `thumbs/840-r90/` etc. `/images/{id}` rotates with Pillow on every request
   (quality 90, nothing kept). `GET /api/settings` gives the page the value,
   which it adds to image addresses as `?r=90` only to defeat the one-year
   browser cache.)

For step 3: web requests must share the Quack connection through a lock (see
section 6); queries take tens of milliseconds, so a lock is enough.


## 12. Camera capture interval by mode [built 2026-11-15]

Alan asked for this ahead of the mode signals existing. Tested with fake
readings only; the real channels and thresholds are not known yet.

- `camera/mode.py`. The optional `[camera]` section of `config.toml` holds a
  default `interval` (60 s) and ordered `[camera.modes.<name>]` tables, each
  with an `interval` and a `when` formula over the latest sensor readings
  (same formula rules as sensor transforms). First true formula wins; a mode
  without `when` is the fallback and must be last.
- `hp-camera` reads the latest reading of the needed channels from `hp-db`
  every `mode_check_seconds` (5). A mode change triggers an image at once and
  restarts the interval clock. The mode name is stored in `images.capture_ctx`.
- Mode is unknown (default interval, `capture_ctx` NULL) when no modes are
  configured, `hp-db` is down, or a needed channel has no good reading newer
  than `max_reading_age` (180 s; must exceed the sensor's `max_interval`).
- Not handled: a signal that flickers around a threshold causes an image on
  every change. Add hysteresis or a minimum dwell if real data shows this.
- Each image still opens and closes the RTSP stream (a few seconds). Fine at
  10 s intervals; not measured on the real camera.


## 13. Frost sensor [built 2026-11-22]

Asked for by Alan in `INSTRUCTIONS.md`. Built and tested on the development
machine against a copy of the mini-PC's database and images (2026-11-16 to
2026-11-21). Not yet installed or run on the mini-PC.
The measurements, the models that were compared and the full list of limits are
in `docs/frost-sensor.md`.

- **`frost` is a sensor in `readings`**, 0 to 10, not `image_metrics` (Alan's
  choice, so hp-web graphs it). Registered by `frost.register_sensor`.
- **Fan running** = `coil_pressure` >= 20 Pa. Every 15 s on clock boundaries a
  score from the plain average of the readings in [t - 7.5 s, t + 7.5 s),
  leaving out readings below 20 Pa (Alan's rule). 23 Pa -> 0, 80 Pa -> 10,
  linear. No reading >= 20 Pa in the span means no pressure score.
- **Fan off**: each image whose latest pressure reading is below 20 Pa, or
  older than 60 s (Alan: score the image if there is no pressure), gets an
  image score at the image's timestamp.
- `coil_pressure` is currently a Setra sensor, not the DG-700, stored on change
  with a 15 s `max_interval`.
- **Code**: `analysis/frost.py` (`compute`, `store`, `backfill`),
  `analysis/frost_image.py` (features and scoring; OpenCV),
  `analysis/frost_train.py`, `analysis/frost_model.json` (fitted numbers),
  `frost/main.py` (`hp-frost`), `hp frost-backfill` and `hp frost-train` in
  `cli.py`, `deploy/hp-frost.service`, new `frost` extra.
- **`hp-frost`** computes [last stored score, now - 20 s) every 5 s. No spool:
  a failed insert is recomputed. Known limit: after an `hp-db` outage it may
  pass a stretch before `hp-sensors`/`hp-camera` have replayed their spools;
  `hp frost-backfill --start ...` repairs that. Backfill deletes then
  re-inserts per 6-hour chunk (`DELETE FROM hp.readings` works over Quack);
  stop `hp-frost` while it runs.
- **Image function**: half-size image, fixed region (rows 32-64%, columns
  8-92%), 44 colour and texture statistics (saturation, blueness, brightness,
  row-to-row gradient), ridge regression with numpy, features limited to their
  training range. Clean coil is blue, frost is white. Trained on 2,082 fan-on
  images. Leave-one-day-out mean absolute error 0.68; scores above 8 read about
  1.7 low (only 11 such images). At 44 fan stops the first image score differed
  from the last pressure score by 1.0 on average (the image is up to a minute
  later, often already in defrost).
- **Not verified**: fan-off images cannot be checked against pressure; the
  camera was moved at least once (about a tenth of the image height) and the
  region is chosen to stay inside the coil for both positions, but a larger
  move needs new `ROI_*` values and retraining; snow on the coil or grille,
  and the frost graph in a browser, are untested. Image scoring takes about
  50 ms per image on the development machine, not measured on the N150.
- hp-web: image scores more than `max_gap_seconds` (300) apart, as with idle
  images every 15-30 min, show as separate points, not a line.
