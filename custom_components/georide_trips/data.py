"""Typed runtime data for the GeoRide Trips integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry

if TYPE_CHECKING:
    from .api import GeoRideTripsAPI
    from .coordinator import (
        GeoRideLifetimeTripsCoordinator,
        GeoRideTrackerStatusCoordinator,
        GeoRideTripsCoordinator,
    )
    from .socket_manager import GeoRideSocketManager


@dataclass
class GeoRideData:
    """Runtime data stored on the config entry (entry.runtime_data)."""

    api: GeoRideTripsAPI
    trackers: list[dict]
    email: str
    coordinators: dict[str, GeoRideTripsCoordinator]
    lifetime_coordinators: dict[str, GeoRideLifetimeTripsCoordinator]
    tracker_status_coordinators: dict[str, GeoRideTrackerStatusCoordinator]
    socket_manager: GeoRideSocketManager | None


type GeoRideConfigEntry = ConfigEntry[GeoRideData]
