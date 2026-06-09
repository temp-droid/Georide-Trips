"""Tests for the GeoRide API client error contract.

Contract under test (spec 1.4 + 1.6):
- Read methods raise GeoRideApiError on transport/HTTP failure — an empty
  list is a valid result, never an error signal.
- Auth failures (bad credentials, 401 after one retry) raise GeoRideAuthError.
- Every authenticated method retries exactly once on 401 after re-login.
- Action methods (set_eco_mode, toggle_lock, sonor_alarm_off) keep their
  bool/None contract: they swallow GeoRideApiError and return False/None.
"""

import pytest

from georide_api import (
    GeoRideApiError,
    GeoRideAuthError,
)

# Nothing listens on port 1: real connection-refused transport error.
DEAD_URL = "http://127.0.0.1:1"

LOGIN = ("POST", "/user/login")
TRACKERS = ("GET", "/user/trackers")
TRIPS = ("GET", "/tracker/42/trips")
POSITIONS = ("GET", "/tracker/42/trips/positions")


# --- login ---------------------------------------------------------------


async def test_login_success_sets_token(api, fake):
    fake.queue(*LOGIN, payload={"authToken": "tok"})
    assert await api.login() is True
    assert api.token == "tok"


async def test_login_bad_credentials_raises_auth_error(api, fake):
    fake.queue(*LOGIN, payload={}, status=401)
    with pytest.raises(GeoRideAuthError):
        await api.login()


async def test_login_transport_error_raises_api_error(api):
    api.base_url = DEAD_URL
    with pytest.raises(GeoRideApiError):
        await api.login()


# --- read methods raise, never return [] on failure ----------------------


async def test_get_trackers_returns_list(api, fake):
    api.token = "tok"
    fake.queue(*TRACKERS, payload=[{"trackerId": 42}])
    assert await api.get_trackers() == [{"trackerId": 42}]


async def test_get_trackers_500_raises_api_error(api, fake):
    api.token = "tok"
    fake.queue(*TRACKERS, payload={}, status=500)
    with pytest.raises(GeoRideApiError):
        await api.get_trackers()


async def test_get_trackers_transport_error_raises_api_error(api):
    """M1: a transport failure must not be reported as 'no trackers'."""
    api.token = "tok"
    api.base_url = DEAD_URL
    with pytest.raises(GeoRideApiError):
        await api.get_trackers()


async def test_get_trackers_retries_once_on_401(api, fake):
    """M3: get_trackers previously had no 401 retry at all."""
    api.token = "expired"
    fake.queue(*TRACKERS, payload={}, status=401)
    fake.queue(*LOGIN, payload={"authToken": "fresh"})
    fake.queue(*TRACKERS, payload=[{"trackerId": 42}])
    assert await api.get_trackers() == [{"trackerId": 42}]
    assert api.token == "fresh"


async def test_get_trips_empty_list_is_not_an_error(api, fake):
    api.token = "tok"
    fake.queue(*TRIPS, payload=[])
    assert await api.get_trips("42") == []


async def test_get_trips_transport_error_raises_api_error(api):
    """M1: a transport failure must not be reported as 'no trips'."""
    api.token = "tok"
    api.base_url = DEAD_URL
    with pytest.raises(GeoRideApiError):
        await api.get_trips("42")


async def test_get_trips_retries_once_on_401(api, fake):
    api.token = "expired"
    fake.queue(*TRIPS, payload={}, status=401)
    fake.queue(*LOGIN, payload={"authToken": "fresh"})
    fake.queue(*TRIPS, payload=[{"id": 1}])
    assert await api.get_trips("42") == [{"id": 1}]
    assert api.token == "fresh"


async def test_get_trips_second_401_raises_auth_error(api, fake):
    api.token = "expired"
    fake.queue(*TRIPS, payload={}, status=401)
    fake.queue(*LOGIN, payload={"authToken": "fresh"})
    fake.queue(*TRIPS, payload={}, status=401)
    with pytest.raises(GeoRideAuthError):
        await api.get_trips("42")


async def test_get_trip_positions_by_date_retries_once_on_401(api, fake):
    """M3: positions endpoint gets the same uniform retry."""
    api.token = "expired"
    fake.queue(*POSITIONS, payload={}, status=401)
    fake.queue(*LOGIN, payload={"authToken": "fresh"})
    fake.queue(*POSITIONS, payload={"positions": [{"latitude": 1.0}]})
    result = await api.get_trip_positions_by_date("42", "2026-01-01", "2026-01-02")
    assert result == [{"latitude": 1.0}]


async def test_get_trip_positions_by_date_normalizes_bare_list(api, fake):
    api.token = "tok"
    fake.queue(*POSITIONS, payload=[{"latitude": 1.0}])
    result = await api.get_trip_positions_by_date("42", "2026-01-01", "2026-01-02")
    assert result == [{"latitude": 1.0}]


# --- action methods keep their bool/None contract -------------------------


async def test_set_eco_mode_returns_false_on_transport_error(api):
    api.token = "tok"
    api.base_url = DEAD_URL
    assert await api.set_eco_mode("42", True) is False


async def test_set_eco_mode_returns_true_on_204(api, fake):
    api.token = "tok"
    fake.queue("PUT", "/tracker/42/eco", status=204)
    assert await api.set_eco_mode("42", True) is True


async def test_toggle_lock_returns_locked_state(api, fake):
    api.token = "tok"
    fake.queue("POST", "/tracker/42/toggleLock", payload={"locked": True})
    assert await api.toggle_lock("42") is True


async def test_toggle_lock_returns_none_on_error(api, fake):
    api.token = "tok"
    fake.queue("POST", "/tracker/42/toggleLock", payload={}, status=500)
    assert await api.toggle_lock("42") is None
