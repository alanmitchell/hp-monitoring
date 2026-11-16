# phil-defrost-monitoring
Data collection related to analysis of the defrost cycle on Phil Kaluza's air-to-water heat pump.

## Running the image capture script from the command line

`hp-camera` saves a JPG of the coil every 60 seconds (or at a rate that depends
on the heat pump's mode, see "Capture interval by mode" below) and records it in
the `images` table of the database. It can be run directly, without systemd, for
testing or one-off use.

One-time setup, from the repo directory:

```bash
uv sync --all-extras    # see "Installing" below: never sync just one extra on the mini-PC
cp config/hp-camera.example.env config/hp-camera.env   # then edit RTSP_URL
chmod 600 config/hp-camera.env
```

It also needs the database token file, `config/hp-db.env` (see "Database" below).

Run it:

```bash
uv run --env-file config/hp-camera.env --env-file config/hp-db.env hp-camera
```

Press Ctrl+C to stop.

Images are written to `~/hp-data/images/coil/<date>/<HH-MM-SS>.jpg`, using local
time. To write somewhere else, set `HP_DATA_DIR`:

```bash
HP_DATA_DIR=/tmp/hp-test uv run --env-file config/hp-camera.env --env-file config/hp-db.env hp-camera
```

### Capture interval by mode

The optional `[camera]` section of `config/config.toml` sets how often images
are taken. Each `[camera.modes.<name>]` table gives a mode an `interval` and a
`when` formula over the latest sensor readings:

```toml
[camera]
interval = 60            # used while the mode is unknown

[camera.modes.defrost]
when = "defrost_valve == 1"
interval = 10

[camera.modes.heating]
when = "compressor_kw > 0.5"
interval = 120

[camera.modes.off]       # no `when`: used when nothing above matched
interval = 900
```

`hp-camera` reads the latest value of each channel named in the formulas from
the database every `mode_check_seconds` (default 5). The first mode whose formula
is true is used. When the mode changes an image is taken at once, and the new
interval starts from there. The mode name is stored with each image in the
`capture_ctx` column.

If `hp-db` is down, or a channel has no good reading in the last
`max_reading_age` seconds (default 180), the mode is unknown and the default
`interval` is used. With no modes configured, images are taken every `interval`
seconds. Restart `hp-camera` after changing the config file.

### Image records

Each saved image gets a row in the `images` table: capture time (UTC, to the
second), path relative to `~/hp-data/images`, and width and height. `hp-db` does
not have to be running. While it is down the images are still saved, their
records wait in `~/hp-data/spool/camera/images.jsonl`, and they are loaded when
`hp-db` is back.

To record images that are on disk but not in the database (images captured
before this feature existed, or after restoring an image archive):

```bash
uv run --env-file config/hp-db.env hp index-images
```

It reads the capture time from each file's name, so run it on the machine (time
zone) that captured the images. It only adds what is missing, so it is safe to
run again, and while `hp-camera` is running.

If the systemd service is also running, stop it first
(`sudo systemctl stop hp-camera`) so the two do not both write images.

To run it as a service instead, see [deploy/README.md](deploy/README.md).

## Installing

On the mini-PC, always install every optional group together:

```bash
uv sync --all-extras
```

`uv sync` makes `.venv` match the command exactly, so it **removes** whatever
the command does not name. `uv sync --extra web` uninstalls OpenCV and the
sensor libraries; plain `uv sync` uninstalls all three groups. A running service
carries on, because it has already loaded its libraries, then fails with
`ModuleNotFoundError` the next time it restarts. `uv run` does not remove
anything.

On a machine that needs one group only (a laptop for analysis), `uv sync --extra
<group>` is fine.

## Database (`hp-db`)

`hp-db` is the only process that opens the DuckDB file
(`~/hp-data/heatpump.duckdb`). It creates or upgrades the schema at startup, then
serves the database to the other processes over DuckDB's Quack protocol on
`quack:localhost` (port 9494, reachable from this machine only).

One-time setup:

```bash
cp config/hp-db.example.env config/hp-db.env   # then set HP_DB_TOKEN
chmod 600 config/hp-db.env
```

Run it (Ctrl+C stops it cleanly):

```bash
uv run --env-file config/hp-db.env hp-db
```

Query it from another terminal while it runs:

```bash
uv run --env-file config/hp-db.env python -c "
from heatpump.common.db import connect
con = connect()
print(con.sql('SELECT * FROM hp.schema_info').fetchall())"
```

Client code calls `heatpump.common.db.connect()` and uses the tables as
`hp.<table>`. Things found by testing Quack on DuckDB 2.0.0:

- One statement cannot read an `hp.*` table while inserting into another
  ("Multiple streaming scans ... not currently supported"). Copy what you need
  into a local temp table first.
- `?` parameters fail in statements that touch `hp.*` tables ("Values were not
  provided for the following parameters"). Put rows into a local temp table
  first, then `INSERT INTO hp.readings SELECT * FROM buf`.
- A Quack server and client inside the same Python process hang. Always use
  separate processes (the tests start `hp-db` as a subprocess).
- After `CONNECT hp`, Python's `con.sql()` fails; `ATTACH` with `hp.` prefixes
  works, so the helper uses that.
- While `hp-db` runs, nothing else can open the `.duckdb` file directly.

The schema lives in `src/heatpump/common/schema.py`. To change it, append a new
entry to `MIGRATIONS`; `hp-db` applies it the next time it starts.

### Checkpoints and copying the database file

DuckDB appends each write to `heatpump.duckdb.wal` first, and moves the log into
`heatpump.duckdb` at a checkpoint. `hp-db` checkpoints every hour, and when it
stops. Set `HP_DB_CHECKPOINT_SECONDS` in `config/hp-db.env` to change the hour
(0 leaves it to DuckDB: at 16 MB of log, or on close).

A copy of `heatpump.duckdb` alone, taken while `hp-db` runs, opens as a normal
database holding everything up to the last checkpoint:

```bash
cp ~/hp-data/heatpump.duckdb /tmp/snapshot.duckdb
uv run python -c "
import duckdb
con = duckdb.connect('/tmp/snapshot.duckdb', read_only=True)
print(con.sql('SELECT max(ts) FROM readings').fetchall())"
```

Do not copy while a checkpoint is being written (on the hour after `hp-db`
started, for about a second): the copy could mix old and new parts of the file.

## Sensors (`hp-sensors`)

`hp-sensors` reads every device listed in `config/config.toml` and stores the
readings through `hp-db`. Each device runs in its own thread, so a slow device
does not delay the others.

One-time setup:

```bash
cp config/config.example.toml config/config.toml   # then describe your devices
```

The example file defines a `demo` device using the `fake` driver, so this works
with no hardware attached. See what a device produces without touching the
database:

```bash
uv run hp-sensors --print                 # all devices
uv run hp-sensors --print --only demo     # one device
```

Run it for real (with `hp-db` running in another terminal):

```bash
uv run --env-file config/hp-db.env hp-sensors
```

How it behaves:

- **Timing**: polled devices are read every `period` seconds on clock boundaries
  (a 1 s device reads at the top of each second). Streaming devices are read
  whenever they send data.
- **What gets stored**: with no `deadband`, every sample. With a `deadband`, only
  samples that differ from the last stored value by more than it, plus one every
  `max_interval` seconds. When a change is stored, the sample just before it is
  stored too, so graphs show a step and not a false ramp. This means **averages
  over time must be time-weighted**, not plain averages of rows.
- **Failures**: a failed read stores a row with `quality = 2` and a NULL value,
  then the device is closed and reopened, waiting 1, 2, 4 ... up to 30 s between
  attempts.
- **Database outages**: every batch is written to `~/hp-data/spool/sensors/`
  before it is inserted, and deleted after. If `hp-db` is down, the files
  accumulate and are loaded when it returns.
- **Sensor names**: each channel name in the config is added to the `sensors`
  table automatically the first time it is seen.

Drivers so far:

- `fake`: made-up values, for trying things without hardware.
- `dg700`: Energy Conservatory DG-700 pressure gauge, one channel in Pascals.
  Needs `uv sync --all-extras`. The driver turns off the gauge's own
  auto-zero and zeroes it itself: when the port is opened, then every 6 hours
  (change with `auto_zero_seconds`).
  A commented-out example is in `config/config.example.toml`.

- `labjack_u3`: LabJack U3-LV. Returns volts for analog pins and 0/1 for
  digital pins; see "Transforms" below. Needs `uv sync --all-extras` and
  LabJack's Exodriver system library:

  ```bash
  sudo apt install build-essential libusb-1.0-0-dev git
  git clone https://github.com/labjack/exodriver.git
  cd exodriver && sudo ./install.sh
  ```

### Transforms

A channel's `transform` setting is a formula that turns the raw reading into the
value that gets stored. It works on any device, not just the LabJack.

```toml
transform = "1 - x"                      # invert a digital input
transform = "(x - 0.5) * 100"            # linear scaling
transform = "thermistor(x, therm_supply, r_fixed=10000, a=..., b=..., c=..., scale='rankine')"
```

- `x` is the channel's own raw reading. Any other name is another channel on the
  **same device** (its transformed value), or a function: `thermistor`, `abs`,
  `min`, `max`, `round`, `sqrt`, `exp`, `log`, `log10`, `sin`, `cos`, `tan`, `atan`.
- A channel with `store = false` is read and can be used in formulas, but is not
  stored. A channel whose formula has no `x` is computed only from other channels.
- Formulas are checked at startup; a mistake stops the program with a message.
- While running, a formula that cannot produce a number (a failed input, a
  division by zero, a shorted or disconnected thermistor) stores a failed reading.
- The deadband applies to the transformed value.
- **To also keep the raw value**, add a second channel on the same pin with no
  transform.

`thermistor(v, v_supply, r_fixed=..., a=..., b=..., c=..., scale=...)` returns
degrees F for a thermistor in a voltage divider. `v` is the voltage at the
junction, `v_supply` the voltage across the divider, `r_fixed` the fixed resistor
in ohms, and `a`, `b`, `c` the Steinhart-Hart coefficients. `scale` says what the
coefficients produce: `'rankine'` or `'kelvin'`. The thermistor is assumed to be
the leg connected to ground; add `thermistor_leg='supply'` if it is the other leg.

Because stored values now depend on the config file, `hp-sensors` saves a copy
of the file into the `config_history` table whenever it starts with a changed one.

Adding a driver: subclass `Device` in `src/heatpump/sensors/devices/`, implement
`read()` to return `{channel_name: value}`, and add it to `DRIVERS` in
`devices/__init__.py`. See `devices/fake.py` for the smallest example.

## Web app (`hp-web`)

`hp-web` serves a page with a graph of the sensor readings and the coil image
for any time on the graph. It is a client of `hp-db`, so `hp-db` must be running.

```bash
uv sync --all-extras
uv run --env-file config/hp-db.env hp-web
```

Open `http://127.0.0.1:8000`. It listens on this machine only. The optional
`[web]` section of `config/config.toml` changes the host, port and
`max_gap_seconds`.

### The page

- **Time range:** the 1 h, 6 h, 24 h and 7 d buttons show that much time up to
  now. The arrows move one screen earlier or later. "From" and "to" set any range.
- **Channels:** pick the channels to graph. Channels with the same unit share a
  panel; on/off channels have their own. A channel always has the same color.
- **Zoom:** drag across the graph, or scroll, to zoom in. The page then fetches
  that range again, so detail gets finer as you zoom, down to every stored
  reading. Double-click, or press "Zoom out", to go back.
- **Averages:** over long ranges the line is an average, and the shaded band
  behind it runs from the lowest to the highest reading, so short spikes still show.
- **Images:** move the pointer over the graph to see the image taken closest to
  that time. Click to hold an image; the arrow keys (or the buttons under the
  image) step to the previous or next one. Esc lets the image follow the
  pointer again. A dotted line on the graph marks the image's time.
- **Latest values:** the table at the bottom lists each channel's newest reading.
- The address bar holds the channels and range, so a view can be bookmarked.
  Times are shown in the browser's time zone.

The page is three files in `src/heatpump/web/static/`, with no build step.
Plotly's JavaScript comes from the `plotly` Python package.

### The API

Interactive documentation of the API is at `http://127.0.0.1:8000/docs`.

| Endpoint | Returns |
|---|---|
| `GET /api/sensors` | the measurement channels |
| `GET /api/latest` | every channel with its newest reading from the last day |
| `GET /api/readings?sensors=a,b&start=...&end=...&max_points=2000` | readings of the named channels |
| `GET /api/images?start=...&end=...` | the images captured in the range (id, time, mode) |
| `GET /images/{image_id}` | one JPG |

`start` and `end` are ISO 8601 times with a time zone, such as
`2026-11-14T05:00:00Z`. Times in the answers are milliseconds since 1970, UTC.

`/api/readings` never returns more than about `max_points` points per channel.
A channel with more stored readings than that in the range is returned as
buckets: `value` is the time-weighted average, `low` and `high` the smallest and
largest reading in each bucket, and `bucket_seconds` the bucket length. Ask for
a shorter range to get finer buckets, and finally the stored readings
themselves (`bucket_seconds` is null). A null in `value` marks a break in the
line: a failed reading, or a silence longer than `max_gap_seconds`, which means
`hp-sensors` was not running.

If `hp-db` is down the API answers 503, and it reconnects by itself when
`hp-db` is back.
