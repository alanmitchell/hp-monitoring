import importlib
from importlib.metadata import entry_points

import pytest

SUBPACKAGES = ["common", "dbserver", "sensors", "camera", "analysis", "web"]
SCRIPTS = ["hp-db", "hp-sensors", "hp-camera", "hp-web", "hp"]


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_imports(name):
    importlib.import_module(f"heatpump.{name}")


@pytest.mark.parametrize("script", SCRIPTS)
def test_console_script_resolves(script):
    (ep,) = entry_points(group="console_scripts", name=script)
    assert callable(ep.load())
