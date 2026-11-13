import os

# Must be set before the first VideoCapture is created. Forces RTSP over TCP.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import time
from datetime import datetime
from pathlib import Path

import cv2

RTSP_URL = os.environ["RTSP_URL"]  # KeyError if not set
OUTPUT_DIR = Path("pics")
INTERVAL_SECONDS = 60
JPEG_QUALITY = 80
WARMUP_FRAMES = 5
TIMEOUT_MS = 10_000


def capture_frame():
    """Open the stream, grab one fresh frame, close the stream."""
    cap = cv2.VideoCapture(
        RTSP_URL,
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


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    next_run = time.monotonic()

    try:
        while True:
            frame = capture_frame()
            if frame is None:
                print(f"{datetime.now().isoformat(timespec='seconds')} capture failed, will retry")
            else:
                filename = OUTPUT_DIR / datetime.now().strftime("%Y%m%d_%H%M%S.jpg")
                cv2.imwrite(str(filename), frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                print(f"Saved {filename}")

            # Schedule against a fixed clock so capture time doesn't accumulate as drift
            next_run += INTERVAL_SECONDS
            time.sleep(max(0, next_run - time.monotonic()))
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
