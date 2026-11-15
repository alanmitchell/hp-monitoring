"""Runs the real `hp-db` in a subprocess and talks to it over Quack.

A subprocess is required: a Quack server and client in one Python process hang.
"""

import signal

import duckdb
import pytest

from heatpump.common import db, schema


def test_migrate_creates_schema_and_is_repeatable():
    con = duckdb.connect()
    assert schema.migrate(con) == 0
    assert schema.migrate(con) == schema.SCHEMA_VERSION  # second run does nothing
    tables = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
    assert {"sensors", "calibrations", "readings", "images", "image_metrics",
            "mode_intervals", "readings_1min", "schema_info"} <= tables
    con.execute("SELECT * FROM readings_cal")


def test_migrate_refuses_a_newer_database():
    con = duckdb.connect()
    schema.migrate(con)
    con.execute("INSERT INTO schema_info (version) VALUES (?)", [schema.SCHEMA_VERSION + 1])
    with pytest.raises(RuntimeError):
        schema.migrate(con)


def test_client_writes_survive_a_clean_shutdown(server):
    con = db.connect()
    con.execute("INSERT INTO hp.sensors (sensor_id, name, kind) VALUES (1, 'supply_temp', 'analog')")
    con.execute("INSERT INTO hp.readings (ts, sensor_id, value) VALUES (now(), 1, 35.5)")
    assert con.execute("SELECT name, value FROM hp.readings_cal").fetchall() == [("supply_temp", 35.5)]
    con.close()

    server.send_signal(signal.SIGTERM)
    assert server.wait(timeout=20) == 0

    # With the server gone, the file can be opened directly
    con = duckdb.connect(str(db.db_path()), read_only=True)
    assert con.execute("SELECT count(*) FROM readings").fetchone()[0] == 1
    assert not db.db_path().with_suffix(".duckdb.wal").exists()  # checkpointed on close


def test_wrong_token_is_rejected(server, monkeypatch):
    monkeypatch.setenv("HP_DB_TOKEN", "wrong-token")
    with pytest.raises(duckdb.Error):
        db.connect().execute("SELECT count(*) FROM hp.readings").fetchall()


def test_missing_token_is_an_error(monkeypatch):
    monkeypatch.delenv("HP_DB_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        db.quack_token()
