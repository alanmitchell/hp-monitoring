"""hp-sensors: config parsing, the change filter, device threads and the writer."""

import queue
import threading
import time
from datetime import UTC, datetime, timedelta

import duckdb
import pytest
from conftest import start_hp_db

from heatpump.common import db
from heatpump.sensors.config import ChannelConfig, DeviceConfig, parse
from heatpump.sensors.device import Device, DeviceThread
from heatpump.sensors.devices import make_device
from heatpump.sensors.filter import QUALITY_FAILED, ChangeFilter, Reading
from heatpump.sensors.writer import Writer

T0 = datetime(2026, 11, 14, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


# ---- config ----

def test_parse_config():
    cfg = parse({"sensors": {"max_interval": 30, "devices": {"lj": {
        "driver": "fake", "period": 1, "port": "/dev/x",
        "channels": {"t1": {"unit": "degC", "deadband": 0.1, "source": "AIN0"},
                     "run": {"kind": "binary", "max_interval": 300}},
    }}}})
    dev = cfg.devices[0]
    assert (dev.name, dev.driver, dev.period, dev.options) == ("lj", "fake", 1.0, {"port": "/dev/x"})
    t1, run = dev.channels
    assert (t1.deadband, t1.max_interval, t1.options) == (0.1, 30.0, {"source": "AIN0"})
    assert (run.kind, run.deadband, run.max_interval) == ("binary", None, 300.0)


def test_duplicate_channel_name_is_rejected():
    dev = {"driver": "fake", "channels": {"t1": {}}}
    with pytest.raises(ValueError, match="t1"):
        parse({"sensors": {"devices": {"a": dev, "b": dev}}})


def test_unknown_driver_is_rejected():
    with pytest.raises(ValueError, match="unknown driver"):
        make_device(DeviceConfig("x", "nope", channels=[ChannelConfig("c")]))


# ---- change filter ----

def test_no_deadband_stores_every_sample():
    f = ChangeFilter("c", None, 60)
    assert [len(f.feed(at(i), 1.0)) for i in range(3)] == [1, 1, 1]


def test_deadband_and_max_interval():
    f = ChangeFilter("c", 0.5, 10)
    assert len(f.feed(at(0), 20.0)) == 1  # first sample
    assert f.feed(at(1), 20.3) == []  # within deadband
    assert f.feed(at(2), 20.4) == []
    # A change stores the last skipped sample too, so the step keeps its shape
    assert f.feed(at(3), 21.0) == [Reading(at(2), "c", 20.4), Reading(at(3), "c", 21.0)]
    assert f.feed(at(12), 21.1) == []
    assert f.feed(at(13), 21.1) == [Reading(at(13), "c", 21.1)]  # 10 s since last stored


def test_failed_read_is_a_change():
    f = ChangeFilter("c", 0.5, 10)
    f.feed(at(0), 20.0)
    assert f.feed(at(1), None, QUALITY_FAILED) == [Reading(at(1), "c", None, QUALITY_FAILED)]
    assert f.feed(at(2), None, QUALITY_FAILED) == []  # still failing: wait for max_interval
    assert len(f.feed(at(3), 20.0)) == 2  # recovery


# ---- device threads ----

class ScriptedDevice(Device):
    """Returns the next item of `script` on each read; an exception item is raised."""

    def __init__(self, config):
        super().__init__(config)
        self.script = []
        self.events = []

    def open(self):
        self.events.append("open")

    def close(self):
        self.events.append("close")

    def read(self):
        if not self.script:
            time.sleep(0.01)
            raise TimeoutError("no more data")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def run_device(device, seconds=0.3):
    out, stop = [], threading.Event()
    thread = DeviceThread(device, out.extend, stop)
    thread.start()
    time.sleep(seconds)
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    return out


def test_polled_device_reads_on_period_boundaries():
    cfg = DeviceConfig("d", "x", period=0.05, channels=[ChannelConfig("c")])
    dev = ScriptedDevice(cfg)
    dev.script = [{"c": float(i)} for i in range(3)]
    out = [r for r in run_device(dev) if r.value is not None]
    assert [r.value for r in out] == [0.0, 1.0, 2.0]
    for r in out:  # each read happened just after a multiple of the period
        assert r.ts.timestamp() % 0.05 < 0.03


def test_failure_stores_bad_reading_and_reconnects():
    cfg = DeviceConfig("d", "x", period=0.02, channels=[ChannelConfig("c", deadband=0.0)])
    dev = ScriptedDevice(cfg)
    dev.script = [{"c": 1.0}, OSError("unplugged"), {"c": 2.0}]
    out = run_device(dev, seconds=1.5)  # includes the 1 s wait before reconnecting
    assert [(r.value, r.quality) for r in out[:3]] == [(1.0, 0), (None, QUALITY_FAILED), (2.0, 0)]
    assert dev.events[:3] == ["open", "close", "open"]


def test_report_together_keeps_pairs_matched():
    class Stream(ScriptedDevice):
        streaming = True

    cfg = DeviceConfig("rtd", "x", report_together=True, channels=[
        ChannelConfig("supply", deadband=0.5), ChannelConfig("return", deadband=0.5)])
    dev = Stream(cfg)
    dev.script = [{"supply": 35.0, "return": 30.0}, {"supply": 35.1, "return": 30.1},
                  {"supply": 36.0, "return": 30.2}]  # only supply moves beyond its deadband
    out = [r for r in run_device(dev, seconds=0.1) if r.value is not None]
    assert [(r.name, r.value) for r in out] == [
        ("supply", 35.0), ("return", 30.0),
        ("supply", 35.1), ("supply", 36.0), ("return", 30.1), ("return", 30.2)]
    assert out[2].ts == out[4].ts and out[3].ts == out[5].ts


def test_fake_driver():
    cfg = DeviceConfig("demo", "fake", period=1, channels=[
        ChannelConfig("t"), ChannelConfig("sw", kind="binary")])
    values = make_device(cfg).read()
    assert 15 <= values["t"] <= 25 and values["sw"] in (0.0, 1.0)


# ---- writer ----

def make_writer(tmp_path):
    q, stop = queue.Queue(), threading.Event()
    channels = [ChannelConfig("supply_temp", unit="degC"), ChannelConfig("run", kind="binary")]
    return q, Writer(q, channels, tmp_path / "spool", stop)


def count_rows():
    con = db.connect()
    try:
        return con.execute("SELECT count(*) FROM hp.readings").fetchone()[0]
    finally:
        con.close()


def test_writer_registers_sensors_and_inserts(server, tmp_path):
    q, writer = make_writer(tmp_path)
    q.put(Reading(at(0), "supply_temp", 35.5))
    q.put(Reading(at(1), "run", 1.0))
    q.put(Reading(at(2), "supply_temp", None, QUALITY_FAILED))
    writer.flush()
    con = db.connect()
    rows = con.execute(
        "SELECT name, value, quality FROM hp.readings JOIN hp.sensors USING (sensor_id) ORDER BY ts"
    ).fetchall()
    assert rows == [("supply_temp", 35.5, 0), ("run", 1.0, 0), ("supply_temp", None, 2)]
    assert con.execute("SELECT epoch(ts) FROM hp.readings ORDER BY ts LIMIT 1").fetchone()[0] == T0.timestamp()
    assert list((tmp_path / "spool").iterdir()) == []


def test_readings_survive_a_database_outage(server, tmp_path):
    q, writer = make_writer(tmp_path)
    q.put(Reading(at(0), "supply_temp", 1.0))
    writer.flush()

    server.kill()  # as abrupt as it gets
    server.wait()
    for i in range(1, 4):  # three flushes while the database is down
        q.put(Reading(at(i), "supply_temp", float(i)))
        writer.flush()
    assert len(list((tmp_path / "spool").iterdir())) == 1

    restarted = start_hp_db()
    try:
        q.put(Reading(at(4), "supply_temp", 4.0))
        writer.flush()
        assert count_rows() == 5
        assert list((tmp_path / "spool").iterdir()) == []
    finally:
        restarted.kill()
        restarted.wait()


def test_spool_left_by_a_previous_run_is_loaded(db_env, tmp_path):
    q, writer = make_writer(tmp_path)  # hp-db is not running at all
    q.put(Reading(at(0), "supply_temp", 1.0))
    writer.flush()

    proc = start_hp_db()
    try:
        _, new_writer = make_writer(tmp_path)  # a fresh process would start like this
        new_writer.flush()
        assert count_rows() == 1
    finally:
        proc.kill()
        proc.wait()


def test_direct_connection_error_type(db_env):
    with pytest.raises(duckdb.Error):  # the writer relies on this to detect an outage
        db.connect()
