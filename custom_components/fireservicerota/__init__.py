"""The FireServiceRota integration."""
import asyncio
from datetime import timedelta
import logging

from pyfireservicerota import (
    ExpiredTokenError,
    FireServiceRota,
    FireServiceRotaIncidents,
    InvalidAuthError,
    InvalidTokenError,
)

from homeassistant.components.binary_sensor import DOMAIN as BINARYSENSOR_DOMAIN
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TOKEN, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN, NOTIFICATION_AUTH_ID, NOTIFICATION_AUTH_TITLE, WSS_BWRURL

MIN_TIME_BETWEEN_UPDATES = timedelta(seconds=60)

_LOGGER = logging.getLogger(__name__)

SUPPORTED_PLATFORMS = {BINARYSENSOR_DOMAIN, SENSOR_DOMAIN, SWITCH_DOMAIN}


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the FireServiceRota component."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up FireServiceRota from a config entry."""
    coordinator = FSRDataUpdateCoordinator(hass, entry)
    await coordinator.async_update()

    if coordinator.token_refresh_failure:
        return False

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    for platform in SUPPORTED_PLATFORMS:
        hass.async_create_task(
            hass.config_entries.async_forward_entry_setup(entry, platform)
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload FireServiceRota config entry."""
    hass.data[DOMAIN][entry.entry_id].stop_ws_listener()

    unload_ok = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, platform)
                for platform in SUPPORTED_PLATFORMS
            ]
        )
    )

    if unload_ok:
        del hass.data[DOMAIN][entry.entry_id]

    return unload_ok


class FSRDataUpdateCoordinator(DataUpdateCoordinator):
    """Getting the latest data from fireservicerota."""

    def __init__(self, hass, entry):
        """Initialize the data object."""
        self._hass = hass
        self._entry = entry

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_method=self.async_update,
            update_interval=MIN_TIME_BETWEEN_UPDATES,
        )

        self._url = entry.data[CONF_URL]
        self._tokens = entry.data[CONF_TOKEN]

        self.token_refresh_failure = False
        self.incident_data = None
        self.incident_id = None

        self.fsr_avail = FireServiceRota(
            base_url=f"https://{self._url}", token_info=self._tokens
        )

        self.fsr_incidents = FireServiceRotaIncidents(on_incident=self.on_incident)

        self.start_ws_listener()

    def construct_url(self) -> str:
        """Return url with latest config values."""
        return WSS_BWRURL.format(
            self._entry.data[CONF_URL], self._entry.data[CONF_TOKEN]["access_token"]
        )

    def on_incident(self, data) -> None:
        """Update the current data."""
        _LOGGER.debug("Got data from websocket listener: %s", data)
        self.incident_data = data
        dispatcher_send(self._hass, f"{DOMAIN}_{self._entry.entry_id}_update")

    def start_ws_listener(self) -> None:
        """Start the websocket listener."""
        _LOGGER.debug("Starting incidents listener")
        self.fsr_incidents.start(self.construct_url())

    def stop_ws_listener(self) -> None:
        """Stop the websocket listener."""
        _LOGGER.debug("Stopping incidents listener")
        self.fsr_incidents.stop()

    async def update_call(self, func, *args):
        """Perform update call and return data."""
        if self.token_refresh_failure:
            return

        try:
            return await self._hass.async_add_executor_job(func, *args)
        except (ExpiredTokenError, InvalidTokenError):
            if await self.async_refresh_tokens():
                return await self._hass.async_add_executor_job(func, *args)

    async def async_update(self) -> None:
        """Get the latest availability data."""
        _LOGGER.debug("Updating availability data")

        return await self.update_call(
            self.fsr_avail.get_availability, str(self._hass.config.time_zone)
        )

    async def async_response_update(self) -> object:
        """Get the latest incident response data."""
        if self.incident_data is None:
            return

        self.incident_id = self.incident_data.get("id")
        _LOGGER.debug("Updating incident response data for id: %s", self.incident_id)

        return await self.update_call(
            self.fsr_avail.get_incident_response, self.incident_id
        )

    async def async_set_response(self, value) -> None:
        """Set incident response status."""
        _LOGGER.debug(
            "Setting incident response for incident '%s' to status '%s'",
            self.incident_id,
            value,
        )

        await self.update_call(
            self.fsr_avail.set_incident_response, self.incident_id, value
        )

    async def async_refresh_tokens(self) -> bool:
        """Refresh tokens and update config entry."""
        self.stop_ws_listener()

        _LOGGER.debug("Refreshing authentication tokens after expiration")
        try:
            token_info = await self._hass.async_add_executor_job(
                self.fsr_avail.refresh_tokens
            )
        except (InvalidAuthError, InvalidTokenError):
            _LOGGER.error("Error occurred while refreshing authentication tokens")
            self._hass.components.persistent_notification.async_create(
                "Cannot refresh tokens, you need to re-add this integration to generate new ones.",
                title=NOTIFICATION_AUTH_TITLE,
                notification_id=NOTIFICATION_AUTH_ID,
            )
            self.token_refresh_failure = True
            self.update_interval = None
            return False

        _LOGGER.debug("Saving new tokens in config entry")
        self._hass.config_entries.async_update_entry(
            self._entry,
            data={
                "auth_implementation": DOMAIN,
                CONF_URL: self._url,
                CONF_TOKEN: token_info,
            },
        )
        self.update_interval = MIN_TIME_BETWEEN_UPDATES
        self.token_refresh_failure = False
        self.start_ws_listener()

        return True
