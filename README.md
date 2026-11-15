# phil-defrost-monitoring
Data collection related to analysis of the defrost cycle on Phil Kaluza's air-to-water heat pump.

## Running the image capture script from the command line

`hp-camera` saves a JPG of the coil every 60 seconds. It can be run directly,
without systemd, for testing or one-off use.

One-time setup, from the repo directory:

```bash
uv sync --extra camera
cp config/hp-camera.example.env config/hp-camera.env   # then edit RTSP_URL
chmod 600 config/hp-camera.env
```

Run it:

```bash
uv run --env-file config/hp-camera.env hp-camera
```

Or pass the camera URL directly instead of using the env file:

```bash
RTSP_URL='rtsp://user:password@192.168.1.50:554/stream1' uv run hp-camera
```

Press Ctrl+C to stop.

Images are written to `~/hp-data/images/coil/<date>/<HH-MM-SS>.jpg`, using local
time. To write somewhere else, set `HP_DATA_DIR`:

```bash
HP_DATA_DIR=/tmp/hp-test uv run --env-file config/hp-camera.env hp-camera
```

If the systemd service is also running, stop it first
(`sudo systemctl stop hp-camera`) so the two do not both write images.

To run it as a service instead, see [deploy/README.md](deploy/README.md).

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
  Needs `uv sync --extra sensors`. The driver turns off the gauge's own
  auto-zero and zeroes it itself: when the port is opened, then every 6 hours
  (change with `auto_zero_seconds`).
  A commented-out example is in `config/config.example.toml`.

- `labjack_u3`: LabJack U3-LV. Returns volts for analog pins and 0/1 for
  digital pins; see "Transforms" below. Needs `uv sync --extra sensors` and
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
