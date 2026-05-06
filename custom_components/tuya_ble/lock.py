"""The Tuya BLE integration — lock platform (v0.4.0).

Provides a native Home Assistant `lock` entity for category-jtmspro Tuya BLE
locks. Backed by the same DP 33 (lock_state) as the legacy switch entity, but
exposes HA's standard lock UI affordances: a clear locked/unlocked state, a
spinner during transient locking/unlocking operations, and the standard
`lock.lock` / `lock.unlock` services.

Why a new entity rather than reusing the switch:
  - The switch's optimistic toggle silently reverts when the underlying
    datapoint takes 3-5 seconds to update (BLE connect + write + ack).
    This looks like nothing happened, leading users to tap repeatedly.
  - HA's lock entity has built-in transient states (`locking`, `unlocking`)
    that the frontend renders with a spinner. That is the right UX for an
    operation that takes seconds rather than milliseconds.

The legacy switch entity remains in place for backward compatibility but
should be hidden via the entity registry once the lock entity is in use.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import time
from typing import Any, Callable

from homeassistant.components.lock import (
    LockEntity,
    LockEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .devices import TuyaBLEData, TuyaBLEEntity, TuyaBLEProductInfo
from .tuya_ble import TuyaBLEDataPointType, TuyaBLEDevice

_LOGGER = logging.getLogger(__name__)


@dataclass
class TuyaBLELockMapping:
    """Mapping from a Tuya category/product to a lock-state DP.

    The DP is read for state and written to lock/unlock. Semantics are:
        DP True  -> lock engaged (HA `locked`)
        DP False -> lock disengaged (HA `unlocked`)

    On the CTL20H this DP is named `auto_lock` in the Tuya schema, but on
    this firmware it actually behaves as a persistent lock-state toggle (see
    memory: "DP 33 = lock_state BOOL, Tuya 'auto_lock' misnomer").
    """

    dp_id: int
    description: LockEntityDescription
    force_add: bool = True
    dp_type: TuyaBLEDataPointType | None = None


@dataclass
class TuyaBLECategoryLockMapping:
    products: dict[str, list[TuyaBLELockMapping]] | None = None
    mapping: list[TuyaBLELockMapping] | None = None


# Lock mappings per Tuya category/product. Currently only the CTL20H smart
# lock; more products can be added here as needed.
mapping: dict[str, TuyaBLECategoryLockMapping] = {
    "jtmspro": TuyaBLECategoryLockMapping(
        products={
            "y2yaegze": [  # CTL20H SmartLock
                TuyaBLELockMapping(
                    dp_id=33,
                    description=LockEntityDescription(
                        key="lock",
                        name=None,  # use device name as the entity name
                    ),
                ),
            ],
        },
    ),
}


def get_mapping_by_device(device: TuyaBLEDevice) -> list[TuyaBLELockMapping]:
    category = mapping.get(device.category)
    if category is not None and category.products is not None:
        product_mapping = category.products.get(device.product_id)
        if product_mapping is not None:
            return product_mapping
    if category is not None and category.mapping is not None:
        return category.mapping
    return []


class TuyaBLELock(TuyaBLEEntity, LockEntity):
    """Representation of a Tuya BLE Lock with native HA lock UX."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator,
        device: TuyaBLEDevice,
        product: TuyaBLEProductInfo,
        mapping: TuyaBLELockMapping,
    ) -> None:
        super().__init__(hass, coordinator, device, product, mapping.description)
        self._mapping = mapping
        # Transient state for the spinner. We use HA's _attr_* pattern rather
        # than a property override because HA's LockEntity lists is_locking
        # and is_unlocking in CACHED_PROPERTIES_WITH_ATTR_; the cache
        # invalidation hooks fire on _attr_* assignment, not on private flag
        # changes.
        self._attr_is_locking = False
        self._attr_is_unlocking = False
        # Confirmation tracking: we cannot use DP 33 (lock_state) to detect
        # "operation completed" because set_value() optimistically updates
        # _value synchronously, before the BLE round-trip starts. So DP 33
        # appears confirmed instantly even though the lock has not moved.
        #
        # DP 47 (motor_state) is only updated when the device pushes a new
        # value after the physical motor moves. If the motor_state value
        # differs from what it was when the user initiated the operation,
        # the lock has actually moved and we can clear the spinner.
        self._motor_state_at_op_start: bool | None = None
        # Operation start wallclock time, used purely for the safety timeout.
        self._operation_started_at: float = 0.0
        self._operation_timeout_seconds: float = 60.0

    @property
    def is_locked(self) -> bool | None:
        """Return true if the lock is engaged.

        DP 33 True  -> locked
        DP 33 False -> unlocked
        DP 33 missing -> unknown (fresh boot, never read)
        """
        datapoint = self._device.datapoints[self._mapping.dp_id]
        if datapoint is None:
            return None
        return bool(datapoint.value)

    def _handle_coordinator_update(self) -> None:
        """Clear transient state ONLY when the lock has actually confirmed.

        Confirmation = DP 47 (motor_state) has been updated by a device push
        with a newer timestamp than our most recent user-initiated operation.

        Why this is non-trivial: when the user taps lock/unlock, our async_lock
        calls datapoint.set_value() which (a) mutates _value synchronously and
        (b) awaits _update_from_user, which fires the coordinator update. Our
        own write thus triggers _handle_coordinator_update before any BLE
        round-trip completes. If we clear the spinner there, it disappears
        immediately and the UI never shows the "locking"/"unlocking" state.

        We additionally clear if a configured safety timeout has elapsed so
        the UI does not show a permanently stuck spinner if the lock never
        confirms (e.g. BLE link died mid-operation).
        """
        if self._attr_is_locking or self._attr_is_unlocking:
            should_clear = False
            # Confirmation path: motor_state value differs from what it was
            # when the user initiated the operation. DP 47 is only updated
            # via device push (we never write to it), so any value change
            # represents the lock physically reporting its new motor state.
            motor_dp = self._device.datapoints[47]
            if (
                motor_dp is not None
                and bool(motor_dp.value) != self._motor_state_at_op_start
            ):
                _LOGGER.debug(
                    "%s: motor_state moved from %s to %s; clearing spinner",
                    self._device.address,
                    self._motor_state_at_op_start,
                    motor_dp.value,
                )
                should_clear = True
            # Timeout path: safety net for never-confirmed operations
            # (BLE link died, lock unreachable, etc).
            elif (
                self._operation_started_at > 0
                and time.time() - self._operation_started_at
                > self._operation_timeout_seconds
            ):
                _LOGGER.debug(
                    "%s: operation timeout (%ss); clearing spinner",
                    self._device.address,
                    self._operation_timeout_seconds,
                )
                should_clear = True
            if should_clear:
                self._attr_is_locking = False
                self._attr_is_unlocking = False
                self._motor_state_at_op_start = None
                self._operation_started_at = 0.0
        super()._handle_coordinator_update()

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the device.

        Writes DP 33 = True. The BLE write takes ~3-5s on a connect-on-demand
        device (connect + auth + write + ack). During that window the UI
        shows the `locking` transient state for clear user feedback.
        """
        _LOGGER.debug("%s: lock requested", self._device.address)
        # Snapshot current motor state so _handle_coordinator_update can
        # detect when it changes (= lock actually moved, spinner can clear).
        motor_dp = self._device.datapoints[47]
        self._motor_state_at_op_start = (
            bool(motor_dp.value) if motor_dp is not None else None
        )
        self._attr_is_locking = True
        self._attr_is_unlocking = False
        self._operation_started_at = time.time()
        self.async_write_ha_state()
        datapoint = self._device.datapoints.get_or_create(
            self._mapping.dp_id,
            TuyaBLEDataPointType.DT_BOOL,
            True,
        )
        if datapoint:
            try:
                await datapoint.set_value(True)
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "%s: lock operation failed", self._device.address
                )
                self._attr_is_locking = False
                self.async_write_ha_state()
                raise

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the device. Writes DP 33 = False."""
        _LOGGER.debug("%s: unlock requested", self._device.address)
        motor_dp = self._device.datapoints[47]
        self._motor_state_at_op_start = (
            bool(motor_dp.value) if motor_dp is not None else None
        )
        self._attr_is_unlocking = True
        self._attr_is_locking = False
        self._operation_started_at = time.time()
        self.async_write_ha_state()
        datapoint = self._device.datapoints.get_or_create(
            self._mapping.dp_id,
            TuyaBLEDataPointType.DT_BOOL,
            False,
        )
        if datapoint:
            try:
                await datapoint.set_value(False)
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "%s: unlock operation failed", self._device.address
                )
                self._attr_is_unlocking = False
                self.async_write_ha_state()
                raise


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tuya BLE lock entities from a config entry."""
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]
    mappings = get_mapping_by_device(data.device)
    entities: list[TuyaBLELock] = []
    for m in mappings:
        if m.force_add or data.device.datapoints.has_id(m.dp_id, m.dp_type):
            entities.append(
                TuyaBLELock(
                    hass,
                    data.coordinator,
                    data.device,
                    data.product,
                    m,
                )
            )
    async_add_entities(entities)
