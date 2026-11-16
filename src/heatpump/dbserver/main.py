"""Entry point for the `hp-db` console script.

Opens the DuckDB file, brings the schema up to date, and serves the database to
the other processes over DuckDB's Quack protocol. It does nothing else, so it has
little reason to crash and take every other connection down with it.

DuckDB first appends each write to a write-ahead log, the `.wal` file next to the
database, and only moves it into the database file at a checkpoint. By itself it
checkpoints when the log reaches 16 MB or the database closes, which can be many
hours apart here. So `hp-db` also checkpoints on a timer, every hour unless
HP_DB_CHECKPOINT_SECONDS says otherwise (0 turns the timer off).
"""

import os
import signal
import threading

import duckdb

from heatpump.common import schema
from heatpump.common.db import db_path, quack_token, quack_uri

DEFAULT_CHECKPOINT_SECONDS = 3600


def checkpoint_seconds() -> float | None:
    """Time between checkpoints from HP_DB_CHECKPOINT_SECONDS, or None for no timer."""
    text = os.environ.get("HP_DB_CHECKPOINT_SECONDS", str(DEFAULT_CHECKPOINT_SECONDS))
    try:
        seconds = float(text)
    except ValueError:
        raise SystemExit(f"HP_DB_CHECKPOINT_SECONDS must be a number, not {text!r}") from None
    return seconds if seconds > 0 else None


def main() -> None:
    token = quack_token()
    uri = quack_uri()
    path = db_path()
    interval = checkpoint_seconds()
    path.parent.mkdir(parents=True, exist_ok=True)

    # systemd stops a service with SIGTERM; Ctrl-C sends SIGINT
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    con = duckdb.connect(str(path))
    try:
        start_version = schema.migrate(con)
        if start_version != schema.SCHEMA_VERSION:
            print(f"Migrated schema from version {start_version} to {schema.SCHEMA_VERSION}")

        # Returns at once; DuckDB serves from its own background threads
        con.execute("CALL quack_serve(?, token = ?)", [uri, token])
        print(f"Serving {path} at {uri} (schema version {schema.SCHEMA_VERSION})")

        # wait() returns False each time the interval passes, True when told to stop
        while not stop.wait(interval):
            try:
                con.execute("CHECKPOINT")
            except duckdb.Error as e:
                print(f"Checkpoint failed, will try again in {interval:g} s: {e}")

        print("Stopping")
        con.execute("CALL quack_stop(?)", [uri])
    finally:
        con.close()  # checkpoints, folding the write-ahead log into the database file


if __name__ == "__main__":
    main()
