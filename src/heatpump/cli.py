"""Entry point for the `hp` console script: ad-hoc maintenance and analysis commands."""

import argparse

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


def main() -> None:
    parser = argparse.ArgumentParser(prog="hp")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "index-images", help="add database records for JPGs that do not have one"
    )
    args = parser.parse_args()
    if args.command == "index-images":
        index_images()


if __name__ == "__main__":
    main()
