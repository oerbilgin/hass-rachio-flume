"""Unit tests for the low-level irrigation utility helpers."""

from __future__ import annotations

# ruff: noqa: S101, S106, TID252
import datetime

import pytest
import requests
from pydantic import ValidationError

from .. import util  # for monkeypatching
from ..util import (
    DeviceType,
    FlumeAuthenticationError,
    FlumeDevice,
    FlumeRequestData,
    FlumeUsageQuery,
    IrrigationMonitorCredentials,
    IrrigationMonitorRequestAuthError,
    IrrigationMonitorRequestDNSError,
    RachioZoneWateringSummary,
    WaterReportDataPoint,
    _create_query_list,
    _create_retry_session,
    poll_for_irrigation_usage,
)


def test_flume_usage_query_as_payload_normalizes_aware_datetimes() -> None:
    """Flume payload serialization should strip tzinfo before formatting."""
    start = datetime.datetime(2024, 6, 1, 12, 0, tzinfo=datetime.UTC)
    end = datetime.datetime(2024, 6, 1, 12, 30, tzinfo=datetime.UTC)

    query = FlumeUsageQuery(
        request_id="Front Yard",
        since_datetime=start,
        until_datetime=end,
    )

    assert query.since_datetime == start.replace(tzinfo=None)
    assert query.until_datetime == end.replace(tzinfo=None)
    assert query.as_payload() == {
        "request_id": "Front Yard",
        "since_datetime": "2024-06-01 12:00:00",
        "until_datetime": "2024-06-01 12:30:00",
        "units": "GALLONS",
        "bucket": "MIN",
        "operation": "SUM",
        "sort_direction": "ASC",
        "group_multiplier": 1,
    }


def test_flume_usage_query_rejects_inverted_time_range() -> None:
    """Flume queries should reject ranges where the end precedes the start."""
    with pytest.raises(ValidationError, match="until_datetime"):
        FlumeUsageQuery(
            request_id="Front Yard",
            since_datetime=datetime.datetime(2024, 6, 1, 12, 30, tzinfo=datetime.UTC),
            until_datetime=datetime.datetime(2024, 6, 1, 12, 0, tzinfo=datetime.UTC),
        )


def test_create_query_list_applies_time_offset() -> None:
    """Query generation should expand the watering window by the offset."""
    start = datetime.datetime(2024, 6, 1, 6, 0, tzinfo=datetime.UTC)
    stop = datetime.datetime(2024, 6, 1, 6, 20, tzinfo=datetime.UTC)
    watering = RachioZoneWateringSummary(
        zone_id=1,
        zone_name="Back Yard",
        last_watering_start_time=start,
        last_watering_stop_time=stop,
        last_watering_duration_minutes=20,
        est_gallons_per_minute=5.0,
        est_gallons_used=100.0,
        sqft=100,
        enabled=True,
    )

    query_list = _create_query_list([watering], time_offset_minutes=5)

    assert len(query_list) == 1
    assert query_list[0].request_id == "Back Yard"
    assert query_list[0].since_datetime == datetime.datetime(
        2024, 6, 1, 5, 55, tzinfo=datetime.UTC
    ).replace(tzinfo=None)
    assert query_list[0].until_datetime == datetime.datetime(
        2024, 6, 1, 6, 25, tzinfo=datetime.UTC
    ).replace(tzinfo=None)


@pytest.mark.parametrize(
    ("total_gallons_used", "total_watering_minutes", "expected_gpm"),
    [
        (30.0, 10.0, 3.0),
        (None, 10.0, None),
        (30.0, 0.0, None),
    ],
)
def test_water_report_data_point_gallons_per_minute(
    total_gallons_used: float | None,
    total_watering_minutes: float,
    expected_gpm: float | None,
) -> None:
    """Gallons-per-minute should be derived only for valid measured windows."""
    datapoint = WaterReportDataPoint(
        zone_name="Side Yard",
        zone_id=2,
        watering_start_time=datetime.datetime(2024, 6, 1, 7, 0, tzinfo=datetime.UTC),
        watering_stop_time=datetime.datetime(2024, 6, 1, 7, 10, tzinfo=datetime.UTC),
        total_watering_minutes=total_watering_minutes,
        total_gallons_used=total_gallons_used,
    )

    assert datapoint.gallons_per_minute == expected_gpm


def test_safe_request_classifies_unauthorized_http_errors() -> None:
    """401/403 responses should be classified as authorization failures."""

    class FakeResponse:
        status_code = 401
        text = '{"error":"invalid_client"}'

        def raise_for_status(self) -> None:
            raise requests.HTTPError("unauthorized", response=self)

    class FakeSession:
        def request(
            self, *, method: str, url: str, timeout: int, **kwargs: object
        ) -> FakeResponse:
            assert method == "GET"
            assert url == "https://example.com/oauth/token"
            assert timeout == 10
            return FakeResponse()

    with pytest.raises(IrrigationMonitorRequestAuthError, match="authorization"):
        util.safe_request(
            "https://example.com/oauth/token",
            session=FakeSession(),
        )


def test_safe_request_classifies_dns_errors() -> None:
    """Name resolution failures should be exposed as DNS-specific errors."""

    class FakeSession:
        def request(
            self, *, method: str, url: str, timeout: int, **kwargs: object
        ) -> requests.Response:
            raise requests.ConnectionError(
                "HTTPSConnection(host='api.flumewater.com', port=443): "
                "Failed to resolve 'api.flumewater.com' "
                "([Errno -3] Try again)"
            )

    with pytest.raises(IrrigationMonitorRequestDNSError, match="DNS"):
        util.safe_request(
            "https://api.flumewater.com/oauth/token",
            method="POST",
            session=FakeSession(),
        )


def test_create_retry_session_mounts_post_retry_adapter() -> None:
    """The shared session should mount adapters that retry transient POST requests."""
    session = _create_retry_session()
    adapter = session.get_adapter("https://api.flumewater.com")
    retries = adapter.max_retries

    assert retries.total == util.REQUEST_RETRY_ATTEMPTS
    assert retries.connect == util.REQUEST_RETRY_ATTEMPTS
    assert retries.read == util.REQUEST_RETRY_ATTEMPTS
    assert retries.status == util.REQUEST_RETRY_ATTEMPTS
    assert retries.backoff_factor == util.REQUEST_RETRY_BACKOFF_FACTOR
    assert retries.allowed_methods == util.REQUEST_RETRYABLE_METHODS
    assert retries.status_forcelist == util.REQUEST_RETRYABLE_STATUS_CODES


def test_flume_client_rejects_unauthorized_token_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unauthorized Flume token requests should surface as auth errors."""

    def fake_safe_request(*args: object, **kwargs: object) -> dict[str, object]:
        raise IrrigationMonitorRequestAuthError("Request authorization failed")

    monkeypatch.setattr(util, "safe_request", fake_safe_request)

    with pytest.raises(FlumeAuthenticationError, match="credentials were rejected"):
        util.FlumeClient(
            flume_user="user@example.com",
            flume_pass="secret",
            flume_client="client-id",
            flume_secret="client-secret",
        )


def test_flume_client_treats_missing_token_data_as_response_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed Flume token payloads should not be treated as auth failures."""
    monkeypatch.setattr(util, "safe_request", lambda *args, **kwargs: {})

    with pytest.raises(util.FlumeTokenError, match="No data field"):
        util.FlumeClient(
            flume_user="user@example.com",
            flume_pass="secret",
            flume_client="client-id",
            flume_secret="client-secret",
        )


def test_poll_for_irrigation_usage_merges_flume_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The polling pipeline should merge Flume totals onto matching zones."""
    expected_total_gallons = 42.5
    expected_gallons_per_minute = 4.25
    watering_windows = [
        RachioZoneWateringSummary(
            zone_id=1,
            zone_name="Front Yard",
            last_watering_start_time=datetime.datetime(
                2024, 6, 1, 6, 0, tzinfo=datetime.UTC
            ),
            last_watering_stop_time=datetime.datetime(
                2024, 6, 1, 6, 10, tzinfo=datetime.UTC
            ),
            last_watering_duration_minutes=10,
            est_gallons_per_minute=4.0,
            est_gallons_used=40.0,
            sqft=100,
            enabled=True,
        ),
        RachioZoneWateringSummary(
            zone_id=2,
            zone_name="Back Yard",
            last_watering_start_time=datetime.datetime(
                2024, 6, 1, 7, 0, tzinfo=datetime.UTC
            ),
            last_watering_stop_time=datetime.datetime(
                2024, 6, 1, 7, 15, tzinfo=datetime.UTC
            ),
            last_watering_duration_minutes=15,
            est_gallons_per_minute=3.0,
            est_gallons_used=45.0,
            sqft=120,
            enabled=True,
        ),
    ]
    captured: dict[str, object] = {}

    class FakeFlumeClient:
        def __init__(self, **kwargs: object) -> None:
            captured["flume_init"] = kwargs
            self.monitors = [
                FlumeDevice(
                    device_id="monitor-1",
                    device_name="Main Monitor",
                    device_timezone="US/Pacific",
                    device_type=DeviceType.SENSOR,
                )
            ]

        def query_usage(
            self,
            device_id: str,
            queries: list[FlumeUsageQuery],
        ) -> list[FlumeRequestData]:
            captured["device_id"] = device_id
            captured["request_ids"] = [query.request_id for query in queries]
            return [
                FlumeRequestData(
                    request_id="Front Yard", total_usage=expected_total_gallons
                )
            ]

    class FakeRachioClient:
        def __init__(self, token: str) -> None:
            captured["rachio_token"] = token

        def get_last_watered_summary(
            self, *, local_timezone: str
        ) -> list[RachioZoneWateringSummary]:
            captured["local_timezone"] = local_timezone
            return watering_windows

    monkeypatch.setattr(util, "FlumeClient", FakeFlumeClient)
    monkeypatch.setattr(util, "RachioClient", FakeRachioClient)

    report = poll_for_irrigation_usage(
        IrrigationMonitorCredentials(
            flume_user="user@example.com",
            flume_pass="secret",
            flume_client_id="client-id",
            flume_client_secret="client-secret",
            rachio_token="rachio-token",
        )
    )

    assert captured == {
        "flume_init": {
            "flume_user": "user@example.com",
            "flume_pass": "secret",
            "flume_client": "client-id",
            "flume_secret": "client-secret",
        },
        "rachio_token": "rachio-token",
        "local_timezone": "US/Pacific",
        "device_id": "monitor-1",
        "request_ids": ["Front Yard", "Back Yard"],
    }
    assert [datapoint.zone_name for datapoint in report] == ["Front Yard", "Back Yard"]
    assert report[0].total_gallons_used == expected_total_gallons
    assert report[0].gallons_per_minute == expected_gallons_per_minute
    assert report[1].total_gallons_used is None
    assert report[1].gallons_per_minute is None


def test_poll_for_irrigation_usage_handles_empty_flume_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty Flume result sets should leave measured usage unset, not crash."""
    watering_windows = [
        RachioZoneWateringSummary(
            zone_id=1,
            zone_name="Front Yard",
            last_watering_start_time=datetime.datetime(
                2024, 6, 1, 6, 0, tzinfo=datetime.UTC
            ),
            last_watering_stop_time=datetime.datetime(
                2024, 6, 1, 6, 10, tzinfo=datetime.UTC
            ),
            last_watering_duration_minutes=10,
            est_gallons_per_minute=4.0,
            est_gallons_used=40.0,
            sqft=100,
            enabled=True,
        )
    ]

    class FakeFlumeClient:
        def __init__(self, **kwargs: object) -> None:
            self.monitors = [
                FlumeDevice(
                    device_id="monitor-1",
                    device_name="Main Monitor",
                    device_timezone="US/Pacific",
                    device_type=DeviceType.SENSOR,
                )
            ]

        def query_usage(
            self,
            device_id: str,
            queries: list[FlumeUsageQuery],
        ) -> list[FlumeRequestData]:
            return []

    class FakeRachioClient:
        def __init__(self, token: str) -> None:
            self.token = token

        def get_last_watered_summary(
            self, *, local_timezone: str
        ) -> list[RachioZoneWateringSummary]:
            return watering_windows

    monkeypatch.setattr(util, "FlumeClient", FakeFlumeClient)
    monkeypatch.setattr(util, "RachioClient", FakeRachioClient)

    report = poll_for_irrigation_usage(
        IrrigationMonitorCredentials(
            flume_user="user@example.com",
            flume_pass="secret",
            flume_client_id="client-id",
            flume_client_secret="client-secret",
            rachio_token="rachio-token",
        )
    )

    assert len(report) == 1
    assert report[0].zone_name == "Front Yard"
    assert report[0].total_gallons_used is None
