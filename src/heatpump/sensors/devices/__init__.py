"""Sensor drivers, looked up by the `driver` setting in the config file."""

from importlib import import_module

from heatpump.sensors.config import DeviceConfig
from heatpump.sensors.device import Device

# Imported only when used, so a machine needs only the libraries for its own hardware
DRIVERS = {
    "dg700": "heatpump.sensors.devices.dg700:DG700",
    "fake": "heatpump.sensors.devices.fake:FakeDevice",
}


def make_device(config: DeviceConfig) -> Device:
    """Create the driver for a device. `driver` may also be a 'module:Class' path."""
    target = DRIVERS.get(config.driver, config.driver)
    if ":" not in target:
        raise ValueError(
            f"Device '{config.name}': unknown driver '{config.driver}'. "
            f"Known drivers: {', '.join(sorted(DRIVERS))}"
        )
    module, cls = target.split(":")
    return getattr(import_module(module), cls)(config)
