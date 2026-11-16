"""Small copies of the coil images, for the image panel of the page.

A full image from the camera is large and slow to send. A thumbnail is made the
first time an image is asked for and kept on disk, under ``thumbs/`` in the data
directory, with the same relative path as the image. Deleting that folder is
safe: thumbnails are made again when needed.
"""

import os
import threading
from pathlib import Path

from PIL import Image

from heatpump.common.paths import data_dir

# Pixels. The image panel is at most 420 wide; twice that keeps it sharp on
# high-resolution screens.
WIDTH = 840
QUALITY = 80


def thumb_root() -> Path:
    return data_dir() / "thumbs" / str(WIDTH)


def thumbnail(image_path: Path, relpath: str) -> Path:
    """Path of the thumbnail of an image, made now if it does not exist yet.

    `relpath` is the image's path relative to the image root. An image no wider
    than a thumbnail is returned as it is. Raises OSError if the file is not a
    readable image.
    """
    target = thumb_root() / relpath
    if target.is_file():
        return target
    with Image.open(image_path) as image:
        if image.width <= WIDTH:
            return image_path
        # For a JPG this decodes at 1/2, 1/4 or 1/8 size, which is much faster
        image.draft("RGB", (WIDTH, WIDTH))
        image.thumbnail((WIDTH, 100 * WIDTH))  # limit the width; keep the shape
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write under another name first, so a half-written file is never served
        temporary = target.with_name(f"{target.name}.{os.getpid()}-{threading.get_ident()}.tmp")
        image.convert("RGB").save(temporary, "JPEG", quality=QUALITY)
    os.replace(temporary, target)
    return target
