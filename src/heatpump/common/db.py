"""Database location, Quack settings, and the client helper used by every process
except `hp-db`.

Only `hp-db` opens the .duckdb file. Everything else calls `connect()` and reaches
the tables through the attached `hp` catalog, e.g. ``hp.readings``.

Things learned by testing Quack on DuckDB 2.0.0 (see README, "Database"):

* ``?`` parameters do not work in statements that touch ``hp.*`` tables. Load
  rows into a local temp table first, then ``INSERT INTO hp.t SELECT * FROM buf``.
* A server and a client in the same Python process hang. Use separate processes.
"""

import os
from pathlib import Path

import duckdb

from heatpump.common.paths import data_dir

DEFAULT_URI = "quack:localhost"  # port 9494, reachable from this machine only
CATALOG = "hp"


def db_path() -> Path:
    """The DuckDB database file. Opened by `hp-db` and nothing else."""
    return data_dir() / "heatpump.duckdb"


def quack_uri() -> str:
    """Where `hp-db` listens. Override with HP_DB_URI, e.g. ``quack:localhost:9500``."""
    return os.environ.get("HP_DB_URI", DEFAULT_URI)


def quack_token() -> str:
    """Shared secret between `hp-db` and its clients, from HP_DB_TOKEN."""
    token = os.environ.get("HP_DB_TOKEN", "")
    if len(token) < 4:
        raise SystemExit(
            "HP_DB_TOKEN is not set (or is shorter than 4 characters). "
            "See config/hp-db.example.env."
        )
    return token


def connect() -> duckdb.DuckDBPyConnection:
    """Connect to `hp-db`. Tables are then available as ``hp.<table>``."""
    con = duckdb.connect()
    con.execute(
        "CREATE SECRET (TYPE quack, TOKEN ?, SCOPE ?)", [quack_token(), quack_uri()]
    )
    con.execute(f"ATTACH '{quack_uri()}' AS {CATALOG} (TYPE quack)")
    return con
