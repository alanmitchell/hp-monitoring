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
