"""GeoRide Trips datetime entities.

Entités datetime rattachées au device de chaque moto :

── Entretien Chaîne ──────────────────────────────────────────────
- date_dernier_entretien_chaine   : date du dernier entretien chaîne

── Entretien Vidange ─────────────────────────────────────────────
- date_dernier_entretien_vidange  : date de la dernière vidange

── Entretien Révision ────────────────────────────────────────────
- date_dernier_entretien_revision : date de la dernière révision

── Plein en attente (usage interne) ──────────────────────────────
- plein_pending_at : horodatage du plein en attente (epoch 1970 = pas de plein en attente)
"""

import logging
from datetime import datetime, timezone

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Sentinel « pas de plein en attente » — convention partagée avec button.py
# (_get_datetime traite l'année 1970 comme None, _set_datetime(None) écrit 1970).
EPOCH_SENTINEL = datetime(1970, 1, 1, tzinfo=timezone.utc)

DATETIME_DESCRIPTIONS = [
    {
        "key": "date_dernier_entretien_chaine",
        "name": "Entretien Chaîne - Date dernier entretien",
        "icon": "mdi:calendar-check",
        "entity_category": EntityCategory.CONFIG,
    },
    {
        "key": "date_dernier_entretien_vidange",
        "name": "Entretien Vidange - Date dernière vidange",
        "icon": "mdi:calendar-check",
        "entity_category": EntityCategory.CONFIG,
    },
    {
        "key": "date_dernier_entretien_revision",
        "name": "Entretien Révision - Date dernière révision",
        "icon": "mdi:calendar-check",
        "entity_category": EntityCategory.CONFIG,
    },
    {
        "key": "plein_pending_at",
        "name": "Plein - Horodatage en attente",
        "icon": "mdi:clock-outline",
        "entity_category": EntityCategory.DIAGNOSTIC,
        "default_epoch": True,
    },
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up GeoRide Trips datetime entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    trackers = data["trackers"]

    entities = []
    for tracker in trackers:
        for desc in DATETIME_DESCRIPTIONS:
            entities.append(GeoRideDateTimeEntity(entry, tracker, desc))

    async_add_entities(entities)
    _LOGGER.info(
        "Added %d datetime entities for %d trackers",
        len(entities),
        len(trackers),
    )


class GeoRideDateTimeEntity(DateTimeEntity, RestoreEntity):
    """Entité datetime persistante rattachée au device GeoRide."""

    def __init__(self, entry: ConfigEntry, tracker: dict, desc: dict) -> None:
        self._entry = entry
        self._tracker = tracker
        self._desc = desc

        self._tracker_id = str(tracker.get("trackerId"))
        self._tracker_name = tracker.get("trackerName", f"Tracker {self._tracker_id}")

        self._attr_unique_id = f"{self._tracker_id}_{desc['key']}"
        self._attr_name = f"{self._tracker_name} {desc['name']}"
        self._attr_icon = desc["icon"]
        self._attr_entity_category = desc.get("entity_category")

        # Valeur par défaut : sentinel 1970 pour plein_pending_at (un défaut
        # à now() simulerait un plein en attente dès l'installation → calcul
        # de plein fantôme au premier verrouillage), sinon maintenant (UTC).
        if desc.get("default_epoch"):
            self._attr_native_value: datetime = EPOCH_SENTINEL
        else:
            self._attr_native_value: datetime = datetime.now(timezone.utc)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._tracker_id)},
            name=f"{self._tracker_name} Trips",
            manufacturer="GeoRide",
            model=self._tracker.get("model", "GeoRide Tracker"),
            sw_version=str(self._tracker.get("softwareVersion", "")),
        )

    async def async_added_to_hass(self) -> None:
        """Restaure le dernier état au redémarrage."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            if last_state.state not in (None, "unknown", "unavailable"):
                try:
                    restored = datetime.fromisoformat(last_state.state)
                    # S'assurer que la datetime est timezone-aware (UTC)
                    if restored.tzinfo is None:
                        restored = restored.replace(tzinfo=timezone.utc)
                    self._attr_native_value = restored
                    _LOGGER.debug(
                        "Restored %s for %s: %s",
                        self._desc["key"],
                        self._tracker_name,
                        restored,
                    )
                except (ValueError, TypeError) as err:
                    _LOGGER.warning(
                        "Could not restore datetime for %s: %s",
                        self._attr_unique_id,
                        err,
                    )

    async def async_set_value(self, value: datetime) -> None:
        """Met à jour la date depuis l'interface ou une automation."""
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        self._attr_native_value = value
        self.async_write_ha_state()
        _LOGGER.debug(
            "Set %s for %s: %s",
            self._desc["key"],
            self._tracker_name,
            value,
        )
