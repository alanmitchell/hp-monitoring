# systemd units

Unit files for the mini-PC. They assume the repo is at `/home/alan/hp-monitoring`,
runs as user `alan`, and that `uv sync --all-extras` has created `.venv` there.

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
