# systemd units

Unit files for the mini-PC. They assume the repo is at `/home/alan/hp-monitoring`,
runs as user `alan`, and that `uv sync --all-extras` has created `.venv` there.

**Always use `uv sync --all-extras` here.** `uv sync --extra camera` (or any
single extra, or none) removes the other groups' libraries from `.venv`, and the
other services then fail with `ModuleNotFoundError` at their next restart.

## hp-db

```bash
cp config/hp-db.example.env config/hp-db.env   # then set HP_DB_TOKEN
chmod 600 config/hp-db.env

sudo cp deploy/hp-db.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hp-db
```

`journalctl -u hp-db -f` follows the log. The service restarts 5 seconds after
any exit or crash. `systemctl stop` lets it close the database cleanly.

## hp-sensors

```bash
cp config/config.example.toml config/config.toml   # then describe your devices
sudo usermod -aG dialout alan                      # serial port access; log out and in

sudo cp deploy/hp-sensors.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hp-sensors
```

It uses the same `config/hp-db.env` token file as hp-db. If hp-db is down,
readings are spooled to `~/hp-data/spool/sensors/` and loaded when it returns.
The service exits and is restarted if a device thread gets stuck.

## hp-camera

```bash
cp config/hp-camera.example.env config/hp-camera.env   # then edit RTSP_URL
chmod 600 config/hp-camera.env

sudo cp deploy/hp-camera.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hp-camera    # start now and at every boot
```

Useful commands:

```bash
systemctl status hp-camera
journalctl -u hp-camera -f               # follow the log
sudo systemctl restart hp-camera         # after pulling new code
```

The service restarts 10 seconds after any exit or crash.

It also reads the `config/hp-db.env` token file, because each image is recorded
in the database. If hp-db is down, images are still saved; their records wait in
`~/hp-data/spool/camera/images.jsonl` and are loaded when hp-db returns.

**Upgrading a unit installed before image records existed:** copy the unit file
again, then record the images already on disk:

```bash
sudo cp deploy/hp-camera.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart hp-camera
uv run --env-file config/hp-db.env hp index-images
```
