"""Turns the [sensors] section of the config file into checked Python objects."""

from dataclasses import dataclass, field

DEVICE_KEYS = {"driver", "period", "report_together", "channels"}
CHANNEL_KEYS = {
    "kind", "unit", "location", "description", "deadband", "max_interval", "transform", "store",
}  # fmt: skip


@dataclass
class ChannelConfig:
    name: str  # unique across all devices; becomes sensors.name in the database
    kind: str = "analog"  # 'analog' | 'binary'
    unit: str | None = None
    location: str | None = None
    description: str | None = None
    deadband: float | None = None  # None = store every sample
    max_interval: float = 60.0  # seconds; store at least this often
    transform: str | None = None  # formula from raw reading to engineering value
    store: bool = True  # False = only used by other channels' transforms
    options: dict = field(default_factory=dict)  # anything else: driver-specific


@dataclass
class DeviceConfig:
    name: str
    driver: str
    period: float | None = None  # seconds between polls; unused by streaming devices
    report_together: bool = False  # if one channel is stored, store them all
    channels: list[ChannelConfig] = field(default_factory=list)
    options: dict = field(default_factory=dict)  # anything else: driver-specific


@dataclass
class SensorsConfig:
    devices: list[DeviceConfig]
    flush_seconds: float = 2.0  # how often readings are written to the database
    watchdog_seconds: float = 120.0  # exit if a thread is stuck this long


def parse(raw: dict) -> SensorsConfig:
    """Build a SensorsConfig from the whole config dict. Raises ValueError on mistakes."""
    section = raw.get("sensors", {})
    default_max_interval = float(section.get("max_interval", 60.0))
    devices = []
    seen: dict[str, str] = {}  # channel name -> device name

    for dev_name, dev in section.get("devices", {}).items():
        if "driver" not in dev:
            raise ValueError(f"Device '{dev_name}' has no 'driver' setting")
        channels = []
        for ch_name, ch in dev.get("channels", {}).items():
            if ch_name in seen:
                raise ValueError(
                    f"Channel name '{ch_name}' is used by both '{seen[ch_name]}' and '{dev_name}'"
                )
            seen[ch_name] = dev_name
            kind = ch.get("kind", "analog")
            if kind not in ("analog", "binary"):
                raise ValueError(f"Channel '{ch_name}': kind must be 'analog' or 'binary'")
            deadband = ch.get("deadband")
            channels.append(
                ChannelConfig(
                    name=ch_name,
                    kind=kind,
                    unit=ch.get("unit"),
                    location=ch.get("location"),
                    description=ch.get("description"),
                    deadband=None if deadband is None else float(deadband),
                    max_interval=float(ch.get("max_interval", default_max_interval)),
                    transform=ch.get("transform"),
                    store=bool(ch.get("store", True)),
                    options={k: v for k, v in ch.items() if k not in CHANNEL_KEYS},
                )
            )
        if not channels:
            raise ValueError(f"Device '{dev_name}' has no channels")
        period = dev.get("period")
        devices.append(
            DeviceConfig(
                name=dev_name,
                driver=dev["driver"],
                period=None if period is None else float(period),
                report_together=bool(dev.get("report_together", False)),
                channels=channels,
                options={k: v for k, v in dev.items() if k not in DEVICE_KEYS},
            )
        )

    return SensorsConfig(
        devices=devices,
        flush_seconds=float(section.get("flush_seconds", 2.0)),
        watchdog_seconds=float(section.get("watchdog_seconds", 120.0)),
    )
