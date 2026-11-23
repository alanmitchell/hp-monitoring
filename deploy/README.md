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

## hp-frost

Computes the `frost` sensor (see "Frost score" in the main README). Needs only
the `config/hp-db.env` token file.

```bash
sudo cp deploy/hp-frost.service /etc/systemd/system/
sudo systemctl daemon-reload
uv run --env-file config/hp-db.env hp frost-backfill   # scores for the data so far
sudo systemctl enable --now hp-frost                   # start now and at every boot
```

Run the backfill first: on a database with no frost scores `hp-frost` starts
from now, otherwise it continues after the last stored score.

```bash
systemctl status hp-frost
journalctl -u hp-frost -f                # follow the log
sudo systemctl restart hp-frost          # after pulling new code
```

To redo past scores (for example after `hp frost-train`), stop the service
during the backfill:

```bash
sudo systemctl stop hp-frost
uv run --env-file config/hp-db.env hp frost-backfill
sudo systemctl start hp-frost
```

## hp-web

```bash
sudo cp deploy/hp-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hp-web
curl -s http://127.0.0.1:8000/api/sensors     # should print the channel list
```

It uses the same `config/hp-db.env` token file as hp-db, and listens on
`127.0.0.1:8000` only. While hp-db is down the page shows an error; it recovers
by itself.

### Publishing it with Tailscale Funnel

Funnel gives the page a public address, `https://<machine>.<tailnet>.ts.net`,
that anyone on the internet can open without Tailscale. Tailscale provides the
HTTPS certificate and passes requests on to hp-web.

One-time setup:

```bash
sudo tailscale funnel --bg 8000
```

The first time, the command prints a link to the Tailscale admin console if
Funnel or HTTPS certificates are not yet enabled for the tailnet. Follow it, then
run the command again. `--bg` keeps Funnel on across reboots.

```bash
tailscale funnel status          # shows the public address
sudo tailscale funnel reset      # takes the page off the internet
```

To publish to your own tailnet only, use `tailscale serve --bg 8000` instead.

What to know about a public page:

- **There is no login.** Anyone with the address sees the graphs and the coil
  images. The address is not a secret: every HTTPS certificate is listed in
  public logs, so scanners find new `ts.net` names within hours.
- **Nothing can be changed through it.** hp-web only reads. The database
  (port 9494) and SSH are not published; Funnel passes on port 8000 only.
- **Each request is limited** (5,000 points per channel, 5,000 image records),
  and search engines are told not to index the site. There is no limit on how
  many requests a visitor makes; heavy traffic would slow the page but not stop
  data collection, which does not depend on hp-web.
- Funnel traffic passes through Tailscale's relay servers, which limit its
  speed. Expect images to load more slowly than on the tailnet.
