"""Moves readings from the queue into the database, without losing any.

Every batch is first appended to a spool file on disk, then the spool files are
loaded into `hp-db` and deleted. If the database is down the files simply
accumulate and are loaded, oldest first, once it is back. Readings also survive
a crash of this program, because they are on disk before the insert is tried.

Known limit: if the program dies after an insert succeeded but before the file
was deleted, that file's rows are inserted again on the next run.
"""

import json
import queue
import threading
import time
from pathlib import Path

import duckdb

from heatpump.common import db
from heatpump.sensors.config import ChannelConfig
from heatpump.sensors.filter import Reading

MAX_LINES_PER_FILE = 50_000

INSERT_SQL = """
INSERT INTO hp.readings (ts, sensor_id, value, quality)
SELECT j.ts, s.sensor_id, j.value, j.quality
FROM read_json('{path}', format = 'newline_delimited', ignore_errors = true,
     columns = {{ts: 'TIMESTAMPTZ', name: 'VARCHAR', value: 'DOUBLE', quality: 'UTINYINT'}}) j
JOIN sensor_ids s USING (name)
ORDER BY j.ts
"""


class Writer(threading.Thread):
    def __init__(
        self,
        readings: queue.Queue[Reading],
        channels: list[ChannelConfig],
        spool_dir: Path,
        stop: threading.Event,
        flush_seconds: float = 2.0,
        config_text: str | None = None,
    ):
        super().__init__(name="writer", daemon=True)
        self.readings = readings
        self.channels = channels
        self.spool_dir = spool_dir
        self.stop_event = stop
        self.flush_seconds = flush_seconds
        self.config_text = config_text  # saved to config_history when it changes
        self.period = flush_seconds
        self.last_alive = time.monotonic()  # watched by the watchdog in main
        self._con: duckdb.DuckDBPyConnection | None = None
        self._db_ok = True  # so the "database down" message prints once per outage
        self._file: Path | None = None  # spool file currently being appended to
        self._file_lines = 0
        spool_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> None:
        while not self.stop_event.wait(self.flush_seconds):
            self.flush()
        self.flush()  # whatever arrived just before the stop
        self._disconnect()

    def flush(self) -> None:
        self.last_alive = time.monotonic()
        self.spool_queue()
        self._load_spool()

    def spool_queue(self) -> None:
        """Append everything waiting in the queue to the current spool file."""
        lines = []
        while True:
            try:
                r = self.readings.get_nowait()
            except queue.Empty:
                break
            row = {"ts": r.ts.isoformat(), "name": r.name, "value": r.value, "quality": r.quality}
            lines.append(json.dumps(row))
        if not lines:
            return
        if self._file is None or self._file_lines >= MAX_LINES_PER_FILE:
            # Zero-padded so that sorting by name is sorting by age
            self._file = self.spool_dir / f"readings-{time.time_ns():020d}.jsonl"
            self._file_lines = 0
        with self._file.open("a") as f:
            f.write("\n".join(lines) + "\n")
        self._file_lines += len(lines)

    def _load_spool(self) -> None:
        files = sorted(self.spool_dir.glob("readings-*.jsonl"))
        if not files:
            return
        try:
            con = self._connect()
            for path in files:
                con.execute(INSERT_SQL.format(path=str(path).replace("'", "''")))
                path.unlink()
                if path == self._file:
                    self._file = None
            if not self._db_ok:
                print("Database is back; spooled readings loaded")
            self._db_ok = True
        except duckdb.Error as e:
            if self._db_ok:
                print(f"Database unavailable, spooling to {self.spool_dir}: {e}")
            self._db_ok = False
            self._disconnect()

    def _connect(self) -> duckdb.DuckDBPyConnection:
        if self._con is None:
            con = db.connect()
            try:
                self._register_sensors(con)
                self._record_config(con)
            except duckdb.Error:
                con.close()
                raise
            self._con = con
        return self._con

    def _disconnect(self) -> None:
        if self._con is not None:
            try:
                self._con.close()
            except duckdb.Error:
                pass
            self._con = None

    def _record_config(self, con: duckdb.DuckDBPyConnection) -> None:
        """Save the config file to `config_history` if it differs from the last saved one."""
        if self.config_text is None:
            return
        last = con.execute(
            "SELECT content FROM hp.config_history WHERE source = 'hp-sensors' "
            "ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if last and last[0] == self.config_text:
            return
        con.execute("CREATE OR REPLACE TEMP TABLE new_config (content VARCHAR)")
        con.execute("INSERT INTO new_config VALUES (?)", [self.config_text])
        con.execute("INSERT INTO hp.config_history SELECT now(), 'hp-sensors', content FROM new_config")
        print("Saved the changed config file to config_history")

    def _register_sensors(self, con: duckdb.DuckDBPyConnection) -> None:
        """Add configured channels missing from the `sensors` table, and build the
        local name -> sensor_id table that INSERT_SQL joins against.

        Quack cannot read one hp.* table while inserting into another in the same
        statement, and parameters do not work on hp.* tables. So everything is
        prepared in local temp tables first.
        """
        ids = dict(con.execute("SELECT name, sensor_id FROM hp.sensors").fetchall())
        new = [ch for ch in self.channels if ch.name not in ids]
        if new:
            next_id = max(ids.values(), default=0) + 1
            con.execute(
                "CREATE OR REPLACE TEMP TABLE new_sensors (sensor_id USMALLINT, name VARCHAR, "
                "kind VARCHAR, unit VARCHAR, location VARCHAR, description VARCHAR)"
            )
            con.executemany(
                "INSERT INTO new_sensors VALUES (?, ?, ?, ?, ?, ?)",
                [
                    [next_id + i, ch.name, ch.kind, ch.unit, ch.location, ch.description]
                    for i, ch in enumerate(new)
                ],
            )
            con.execute("INSERT INTO hp.sensors SELECT * FROM new_sensors")
            ids.update({ch.name: next_id + i for i, ch in enumerate(new)})
            print(f"Registered new sensors: {', '.join(ch.name for ch in new)}")
        con.execute("CREATE OR REPLACE TEMP TABLE sensor_ids (name VARCHAR, sensor_id USMALLINT)")
        con.executemany("INSERT INTO sensor_ids VALUES (?, ?)", [list(item) for item in ids.items()])
