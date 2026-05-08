"""
Store shared constants for Irrigation Monitor.

This module keeps the integration domain name, config keys, attribution text,
and logger in one place so other modules can import the same values instead of
duplicating string literals.

Keeping these values centralized makes refactors safer and helps avoid bugs
caused by mismatched config key names.
"""

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "irrigation_monitor"
CONF_FLUME_USER = "flume_user"
CONF_FLUME_PASS = "flume_pass"  # noqa: S105
CONF_FLUME_CLIENT_ID = "flume_client_id"
CONF_FLUME_CLIENT_SECRET = "flume_client_secret"  # noqa: S105
CONF_RACHIO_TOKEN = "rachio_token"  # noqa: S105
CONF_FLUME_DEVICE_INDEX = "flume_device_index"
ATTRIBUTION = "Data provided by Flume and Rachio"
