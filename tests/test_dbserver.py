"""Runs the real `hp-db` in a subprocess and talks to it over Quack.

A subprocess is required: a Quack server and client in one Python process hang.
"""

import shutil
import signal
import time

import duckdb
import pytest
from conftest import start_hp_db

from heatpump.common import db, schema
from heatpump.dbserver import main


def test_migrate_creates_schema_and_is_repeatable():
    con = duckdb.connect()
    assert schema.migrate(con) == 0
    assert schema.migrate(con) == schema.SCHEMA_VERSION  # second run does nothing
    tables = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
    assert {"sensors", "calibrations", "readings", "images", "image_metrics",
            "mode_intervals", "readings_1min", "schema_info", "config_history"} <= tables
    con.execute("SELECT * FROM readings_cal")


def test_migrate_upgrades_an_older_database():
    con = duckdb.connect()
    schema.current_version(con)  # creates schema_info
    con.execute(schema.MIGRATIONS[0])
    con.execute("INSERT INTO schema_info (version) VALUES (1)")
    con.execute("INSERT INTO sensors (sensor_id, name, kind) VALUES (1, 'keep_me', 'analog')")
    assert schema.migrate(con) == 1
    assert schema.current_version(con) == schema.SCHEMA_VERSION
    con.execute("SELECT ts, source, content FROM config_history")
    assert con.execute("SELECT name FROM sensors").fetchall() == [("keep_me",)]


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


def test_timed_checkpoint_moves_writes_into_the_database_file(db_env, monkeypatch, tmp_path):
    monkeypatch.setenv("HP_DB_CHECKPOINT_SECONDS", "0.5")
    proc = start_hp_db()
    try:
        con = db.connect()
        con.execute("INSERT INTO hp.sensors (sensor_id, name, kind) VALUES (1, 'a', 'analog')")
        con.execute("INSERT INTO hp.readings (ts, sensor_id, value) VALUES (now(), 1, 35.5)")
        # The client stays connected: a checkpoint must not need the clients gone
        time.sleep(2)
        # A copy of the database file alone, without its .wal, while hp-db runs
        copy = tmp_path / "copy.duckdb"
        shutil.copy(db.db_path(), copy)
        local = duckdb.connect(str(copy), read_only=True)
        assert local.execute("SELECT value FROM readings_cal").fetchall() == [(35.5,)]
        con.execute("INSERT INTO hp.readings (ts, sensor_id, value) VALUES (now(), 1, 36)")
        assert con.execute("SELECT count(*) FROM hp.readings").fetchone()[0] == 2
    finally:
        proc.kill()
        proc.wait()


def test_checkpoint_setting(monkeypatch):
    monkeypatch.delenv("HP_DB_CHECKPOINT_SECONDS", raising=False)
    assert main.checkpoint_seconds() == 3600
    monkeypatch.setenv("HP_DB_CHECKPOINT_SECONDS", "0")
    assert main.checkpoint_seconds() is None
    monkeypatch.setenv("HP_DB_CHECKPOINT_SECONDS", "hourly")
    with pytest.raises(SystemExit):
        main.checkpoint_seconds()


def test_wrong_token_is_rejected(server, monkeypatch):
    monkeypatch.setenv("HP_DB_TOKEN", "wrong-token")
    with pytest.raises(duckdb.Error):
        db.connect().execute("SELECT count(*) FROM hp.readings").fetchall()


def test_missing_token_is_an_error(monkeypatch):
    monkeypatch.delenv("HP_DB_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        db.quack_token()
