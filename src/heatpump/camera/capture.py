"""Entry point for the `hp-camera` console script: periodically save a JPG of the coil.

Files are stored under the image root as <camera>/<local date>/<local time>.jpg,
for example ``coil/2026-11-13/20-32-15.jpg``.
"""

import os

# Must be set before the first VideoCapture is created. Forces RTSP over TCP.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import cv2

from heatpump.common.paths import image_root

CAMERA = "coil"
INTERVAL_SECONDS = 60
JPEG_QUALITY = 80
WARMUP_FRAMES = 5
TIMEOUT_MS = 10_000
# The script's clock reads this much later than the time the camera stamps on the
# image, so it is subtracted to get the capture time.
CAPTURE_LAG = timedelta(seconds=2)


def image_relpath(ts: datetime, camera: str = CAMERA) -> Path:
    """Path of an image captured at `ts` (timezone-aware), relative to the image root.

    Folder and file names use local time so people can browse them. During the
    hour that repeats when daylight saving time ends, files from the second pass
    through the hour get a 'b' suffix (01-30-00b.jpg) so they do not collide
    with the first pass.
    """
    local = datetime.fromtimestamp(ts.timestamp())  # naive local time; fold=1 on the repeat
    suffix = "b" if local.fold else ""
    return Path(camera) / f"{local:%Y-%m-%d}" / f"{local:%H-%M-%S}{suffix}.jpg"


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


def main() -> None:
    try:
        rtsp_url = os.environ["RTSP_URL"]
    except KeyError:
        raise SystemExit("Set the RTSP_URL environment variable to the camera's stream URL")

    print(f"Saving images under {image_root()}")
    next_run = time.monotonic()

    try:
        while True:
            frame = capture_frame(rtsp_url)
            # UTC capture time: names the file and will go in the database record
            ts = datetime.now(UTC) - CAPTURE_LAG
            if frame is None:
                print(f"{ts.astimezone().isoformat(timespec='seconds')} capture failed, will retry")
            else:
                try:
                    print(f"Saved {save_frame(frame, ts)}")
                except OSError as e:
                    print(f"{ts.astimezone().isoformat(timespec='seconds')} {e}")

            # Schedule against a fixed clock so capture time doesn't accumulate as drift
            next_run += INTERVAL_SECONDS
            time.sleep(max(0, next_run - time.monotonic()))
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
