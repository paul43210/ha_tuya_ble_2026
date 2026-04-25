"""The Tuya BLE integration."""
from __future__ import annotations

from dataclasses import dataclass, field

import logging
import time
from typing import Callable

from homeassistant.components.button import (
    ButtonEntityDescription,
    ButtonEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .devices import TuyaBLEData, TuyaBLEEntity, TuyaBLEProductInfo
from .tuya_ble import TuyaBLEDataPointType, TuyaBLEDevice

_LOGGER = logging.getLogger(__name__)


TuyaBLEButtonIsAvailable = Callable[["TuyaBLEButton", TuyaBLEProductInfo], bool] | None


@dataclass
class TuyaBLEButtonMapping:
    dp_id: int
    description: ButtonEntityDescription
    force_add: bool = True
    dp_type: TuyaBLEDataPointType | None = None
    is_available: TuyaBLEButtonIsAvailable = None
    # If raw_subcmd is set, pressing the button sends a raw V4 subcommand
    # instead of writing a DP. raw_payload_builder returns the payload bytes
    # at press time (so timestamps can be current).
    raw_subcmd: int | None = None
    raw_payload_builder: Callable[[TuyaBLEDevice], bytes] | None = None


def is_fingerbot_in_push_mode(self: TuyaBLEButton, product: TuyaBLEProductInfo) -> bool:
    result: bool = True
    if product.fingerbot:
        datapoint = self._device.datapoints[product.fingerbot.mode]
        if datapoint:
            result = datapoint.value == 0
    return result


@dataclass
class TuyaBLEFingerbotModeMapping(TuyaBLEButtonMapping):
    description: ButtonEntityDescription = field(
        default_factory=lambda: ButtonEntityDescription(
            key="push",
        )
    )
    is_available: TuyaBLEButtonIsAvailable = is_fingerbot_in_push_mode


@dataclass
class TuyaBLECategoryButtonMapping:
    products: dict[str, list[TuyaBLEButtonMapping]] | None = None
    mapping: list[TuyaBLEButtonMapping] | None = None


# jtmspro fallback BLE user_id — captured from Paul's original Smart Life
# pairing session. The unlock payload uses cloud-derived user_id (from the
# ble_unlock_check DP) by preference; this constant is only used if the
# cloud value is unavailable. Kept in sync with tuya_ble.py fallback.
JTMSPRO_BLE_USER_ID_FALLBACK = b"84042128"


def _build_jtmspro_unlock_payload(device: TuyaBLEDevice) -> bytes:
    """Build the 19-byte subcmd 0x47 unlock payload.

    Format (verified against HCI capture of Smart Life unlock):
      ff ff 00 02             4-byte constant prefix
      [user_id_ascii:8]       registered BLE user identifier
      01                      unlock type flag
      [timestamp:4 BE]        current Unix timestamp
      00 01                   trailer ("01" seems to indicate "act on it")
    """
    import struct
    # Use the shared user_id resolver on the device (cloud + fallback).
    user_id = device._get_jtmspro_user_id()
    ts = int(time.time())
    payload = bytearray(b"\xff\xff\x00\x02")
    payload += user_id               # 8 bytes
    payload += b"\x01"               # unlock type flag
    payload += struct.pack(">I", ts)
    payload += b"\x00\x01"           # trailer
    return bytes(payload)


mapping: dict[str, TuyaBLECategoryButtonMapping] = {
    "szjqr": TuyaBLECategoryButtonMapping(
        products={
            **dict.fromkeys(
                ["3yqdo5yt", "xhf790if"],  # CubeTouch 1s and II
                [
                    TuyaBLEFingerbotModeMapping(dp_id=1),
                ],
            ),
            **dict.fromkeys(
                [
                    "blliqpsj",
                    "ndvkgsrm",
                    "yiihr7zh", 
                    "neq16kgd"
                ],  # Fingerbot Plus
                [
                    TuyaBLEFingerbotModeMapping(dp_id=2),
                ],
            ),
            **dict.fromkeys(
                [
                    "ltak7e1p",
                    "y6kttvd6",
                    "yrnk7mnn",
                    "nvr2rocq",
                    "bnt7wajf",
                    "rvdceqjh",
                    "5xhbk964",
                ],  # Fingerbot
                [
                    TuyaBLEFingerbotModeMapping(dp_id=2),
                ],
            ),
        },
    ),
    "znhsb": TuyaBLECategoryButtonMapping(
        products={
            "cdlandip":  # Smart water bottle
            [
                TuyaBLEButtonMapping(
                    dp_id=109,
                    description=ButtonEntityDescription(
                        key="bright_lid_screen",
                    ),
                ),
            ],
        },
    ),
    "jtmspro": TuyaBLECategoryButtonMapping(
        products={
            "y2yaegze":  # CTL20H SmartLock
            [
                TuyaBLEButtonMapping(
                    # dp_id 0 is a sentinel — we ignore DP for raw subcmd buttons.
                    # force_add=True (default) ensures the entity registers
                    # even though there's no DP record to match.
                    dp_id=0,
                    description=ButtonEntityDescription(
                        key="ble_unlock",
                        icon="mdi:lock-open-variant",
                    ),
                    raw_subcmd=0x47,
                    raw_payload_builder=_build_jtmspro_unlock_payload,
                ),
            ],
        },
    ),
}


def get_mapping_by_device(device: TuyaBLEDevice) -> list[TuyaBLECategoryButtonMapping]:
    category = mapping.get(device.category)
    if category is not None and category.products is not None:
        product_mapping = category.products.get(device.product_id)
        if product_mapping is not None:
            return product_mapping
        if category.mapping is not None:
            return category.mapping
        else:
            return []
    else:
        return []


class TuyaBLEButton(TuyaBLEEntity, ButtonEntity):
    """Representation of a Tuya BLE Button."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator,
        device: TuyaBLEDevice,
        product: TuyaBLEProductInfo,
        mapping: TuyaBLEButtonMapping,
    ) -> None:
        super().__init__(hass, coordinator, device, product, mapping.description)
        self._mapping = mapping

    def press(self) -> None:
        """Press the button."""
        # Path 1: raw V4 subcommand (e.g. jtmspro unlock)
        if self._mapping.raw_subcmd is not None:
            payload = b""
            if self._mapping.raw_payload_builder is not None:
                payload = self._mapping.raw_payload_builder(self._device)
            _LOGGER.info(
                "Sending raw V4 subcommand %#x with %d-byte payload",
                self._mapping.raw_subcmd, len(payload),
            )
            # For jtmspro unlock (subcmd 0x47), the lock requires a recent
            # subcmd 0x45 user-auth on the same session before it will
            # honor the unlock. Some lock firmwares prompt for this auth
            # automatically on a timer; others (e.g. fresh-paired locks
            # post factory-reset) do not. Sending 0x45 proactively before
            # 0x47 is idempotent (it does NOT register a new user, it
            # re-authorizes the existing one) and matches what Smart Life
            # does internally.
            if (
                self._mapping.raw_subcmd == 0x47
                and getattr(self._device, "category", None) == "jtmspro"
            ):
                self._hass.create_task(
                    self._device.send_jtmspro_unlock(payload)
                )
            else:
                self._hass.create_task(
                    self._device.send_raw_command_v4(
                        self._mapping.raw_subcmd, payload
                    )
                )
            return

        # Path 2: DP toggle (fingerbot-style)
        datapoint = self._device.datapoints.get_or_create(
            self._mapping.dp_id,
            TuyaBLEDataPointType.DT_BOOL,
            False,
        )
        if datapoint:
            self._hass.create_task(
                datapoint.set_value(not bool(datapoint.value))
            )

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        result = super().available
        if result and self._mapping.is_available:
            result = self._mapping.is_available(self, self._product)
        return result


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Tuya BLE sensors."""
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]
    mappings = get_mapping_by_device(data.device)
    entities: list[TuyaBLEButton] = []
    for mapping in mappings:
        if mapping.force_add or data.device.datapoints.has_id(
            mapping.dp_id, mapping.dp_type
        ):
            entities.append(
                TuyaBLEButton(
                    hass,
                    data.coordinator,
                    data.device,
                    data.product,
                    mapping,
                )
            )
    async_add_entities(entities)
