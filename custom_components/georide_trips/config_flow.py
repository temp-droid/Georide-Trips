"""Config flow for GeoRide Trips integration."""

import logging
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    DOMAIN,
    CONF_SCAN_INTERVAL,
    CONF_LIFETIME_SCAN_INTERVAL,
    CONF_TRIPS_DAYS_BACK,
    CONF_SOCKETIO_ENABLED,
    CONF_TRACKER_SCAN_INTERVAL,
    CONF_GPS_MIN_ACCURACY,
    CONF_GPS_MIN_DISTANCE,
    CONF_DRIVE_TYPE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_LIFETIME_SCAN_INTERVAL,
    DEFAULT_TRIPS_DAYS_BACK,
    DEFAULT_SOCKETIO_ENABLED,
    DEFAULT_TRACKER_SCAN_INTERVAL,
    DEFAULT_GPS_MIN_ACCURACY,
    DEFAULT_GPS_MIN_DISTANCE,
    DEFAULT_DRIVE_TYPE,
    DRIVE_TYPES,
)
from .api import GeoRideApiError, GeoRideAuthError, GeoRideTripsAPI

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def validate_credentials(hass: HomeAssistant, email: str, password: str):
    """Validate credentials by attempting to login.

    Raises GeoRideAuthError on bad credentials, GeoRideApiError on
    transport/API failure.
    """
    session = async_get_clientsession(hass)
    api = GeoRideTripsAPI(email, password, session)

    await api.login()
    trackers = await api.get_trackers()

    return {"token": api.token, "trackers": trackers, "email": email}


class GeoRideTripsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for GeoRide Trips."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        # Only a single instance supported: entity unique_ids are not
        # prefixed by config entry (assumed single-account limitation).
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        errors = {}

        if user_input is not None:
            try:
                await validate_credentials(
                    self.hass, user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
                )

                await self.async_set_unique_id(user_input[CONF_EMAIL])
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"GeoRide Trips ({user_input[CONF_EMAIL]})", data=user_input
                )

            except GeoRideAuthError:
                errors["base"] = "invalid_auth"
            except GeoRideApiError:
                errors["base"] = "cannot_connect"
            except Exception as err:
                _LOGGER.exception("Unexpected exception: %s", err)
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )

    async def async_step_reauth(self, entry_data):
        """Handle reauth triggered by ConfigEntryAuthFailed."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Re-prompt for the password and validate it."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        errors = {}

        if user_input is not None:
            try:
                await validate_credentials(
                    self.hass, entry.data[CONF_EMAIL], user_input[CONF_PASSWORD]
                )
            except GeoRideAuthError:
                errors["base"] = "invalid_auth"
            except GeoRideApiError:
                errors["base"] = "cannot_connect"
            except Exception as err:
                _LOGGER.exception("Unexpected exception: %s", err)
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"email": entry.data[CONF_EMAIL]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return GeoRideTripsOptionsFlow()


class GeoRideTripsOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for GeoRide Trips.

    No __init__: the base class exposes self.config_entry
    (assigning it yourself is deprecated since HA 2024.11).
    """

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_scan = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        current_lifetime = self.config_entry.options.get(
            CONF_LIFETIME_SCAN_INTERVAL, DEFAULT_LIFETIME_SCAN_INTERVAL
        )
        current_days = self.config_entry.options.get(
            CONF_TRIPS_DAYS_BACK, DEFAULT_TRIPS_DAYS_BACK
        )
        current_socketio = self.config_entry.options.get(
            CONF_SOCKETIO_ENABLED, DEFAULT_SOCKETIO_ENABLED
        )
        current_tracker_scan = self.config_entry.options.get(
            CONF_TRACKER_SCAN_INTERVAL, DEFAULT_TRACKER_SCAN_INTERVAL
        )
        current_gps_accuracy = self.config_entry.options.get(
            CONF_GPS_MIN_ACCURACY, DEFAULT_GPS_MIN_ACCURACY
        )
        current_gps_min_distance = self.config_entry.options.get(
            CONF_GPS_MIN_DISTANCE, DEFAULT_GPS_MIN_DISTANCE
        )
        current_drive_type = self.config_entry.options.get(
            CONF_DRIVE_TYPE, DEFAULT_DRIVE_TYPE
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SOCKETIO_ENABLED,
                        default=current_socketio,
                    ): bool,
                    vol.Optional(
                        CONF_TRACKER_SCAN_INTERVAL,
                        default=current_tracker_scan,
                    ): vol.All(vol.Coerce(int), vol.Range(min=60, max=3600)),
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=current_scan,
                    ): vol.All(vol.Coerce(int), vol.Range(min=300, max=86400)),
                    vol.Optional(
                        CONF_LIFETIME_SCAN_INTERVAL,
                        default=current_lifetime,
                    ): vol.All(vol.Coerce(int), vol.Range(min=3600, max=604800)),
                    vol.Optional(
                        CONF_TRIPS_DAYS_BACK,
                        default=current_days,
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
                    vol.Optional(
                        CONF_GPS_MIN_ACCURACY,
                        default=current_gps_accuracy,
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=10000)),
                    vol.Optional(
                        CONF_GPS_MIN_DISTANCE,
                        default=current_gps_min_distance,
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=500)),
                    vol.Optional(
                        CONF_DRIVE_TYPE,
                        default=current_drive_type,
                    ): vol.In(DRIVE_TYPES),
                }
            ),
        )
