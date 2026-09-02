"""Tests for the TrueNAS config flow."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_NAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.truenas.const import DOMAIN

USER_INPUT = {
    CONF_NAME: "TrueNAS",
    CONF_HOST: "http://10.0.0.1",
    CONF_API_KEY: "api-key",
    CONF_VERIFY_SSL: False,
}


class StubAPI:
    """Stub replacing TrueNASAPI in the config flow."""

    instances: list["StubAPI"] = []

    def __init__(self, host, api_key, verify_ssl):
        """Record the constructor arguments."""
        self.host = host
        self.disconnected = False
        StubAPI.instances.append(self)

    def connection_test(self):
        """Return the canned connection result."""
        return StubAPI.result

    def disconnect(self):
        """Record that the socket was released."""
        self.disconnected = True


@pytest.fixture(autouse=True)
def reset_stub():
    """Reset the stub between tests."""
    StubAPI.instances = []
    StubAPI.result = (True, "")
    yield


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    """A reachable TrueNAS creates a config entry and releases the socket."""
    with patch("custom_components.truenas.config_flow.TrueNASAPI", StubAPI):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == USER_INPUT
    assert StubAPI.instances[0].disconnected is True


async def test_user_flow_connection_error(hass: HomeAssistant) -> None:
    """A failed connection is surfaced on the host field."""
    StubAPI.result = (False, "connection_refused")
    with patch("custom_components.truenas.config_flow.TrueNASAPI", StubAPI):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_HOST: "connection_refused"}
    assert StubAPI.instances[0].disconnected is True


async def test_user_flow_invalid_host(hass: HomeAssistant) -> None:
    """An unparsable host is rejected before any connection attempt."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_HOST: "ftp://10.0.0.1"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_HOST: "invalid_hostname"}


async def test_user_flow_unknown_error_code(hass: HomeAssistant) -> None:
    """A connection failure without an error code still shows a message."""
    StubAPI.result = (False, "")
    with patch("custom_components.truenas.config_flow.TrueNASAPI", StubAPI):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["errors"] == {CONF_HOST: "cannot_connect"}
