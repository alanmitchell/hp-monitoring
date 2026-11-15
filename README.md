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
