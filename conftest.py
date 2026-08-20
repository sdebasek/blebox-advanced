"""Test configuration.

Home Assistant fixtures come from pytest-homeassistant-custom-component; the
pure action-API tests do not use them.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load `custom_components/` in every Home Assistant test."""
    return
