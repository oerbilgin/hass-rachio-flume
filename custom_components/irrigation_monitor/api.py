"""
Wrap external data fetching for the Irrigation Monitor integration.

This module sits between Home Assistant and the lower-level helper functions in
util.py. It gives the rest of the integration one consistent interface for:
- validating credentials during config flow setup
- fetching fresh irrigation report data for the coordinator
- translating raw exceptions into integration-specific error types

The main class here does not talk directly to entities. Instead, the
coordinator calls it whenever Home Assistant needs a refreshed snapshot of the
combined Flume and Rachio data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .const import (
    CONF_FLUME_CLIENT_ID,
    CONF_FLUME_CLIENT_SECRET,
    CONF_FLUME_DEVICE_INDEX,
    CONF_FLUME_PASS,
    CONF_FLUME_USER,
    CONF_RACHIO_TOKEN,
)
from .util import (
    FlumeTokenError,
    RachioClient,
    WaterReportDataPoint,
    poll_for_irrigation_usage,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant


class IrrigationMonitorApiClientError(Exception):
    """Base exception for integration-level fetch or processing failures."""


class IrrigationMonitorApiClientAuthenticationError(IrrigationMonitorApiClientError):
    """Raised when Flume or Rachio credentials are rejected."""


class IrrigationMonitorApiClientCommunicationError(IrrigationMonitorApiClientError):
    """Raised when the integration can reach the API but gets unusable data."""


class IrrigationMonitorApiClient:
    """
    Expose one async-friendly interface for validation and data refreshes.

    The rest of the integration does not call util.py directly. Instead it uses
    this wrapper so Home Assistant-facing code only has to deal with a small set
    of methods and integration-specific exceptions.
    """

    def __init__(self, config: Mapping[str, Any]) -> None:
        """Store config entry data needed to talk to Flume and Rachio."""
        self._config = config

    def _get_flume_device_index(self) -> int:
        """Return the Flume device index as an integer."""
        return int(self._config.get(CONF_FLUME_DEVICE_INDEX, 0))

    async def async_validate_credentials(self, hass: HomeAssistant) -> None:
        """
        Validate credentials during config flow setup.

        The work is moved to an executor because the underlying HTTP logic is
        synchronous and should not block Home Assistant's event loop.
        """
        await hass.async_add_executor_job(self._validate_credentials)

    async def async_get_data(
        self, hass: HomeAssistant | None
    ) -> list[WaterReportDataPoint]:
        """
        Return the latest combined irrigation report.

        The returned object is one item per Rachio zone enriched with Flume
        usage totals.
        """
        try:
            if hass is None:
                return await self._async_call_poll_in_thread()
            return await hass.async_add_executor_job(
                poll_for_irrigation_usage,
                self._config[CONF_FLUME_USER],
                self._config[CONF_FLUME_PASS],
                self._config[CONF_FLUME_CLIENT_ID],
                self._config[CONF_FLUME_CLIENT_SECRET],
                self._config[CONF_RACHIO_TOKEN],
                self._get_flume_device_index(),
            )
        except FlumeTokenError as exception:
            raise IrrigationMonitorApiClientAuthenticationError(
                exception
            ) from exception
        except IndexError as exception:
            raise IrrigationMonitorApiClientCommunicationError(exception) from exception
        except Exception as exception:
            raise IrrigationMonitorApiClientError(exception) from exception

    def _validate_credentials(self) -> None:
        """
        Run a full data fetch as a simple credential and connectivity check.

        This is a pragmatic validation path for the prototype integration:
        if the combined polling flow works, the credentials and device index are
        considered usable.
        """
        try:
            poll_for_irrigation_usage(
                self._config[CONF_FLUME_USER],
                self._config[CONF_FLUME_PASS],
                self._config[CONF_FLUME_CLIENT_ID],
                self._config[CONF_FLUME_CLIENT_SECRET],
                self._config[CONF_RACHIO_TOKEN],
                self._get_flume_device_index(),
            )
        except FlumeTokenError as exception:
            raise IrrigationMonitorApiClientAuthenticationError(
                exception
            ) from exception
        except IndexError as exception:
            raise IrrigationMonitorApiClientCommunicationError(exception) from exception
        except Exception as exception:
            try:
                RachioClient(token=self._config[CONF_RACHIO_TOKEN])
            except Exception as rachio_exception:
                raise IrrigationMonitorApiClientAuthenticationError(
                    rachio_exception
                ) from rachio_exception
            raise IrrigationMonitorApiClientError(exception) from exception

    async def _async_call_poll_in_thread(self) -> list[WaterReportDataPoint]:
        """Run the same polling logic outside a Home Assistant executor context."""
        return poll_for_irrigation_usage(
            self._config[CONF_FLUME_USER],
            self._config[CONF_FLUME_PASS],
            self._config[CONF_FLUME_CLIENT_ID],
            self._config[CONF_FLUME_CLIENT_SECRET],
            self._config[CONF_RACHIO_TOKEN],
            self._get_flume_device_index(),
        )
