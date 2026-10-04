"""Pytest configuration for Ocea Smart Building tests."""

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture
def recorder_component_loaded(hass):
    """Mark Recorder loaded in tests that only exercise config-entry flows."""
    hass.config.components.add("recorder")
