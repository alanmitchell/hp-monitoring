"""Entry point for the `hp-frost` console script: keeps the derived `frost`
sensor up to date.

Every few seconds it computes the frost scores (see `analysis/frost.py`) from
where it left off up to a little before now, and stores them. Where it left off
is the newest stored frost score, so after a stop, a crash or a database outage
it catches up by itself. On a database with no frost scores it starts from now;
use `hp frost-backfill` for the history.

It stores nothing of its own on disk: a score that could not be stored is simply
computed again.

Known limit: after a database outage, `hp-sensors` and `hp-camera` load what they
spooled meanwhile. If `hp-frost` gets to that stretch before they have finished,
its scores there are incomplete. `hp frost-backfill --start ...` repairs it.
"""

import signal
import threading
from datetime import datetime, timedelta

import duckdb

from heatpump.analysis import frost
from heatpump.common import db
from heatpump.common.paths import image_root
from heatpump.common.timeutil import utc_now

CHECK_SECONDS = 5
# Scores are computed up to this long ago. A pressure score needs the readings
# of the next 7.5 s, and collectors take a few seconds to write to the database.
SETTLE = timedelta(seconds=20)
# When catching up, store after each stretch of this length
CHUNK = timedelta(hours=1)


def catch_up(con: duckdb.DuckDBPyConnection, done_until: datetime, end: datetime) -> datetime:
    """Compute and store the scores in [done_until, end). Returns the new `done_until`."""
    while done_until < end:
        b = min(done_until + CHUNK, end)
        frost.store(con, frost.compute(con, done_until, b))
        done_until = b
    return done_until


def start_time(con: duckdb.DuckDBPyConnection) -> datetime:
    """Where to begin: just after the newest stored score, or now if there are none."""
    last = frost.last_score_time(con)
    if last is None:
        print("No frost scores stored yet; starting from now")
        return utc_now() - SETTLE
    print(f"Continuing after the last frost score, {last.astimezone().isoformat(timespec='seconds')}")
    return last + timedelta(milliseconds=1)


def main() -> None:
    db.quack_token()  # stop now, with a clear message, if HP_DB_TOKEN is missing
    from heatpump.analysis import frost_image

    print(f"Frost model {frost_image.load_model()['version']}; images under {image_root()}")

    # systemd stops a service with SIGTERM; Ctrl-C sends SIGINT
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    con: duckdb.DuckDBPyConnection | None = None
    done_until: datetime | None = None
    db_ok = True  # so the "database down" message prints once per outage
    while not stop.is_set():
        try:
            if con is None:
                con = db.connect()
                frost.register_sensor(con)
            if done_until is None:
                done_until = start_time(con)
            done_until = catch_up(con, done_until, utc_now() - SETTLE)
            if not db_ok:
                print("Database is back")
            db_ok = True
        except duckdb.Error as e:
            if db_ok:
                print(f"Database unavailable, will keep trying: {e}")
            db_ok = False
            if con is not None:
                try:
                    con.close()
                except duckdb.Error:
                    pass
                con = None
            # What was stored before the error is in the database, so ask it again
            done_until = None
        stop.wait(CHECK_SECONDS)

    print("Stopped.")
    if con is not None:
        con.close()


if __name__ == "__main__":
    main()
