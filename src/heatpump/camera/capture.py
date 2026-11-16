"""Entry point for the `hp-camera` console script: periodically save a JPG of the coil
and record it in the `images` table.

Files are stored under the image root as <camera>/<local date>/<local time>.jpg,
for example ``coil/2026-11-13/20-32-15.jpg``. The JPG is saved first. Its database
row goes through a spool file, so images taken while `hp-db` is down are recorded
once it is back.

How often an image is taken depends on the heat pump's mode; see `camera/mode.py`.
"""

import os

# Must be set before the first VideoCapture is created. Forces RTSP over TCP.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import cv2

from heatpump.camera import mode as camera_mode
from heatpump.camera.records import ImageSpool, image_relpath, image_row
from heatpump.common import db
from heatpump.common.paths import data_dir, image_root

JPEG_QUALITY = 80
WARMUP_FRAMES = 5
TIMEOUT_MS = 10_000
# The script's clock reads this much later than the time the camera stamps on the
# image, so it is subtracted to get the capture time.
CAPTURE_LAG = timedelta(seconds=2)


def capture_frame(rtsp_url: str):
    """Open the stream, grab one fresh frame, close the stream."""
    cap = cv2.VideoCapture(
        rtsp_url,
        cv2.CAP_FFMPEG,
        [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, TIMEOUT_MS,
            cv2.CAP_PROP_READ_TIMEOUT_MSEC, TIMEOUT_MS,
        ],
    )
    try:
        if not cap.isOpened():
            return None
        # The first frames after connecting can be stale or corrupt
        for _ in range(WARMUP_FRAMES):
            cap.grab()
        ok, frame = cap.read()
        return frame if ok else None
    finally:
        cap.release()


def save_frame(frame, ts: datetime) -> Path:
    """Write `frame` as a JPG under the image root; return its path relative to that root."""
    relpath = image_relpath(ts)
    path = image_root() / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]):
        raise OSError(f"could not write {path}")
    return relpath


def take_image(rtsp_url: str, spool: ImageSpool, mode: str | None) -> None:
    """Capture one image, save it and queue its database record."""
    frame = capture_frame(rtsp_url)
    # UTC capture time, to the whole second: names the file and goes in the
    # database record, so the two always agree
    ts = (datetime.now(UTC) - CAPTURE_LAG).replace(microsecond=0)
    if frame is None:
        print(f"{ts.astimezone().isoformat(timespec='seconds')} capture failed, will retry")
        return
    try:
        relpath = save_frame(frame, ts)
        height, width = frame.shape[:2]
        spool.add(image_row(ts, relpath, (width, height), mode))
        print(f"Saved {relpath}")
    except OSError as e:
        print(f"{ts.astimezone().isoformat(timespec='seconds')} {e}")


def main() -> None:
    try:
        rtsp_url = os.environ["RTSP_URL"]
    except KeyError:
        raise SystemExit("Set the RTSP_URL environment variable to the camera's stream URL")

    db.quack_token()  # stop now, with a clear message, if HP_DB_TOKEN is missing
    cfg = camera_mode.load()

    print(f"Saving images under {image_root()}")
    print(f"An image every {cfg.interval:g} s while the heat pump mode is unknown")
    for m in cfg.modes:
        print(f"An image every {m.interval:g} s in mode '{m.name}'")
    spool = ImageSpool(data_dir() / "spool" / "camera" / "images.jsonl")
    modes = camera_mode.ModeReader(cfg)
    mode = None  # None = unknown
    first = True
    next_capture = time.monotonic()

    try:
        while True:
            new_mode = modes.current()
            changed = first or new_mode is not mode
            if changed and (new_mode is not None or not first):
                print(f"Heat pump mode: {new_mode.name if new_mode else 'unknown'}")
            mode, first = new_mode, False

            # An image is due on schedule, and at once whenever the mode changes
            now = time.monotonic()
            if changed or now >= next_capture:
                take_image(rtsp_url, spool, mode.name if mode else None)
                interval = mode.interval if mode else cfg.interval
                # Schedule against a fixed clock so capture time doesn't accumulate
                # as drift; start a new clock when the mode changes
                next_capture = (now if changed else next_capture) + interval
            # Also when no image was taken, so a backlog drains once hp-db is back
            spool.load()

            wait = next_capture - time.monotonic()
            if modes.uses_readings:
                wait = min(wait, cfg.mode_check_seconds)
            time.sleep(max(0, wait))
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        spool.close()
        modes.close()


if __name__ == "__main__":
    main()
