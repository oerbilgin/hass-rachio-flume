"""
Define the UI configuration flow for Irrigation Monitor.

Home Assistant uses this module when a user adds the integration through the
Settings -> Devices & Services UI.

This module contains:
- the form fields shown to the user
- validation logic for Flume and Rachio credentials
- the unique-id logic that prevents duplicate entries
- creation of the saved config entry data used at runtime

If you want to change what the setup form asks for, or how authentication is
validated, this is the module to edit.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector
from slugify import slugify

from .api import (
    IrrigationMonitorApiClient,
    IrrigationMonitorApiClientAuthenticationError,
    IrrigationMonitorApiClientCommunicationError,
    IrrigationMonitorApiClientError,
)
from .const import (
    CONF_FLUME_CLIENT_ID,
    CONF_FLUME_CLIENT_SECRET,
    CONF_FLUME_DEVICE_INDEX,
    CONF_FLUME_PASS,
    CONF_FLUME_USER,
    CONF_RACHIO_TOKEN,
    DOMAIN,
    LOGGER,
)


class IrrigationConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """
    Collect credentials from the UI and create one config entry.

    A config flow is Home Assistant's wizard-like setup object. This class owns
    the user-facing form, handles validation, and decides when an integration
    entry should be created or rejected.
    """

    VERSION = 1

    _reauth_entry: config_entries.ConfigEntry | None = None

    @staticmethod
    def _normalize_user_input(user_input: dict) -> dict:
        """Convert selector output into the types the integration expects."""
        normalized_input = dict(user_input)
        normalized_input[CONF_FLUME_DEVICE_INDEX] = int(
            normalized_input.get(CONF_FLUME_DEVICE_INDEX, 0)
        )
        return normalized_input

    @staticmethod
    def _build_schema(user_input: dict[str, Any] | None = None) -> vol.Schema:
        """Build the shared schema used by initial setup and reauth."""
        return vol.Schema(
            {
                vol.Required(
                    CONF_FLUME_USER,
                    default=(user_input or {}).get(CONF_FLUME_USER, vol.UNDEFINED),
                ): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.TEXT,
                    ),
                ),
                vol.Required(CONF_FLUME_PASS): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.PASSWORD,
                    ),
                ),
                vol.Required(
                    CONF_FLUME_CLIENT_ID,
                    default=(user_input or {}).get(CONF_FLUME_CLIENT_ID, vol.UNDEFINED),
                ): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.TEXT,
                    ),
                ),
                vol.Required(CONF_FLUME_CLIENT_SECRET): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.PASSWORD,
                    ),
                ),
                vol.Required(CONF_RACHIO_TOKEN): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.PASSWORD,
                    ),
                ),
                vol.Optional(
                    CONF_FLUME_DEVICE_INDEX,
                    default=(user_input or {}).get(CONF_FLUME_DEVICE_INDEX, 0),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        mode=selector.NumberSelectorMode.BOX,
                        step=1,
                    ),
                ),
            }
        )

    async def async_step_reauth(
        self,
        entry_data: dict[str, Any],
    ) -> config_entries.ConfigFlowResult:
        """Start Home Assistant's reauthentication flow for an existing entry."""
        entry_id = self.context.get("entry_id")
        if not isinstance(entry_id, str):
            return self.async_abort(reason="entry_id key not found in context")

        self._reauth_entry = self.hass.config_entries.async_get_entry(entry_id)
        return await self.async_step_reauth_confirm(entry_data)

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Collect replacement credentials and update the existing entry."""
        errors: dict[str, str] = {}
        if self._reauth_entry is None:
            return self.async_abort(reason="reauth_entry not found")

        merged_input = dict(self._reauth_entry.data)
        merged_input.update(user_input or {})

        if user_input is not None:
            merged_input = self._normalize_user_input(merged_input)
            try:
                await self._test_credentials(merged_input)
            except IrrigationMonitorApiClientAuthenticationError as exception:
                LOGGER.warning(exception)
                errors["base"] = "auth"
            except IrrigationMonitorApiClientCommunicationError as exception:
                LOGGER.error(exception)
                errors["base"] = "connection"
            except IrrigationMonitorApiClientError as exception:
                LOGGER.exception(exception)
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    self._reauth_entry,
                    data_updates=merged_input,
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self._build_schema(merged_input),
            errors=errors,
        )

    async def async_step_user(
        self,
        user_input: dict | None = None,
    ) -> config_entries.ConfigFlowResult:
        """
        Show and process the main setup form.

        On first display this returns the form schema. Once the user submits
        credentials, it validates them, assigns a stable unique ID, and creates
        the config entry Home Assistant will later load through __init__.py.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = self._normalize_user_input(user_input)
            try:
                await self._test_credentials(user_input)
            except IrrigationMonitorApiClientAuthenticationError as exception:
                LOGGER.warning(exception)
                errors["base"] = "auth"
            except IrrigationMonitorApiClientCommunicationError as exception:
                LOGGER.error(exception)
                errors["base"] = "connection"
            except IrrigationMonitorApiClientError as exception:
                LOGGER.exception(exception)
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(
                    f"{slugify(user_input[CONF_FLUME_USER])}-{user_input[CONF_FLUME_DEVICE_INDEX]}"
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Irrigation Monitor {user_input[CONF_FLUME_DEVICE_INDEX]}",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self._build_schema(user_input),
            errors=errors,
        )

    async def _test_credentials(self, user_input: dict) -> None:
        """Delegate credential validation to the API client wrapper."""
        client = IrrigationMonitorApiClient(user_input)
        await client.async_validate_credentials(self.hass)
