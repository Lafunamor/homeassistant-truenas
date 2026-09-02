"""Tests for the TrueNAS config flow."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import (
    CONF_API_KEY,
    CONF_HOST,
    CONF_NAME,
    CONF_SSL,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.truenas.const import DOMAIN

USER_INPUT = {
    CONF_NAME: "TrueNAS",
    CONF_HOST: "10.0.0.1",
    CONF_API_KEY: "api-key",
    CONF_SSL: False,
    CONF_VERIFY_SSL: False,
}


class StubAPI:
    """Stub replacing TrueNASAPI in the config flow."""

    instances: list["StubAPI"] = []

    def __init__(self, host, api_key, verify_ssl, use_ssl=True):
        """Record the constructor arguments."""
        self.host = host
        self.use_ssl = use_ssl
        self.url = f"{'wss' if use_ssl else 'ws'}://{host}/api/current"
        self.disconnected = False
        StubAPI.instances.append(self)

    async def connection_test(self):
        """Return the canned connection result."""
        if StubAPI.results_by_scheme is not None:
            return StubAPI.results_by_scheme[self.use_ssl]

        return StubAPI.result

    async def disconnect(self):
        """Record that the socket was released."""
        self.disconnected = True


@pytest.fixture(autouse=True)
def reset_stub():
    """Reset the stub between tests."""
    StubAPI.instances = []
    StubAPI.result = (True, "")
    StubAPI.results_by_scheme = None
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
    assert StubAPI.instances[0].use_ssl is False
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


async def test_user_flow_falls_back_to_plain_http(hass: HomeAssistant) -> None:
    """Nothing listening on wss:// makes the flow try ws:// and store it."""
    StubAPI.results_by_scheme = {True: (False, "connection_refused"), False: (True, "")}
    with patch("custom_components.truenas.config_flow.TrueNASAPI", StubAPI):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_SSL: True}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SSL] is False
    assert [api.use_ssl for api in StubAPI.instances] == [True, False]


async def test_user_flow_does_not_downgrade_after_tls_error(
    hass: HomeAssistant,
) -> None:
    """A certificate problem must not silently fall back to plaintext."""
    StubAPI.results_by_scheme = {
        True: (False, "certificate_verify_failed"),
        False: (True, ""),
    }
    with patch("custom_components.truenas.config_flow.TrueNASAPI", StubAPI):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_SSL: True}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_HOST: "certificate_verify_failed"}
    assert [api.use_ssl for api in StubAPI.instances] == [True]


async def test_user_flow_respects_pinned_scheme(hass: HomeAssistant) -> None:
    """A scheme in the host field disables the fallback probe."""
    StubAPI.results_by_scheme = {True: (False, "connection_refused"), False: (True, "")}
    with patch("custom_components.truenas.config_flow.TrueNASAPI", StubAPI):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {**USER_INPUT, CONF_HOST: "https://10.0.0.1", CONF_SSL: True},
        )

    assert result["type"] is FlowResultType.FORM
    assert len(StubAPI.instances) == 1


async def test_user_flow_stops_on_invalid_key(hass: HomeAssistant) -> None:
    """A reachable endpoint with a bad key is not retried on the other scheme."""
    StubAPI.results_by_scheme = {True: (False, "invalid_key"), False: (True, "")}
    with patch("custom_components.truenas.config_flow.TrueNASAPI", StubAPI):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_SSL: True}
        )

    assert result["errors"] == {CONF_HOST: "invalid_key"}
    assert len(StubAPI.instances) == 1


async def test_key_rejected_over_plaintext_is_explained(hass: HomeAssistant) -> None:
    """A key rejected on the plain HTTP fallback names the likely cause."""
    StubAPI.results_by_scheme = {
        True: (False, "connection_refused"),
        False: (False, "invalid_key"),
    }
    with patch("custom_components.truenas.config_flow.TrueNASAPI", StubAPI):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_SSL: True}
        )

    assert result["errors"] == {CONF_HOST: "invalid_key_insecure"}


async def test_key_rejected_over_tls_is_reported_as_is(hass: HomeAssistant) -> None:
    """Over TLS a rejected key is simply a rejected key."""
    StubAPI.results_by_scheme = {True: (False, "invalid_key"), False: (True, "")}
    with patch("custom_components.truenas.config_flow.TrueNASAPI", StubAPI):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_SSL: True}
        )

    assert result["errors"] == {CONF_HOST: "invalid_key"}


async def test_options_flow_stores_the_setting(hass: HomeAssistant) -> None:
    """The disk temperature setting can be changed after setup."""
    from custom_components.truenas.const import CONF_DISK_TEMPERATURES

    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DISK_TEMPERATURES: False}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_DISK_TEMPERATURES] is False


async def test_options_flow_defaults_to_reading_temperatures(
    hass: HomeAssistant,
) -> None:
    """Existing entries keep the previous behaviour."""
    from custom_components.truenas.const import CONF_DISK_TEMPERATURES

    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["data_schema"]({})[CONF_DISK_TEMPERATURES] is True
