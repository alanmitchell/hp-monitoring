"""Small copies of the coil images, for the image panel of the page, and rotation.

A full image from the camera is large and slow to send. A thumbnail is made the
first time an image is asked for and kept on disk, under ``thumbs/`` in the data
directory, with the same relative path as the image. Deleting that folder is
safe: thumbnails are made again when needed.

Images can be shown rotated (`image_rotate` in the `[web]` section of the config
file). The files from the camera are never changed: a thumbnail is rotated as it
is made, and a full-size image each time it is sent.
"""

import io
import os
import threading
from pathlib import Path

from PIL import Image

from heatpump.common.paths import data_dir

# Pixels. The image panel is at most 420 wide; twice that keeps it sharp on
# high-resolution screens.
WIDTH = 840
QUALITY = 80
FULL_SIZE_QUALITY = 90  # a rotated full-size image is compressed again; keep the loss small

# Degrees clockwise -> Pillow's operation (which counts anticlockwise)
ROTATIONS = {
    0: None,
    90: Image.Transpose.ROTATE_270,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}


def thumb_root(rotate: int = 0) -> Path:
    # A folder per rotation, so a changed setting never shows old thumbnails
    name = f"{WIDTH}-r{rotate}" if rotate else str(WIDTH)
    return data_dir() / "thumbs" / name


def _turned(image: Image.Image, rotate: int) -> Image.Image:
    operation = ROTATIONS[rotate]
    return image if operation is None else image.transpose(operation)


def thumbnail(image_path: Path, relpath: str, rotate: int = 0) -> Path:
    """Path of the thumbnail of an image, made now if it does not exist yet.

    `relpath` is the image's path relative to the image root; `rotate` is
    degrees clockwise (0, 90, 180 or 270). An image that is not rotated and is
    no wider than a thumbnail is returned as it is. Raises OSError if the file
    is not a readable image.
    """
    target = thumb_root(rotate) / relpath
    if target.is_file():
        return target
    with Image.open(image_path) as image:
        if not rotate and image.width <= WIDTH:
            return image_path
        # For a JPG this decodes at 1/2, 1/4 or 1/8 size, which is much faster
        image.draft("RGB", (WIDTH, WIDTH))
        small = _turned(image, rotate)
        small.thumbnail((WIDTH, 100 * WIDTH))  # limit the width; keep the shape
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write under another name first, so a half-written file is never served
        temporary = target.with_name(f"{target.name}.{os.getpid()}-{threading.get_ident()}.tmp")
        small.convert("RGB").save(temporary, "JPEG", quality=QUALITY)
    os.replace(temporary, target)
    return target


def rotated_jpeg(image_path: Path, rotate: int) -> bytes:
    """The full-size image turned `rotate` degrees clockwise, as JPG data.

    Nothing is kept on disk. Raises OSError if the file is not a readable image.
    """
    with Image.open(image_path) as image:
        data = io.BytesIO()
        _turned(image, rotate).convert("RGB").save(data, "JPEG", quality=FULL_SIZE_QUALITY)
    return data.getvalue()
