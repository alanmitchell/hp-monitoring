"""Entry point for the `hp-db` console script.

Opens the DuckDB file, brings the schema up to date, and serves the database to
the other processes over DuckDB's Quack protocol. It does nothing else, so it has
little reason to crash and take every other connection down with it.
"""

import signal
import threading

import duckdb

from heatpump.common import schema
from heatpump.common.db import db_path, quack_token, quack_uri


def main() -> None:
    token = quack_token()
    uri = quack_uri()
    path = db_path()
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

        stop.wait()

        print("Stopping")
        con.execute("CALL quack_stop(?)", [uri])
    finally:
        con.close()  # checkpoints, folding the write-ahead log into the database file


if __name__ == "__main__":
    main()
