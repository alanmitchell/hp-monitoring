"""Where the project's data lives on disk."""

import os
from pathlib import Path


def data_dir() -> Path:
    """Root for all collected data. Defaults to ~/hp-data; override with HP_DATA_DIR."""
    return Path(os.environ.get("HP_DATA_DIR", Path.home() / "hp-data"))


def image_root() -> Path:
    """Root of the image archive. Image paths in the database are relative to this."""
    return data_dir() / "images"
