"""Entry point for the `hp` console script: ad-hoc maintenance and analysis commands."""

import argparse
from datetime import datetime

from heatpump.camera.records import image_row, insert_images, jpeg_size, ts_from_relpath
from heatpump.common import db
from heatpump.common.paths import image_root


def index_images() -> None:
    """Add an `images` row for every JPG under the image root that has none.

    Safe to run again, and while `hp-camera` is running. Run it on the machine
    (time zone) that captured the images, because file names are in local time.
    """
    root = image_root()
    rows = []
    skipped = 0
    for path in sorted(root.glob("*/*/*.jpg")):
        relpath = path.relative_to(root)
        ts = ts_from_relpath(relpath)
        if ts is None:
            print(f"Skipped {relpath}: not named <camera>/<YYYY-MM-DD>/<HH-MM-SS>.jpg")
            skipped += 1
            continue
        rows.append(image_row(ts, relpath, jpeg_size(path)))

    con = db.connect()
    try:
        added = insert_images(con, rows)
    finally:
        con.close()
    print(
        f"{len(rows) + skipped} JPGs under {root}: {added} added, "
        f"{len(rows) - added} already in the database, {skipped} skipped"
    )


def parse_time(text: str) -> datetime:
    """A time from the command line. Without a zone it is this machine's local time."""
    return datetime.fromisoformat(text).astimezone()


def frost_backfill(start: datetime, end: datetime | None) -> None:
    """Replace the stored frost scores from `start` on with freshly computed ones.

    Stop `hp-frost` first so the two do not store the same scores twice; when
    started again it continues after the last score.
    """
    from heatpump.analysis import frost
    from heatpump.common.timeutil import utc_now
    from heatpump.frost.main import SETTLE

    end = end or utc_now() - SETTLE
    con = db.connect()
    try:
        count = frost.backfill(con, start, end)
    finally:
        con.close()
    print(f"Stored {count} frost scores")


def frost_train(start: datetime, end: datetime | None) -> None:
    """Fit the frost-from-image function again and rewrite frost_model.json."""
    from heatpump.analysis import frost_train as training
    from heatpump.common.timeutil import utc_now

    con = db.connect()
    try:
        training.train(con, start, end or utc_now())
    finally:
        con.close()


def main() -> None:
    from heatpump.analysis.frost import HISTORY_START

    parser = argparse.ArgumentParser(prog="hp")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "index-images", help="add database records for JPGs that do not have one"
    )
    for name, text in (
        ("frost-backfill", "compute and store frost scores for past data (replaces old ones)"),
        ("frost-train", "fit the frost-from-image function again"),
    ):
        sub = commands.add_parser(name, help=text)
        sub.add_argument(
            "--start", type=parse_time, default=HISTORY_START,
            help="first time to include, e.g. 2026-11-16T13:42 (local time unless a zone "
            "is given). Default: 2026-11-16 1:42 PM Alaska Standard Time",
        )
        sub.add_argument("--end", type=parse_time, help="time to stop before. Default: now")
    args = parser.parse_args()
    if args.command == "index-images":
        index_images()
    elif args.command == "frost-backfill":
        frost_backfill(args.start, args.end)
    elif args.command == "frost-train":
        frost_train(args.start, args.end)


if __name__ == "__main__":
    main()
