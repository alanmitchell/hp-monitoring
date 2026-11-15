"""Loads the TOML configuration file.

The file is config/config.toml, relative to the directory the program is started
from. Override with HP_CONFIG. See config/config.example.toml.
"""

import os
import tomllib
from pathlib import Path


def config_path() -> Path:
    return Path(os.environ.get("HP_CONFIG", "config/config.toml"))


def load_config() -> dict:
    path = config_path()
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise SystemExit(
            f"Config file {path} not found. Copy config/config.example.toml to it, "
            "or set HP_CONFIG."
        ) from None
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"Config file {path} is not valid TOML: {e}") from None
