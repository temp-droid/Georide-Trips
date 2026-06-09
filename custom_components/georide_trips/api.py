"""GeoRide API Client."""

import logging
import aiohttp
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

_LOGGER = logging.getLogger(__name__)


class GeoRideApiError(Exception):
    """Transport or HTTP error while talking to the GeoRide API.

    Raised instead of returning an empty result so callers (coordinators)
    can distinguish "fetch failed" from "no data" and keep previous data.
    """


class GeoRideAuthError(GeoRideApiError):
    """Authentication failed (bad credentials or unrecoverable 401)."""


class GeoRideTripsAPI:
    """GeoRide Trips API Client."""

    def __init__(self, email: str, password: str, session: aiohttp.ClientSession):
        """Initialize the API client."""
        self.email = email
        self.password = password
        self.session = session
        self.base_url = "https://api.georide.fr"
        self.token = None

    async def login(self) -> bool:
        """Login to GeoRide API.

        Raises GeoRideAuthError on rejected credentials, GeoRideApiError on
        any other HTTP/transport failure. Returns True on success.
        """
        url = f"{self.base_url}/user/login"
        data = {"email": self.email, "password": self.password}

        try:
            async with self.session.post(url, data=data) as response:
                if response.status == 200:
                    result = await response.json()
                    self.token = result.get("authToken")
                    _LOGGER.debug("Successfully logged in to GeoRide API")
                    return True
                if response.status in (401, 403):
                    raise GeoRideAuthError(f"Login rejected: HTTP {response.status}")
                raise GeoRideApiError(f"Login failed: HTTP {response.status}")
        except GeoRideApiError:
            raise
        except Exception as err:
            raise GeoRideApiError(f"Error during login: {err}") from err

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        _retry: bool = True,
    ) -> Any:
        """Perform an authenticated request and return the parsed JSON body.

        Comportement uniforme pour tous les endpoints :
        - login automatique si aucun token,
        - retry unique sur 401 après ré-authentification,
        - GeoRideApiError sur erreur HTTP/transport (jamais de [] silencieux),
        - GeoRideAuthError si le 401 persiste après re-login.
        """
        if not self.token:
            await self.login()

        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self.token}"}

        try:
            async with self.session.request(
                method, url, headers=headers, params=params, json=json
            ) as response:
                if response.status == 204:
                    return None
                if response.status == 200:
                    try:
                        return await response.json()
                    except (aiohttp.ContentTypeError, ValueError):
                        # Certains endpoints d'action renvoient un corps vide
                        return None
                if response.status == 401:
                    if _retry:
                        _LOGGER.warning("Token expired, re-authenticating...")
                        await self.login()
                        return await self._request(
                            method, path, params=params, json=json, _retry=False
                        )
                    raise GeoRideAuthError(
                        f"{method} {path}: still unauthorized after re-login"
                    )
                text = await response.text()
                raise GeoRideApiError(
                    f"{method} {path}: HTTP {response.status} {text[:200]}"
                )
        except GeoRideApiError:
            raise
        except Exception as err:
            raise GeoRideApiError(f"{method} {path}: {err}") from err

    async def get_trackers(self) -> List[Dict[str, Any]]:
        """Get list of trackers."""
        return await self._request("GET", "/user/trackers") or []

    async def get_trips(
        self,
        tracker_id: str,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Get trips for a tracker."""
        # Default to last 30 days
        if from_date is None:
            from_date = datetime.now(timezone.utc) - timedelta(days=30)
        if to_date is None:
            to_date = datetime.now(timezone.utc)

        # Convertir en UTC puis formater sans fuseau (l'API GeoRide attend de l'UTC pur)
        if from_date.tzinfo is not None:
            from_date = from_date.astimezone(timezone.utc).replace(tzinfo=None)
        if to_date.tzinfo is not None:
            to_date = to_date.astimezone(timezone.utc).replace(tzinfo=None)

        params = {
            "from": from_date.strftime("%Y%m%dT%H%M%S"),
            "to": to_date.strftime("%Y%m%dT%H%M%S"),
        }

        trips = (
            await self._request("GET", f"/tracker/{tracker_id}/trips", params=params)
            or []
        )
        _LOGGER.debug("Retrieved %d trips for tracker %s", len(trips), tracker_id)
        return trips

    async def get_last_position(self, tracker_id: str) -> Optional[Dict[str, Any]]:
        """Get last known position via the last trip's positions endpoint.

        L'API GeoRide n'expose pas d'endpoint /positions/last.
        On récupère les trips des dernières 24h et on prend la dernière position du dernier trip.
        """
        # Récupérer les trips récents (24h)
        from_date = datetime.now(timezone.utc) - timedelta(hours=24)
        to_date = datetime.now(timezone.utc)
        trips = await self.get_trips(tracker_id, from_date, to_date)

        if not trips:
            # Élargir à 7 jours si aucun trip dans les 24h
            from_date = datetime.now(timezone.utc) - timedelta(days=7)
            trips = await self.get_trips(tracker_id, from_date, to_date)

        if not trips:
            _LOGGER.debug(
                "No recent trips for tracker %s, cannot get last position", tracker_id
            )
            return None

        # Prendre le trip le plus récent
        last_trip = sorted(
            trips, key=lambda t: t.get("endDate", t.get("startDate", ""))
        )[-1]
        trip_start = last_trip.get("startDate") or last_trip.get("startTime")
        trip_end = last_trip.get("endDate") or last_trip.get("endTime")

        if not trip_start or not trip_end:
            _LOGGER.debug("Trip has no start/end date for tracker %s", tracker_id)
            return None

        # Récupérer les positions de ce trip
        positions = await self.get_trip_positions_by_date(
            tracker_id, trip_start, trip_end
        )

        if not positions:
            return None

        # Retourner la dernière position
        last = positions[-1]
        _LOGGER.debug("Last position for tracker %s: %s", tracker_id, last)
        return last

    async def get_trip_positions_by_date(
        self,
        tracker_id: str,
        from_date: str,
        to_date: str,
    ) -> List[Dict[str, Any]]:
        """Get positions for a trip by date range (ISO 8601 strings)."""
        data = await self._request(
            "GET",
            f"/tracker/{tracker_id}/trips/positions",
            params={"from": from_date, "to": to_date},
        )
        # L'API retourne {"positions": [...]} ou directement [...]
        if isinstance(data, dict):
            return data.get("positions", [])
        return data or []

    async def get_trip_positions(
        self, tracker_id: str, trip_id: str
    ) -> List[Dict[str, Any]]:
        """Get positions for a specific trip."""
        return (
            await self._request(
                "GET", f"/tracker/{tracker_id}/trip/{trip_id}/positions"
            )
            or []
        )

    async def set_eco_mode(self, tracker_id: str, enabled: bool) -> bool:
        """Enable or disable eco mode for a tracker.

        Endpoint: PUT /tracker/{tracker_id}/eco
        Body: {"isInEco": true/false}
        """
        try:
            await self._request(
                "PUT", f"/tracker/{tracker_id}/eco", json={"isInEco": enabled}
            )
        except GeoRideApiError as err:
            _LOGGER.error("Failed to set eco mode for tracker %s: %s", tracker_id, err)
            return False
        _LOGGER.info(
            "Eco mode %s for tracker %s",
            "enabled" if enabled else "disabled",
            tracker_id,
        )
        return True

    async def sonor_alarm_off(self, tracker_id: str) -> bool:
        """Arrêter l'alarme sonore du tracker (GeoRide 3 uniquement).

        Endpoint: POST /tracker/{tracker_id}/sonor-alarm/off
        """
        try:
            await self._request("POST", f"/tracker/{tracker_id}/sonor-alarm/off")
        except GeoRideApiError as err:
            _LOGGER.error("Failed sonor alarm off for tracker %s: %s", tracker_id, err)
            return False
        _LOGGER.info("Sonor alarm OFF for tracker %s", tracker_id)
        return True

    async def toggle_lock(self, tracker_id: str) -> bool | None:
        """Toggle the lock state of a tracker.

        Endpoint: POST /tracker/{tracker_id}/toggleLock
        Returns the new locked state (True/False), or None on error.
        """
        try:
            result = await self._request("POST", f"/tracker/{tracker_id}/toggleLock")
        except GeoRideApiError as err:
            _LOGGER.error("Failed to toggle lock for tracker %s: %s", tracker_id, err)
            return None
        locked = (result or {}).get("locked")
        _LOGGER.info("Tracker %s lock toggled -> locked=%s", tracker_id, locked)
        return locked
