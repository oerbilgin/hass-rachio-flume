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
from typing import Any, cast

import pandas as pd
import pytz
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)
import requests

logger = logging.getLogger(__name__)

FLUME_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def safe_request(url: str, timeout: int = 10, method=requests.get, **kwargs) -> dict:
    """Make an HTTP request and return parsed JSON or an empty dict on failure.

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
    device_timezone: str | None = None
    device_type: DeviceType = DeviceType.UNKNOWN

class FlumeTokenError(Exception):
    """Raised when Flume does not return an access token."""

    pass

class FlumeDeviceError(Exception):
    """Raised when the expected Flume device is not found."""

    pass


class FlumeUsageQuery(BaseModel):
    """Validate and serialize one Flume usage query payload."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    bucket: str = Field(default="MIN", min_length=1)
    since_datetime: datetime.datetime | str | None = None
    until_datetime: datetime.datetime | str | None = None
    group_multiplier: str | None = None
    operation: str | None = None
    sort_direction: str | None = None
    units: str | None = "GALLONS"

    @field_validator("since_datetime", "until_datetime", mode="before")
    @classmethod
    def _normalize_timestamp(cls, value: Any) -> datetime.datetime | None:
        if value in (None, ""):
            return None
        if isinstance(value, pd.Timestamp):
            value = value.to_pydatetime()
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
    def _serialize_timestamp(
        self, value: datetime.datetime | str | None
    ) -> str | None:
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
                    "until_datetime must be greater than or equal to "
                    "since_datetime"
                )
                raise ValueError(message)
        return self

    def as_payload(self) -> dict[str, Any]:
        """Return the validated Flume payload with omitted null fields."""
        return self.model_dump(exclude_none=True)

class FlumeClient:
    """Minimal synchronous client for the Flume API.

    This class handles authentication, device discovery, and water usage query
    requests for the selected Flume monitor.
    """

    def __init__(
        self, flume_user: str, flume_pass: str, flume_client: str, flume_secret: str
    ):
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
            raise FlumeTokenError("Request to get Flume access token failed") from exception
        if data := response_json.get("data"):
            try:
                access_token = data[0].get("access_token")
                if not access_token:
                    raise FlumeTokenError(f"No access_token field in Flume token response: '{response_json}'")
            except IndexError:
                raise FlumeTokenError(f"No data array in Flume token response: '{response_json}'")
        else:
            raise FlumeTokenError(f"No data field in Flume token response: '{response_json}'")
        return access_token

    def _get_user_id(self) -> int:
        """Extract the Flume user ID encoded in the returned JWT."""
        try:
            jwt_payload = self.token.split(".")[1]
        except IndexError:
            raise FlumeTokenError(f"Invalid JWT token format: {self.token}")
        try:
            data = json.loads(base64.b64decode(bytes(jwt_payload, "utf-8") + b"=="))
        except Exception as exception:
            raise FlumeTokenError(f"Failed to decode JWT token: {self.token}") from exception
        user_id = data.get("user_id")
        if not user_id:
            raise FlumeTokenError(f"No user_id field in decodedFlume token response: '{data}'")
        return user_id

    def _get_my_flume_info(self) -> dict[str, Any]:
        """Fetch the user's Flume devices and location metadata."""
        url = f"https://api.flumewater.com/users/{self.user_id}/devices?location=true"
        try:
            return safe_request(url, headers={"Authorization": f"Bearer {self.token}"})
        except Exception as exception:
            raise FlumeTokenError("Request to get Flume device info failed") from exception

    @property
    def devices(self) -> list[FlumeDevice]:
        """Return normalized device metadata for all discovered Flume devices."""
        result = []
        device_data_list = self._flume_info.get("data", [])
        for device_data in device_data_list:
            location_data = device_data.get("location", {})
            device = FlumeDevice(
                device_id=device_data.get("id"),
                device_name=f"{location_data.get('name', '')} ({location_data.get('address', '')})",
                device_timezone=location_data.get("tz"),
                device_type=DeviceType(device_data.get("type", 0)),
            )
            result.append(device)
        if not result:
            raise FlumeDeviceError(f"No devices found in Flume info response: '{self._flume_info}'")
        return result

    @property
    def monitors(self) -> list[FlumeDevice]:
        """Return only Flume devices that represent monitor sensors."""
        monitors = [x for x in self.devices if x.device_type == DeviceType.SENSOR]
        if not monitors:
            raise FlumeDeviceError(f"No Flume monitor devices found: '{self.devices}'")
        return monitors

    @staticmethod
    def create_single_query(
        request_id: str,
        bucket: str = "MIN",
        start_time: datetime.datetime | pd.Timestamp | str | None = None,
        end_time: datetime.datetime | pd.Timestamp | str | None = None,
        group_multiplier: str | None = None,
        operation: str | None = None,
        sort_direction: str | None = None,
        units: str | None = "GALLONS",
    ) -> dict[str, Any]:
        """Build one Flume usage query payload for a requested time range."""
        return FlumeUsageQuery(
            request_id=request_id,
            bucket=bucket,
            since_datetime=start_time,
            until_datetime=end_time,
            group_multiplier=group_multiplier,
            operation=operation,
            sort_direction=sort_direction,
            units=units,
        ).as_payload()

    def query_usage(self, device_id: str, queries: list[dict], max_query_n: int = 10):
        """Execute one or more Flume usage queries and return a combined DataFrame."""
        url = (
            f"https://api.flumewater.com/users/{self.user_id}/devices/{device_id}/query"
        )
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        qlist = [
            queries[i : i + max_query_n] for i in range(0, len(queries), max_query_n)
        ]
        data_collect = []
        for q in qlist:
            payload = {"queries": q}
            result = safe_request(
                url, method=requests.post, json=payload, headers=headers
            )
            if result:
                for query_result in result.get("data", []):
                    for request_id, data in query_result.items():
                        tmp = pd.DataFrame(data)
                        tmp["request_id"] = request_id
                        data_collect.append(tmp)
        if data_collect:
            df = pd.concat(data_collect)
            df["datetime"] = pd.to_datetime(df["datetime"])
            return df
        return pd.DataFrame()


class RachioClient:
    """Minimal synchronous client for the Rachio API.

    The integration uses this class to discover recently watered zones and the
    time windows that should be matched against Flume flow data.
    """

    def __init__(self, token: str):
        """Store the token and resolve the current Rachio person ID."""
        self.token = token
        self.person_id = self._get_person_id()

    def _get_person_id(self):
        """Fetch the authenticated Rachio account identifier."""
        r = safe_request(
            "https://api.rach.io/1/public/person/info",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        return r.get("id")

    def _get_my_rachio_info(self):
        """Fetch the full Rachio person payload, including devices and zones."""
        return safe_request(
            f"https://api.rach.io/1/public/person/{self.person_id}",
            headers={"Authorization": f"Bearer {self.token}"},
        )

    @staticmethod
    def _get_last_water_time_for_zone(zone_info, local_timezone: str = "US/Pacific"):
        """Convert one Rachio zone payload into a normalized watering summary row."""
        watering_duration = zone_info["lastWateredDuration"]
        watering_duration_td = datetime.timedelta(seconds=watering_duration)
        last_water_start_timestamp = zone_info["lastWateredDate"]
        last_water_start_datetime = datetime.datetime.fromtimestamp(
            last_water_start_timestamp / 1000,
            tz=datetime.timezone.utc,
        ).astimezone(pytz.timezone(local_timezone))
        last_water_stop_datetime = last_water_start_datetime + watering_duration_td
        sqft = zone_info["yardAreaSquareFeet"]
        inph = zone_info["customNozzle"]["inchesPerHour"]
        gpm = 0.62 * sqft * inph / 60
        m = (last_water_stop_datetime - last_water_start_datetime).seconds / 60
        est_gallons = m * gpm
        return {
            "zone_id": zone_info["zoneNumber"],
            "zone_name": zone_info["name"],
            "last_watering_start_time": last_water_start_datetime,
            "last_watering_stop_time": last_water_stop_datetime,
            "est_gallons_per_minute": gpm,
            "est_gallons_used": est_gallons,
            "sqft": zone_info["yardAreaSquareFeet"],
            "enabled": zone_info["enabled"],
        }

    def get_last_watered_summary(
        self, enabled_only=True, local_timezone: str = "US/Pacific"
    ):
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
        return pd.DataFrame(collect)


def _create_query_list(
    flume_client: FlumeClient,
    todays_watering: pd.DataFrame,
    time_offset_minutes: int = 0,
) -> list[dict]:
    """Build one Flume query per watered zone using the Rachio time windows."""
    time_offset = datetime.timedelta(minutes=time_offset_minutes)
    query_list = [
        flume_client.create_single_query(
            request_id=row["zone_name"],
            start_time=row["last_watering_start_time"] - time_offset,
            end_time=row["last_watering_stop_time"] + time_offset,
        )
        for _, row in todays_watering.iterrows()
    ]
    return query_list


def _summarize_watering(grp):
    """Reduce minute-level Flume readings into one per-zone summary row."""
    total_gallons = grp["value"].sum()
    total_time = grp["datetime"].max() - grp["datetime"].min()
    total_seconds = total_time.total_seconds()
    total_minutes = total_seconds / 60
    gpm = total_gallons / total_minutes
    data = {
        "total_gallons": total_gallons,
        "total_watering_minutes": total_minutes,
        "gallons_per_minute": gpm,
    }
    return pd.Series(data)


def poll_for_irrigation_usage(
    flume_user: str,
    flume_pass: str,
    flume_client_id: str,
    flume_client_secret: str,
    rachio_token: str,
    flume_device_index: int = 0,
) -> pd.DataFrame:
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
        flume_user=flume_user,
        flume_pass=flume_pass,
        flume_client=flume_client_id,
        flume_secret=flume_client_secret,
    )
    monitor = flume_client.monitors[flume_device_index]
    rachio_client = RachioClient(token=rachio_token)
    todays_watering = rachio_client.get_last_watered_summary(
        local_timezone=monitor["device_timezone"]
    )
    query_list = _create_query_list(
        flume_client=flume_client, todays_watering=todays_watering
    )
    water_data = flume_client.query_usage(
        device_id=monitor["device_id"], queries=query_list
    )
    water_data = water_data.rename({"request_id": "zone_name"}, axis=1)
    if water_data is None or water_data.empty:
        # No water data returned; create an empty summary with expected columns
        water_data_summary = pd.DataFrame(
            columns=[
                "zone_name",
                "total_gallons",
                "total_watering_minutes",
                "gallons_per_minute",
            ]
        )
    else:
        water_data_summary = (
            water_data
            .groupby("zone_name")
            .apply(_summarize_watering, include_groups=False)  # type: ignore
            .reset_index()
        )
    irrigation_report = water_data_summary.merge(todays_watering, how="right")
    return irrigation_report


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv

    load_dotenv("../.env")

    flume_user = os.environ["flume_user"]
    flume_pass = os.environ["flume_pass"]
    flume_client_id = os.environ["flume_client_id"]
    flume_client_secret = os.environ["flume_client_secret"]
    rachio_token = os.environ["rachio_token"]

    report = poll_for_irrigation_usage(
        flume_user=flume_user,
        flume_pass=flume_pass,
        flume_client_id=flume_client_id,
        flume_client_secret=flume_client_secret,
        rachio_token=rachio_token,
        flume_device_index=0,
    )
    print(report)
