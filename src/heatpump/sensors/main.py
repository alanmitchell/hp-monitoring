"""Entry point for the `hp-sensors` console script.

Runs one thread per configured device plus one writer thread. See the
[sensors] section of config/config.example.toml.
"""

import argparse
import os
import queue
import signal
import threading
import time

from heatpump.common.config import load_config
from heatpump.common.db import quack_token
from heatpump.common.paths import data_dir
from heatpump.sensors.config import parse
from heatpump.sensors.device import DeviceThread
from heatpump.sensors.devices import make_device
from heatpump.sensors.filter import Reading
from heatpump.sensors.writer import Writer

WATCHDOG_CHECK_SECONDS = 5


def print_readings(readings: list[Reading]) -> None:
    for r in readings:
        value = "FAILED" if r.value is None else f"{r.value:.4f}"
        print(f"{r.ts.astimezone():%H:%M:%S.%f}"[:-3], f"{r.name:<24} {value}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Read the sensors and store the readings.")
    parser.add_argument(
        "--only", action="append", metavar="DEVICE",
        help="run just this device (repeat for several); default is all devices",
    )
    parser.add_argument(
        "--print", action="store_true", dest="print_only",
        help="print the readings that would be stored instead of writing to the database",
    )
    args = parser.parse_args(argv)

    try:
        config = parse(load_config())
        devices = config.devices
        if args.only:
            unknown = set(args.only) - {d.name for d in devices}
            if unknown:
                raise ValueError(f"No such device in the config: {', '.join(sorted(unknown))}")
            devices = [d for d in devices if d.name in args.only]
        if not devices:
            raise ValueError("No devices are configured under [sensors.devices]")

        stop = threading.Event()
        readings: queue.Queue[Reading] = queue.Queue()
        writer = None
        if args.print_only:
            emit = print_readings
        else:
            quack_token()  # fail now, with a clear message, if the token is missing
            # Separate spool per set of devices, so two hp-sensors processes never share one
            spool_name = "sensors-" + "-".join(sorted(args.only)) if args.only else "sensors"
            writer = Writer(
                readings,
                [ch for d in devices for ch in d.channels],
                data_dir() / "spool" / spool_name,
                stop,
                config.flush_seconds,
            )

            def emit(batch: list[Reading]) -> None:
                for r in batch:
                    readings.put(r)

        threads = [DeviceThread(make_device(d), emit, stop) for d in devices]
    except ValueError as e:
        raise SystemExit(f"Configuration error: {e}") from None

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    for t in threads:
        t.start()
    if writer:
        writer.start()
    print(f"Reading devices: {', '.join(d.name for d in devices)}")

    # Watchdog: a thread that stops updating last_alive is stuck in a call that
    # will never return. A thread cannot be killed, so exit and let systemd restart us.
    stuck = None
    while not stop.wait(WATCHDOG_CHECK_SECONDS):
        now = time.monotonic()
        for t in [*threads, *([writer] if writer else [])]:
            limit = max(config.watchdog_seconds, 3 * t.period)
            if now - t.last_alive > limit:
                print(f"Thread '{t.name}' has been stuck for over {limit:.0f} s; exiting")
                stuck = t
                stop.set()
                break

    print("Stopping")
    for t in threads:
        if t is not stuck:
            t.join(timeout=5)
    if writer:
        if writer is stuck:
            writer.spool_queue()  # save what is still queued; the next run loads it
        else:
            writer.join(timeout=30)
    if stuck:
        os._exit(1)  # a stuck thread would block a normal exit


if __name__ == "__main__":
    main()
