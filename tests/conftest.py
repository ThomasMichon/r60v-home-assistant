"""Shared pytest fixtures for the Rocket R60V integration tests.

Uses ``pytest-homeassistant-custom-component`` to spin up a real Home Assistant
core in-process. The ``enable_custom_integrations`` fixture (provided by that
plugin) clears HA's custom-integration cache so it re-scans for integrations.

The plugin ships its own ``custom_components`` package (its ``testing_config``),
which shadows this repo's top-level ``custom_components`` on import. To let HA
discover ``custom_components/rocket_r60v`` from this repo, we extend that
package's ``__path__`` to include this repo's integration directory.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_CUSTOM_COMPONENTS = str(REPO_ROOT / "custom_components")

# Registers the plugin's fixtures (``hass``, ``enable_custom_integrations``, ...).
pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable HA to load this repo's custom integration in every test.

    ``enable_custom_integrations`` clears the cached custom-component scan; we
    then graft this repo's ``custom_components`` directory onto the plugin's
    ``custom_components`` package so ``custom_components.rocket_r60v`` resolves
    to the integration under test.
    """
    import custom_components

    if REPO_CUSTOM_COMPONENTS not in custom_components.__path__:
        custom_components.__path__.append(REPO_CUSTOM_COMPONENTS)

    yield
