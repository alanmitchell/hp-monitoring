"""Shared fixtures."""

import os
import socket
import subprocess
import sys

import pytest


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


def start_hp_db() -> subprocess.Popen:
    """Start the real hp-db using the current environment; wait until it serves."""
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "heatpump.dbserver.main"],
        env=os.environ.copy(), stdout=subprocess.PIPE, text=True,
    )
    # A new database prints a "Migrated schema" line first
    assert any("Serving" in proc.stdout.readline() for _ in range(2))
    return proc


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    """Point HP_DATA_DIR, HP_DB_URI and HP_DB_TOKEN at a throwaway location."""
    monkeypatch.setenv("HP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HP_DB_URI", f"quack:localhost:{free_port()}")
    monkeypatch.setenv("HP_DB_TOKEN", "test-token")


@pytest.fixture
def server(db_env):
    """A running hp-db. A Quack server and client hang in one process, hence the subprocess."""
    proc = start_hp_db()
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()
