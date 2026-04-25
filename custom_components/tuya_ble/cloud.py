"""The Tuya BLE integration."""
from __future__ import annotations

import logging

from dataclasses import dataclass
import json
from typing import Any, Iterable

from homeassistant.const import CONF_ADDRESS, CONF_DEVICE_ID
from homeassistant.core import HomeAssistant
CONF_ACCESS_ID = "access_id"
CONF_ACCESS_SECRET = "access_secret"
CONF_APP_TYPE = "tuya_app_type"
CONF_AUTH_TYPE = "auth_type"
CONF_COUNTRY_CODE = "country_code"
CONF_ENDPOINT = "endpoint"
CONF_PASSWORD = "password"
CONF_USERNAME = "username"
TUYA_DOMAIN = "tuya"
TUYA_RESPONSE_RESULT = "result"
TUYA_RESPONSE_SUCCESS = "success"
from homeassistant.helpers.entity import DeviceInfo, EntityDescription
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from tuya_iot import (
    TuyaOpenAPI,
    AuthType,
    TuyaOpenMQ,
    TuyaDeviceManager,
)

from .tuya_ble import (
    AbstaractTuyaBLEDeviceManager,
    TuyaBLEDevice,
    TuyaBLEDeviceCredentials,
)

from .const import (
    CONF_PRODUCT_MODEL,
    CONF_UUID,
    CONF_LOCAL_KEY,
    CONF_CATEGORY,
    CONF_PRODUCT_ID,
    CONF_DEVICE_NAME,
    CONF_PRODUCT_NAME,
    CONF_BLE_USER_ID,
    DOMAIN,
    TUYA_API_DEVICES_URL,
    TUYA_API_FACTORY_INFO_URL,
    TUYA_FACTORY_INFO_MAC,
)

_LOGGER = logging.getLogger(__name__)


def _parse_jtmspro_ble_user_id(status: list | None) -> str | None:
    """Extract 8-byte ASCII user_id from a jtmspro device's ble_unlock_check DP.

    ble_unlock_check is a base64 status field the lock populates after each BLE
    unlock. It embeds the user_id of the most recent unlocker. Wire format:
      [0:2]    index (incremented per unlock)
      [2:4]    separator (0xffff)
      [4:12]   user_id (8 ASCII digits)
      [12:13]  type flag
      [13:17]  unix timestamp (BE)
      [17:]    trailer

    Returns None if status is missing/empty (lock never unlocked via Smart Life
    since pairing), malformed, or doesn't match the expected format.
    """
    import base64 as _b64
    if not status:
        return None
    for item in status:
        if not isinstance(item, dict) or item.get("code") != "ble_unlock_check":
            continue
        value = item.get("value")
        if not value:
            return None
        try:
            raw = _b64.b64decode(value)
        except Exception:
            return None
        if len(raw) < 12 or raw[2:4] != b"\xff\xff":
            return None
        user_id = raw[4:12]
        if not user_id.isdigit():
            return None
        return user_id.decode("ascii")
    return None



@dataclass
class TuyaCloudCacheItem:
    api: TuyaOpenAPI | None
    login: dict[str, Any]
    credentials: dict[str, dict[str, Any]]


CONF_TUYA_LOGIN_KEYS = [
    CONF_ENDPOINT,
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_AUTH_TYPE,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_COUNTRY_CODE,
    CONF_APP_TYPE,
]

CONF_TUYA_DEVICE_KEYS = [
    CONF_UUID,
    CONF_LOCAL_KEY,
    CONF_DEVICE_ID,
    CONF_CATEGORY,
    CONF_PRODUCT_ID,
    CONF_DEVICE_NAME,
    CONF_PRODUCT_NAME,
    CONF_PRODUCT_MODEL,
]

_cache: dict[str, TuyaCloudCacheItem] = {}


class HASSTuyaBLEDeviceManager(AbstaractTuyaBLEDeviceManager):
    """Cloud connected manager of the Tuya BLE devices credentials."""

    def __init__(self, hass: HomeAssistant, data: dict[str, Any]) -> None:
        assert hass is not None
        self._hass = hass
        self._data = data

    @staticmethod
    def _is_login_success(response: dict[Any, Any]) -> bool:
        return bool(response.get(TUYA_RESPONSE_SUCCESS, False))

    @staticmethod
    def _get_cache_key(data: dict[str, Any]) -> str:
        key_dict = {key: data.get(key) for key in CONF_TUYA_LOGIN_KEYS}
        return json.dumps(key_dict)

    @staticmethod
    def _has_login(data: dict[Any, Any]) -> bool:
        for key in CONF_TUYA_LOGIN_KEYS:
            if data.get(key) is None:
                return False
        return True

    @staticmethod
    def _has_credentials(data: dict[Any, Any]) -> bool:
        for key in CONF_TUYA_DEVICE_KEYS:
            if data.get(key) is None:
                return False
        return True

    async def _login(self, data: dict[str, Any], add_to_cache: bool) -> dict[Any, Any]:
        """Login into Tuya cloud using credentials from data dictionary."""
        global _cache

        if len(data) == 0:
            return {}

        api = TuyaOpenAPI(
            endpoint=data.get(CONF_ENDPOINT, ""),
            access_id=data.get(CONF_ACCESS_ID, ""),
            access_secret=data.get(CONF_ACCESS_SECRET, ""),
            auth_type=data.get(CONF_AUTH_TYPE, ""),
        )
        api.set_dev_channel("hass")

        response = await self._hass.async_add_executor_job(
            api.connect,
            data.get(CONF_USERNAME, ""),
            data.get(CONF_PASSWORD, ""),
            data.get(CONF_COUNTRY_CODE, "").split("-")[0],
            data.get(CONF_APP_TYPE, ""),
        )

        _LOGGER.warning(
            "Login attempt response for %s: success=%s, code=%s, msg=%s",
            data.get(CONF_USERNAME, "?"),
            response.get("success"),
            response.get("code"),
            response.get("msg"),
        )
        _LOGGER.warning(
            "Login params: endpoint=%s, auth_type=%s, app_type=%s, country=%s",
            data.get(CONF_ENDPOINT, "?"),
            data.get(CONF_AUTH_TYPE, "?"),
            data.get(CONF_APP_TYPE, "?"),
            data.get(CONF_COUNTRY_CODE, "?"),
        )
        if self._is_login_success(response):
            _LOGGER.debug("Successful login for %s", data[CONF_USERNAME])
            if add_to_cache:
                auth_type = data[CONF_AUTH_TYPE]
                if type(auth_type) is AuthType:
                    data[CONF_AUTH_TYPE] = auth_type.value
                cache_key = self._get_cache_key(data)
                cache_item = _cache.get(cache_key)
                if cache_item:
                    cache_item.api = api
                    cache_item.login = data
                else:
                    _cache[cache_key] = TuyaCloudCacheItem(api, data, {})

        return response

    def _check_login(self) -> bool:
        cache_key = self._get_cache_key(self._data)
        return _cache.get(cache_key) != None

    async def login(self, add_to_cache: bool = False) -> dict[Any, Any]:
        return await self._login(self._data, add_to_cache)

    async def _fill_cache_item(self, item: TuyaCloudCacheItem) -> None:
        devices_response = await self._hass.async_add_executor_job(
            item.api.get,
            TUYA_API_DEVICES_URL % (item.api.token_info.uid),
        )
        if devices_response.get(TUYA_RESPONSE_SUCCESS):
            devices = devices_response.get(TUYA_RESPONSE_RESULT)
            if isinstance(devices, Iterable):
                for device in devices:
                    fi_response = await self._hass.async_add_executor_job(
                        item.api.get,
                        TUYA_API_FACTORY_INFO_URL % (device.get("id")),
                    )
                    fi_response_result = fi_response.get(TUYA_RESPONSE_RESULT)
                    _LOGGER.warning(
                        "Factory info for device %s: %s",
                        device.get("id"),
                        fi_response_result,
                    )
                    if fi_response_result and len(fi_response_result) > 0:
                        factory_info = fi_response_result[0]
                        if factory_info and (TUYA_FACTORY_INFO_MAC in factory_info):
                            mac = ":".join(
                                factory_info[TUYA_FACTORY_INFO_MAC][i : i + 2]
                                for i in range(0, 12, 2)
                            ).upper()
                            # jtmspro (BLE smart lock): extract BLE user_id from
                            # cloud status (ble_unlock_check). Survives factory
                            # reset after one re-pair + one Smart Life unlock.
                            ble_user_id = None
                            if device.get("category") == "jtmspro":
                                ble_user_id = _parse_jtmspro_ble_user_id(
                                    device.get("status")
                                )
                                if ble_user_id:
                                    _LOGGER.debug(
                                        "jtmspro %s: extracted BLE user_id %s",
                                        device.get("id"),
                                        ble_user_id,
                                    )
                                else:
                                    _LOGGER.warning(
                                        "jtmspro %s: no BLE user_id in cloud "
                                        "status; will fall back to hardcoded "
                                        "default. Unlock via Smart Life once "
                                        "to populate ble_unlock_check.",
                                        device.get("id"),
                                    )
                            item.credentials[mac] = {
                                CONF_ADDRESS: mac,
                                CONF_UUID: device.get("uuid"),
                                CONF_LOCAL_KEY: device.get("local_key"),
                                CONF_DEVICE_ID: device.get("id"),
                                CONF_CATEGORY: device.get("category"),
                                CONF_PRODUCT_ID: device.get("product_id"),
                                CONF_DEVICE_NAME: device.get("name"),
                                CONF_PRODUCT_MODEL: device.get("model"),
                                CONF_PRODUCT_NAME: device.get("product_name"),
                                CONF_BLE_USER_ID: ble_user_id,
                            }

    async def build_cache(self) -> None:
        # Always refresh per cache entry — the cloud's status DPs (notably
        # ble_unlock_check, which carries the BLE user_id) can change
        # between HA sessions when a lock has been factory-reset and
        # re-paired. Skipping the refresh leaves HA using stale credentials
        # forever. See v0.3.3 changelog.
        global _cache
        data = {}
        tuya_config_entries = self._hass.config_entries.async_entries(TUYA_DOMAIN)
        for config_entry in tuya_config_entries:
            data.clear()
            data.update(config_entry.data)
            key = self._get_cache_key(data)
            if self._is_login_success(await self._login(data, True)):
                item = _cache.get(key)
                if item is not None:
                    item.credentials.clear()
                    await self._fill_cache_item(item)

        ble_config_entries = self._hass.config_entries.async_entries(DOMAIN)
        for config_entry in ble_config_entries:
            data.clear()
            data.update(config_entry.options)
            key = self._get_cache_key(data)
            if self._is_login_success(await self._login(data, True)):
                item = _cache.get(key)
                if item is not None:
                    item.credentials.clear()
                    await self._fill_cache_item(item)

    def get_login_from_cache(self) -> None:
        global _cache
        for cache_item in _cache.values():
            self._data.update(cache_item.login)
            break

    async def get_device_credentials(
        self,
        address: str,
        force_update: bool = False,
        save_data: bool = False,
    ) -> TuyaBLEDeviceCredentials | None:
        """Get credentials of the Tuya BLE device."""
        global _cache
        item: TuyaCloudCacheItem | None = None
        credentials: dict[str, any] | None = None
        result: TuyaBLEDeviceCredentials | None = None

        if not force_update and self._has_credentials(self._data):
            credentials = self._data.copy()
        else:
            cache_key: str | None = None
            if self._has_login(self._data):
                cache_key = self._get_cache_key(self._data)
            else:
                for key in _cache.keys():
                    if _cache[key].credentials.get(address) is not None:
                        cache_key = key
                        break
            if cache_key:
                item = _cache.get(cache_key)
            if item is None or force_update:
                if self._is_login_success(await self.login(True)):
                    item = _cache.get(cache_key)
                    if item:
                        await self._fill_cache_item(item)

            if item:
                credentials = item.credentials.get(address)

        if credentials:
            result = TuyaBLEDeviceCredentials(
                credentials.get(CONF_UUID, ""),
                credentials.get(CONF_LOCAL_KEY, ""),
                credentials.get(CONF_DEVICE_ID, ""),
                credentials.get(CONF_CATEGORY, ""),
                credentials.get(CONF_PRODUCT_ID, ""),
                credentials.get(CONF_DEVICE_NAME, ""),
                credentials.get(CONF_PRODUCT_MODEL, ""),
                credentials.get(CONF_PRODUCT_NAME, ""),
                credentials.get(CONF_BLE_USER_ID),
            )
            _LOGGER.debug("Retrieved: %s", result)
            if save_data:
                if item:
                    self._data.update(item.login)
                self._data.update(credentials)

        return result

    @property
    def data(self) -> dict[str, Any]:
        return self._data
