"""
Implement the low-level Flume and Rachio data collection logic.

This module contains the raw helper code that talks to the Flume and Rachio
HTTP APIs and combines their responses into a single irrigation usage report.

It includes:
- lightweight request helpers and error types
- small client classes for Flume and Rachio
- logic to build Flume usage queries for recently watered Rachio zones
- aggregation code that converts minute-level water data into per-zone totals

Compared with api.py, this module is closer to the vendor APIs and data
manipulation details. If something looks wrong in the calculated irrigation
report itself, this is usually the module to inspect first.
"""

import base64
import datetime
import json
import logging
from dataclasses import dataclass
from enum import Enum
from functools import reduce
from typing import TYPE_CHECKING, Any, Literal, cast

import pytz
import requests
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_serializer,
    field_validator,
    model_validator,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

FLUME_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def safe_request(
    url: str,
    timeout: int = 10,
    method: Callable = requests.get,
    **kwargs: dict[str, Any],
) -> dict:
    """
    Make an HTTP request and return parsed JSON or an empty dict on failure.

    This helper keeps the lower-level client code compact and ensures request
    failures are logged in one place.
    """
    try:
        response = method(url, timeout=timeout, **kwargs)
        response.raise_for_status()
        return response.json()
    except Exception:
        logger.exception("Request failed: %s", url)
    return {}


class DeviceType(Enum):
    """Describe the Flume device types that appear in device listings."""

    UNKNOWN = 0
    BRIDGE = 1
    SENSOR = 2


@dataclass
class FlumeDevice:
    """Represent the Flume device metadata needed for queries and display."""

    device_id: str
    device_name: str
    device_timezone: str
    device_type: DeviceType


class FlumeTokenError(Exception):
    """Raised when Flume authentication or metadata retrieval fails."""

    def __init__(self, message: str, detail: Any | None = None) -> None:
        """Initialize the error with an optional detail payload."""
        if detail is not None:
            message = f"{message}: {detail!r}"
        super().__init__(message)


class FlumeDeviceError(Exception):
    """Raised when the expected Flume device is not found."""

    def __init__(self, message: str, detail: Any | None = None) -> None:
        """Initialize the error with an optional detail payload."""
        if detail is not None:
            message = f"{message}: {detail!r}"
        super().__init__(message)


@dataclass(frozen=True)
class IrrigationMonitorCredentials:
    """Store the vendor credentials needed for one polling run."""

    flume_user: str
    flume_pass: str
    flume_client_id: str
    flume_client_secret: str
    rachio_token: str


class FlumeUsageQuery(BaseModel):
    """Validate and serialize one Flume usage query payload."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(
        min_length=1,
        description=(
            "Key used to identify this query in the Flume response when "
            "multiple queries are sent together."
        ),
    )
    since_datetime: datetime.datetime | str = Field(
        description=(
            "Restrict the query range to samples since this datetime. The "
            "value has no offset and represents time in the device timezone. "
            "Up to one year of data can be queried."
        ),
    )
    until_datetime: datetime.datetime | str | None = Field(
        default_factory=datetime.datetime.now,
        description=(
            "Restrict the query range to samples until this datetime. The "
            "value has no offset and represents time in the device timezone. "
            "Defaults to now. Up to one year of data can be queried."
        ),
    )
    units: Literal["GALLONS", "LITERS", "CUBIC_FEET", "CUBIC_METERS"] = Field(
        default="GALLONS",
        description="Unit of measurement used for returned water usage values.",
    )

    @field_validator("since_datetime", "until_datetime", mode="before")
    @classmethod
    def _normalize_timestamp(cls, value: Any) -> datetime.datetime | None:
        if value in (None, ""):
            return None
        if isinstance(value, datetime.datetime):
            return cls._normalize_datetime(value)
        if isinstance(value, str):
            try:
                parsed = datetime.datetime.fromisoformat(value)
                return cls._normalize_datetime(parsed)
            except ValueError as exception:
                message = "Timestamp must be a datetime or an ISO-like string"
                raise ValueError(message) from exception
        message = "Timestamp must be a datetime, pandas Timestamp, or string"
        raise TypeError(message)

    @staticmethod
    def _normalize_datetime(value: datetime.datetime) -> datetime.datetime:
        if value.tzinfo is None:
            return value
        return value.replace(tzinfo=None)

    @field_serializer("since_datetime", "until_datetime")
    def _serialize_timestamp(self, value: datetime.datetime | str | None) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.datetime.fromisoformat(value)
        return value.strftime(FLUME_DATETIME_FORMAT)

    @model_validator(mode="after")
    def _validate_time_range(self) -> FlumeUsageQuery:
        if self.since_datetime and self.until_datetime:
            start_time = cast("datetime.datetime", self.since_datetime)
            end_time = cast("datetime.datetime", self.until_datetime)
            if end_time < start_time:
                message = (
                    "until_datetime must be greater than or equal to since_datetime"
                )
                raise ValueError(message)
        return self

    def as_payload(self) -> dict[str, Any]:
        """
        Return the validated Flume payload options.

        Also adds the options we require for this integration.
        """
        static_options = {
            "bucket": "MIN",
            "operation": "SUM",
            "sort_direction": "ASC",
            "group_multiplier": 1,
        }

        payload = self.model_dump(exclude_none=True)

        return payload | static_options


@dataclass
class FlumeRequestData:
    """Relevant parts of a Flume usage query response for one request."""

    request_id: str
    total_usage: float

    def to_usage_dict(self) -> dict[str, float]:
        """
        Convert the Flume request data into a dict format.

        Used for report generation.
        """
        return {self.request_id: self.total_usage}


class FlumeClient:
    """
    Minimal synchronous client for the Flume API.

    This class handles authentication, device discovery, and water usage query
    requests for the selected Flume monitor.
    """

    def __init__(
        self, flume_user: str, flume_pass: str, flume_client: str, flume_secret: str
    ) -> None:
        """Authenticate with Flume and cache the user's device metadata."""
        self.flume_user = flume_user
        self.flume_pass = flume_pass
        self.flume_client = flume_client
        self.flume_secret = flume_secret

        self.token = self._get_flume_access_token()
        self.user_id = self._get_user_id()
        self._flume_info = self._get_my_flume_info()

    def _get_flume_access_token(self) -> str:
        """Exchange username/password credentials for a Flume access token."""
        url = "https://api.flumewater.com/oauth/token"
        payload = {
            "grant_type": "password",
            "username": self.flume_user,
            "password": self.flume_pass,
            "client_id": self.flume_client,
            "client_secret": self.flume_secret,
        }
        headers = {"accept": "application/json", "content-type": "application/json"}
        try:
            response_json = safe_request(
                url, method=requests.post, json=payload, headers=headers
            )
        except Exception as exception:
            msg = "Request to get Flume access token failed"
            raise FlumeTokenError(msg) from exception
        if data := response_json.get("data"):
            try:
                access_token = data[0].get("access_token")
                if not access_token:
                    msg = "No access_token field in Flume token response"
                    raise FlumeTokenError(msg, response_json)
            except IndexError as exception:
                msg = "No data array in Flume token response"
                raise FlumeTokenError(msg, response_json) from exception
        else:
            msg = "No data field in Flume token response"
            raise FlumeTokenError(msg, response_json)
        return access_token

    def _get_user_id(self) -> int:
        """Extract the Flume user ID encoded in the returned JWT."""
        try:
            jwt_payload = self.token.split(".")[1]
        except IndexError as exception:
            msg = "Invalid JWT token format"
            raise FlumeTokenError(msg, self.token) from exception
        try:
            data = json.loads(base64.b64decode(bytes(jwt_payload, "utf-8") + b"=="))
        except Exception as exception:
            msg = "Failed to decode JWT token"
            raise FlumeTokenError(msg, self.token) from exception
        user_id = data.get("user_id")
        if not user_id:
            msg = "No user_id field in decoded Flume token response"
            raise FlumeTokenError(msg, data)
        return user_id

    def _get_my_flume_info(self) -> dict[str, Any]:
        """Fetch the user's Flume devices and location metadata."""
        url = f"https://api.flumewater.com/users/{self.user_id}/devices?location=true"
        try:
            return safe_request(url, headers={"Authorization": f"Bearer {self.token}"})
        except Exception as exception:
            msg = "Request to get Flume device info failed"
            raise FlumeTokenError(msg) from exception

    @property
    def devices(self) -> list[FlumeDevice]:
        """Return normalized device metadata for all discovered Flume devices."""
        result = []
        device_data_list = self._flume_info.get("data", [])
        for device_data in device_data_list:
            location_data = device_data.get("location", {})
            device = FlumeDevice(
                device_id=device_data.get("id"),
                device_name=(
                    f"{location_data.get('name', '')} "
                    f"({location_data.get('address', '')})"
                ),
                device_timezone=location_data.get("tz"),
                device_type=DeviceType(device_data.get("type", 0)),
            )
            result.append(device)
        if not result:
            msg = "No devices found in Flume info response"
            raise FlumeDeviceError(msg, self._flume_info)
        return result

    @property
    def monitors(self) -> list[FlumeDevice]:
        """Return only Flume devices that represent monitor sensors."""
        monitors = [x for x in self.devices if x.device_type == DeviceType.SENSOR]
        if not monitors:
            msg = "No Flume monitor devices found"
            raise FlumeDeviceError(msg, self.devices)
        return monitors

    def query_usage(
        self, device_id: str, queries: list[FlumeUsageQuery], max_query_n: int = 10
    ) -> list[FlumeRequestData]:
        """Execute one or more Flume usage queries and return a combined DataFrame."""
        url = (
            f"https://api.flumewater.com/users/{self.user_id}/devices/{device_id}/query"
        )
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        # chunk queries into groups of max_query_n to avoid overwhelming the Flume API
        qlist = [
            queries[i : i + max_query_n] for i in range(0, len(queries), max_query_n)
        ]
        data_collect = []
        for q in qlist:
            payload = {"queries": [query.as_payload() for query in q]}
            result = safe_request(
                url, method=requests.post, json=payload, headers=headers
            )
            if result:
                for query_result in result.get("data", []):
                    for request_id, data in query_result.items():
                        data_collect.append(
                            FlumeRequestData(
                                request_id=request_id,
                                total_usage=data[0].get("value"),
                            )
                        )
        return data_collect


class RachioClientError(Exception):
    """Raised when Rachio API requests fail."""

    def __init__(self, message: str, detail: Any | None = None) -> None:
        """Initialize the error with an optional detail payload."""
        if detail is not None:
            message = f"{message}: {detail!r}"
        super().__init__(message)


@dataclass
class RachioZoneWateringSummary:
    """Summarize the last observed watering run for a Rachio zone."""

    zone_id: int
    zone_name: str
    last_watering_start_time: datetime.datetime
    last_watering_stop_time: datetime.datetime
    last_watering_duration_minutes: float
    est_gallons_per_minute: float
    est_gallons_used: float
    sqft: int
    enabled: bool


class RachioClient:
    """
    Minimal synchronous client for the Rachio API.

    The integration uses this class to discover recently watered zones and the
    time windows that should be matched against Flume flow data.
    """

    def __init__(self, token: str) -> None:
        """Store the token and resolve the current Rachio person ID."""
        self._token = token
        self._person_id = self._get_person_id()

    def _get_person_id(self) -> str:
        """Fetch the authenticated Rachio account identifier."""
        try:
            r = safe_request(
                "https://api.rach.io/1/public/person/info",
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except Exception as exception:
            msg = "Request to get Rachio person ID failed"
            raise RachioClientError(msg) from exception
        if not isinstance(r, dict):
            msg = "Unexpected Rachio person info response format"
            raise RachioClientError(msg, r)

        if not (person_id := r.get("id")):
            msg = "No 'id' field in Rachio person info response"
            raise RachioClientError(msg, r)
        return person_id

    def _get_my_rachio_info(self) -> dict[str, Any]:
        """Fetch the full Rachio person payload, including devices and zones."""
        try:
            return safe_request(
                f"https://api.rach.io/1/public/person/{self._person_id}",
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except Exception as exception:
            msg = "Request to get Rachio person info failed"
            raise RachioClientError(msg) from exception

    @staticmethod
    def _get_last_water_time_for_zone(
        zone_info: dict[str, Any], local_timezone: str = "US/Pacific"
    ) -> RachioZoneWateringSummary:
        """Convert one Rachio zone payload into a normalized watering summary row."""
        watering_duration = zone_info["lastWateredDuration"]
        watering_duration_td = datetime.timedelta(seconds=watering_duration)
        last_water_start_timestamp = zone_info["lastWateredDate"]
        last_water_start_datetime = datetime.datetime.fromtimestamp(
            last_water_start_timestamp / 1000,
            tz=datetime.UTC,
        ).astimezone(pytz.timezone(local_timezone))
        last_water_stop_datetime = last_water_start_datetime + watering_duration_td
        sqft = zone_info["yardAreaSquareFeet"]
        inph = zone_info["customNozzle"]["inchesPerHour"]
        gpm = 0.62 * sqft * inph / 60
        m = (last_water_stop_datetime - last_water_start_datetime).seconds / 60
        est_gallons = m * gpm

        return RachioZoneWateringSummary(
            zone_id=zone_info["zoneNumber"],
            zone_name=zone_info["name"],
            last_watering_start_time=last_water_start_datetime,
            last_watering_stop_time=last_water_stop_datetime,
            last_watering_duration_minutes=m,
            est_gallons_per_minute=gpm,
            est_gallons_used=est_gallons,
            sqft=zone_info["yardAreaSquareFeet"],
            enabled=zone_info["enabled"],
        )

    def get_last_watered_summary(
        self, *, enabled_only: bool = True, local_timezone: str = "US/Pacific"
    ) -> list[RachioZoneWateringSummary]:
        """Return a DataFrame summarizing the most recent watering per zone."""
        r = self._get_my_rachio_info()
        collect = []
        for zone_info in r["devices"][0]["zones"]:
            if enabled_only and not zone_info["enabled"]:
                continue
            collect.append(
                self._get_last_water_time_for_zone(
                    zone_info, local_timezone=local_timezone
                )
            )
        return collect


def _create_query_list(
    todays_watering: list[RachioZoneWateringSummary],
    time_offset_minutes: int = 0,
) -> list[FlumeUsageQuery]:
    """Build one Flume query per watered zone using the Rachio time windows."""
    time_offset = datetime.timedelta(minutes=time_offset_minutes)
    return [
        FlumeUsageQuery(
            request_id=zws.zone_name,
            since_datetime=zws.last_watering_start_time - time_offset,
            until_datetime=zws.last_watering_stop_time + time_offset,
        )
        for zws in todays_watering
    ]


class WaterReportDataPoint(BaseModel):
    """Represent one merged Rachio and Flume irrigation report row."""

    # Rachio last watering data
    zone_name: str = Field(
        ..., description="Name of the watered zone as reported by Rachio."
    )
    zone_id: int = Field(
        ..., description="Identifier of the watered zone as reported by Rachio."
    )
    watering_start_time: datetime.datetime = Field(
        ...,
        description=(
            "Start time of the most recent watering event for this zone "
            "as reported by Rachio."
        ),
    )
    watering_stop_time: datetime.datetime = Field(
        ...,
        description=(
            "Stop time of the most recent watering event for this zone "
            "as reported by Rachio."
        ),
    )
    total_watering_minutes: float = Field(
        ...,
        description=(
            "Total minutes of watering during the window as measured by Rachio."
        ),
    )
    # Flume usage
    total_gallons_used: float | None = Field(
        ...,
        description=(
            "Total gallons used during the watering window as measured by Flume."
        ),
    )

    @computed_field(return_type=float | None)
    @property
    def gallons_per_minute(self) -> float | None:
        """Calculate the average gallons per minute during the watering window."""
        if self.total_watering_minutes > 0 and self.total_gallons_used is not None:
            return self.total_gallons_used / self.total_watering_minutes
        return None


def poll_for_irrigation_usage(
    credentials: IrrigationMonitorCredentials,
    flume_device_index: int = 0,
) -> list[WaterReportDataPoint]:
    """
    Build the integration's final irrigation report.

    This is the core data pipeline:
    1. authenticate with Flume and choose a monitor
    2. fetch recently watered zones from Rachio
    3. query matching Flume usage windows
    4. aggregate the flow data per zone
    5. merge the measured usage with the Rachio zone metadata
    """
    flume_client = FlumeClient(
        flume_user=credentials.flume_user,
        flume_pass=credentials.flume_pass,
        flume_client=credentials.flume_client_id,
        flume_secret=credentials.flume_client_secret,
    )
    monitor = flume_client.monitors[flume_device_index]
    rachio_client = RachioClient(token=credentials.rachio_token)
    todays_watering = rachio_client.get_last_watered_summary(
        local_timezone=monitor.device_timezone
    )
    query_list = _create_query_list(todays_watering=todays_watering)
    water_data = flume_client.query_usage(
        device_id=monitor.device_id, queries=query_list
    )

    # Merge the Flume totals into a single lookup keyed by Rachio zone name.
    water_data_dict = reduce(
        lambda a, b: a | b, [x.to_usage_dict() for x in water_data]
    )

    # construct the final report data
    report_data = []
    for watering in todays_watering:
        datapoint = WaterReportDataPoint(
            zone_name=watering.zone_name,
            zone_id=watering.zone_id,
            watering_start_time=watering.last_watering_start_time,
            watering_stop_time=watering.last_watering_stop_time,
            total_gallons_used=water_data_dict.get(watering.zone_name, None),
            total_watering_minutes=watering.last_watering_duration_minutes,
        )
        report_data.append(datapoint)

    return report_data
