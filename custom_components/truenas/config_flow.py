"""Config flow to configure TrueNAS."""

from __future__ import annotations

from collections.abc import Mapping
from logging import getLogger
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    CONN_CLASS_LOCAL_POLL,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import (
    CONF_API_KEY,
    CONF_HOST,
    CONF_NAME,
    CONF_SSL,
    CONF_VERIFY_SSL,
)
from homeassistant.core import callback

from .const import (
    DEFAULT_DEVICE_NAME,
    DEFAULT_HOST,
    DEFAULT_SSL,
    DEFAULT_SSL_VERIFY,
    DOMAIN,
)
from .api import RETRYABLE_SCHEME_ERRORS, TrueNASAPI, has_scheme

_LOGGER = getLogger(__name__)


def _base_schema(truenas_config: Mapping[str, Any]) -> vol.Schema:
    """Generate base schema."""
    base_schema = {
        vol.Required(
            CONF_NAME, default=truenas_config.get(CONF_NAME) or DEFAULT_DEVICE_NAME
        ): str,
        vol.Required(
            CONF_HOST, default=truenas_config.get(CONF_HOST) or DEFAULT_HOST
        ): str,
        vol.Required(CONF_API_KEY, default=truenas_config.get(CONF_API_KEY) or ""): str,
        vol.Required(
            CONF_SSL,
            default=truenas_config.get(CONF_SSL, DEFAULT_SSL),
        ): bool,
        vol.Required(
            CONF_VERIFY_SSL,
            default=truenas_config.get(CONF_VERIFY_SSL) or DEFAULT_SSL_VERIFY,
        ): bool,
    }

    return vol.Schema(base_schema)


def _reconfigure_schema(truenas_config: Mapping[str, Any]) -> vol.Schema:
    """Generate base schema."""
    base_schema = {
        vol.Required(
            CONF_HOST, default=truenas_config.get(CONF_HOST) or DEFAULT_HOST
        ): str,
        vol.Required(CONF_API_KEY, default=truenas_config.get(CONF_API_KEY) or ""): str,
        vol.Required(
            CONF_SSL,
            default=truenas_config.get(CONF_SSL, DEFAULT_SSL),
        ): bool,
        vol.Required(
            CONF_VERIFY_SSL,
            default=truenas_config.get(CONF_VERIFY_SSL) or DEFAULT_SSL_VERIFY,
        ): bool,
    }

    return vol.Schema(base_schema)


# ---------------------------
#   configured_instances
# ---------------------------
@callback
def configured_instances(hass):
    """Return a set of configured instances."""
    return {
        entry.data[CONF_NAME] for entry in hass.config_entries.async_entries(DOMAIN)
    }


# ---------------------------
#   TrueNASConfigFlow
# ---------------------------
class TrueNASConfigFlow(ConfigFlow, domain=DOMAIN):
    """TrueNASConfigFlow class."""

    VERSION = 1
    CONNECTION_CLASS = CONN_CLASS_LOCAL_POLL

    def __init__(self) -> None:
        """Initialize the config flow."""
        self.truenas_config: dict[str, Any] = {}

    async def _async_test_connection(
        self, truenas_config: dict[str, Any]
    ) -> str | None:
        """Find a working endpoint for the configured host.

        The preferred scheme is tried first. If nothing is listening there,
        and the user did not pin a scheme in the host field, the other scheme
        is tried as well and the result is stored on the entry.
        """
        host = truenas_config[CONF_HOST]
        preferred = truenas_config.get(CONF_SSL, DEFAULT_SSL)
        candidates = [preferred]
        if not has_scheme(host):
            candidates.append(not preferred)

        first_error: str | None = None
        for use_ssl in candidates:
            errorcode = await self._async_probe(truenas_config, use_ssl)
            if errorcode is None:
                truenas_config[CONF_SSL] = use_ssl
                return None

            if errorcode == "invalid_key" and not use_ssl:
                # TrueNAS answers on the plain HTTP port but refuses to
                # authenticate an API key over an unencrypted transport, so a
                # rejected key here usually means the HTTPS port was missed.
                errorcode = "invalid_key_insecure"

            first_error = first_error or errorcode
            if errorcode not in RETRYABLE_SCHEME_ERRORS:
                return errorcode

        return first_error or "cannot_connect"

    async def _async_probe(
        self, truenas_config: dict[str, Any], use_ssl: bool
    ) -> str | None:
        """Return an error code when one endpoint cannot be reached."""
        try:
            api = TrueNASAPI(
                truenas_config[CONF_HOST],
                truenas_config[CONF_API_KEY],
                truenas_config[CONF_VERIFY_SSL],
                use_ssl,
            )
        except ValueError as err:
            _LOGGER.error("TrueNAS invalid host (%s)", err)
            return "invalid_hostname"

        try:
            conn, errorcode = await api.connection_test()
        finally:
            await api.disconnect()

        if conn:
            _LOGGER.info("TrueNAS reachable at %s", api.url)
            return None

        _LOGGER.debug("TrueNAS %s not reachable (%s)", api.url, errorcode)
        return errorcode or "cannot_connect"

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user."""
        truenas_config = self.truenas_config
        errors = {}

        if user_input is not None:
            truenas_config.update(user_input)

            # Check if instance with this name already exists
            if truenas_config[CONF_NAME] in configured_instances(self.hass):
                errors["base"] = "name_exists"

            # Test connection
            errorcode = await self._async_test_connection(truenas_config)
            if errorcode:
                errors[CONF_HOST] = errorcode

            # Save instance
            if not errors:
                return self.async_create_entry(
                    title=truenas_config[CONF_NAME], data=truenas_config
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_base_schema(truenas_config),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle an API key the NAS no longer accepts."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new API key for an existing entry."""
        reauth_entry = self._get_reauth_entry()
        errors = {}

        if user_input is not None:
            truenas_config = {**reauth_entry.data, **user_input}
            errorcode = await self._async_test_connection(truenas_config)
            if errorcode:
                errors["base"] = errorcode
            else:
                return self.async_update_reload_and_abort(
                    reauth_entry, data_updates=truenas_config
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): str}),
            errors=errors,
            description_placeholders={"name": reauth_entry.title},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        truenas_config = self.truenas_config
        reconfigure_entry = self._get_reconfigure_entry()
        errors = {}

        if user_input is not None:
            truenas_config.update(user_input)

            # Test connection
            errorcode = await self._async_test_connection(truenas_config)
            if errorcode:
                errors[CONF_HOST] = errorcode

            # Save instance
            if not errors:
                return self.async_update_reload_and_abort(
                    self._get_reconfigure_entry(),
                    title=reconfigure_entry.data[CONF_NAME],
                    data_updates=truenas_config,
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_reconfigure_schema(reconfigure_entry.data),
            errors=errors,
        )
