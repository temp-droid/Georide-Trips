"""Test fixtures.

api.py has no homeassistant imports, so it is loaded directly as a
standalone module ("georide_api"). This keeps the Phase-1 harness free of
the full Home Assistant test stack; package-level imports
(custom_components.georide_trips.*) become available in CI once
pytest-homeassistant-custom-component is in play.
"""

import importlib.util
import sys
from pathlib import Path

_API_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "georide_trips"
    / "api.py"
)
_spec = importlib.util.spec_from_file_location("georide_api", _API_PATH)
georide_api = importlib.util.module_from_spec(_spec)
sys.modules["georide_api"] = georide_api
_spec.loader.exec_module(georide_api)
