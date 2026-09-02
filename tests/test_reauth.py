"""Tests for re-authenticating a revoked TrueNAS API key."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_NAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.truenas.const import DOMAIN

ENTRY_DATA = {
    CONF_NAME: "TrueNAS",
    CONF_HOST: "10.0.0.1",
    CONF_API_KEY: "old-key",
    CONF_VERIFY_SSL: False,
}


@pytest.fixture(name="api")
def api_fixture():
    """An API stub whose key the NAS rejects."""
    api = MagicMock()
    api.connected.return_value = False
    api.error = "invalid_key"
    api.connect = AsyncMock(return_value=False)
    api.disconnect = AsyncMock()
    api.query = AsyncMock(return_value=None)
    with patch("custom_components.truenas.coordinator.TrueNASAPI", return_value=api):
        yield api


async def test_rejected_key_starts_reauth(hass: HomeAssistant, api) -> None:
    """A revoked key asks the user for a new one instead of retrying forever."""
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_reauth_stores_the_new_key(hass: HomeAssistant, api) -> None:
    """Submitting a working key updates the entry."""
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    flow = hass.config_entries.flow.async_progress_by_handler(DOMAIN)[0]
    result = await hass.config_entries.flow.async_configure(flow["flow_id"])
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    class WorkingAPI:
        url = "wss://10.0.0.1/api/current"

        def __init__(self, *args, **kwargs):
            pass

        async def connection_test(self):
            return True, ""

        async def disconnect(self):
            return None

    with patch("custom_components.truenas.config_flow.TrueNASAPI", WorkingAPI):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: "new-key"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == "new-key"
    # The rest of the configuration is preserved.
    assert entry.data[CONF_HOST] == "10.0.0.1"
