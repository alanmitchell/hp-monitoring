"""Entry point for the `hp-web` console script: serves the web app with uvicorn.

Settings come from the optional `[web]` section of the config file::

    [web]
    host = "127.0.0.1"       # this machine only; `tailscale serve` publishes it
    port = 8000
    max_gap_seconds = 300    # readings further apart than this are drawn as a gap
"""

import uvicorn

from heatpump.analysis import queries
from heatpump.common import db
from heatpump.common.config import config_path, load_config
from heatpump.web.app import create_app

DEFAULTS = {"host": "127.0.0.1", "port": 8000, "max_gap_seconds": queries.DEFAULT_MAX_GAP}


def load_settings() -> dict:
    """The `[web]` section merged over the defaults; the defaults if there is no file."""
    section = load_config().get("web", {}) if config_path().exists() else {}
    unknown = set(section) - set(DEFAULTS)
    if unknown:
        raise SystemExit(f"Config file {config_path()}: [web]: unknown setting '{min(unknown)}'")
    settings = {**DEFAULTS, **section}
    numbers_ok = (
        isinstance(settings["port"], int)
        and isinstance(settings["max_gap_seconds"], (int, float))
        and settings["max_gap_seconds"] > 0
    )
    if not isinstance(settings["host"], str) or not numbers_ok:
        raise SystemExit(
            f"Config file {config_path()}: [web]: host must be text, port a whole "
            "number and max_gap_seconds a number greater than 0"
        )
    return settings


def main() -> None:
    db.quack_token()  # stop now, with a clear message, if HP_DB_TOKEN is missing
    settings = load_settings()
    app = create_app(max_gap=float(settings["max_gap_seconds"]))
    uvicorn.run(app, host=settings["host"], port=settings["port"])


if __name__ == "__main__":
    main()
