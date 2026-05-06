from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
from collections.abc import Callable
from struct import pack, unpack

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakDBusError
from bleak_retry_connector import BLEAK_BACKOFF_TIME
from bleak_retry_connector import BLEAK_RETRY_EXCEPTIONS
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakError,
    BleakNotFoundError,
    establish_connection,
)
from Crypto.Cipher import AES

from .const import (
    CHARACTERISTIC_NOTIFY,
    CHARACTERISTIC_NOTIFY_OLD,
    CHARACTERISTIC_WRITE,
    CHARACTERISTIC_WRITE_OLD,
    GATT_MTU,
    MANUFACTURER_DATA_ID,
    RESPONSE_WAIT_TIMEOUT,
    SERVICE_UUID,
    TuyaBLECode,
    TuyaBLEDataPointType,
)
from .exceptions import (
    TuyaBLEDataCRCError,
    TuyaBLEDataFormatError,
    TuyaBLEDataLengthError,
    TuyaBLEDeviceError,
    TuyaBLEEnumValueError,
)
from .manager import AbstaractTuyaBLEDeviceManager, TuyaBLEDeviceCredentials

_LOGGER = logging.getLogger(__name__)


BLEAK_EXCEPTIONS = (*BLEAK_RETRY_EXCEPTIONS, OSError)


class TuyaBLEDataPoint:
    def __init__(
        self,
        owner: TuyaBLEDataPoints,
        id: int,
        timestamp: float,
        flags: int,
        type: TuyaBLEDataPointType,
        value: bytes | bool | int | str,
    ) -> None:
        self._owner = owner
        self._id = id
        self._value = value
        self._changed_by_device = False
        self._update_from_device(timestamp, flags, type, value)

    def _update_from_device(
        self,
        timestamp: float,
        flags: int,
        type: TuyaBLEDataPointType,
        value: bytes | bool | int | str,
    ) -> None:
        self._timestamp = timestamp
        self._flags = flags
        self._type = type
        self._changed_by_device = self._value != value
        self._value = value

    def _get_value(self) -> bytes:
        match self._type:
            case TuyaBLEDataPointType.DT_RAW | TuyaBLEDataPointType.DT_BITMAP:
                return self._value
            case TuyaBLEDataPointType.DT_BOOL:
                return pack(">B", 1 if self._value else 0)
            case TuyaBLEDataPointType.DT_VALUE:
                return pack(">i", self._value)
            case TuyaBLEDataPointType.DT_ENUM:
                if self._value > 0xFFFF:
                    return pack(">I", self._value)
                elif self._value > 0xFF:
                    return pack(">H", self._value)
                else:
                    return pack(">B", self._value)
            case TuyaBLEDataPointType.DT_STRING:
                return self._value.encode()

    @property
    def id(self) -> int:
        return self._id

    @property
    def timestamp(self) -> float:
        return self._timestamp

    @property
    def flags(self) -> int:
        return self._flags

    @property
    def type(self) -> TuyaBLEDataPointType:
        return self._type

    @property
    def value(self) -> bytes | bool | int | str:
        return self._value

    @property
    def changed_by_device(self) -> bool:
        return self._changed_by_device

    async def set_value(self, value: bytes | bool | int | str) -> None:
        match self._type:
            case TuyaBLEDataPointType.DT_RAW | TuyaBLEDataPointType.DT_BITMAP:
                self._value = bytes(value)
            case TuyaBLEDataPointType.DT_BOOL:
                self._value = bool(value)
            case TuyaBLEDataPointType.DT_VALUE:
                self._value = int(value)
            case TuyaBLEDataPointType.DT_ENUM:
                value = int(value)
                if value >= 0:
                    self._value = value
                else:
                    raise TuyaBLEEnumValueError()

            case TuyaBLEDataPointType.DT_STRING:
                self._value = str(value)

        self._changed_by_device = False
        await self._owner._update_from_user(self._id)


class TuyaBLEDataPoints:
    def __init__(self, owner: TuyaBLEDevice) -> None:
        self._owner = owner
        self._datapoints: dict[int, TuyaBLEDataPoint] = {}
        self._update_started: int = 0
        self._updated_datapoints: list[int] = []

    def __len__(self) -> int:
        return len(self._datapoints)

    def __getitem__(self, key: int) -> TuyaBLEDataPoint | None:
        return self._datapoints.get(key)

    def has_id(self, id: int, type: TuyaBLEDataPointType | None = None) -> bool:
        return (id in self._datapoints) and (
            (type is None) or (self._datapoints[id].type == type)
        )

    def get_or_create(
        self,
        id: int,
        type: TuyaBLEDataPointType,
        value: bytes | bool | int | str | None = None,
    ) -> TuyaBLEDataPoint:
        datapoint = self._datapoints.get(id)
        if datapoint:
            return datapoint
        datapoint = TuyaBLEDataPoint(self, id, time.time(), 0, type, value)
        self._datapoints[id] = datapoint
        return datapoint

    def begin_update(self) -> None:
        self._update_started += 1

    async def end_update(self) -> None:
        if self._update_started > 0:
            self._update_started -= 1
            if self._update_started == 0 and len(self._updated_datapoints) > 0:
                await self._owner._send_datapoints(self._updated_datapoints)
                self._updated_datapoints = []

    def _update_from_device(
        self,
        dp_id: int,
        timestamp: float,
        flags: int,
        type: TuyaBLEDataPointType,
        value: bytes | bool | int | str,
    ) -> None:
        dp = self._datapoints.get(dp_id)
        if dp:
            dp._update_from_device(timestamp, flags, type, value)
        else:
            self._datapoints[dp_id] = TuyaBLEDataPoint(
                self, dp_id, timestamp, flags, type, value
            )

    async def _update_from_user(self, dp_id: int) -> None:
        if self._update_started > 0:
            if dp_id in self._updated_datapoints:
                self._updated_datapoints.remove(dp_id)
            self._updated_datapoints.append(dp_id)
        else:
            await self._owner._send_datapoints([dp_id])


global_connect_lock = asyncio.Lock()


# v0.4.0: Connect-on-demand lifecycle
#
# For battery-powered BLE devices like the CTL20H cabinet locks, holding a
# persistent BLE connection drains the lock's batteries in weeks rather than
# years. Smart Life behaves differently: it connects only when the user asks
# to act on the lock, then disconnects shortly after.
#
# CONNECT_ON_DEMAND_CATEGORIES lists Tuya categories that should follow this
# lifecycle: connect on operation, disconnect after a short idle, no
# auto-reconnect on unexpected disconnect, only periodic poll for telemetry.
#
# Categories not in this set keep the original always-connected behavior
# (fingerbots, plant sensors, etc. that need real-time data).
CONNECT_ON_DEMAND_CATEGORIES: set[str] = {"jtmspro"}

# Seconds after the last write/read activity before disconnecting on idle.
# Long enough to handle a quick second operation (toggle and immediately
# toggle back) without paying the reconnect cost; short enough that we do not
# linger on the radio. 5 seconds is a balance.
IDLE_DISCONNECT_SECONDS: dict[str, float] = {"jtmspro": 5.0}

# Periodic poll interval for telemetry refresh (battery, lock state).
# 24 hours per Paul's preference for cabinet locks - rare enough to be near
# zero battery cost, frequent enough to catch a stuck low-battery state
# within a day.
PERIODIC_POLL_INTERVAL_SECONDS: dict[str, float] = {"jtmspro": 24 * 3600.0}

# After this many consecutive failed connect attempts, fall back to
# always-connected behavior for safety until next HA restart. Defensive
# guardrail: better an unreliable lock that drains batteries faster than
# an unreachable lock that silently never accepts an unlock command.
MAX_CONSECUTIVE_CONNECT_FAILURES_BEFORE_FALLBACK: int = 5


class TuyaBLEDevice:
    def __init__(
        self,
        device_manager: AbstaractTuyaBLEDeviceManager,
        ble_device: BLEDevice,
        advertisement_data: AdvertisementData | None = None,
    ) -> None:
        """Init the TuyaBLE."""
        self._device_manager = device_manager
        self._device_info: TuyaBLEDeviceCredentials | None = None
        self._ble_device = ble_device
        self._advertisement_data = advertisement_data
        self._operation_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._client: BleakClientWithServiceCache | None = None
        self._expected_disconnect = False
        self._connected_callbacks: list[Callable[[], None]] = []
        self._callbacks: list[Callable[[list[TuyaBLEDataPoint]], None]] = []
        self._disconnected_callbacks: list[Callable[[], None]] = []
        self._current_seq_num = 1
        self._seq_num_lock = asyncio.Lock()

        self._is_bound = False
        self._flags = 0
        self._protocol_version = 2
        self._outbound_idx = 0  # V4 outgoing command counter (shared across DP writes and subcommands)

        # v0.4.0 connect-on-demand state
        self._idle_disconnect_handle: asyncio.TimerHandle | None = None
        self._periodic_poll_task: asyncio.Task | None = None
        self._consecutive_connect_failures: int = 0
        self._connect_on_demand_disabled: bool = False  # Safety fallback flag

        self._device_version: str = ""
        self._protocol_version_str: str = ""
        self._hardware_version: str = ""

        self._device_info: TuyaBLEDeviceCredentials | None = None

        self._auth_key: bytes | None = None
        self._local_key: bytes | None = None
        self._login_key: bytes | None = None
        self._session_key: bytes | None = None

        self._is_paired = False

        self._input_buffer: bytearray | None = None
        self._input_expected_packet_num = 0
        self._input_expected_length = 0
        self._input_expected_responses: dict[int,
                                             asyncio.Future[int] | None] = {}
        # self._input_future: asyncio.Future[int] | None = None

        self._datapoints = TuyaBLEDataPoints(self)

    def set_ble_device_and_advertisement_data(
        self, ble_device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        """Set the ble device."""
        self._ble_device = ble_device
        self._advertisement_data = advertisement_data

    async def initialize(self) -> None:
        _LOGGER.debug("%s: Initializing", self.address)
        if await self._update_device_info():
            self._decode_advertisement_data()
            
    def _build_pairing_request(self) -> bytes:
        # Match Smart Life app wire format for Tuya BLE proto v4: 46-byte
        # payload = uuid(16) + local_key(6) + device_id(16) + zero_pad(6) +
        # member_index(2, big-endian, 0x0001 = master user).
        result = bytearray()

        result += self._device_info.uuid.encode()
        result += self._local_key
        result += self._device_info.device_id.encode()
        # Pad zeros up to 44, then append 0x0001 member index -> 46 bytes total.
        for _ in range(44 - len(result)):
            result += b"\x00"
        result += b"\x00\x01"

        return result

    async def pair(self) -> None:
        """
        _LOGGER.debug("%s: Sending pairing request: %s",
            self.address, data.hex()
        )
        """
        await self._send_packet(
            TuyaBLECode.FUN_SENDER_PAIR, self._build_pairing_request()
        )

    async def update(self) -> None:
        _LOGGER.debug("%s: Updating", self.address)
        await self._send_packet(TuyaBLECode.FUN_SENDER_DEVICE_STATUS, bytes())

    async def _update_device_info(self) -> bool:
        if self._device_info is None:
            if self._device_manager:
                # force_update=True: bypass the config-entry persistent data
                # shortcut and always re-fetch credentials from the Tuya
                # cloud. This is what catches user_id rotation after a
                # factory-reset + Smart Life re-pair (the lock generates a
                # new user_id, the cloud reflects it, but the credentials
                # we previously saved to entry.data are now stale).
                # Cost: one cloud round-trip per BLE reconnect — acceptable
                # for cabinet locks that connect a few times per day.
                self._device_info = await self._device_manager.get_device_credentials(
                    self._ble_device.address, True
                )
            if self._device_info:
                self._local_key = self._device_info.local_key[:6].encode()
                self._login_key = hashlib.md5(self._local_key).digest()

        return self._device_info is not None

    def _decode_advertisement_data(self) -> None:
        raw_product_id: bytes | None = None
        # raw_product_key: bytes | None = None
        raw_uuid: bytes | None = None
        if self._advertisement_data:
            if self._advertisement_data.service_data:
                service_data = self._advertisement_data.service_data.get(
                    SERVICE_UUID)
                if service_data and len(service_data) > 1:
                    match service_data[0]:
                        case 0:
                            raw_product_id = service_data[1:]
                        # case 1:
                        #    raw_product_key = service_data[1:]

            if self._advertisement_data.manufacturer_data:
                manufacturer_data = self._advertisement_data.manufacturer_data.get(
                    MANUFACTURER_DATA_ID
                )
                if manufacturer_data and len(manufacturer_data) > 6:
                    self._is_bound = (manufacturer_data[0] & 0x80) != 0
                    self._protocol_version = manufacturer_data[1]
                    raw_uuid = manufacturer_data[6:]
                    if raw_product_id:
                        key = hashlib.md5(raw_product_id).digest()
                        cipher = AES.new(key, AES.MODE_CBC, key)
                        raw_uuid = cipher.decrypt(raw_uuid)
                        self._uuid = raw_uuid.decode("utf-8")

    @property
    def address(self) -> str:
        """Return the address."""
        return self._ble_device.address

    @property
    def name(self) -> str:
        """Get the name of the device."""
        if self._device_info:
            return self._device_info.device_name
        else:
            return self._ble_device.name or self._ble_device.address

    @property
    def rssi(self) -> int | None:
        """Get the rssi of the device."""
        if self._advertisement_data:
            return self._advertisement_data.rssi
        return None

    @property
    def uuid(self) -> str:
        if self._device_info is not None:
            return self._device_info.uuid
        else:
            return ""

    @property
    def local_key(self) -> str:
        if self._device_info is not None:
            return self._device_info.local_key
        else:
            return ""

    @property
    def category(self) -> str:
        if self._device_info is not None:
            return self._device_info.category
        else:
            return ""

    @property
    def device_id(self) -> str:
        if self._device_info is not None:
            return self._device_info.device_id
        else:
            return ""

    @property
    def product_id(self) -> str:
        if self._device_info is not None:
            return self._device_info.product_id
        else:
            return ""

    @property
    def product_model(self) -> str:
        if self._device_info is not None:
            return self._device_info.product_model
        else:
            return ""

    @property
    def product_name(self) -> str:
        if self._device_info is not None:
            return self._device_info.product_name
        else:
            return ""

    @property
    def device_version(self) -> str:
        return self._device_version

    @property
    def hardware_version(self) -> str:
        return self._hardware_version

    @property
    def protocol_version(self) -> str:
        return self._protocol_version_str

    @property
    def datapoints(self) -> TuyaBLEDataPoints:
        """Get datapoints exposed by device."""
        return self._datapoints

    def get_or_create_datapoint(
        self,
        id: int,
        type: TuyaBLEDataPointType,
        value: bytes | bool | int | str | None = None,
    ) -> TuyaBLEDataPoint:
        """Get datapoints exposed by device."""

    def _fire_connected_callbacks(self) -> None:
        """Fire the callbacks."""
        for callback in self._connected_callbacks:
            callback()

    def register_connected_callback(
        self, callback: Callable[[], None]
    ) -> Callable[[], None]:
        """Register a callback to be called when device disconnected."""

        def unregister_callback() -> None:
            self._connected_callbacks.remove(callback)

        self._connected_callbacks.append(callback)
        return unregister_callback

    def _fire_callbacks(self, datapoints: list[TuyaBLEDataPoint]) -> None:
        """Fire the callbacks."""
        for callback in self._callbacks:
            callback(datapoints)

    def register_callback(
        self,
        callback: Callable[[list[TuyaBLEDataPoint]], None],
    ) -> Callable[[], None]:
        """Register a callback to be called when the state changes."""

        def unregister_callback() -> None:
            self._callbacks.remove(callback)

        self._callbacks.append(callback)
        return unregister_callback

    def _fire_disconnected_callbacks(self) -> None:
        """Fire the callbacks."""
        for callback in self._disconnected_callbacks:
            callback()

    def register_disconnected_callback(
        self, callback: Callable[[], None]
    ) -> Callable[[], None]:
        """Register a callback to be called when device disconnected."""

        def unregister_callback() -> None:
            self._disconnected_callbacks.remove(callback)

        self._disconnected_callbacks.append(callback)
        return unregister_callback

    async def start(self):
        """Start the TuyaBLE."""
        _LOGGER.debug("%s: Starting...", self.address)
        # v0.4.0: For connect-on-demand categories, schedule a periodic
        # poll task to refresh telemetry (battery %, lock state) without
        # holding a permanent connection.
        if self.category in CONNECT_ON_DEMAND_CATEGORIES:
            interval = PERIODIC_POLL_INTERVAL_SECONDS.get(self.category)
            if interval and self._periodic_poll_task is None:
                self._periodic_poll_task = asyncio.create_task(
                    self._periodic_poll_loop(interval)
                )
                _LOGGER.debug(
                    "%s: scheduled periodic poll every %.0fs",
                    self.address,
                    interval,
                )

    async def stop(self) -> None:
        """Stop the TuyaBLE."""
        _LOGGER.debug("%s: Stop", self.address)
        # v0.4.0: cancel periodic poll if it is running.
        if self._periodic_poll_task is not None:
            self._periodic_poll_task.cancel()
            self._periodic_poll_task = None
        self._cancel_idle_disconnect()
        await self._execute_disconnect()

    async def _periodic_poll_loop(self, interval_seconds: float) -> None:
        """Background task that periodically wakes the device for a telemetry refresh.

        For connect-on-demand devices, this is the only way to keep state
        moderately fresh without holding a permanent BLE connection.

        On each tick: connect, exchange device-info / status, schedule the
        idle disconnect (will fire after IDLE_DISCONNECT_SECONDS), sleep.

        Failures are logged but never raised to the loop body. The retry on
        next tick is the recovery path. The safety-fallback in
        _ensure_connected handles the runaway-failure case.
        """
        try:
            # Small initial offset so all devices do not poll simultaneously.
            await asyncio.sleep(60)
            while True:
                if self._connect_on_demand_disabled:
                    # Once we have fallen back to always-connected, the
                    # always-connected loop handles freshness; stop polling.
                    _LOGGER.debug(
                        "%s: connect-on-demand disabled; stopping periodic poll",
                        self.address,
                    )
                    return
                try:
                    _LOGGER.debug("%s: periodic poll tick", self.address)
                    await self._ensure_connected()
                    # _ensure_connected already does FUN_SENDER_DEVICE_INFO
                    # and the lock pushes its DPs after pairing, so just
                    # reaching this point gives us a fresh snapshot.
                    self._maybe_schedule_idle_disconnect()
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug(
                        "%s: periodic poll failed: %s",
                        self.address,
                        exc,
                    )
                await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            _LOGGER.debug("%s: periodic poll cancelled", self.address)
            raise

    def _disconnected(self, client: BleakClientWithServiceCache) -> None:
        """Disconnected callback."""
        was_paired = self._is_paired
        self._is_paired = False
        self._fire_disconnected_callbacks()
        if self._expected_disconnect:
            _LOGGER.debug(
                "%s: Disconnected from device; RSSI: %s",
                self.address,
                self.rssi,
            )
            return
        self._client = None
        _LOGGER.warning(
            "%s: Device unexpectedly disconnected; RSSI: %s",
            self.address,
            self.rssi,
        )
        if was_paired:
            # v0.4.0: For connect-on-demand categories, the lock has either
            # voluntarily disconnected after the radio's keep-alive timeout
            # (normal) or our idle disconnect just fired (also normal).
            # Do NOT auto-reconnect; the next operation or scheduled poll
            # will reconnect when needed. This is the entire point - leaving
            # the lock disconnected most of the time saves battery.
            #
            # The fallback flag preserves the original behavior if we have
            # had repeated connect failures (lock genuinely unreachable).
            if (
                self.category in CONNECT_ON_DEMAND_CATEGORIES
                and not self._connect_on_demand_disabled
            ):
                _LOGGER.debug(
                    "%s: connect-on-demand mode; not auto-reconnecting",
                    self.address,
                )
                return
            _LOGGER.debug(
                "%s: Scheduling reconnect; RSSI: %s",
                self.address,
                self.rssi,
            )
            asyncio.create_task(self._reconnect())

    def _maybe_schedule_idle_disconnect(self) -> None:
        """Schedule a disconnect after the configured idle window.

        Used by connect-on-demand categories (e.g. jtmspro locks) to release
        the BLE link shortly after each operation. Cancels any previously
        scheduled idle disconnect first, so multiple back-to-back operations
        push the deadline forward rather than pile up.

        No-op for categories not in CONNECT_ON_DEMAND_CATEGORIES, or when
        the safety-fallback flag has been raised.
        """
        if self.category not in CONNECT_ON_DEMAND_CATEGORIES:
            return
        if self._connect_on_demand_disabled:
            return
        delay = IDLE_DISCONNECT_SECONDS.get(self.category)
        if not delay:
            return
        # Cancel any pending idle-disconnect; reschedule from now.
        if self._idle_disconnect_handle is not None:
            self._idle_disconnect_handle.cancel()
            self._idle_disconnect_handle = None
        loop = asyncio.get_running_loop()
        self._idle_disconnect_handle = loop.call_later(
            delay, self._fire_idle_disconnect
        )
        _LOGGER.debug(
            "%s: idle disconnect scheduled in %.1fs", self.address, delay
        )

    def _fire_idle_disconnect(self) -> None:
        """Idle-disconnect timer callback: actually trigger the disconnect."""
        self._idle_disconnect_handle = None
        # _disconnect creates a task; safe from a sync callback.
        self._disconnect()

    def _cancel_idle_disconnect(self) -> None:
        """Cancel any pending idle disconnect (e.g. before a fresh operation)."""
        if self._idle_disconnect_handle is not None:
            self._idle_disconnect_handle.cancel()
            self._idle_disconnect_handle = None

    def _disconnect(self) -> None:
        """Disconnect from device."""
        # Always cancel the idle timer in case _disconnect is called for
        # another reason (HA shutdown, integration unload, etc.).
        self._cancel_idle_disconnect()
        asyncio.create_task(self._execute_timed_disconnect())

    async def _execute_timed_disconnect(self) -> None:
        """Execute timed disconnection."""
        _LOGGER.debug(
            "%s: Disconnecting",
            self.address,
        )
        await self._execute_disconnect()

    async def _execute_disconnect(self) -> None:
        """Execute disconnection."""
        async with self._connect_lock:
            client = self._client
            self._expected_disconnect = True
            self._client = None
            if client and client.is_connected:
                try:
                    await client.stop_notify(getattr(self, "_char_notify", CHARACTERISTIC_NOTIFY))
                except Exception:
                    pass
                await client.disconnect()
        async with self._seq_num_lock:
            self._current_seq_num = 1
        self._outbound_idx = 0

    async def _ensure_connected(self) -> None:
        """Ensure connection to device is established."""
        global global_connect_lock
        if self._expected_disconnect:
            return
        if self._connect_lock.locked():
            _LOGGER.debug(
                "%s: Connection already in progress,"
                " waiting for it to complete; RSSI: %s",
                self.address,
                self.rssi,
            )
        if self._client and self._client.is_connected and self._is_paired:
            return
        async with self._connect_lock:
            # Check again while holding the lock
            await asyncio.sleep(0.01)
            if self._client and self._client.is_connected and self._is_paired:
                return
            attempts_count = 100
            while attempts_count > 0:
                attempts_count -= 1
                if attempts_count == 0:
                    _LOGGER.error(
                        "%s: Connecting, all attempts failed; RSSI: %s",
                        self.address,
                        self.rssi,
                    )
                    raise BleakNotFoundError()
                try:
                    async with global_connect_lock:
                        _LOGGER.debug(
                            "%s: Connecting; RSSI: %s", self.address, self.rssi
                        )
                        client = await establish_connection(
                            BleakClientWithServiceCache,
                            self._ble_device,
                            self.address,
                            self._disconnected,
                            use_services_cache=True,
                            ble_device_callback=lambda: self._ble_device,
                        )
                except BleakNotFoundError:
                    _LOGGER.error(
                        "%s: device not found, not in range, or poor RSSI: %s",
                        self.address,
                        self.rssi,
                        exc_info=True,
                    )
                    continue
                except BLEAK_EXCEPTIONS:
                    _LOGGER.debug(
                        "%s: communication failed", self.address, exc_info=True
                    )
                    continue
                except:
                    _LOGGER.debug("%s: unexpected error",
                                  self.address, exc_info=True)
                    continue

                if client and client.is_connected:
                    _LOGGER.debug("%s: Connected; RSSI: %s",
                                  self.address, self.rssi)
                    self._client = client
                    try:
                        await self._client.start_notify(
                            CHARACTERISTIC_NOTIFY, self._notification_handler
                        )
                        self._char_notify = CHARACTERISTIC_NOTIFY
                        self._char_write = CHARACTERISTIC_WRITE
                    except Exception:
                        try:
                            await self._client.start_notify(
                                CHARACTERISTIC_NOTIFY_OLD, self._notification_handler
                            )
                            self._char_notify = CHARACTERISTIC_NOTIFY_OLD
                            self._char_write = CHARACTERISTIC_WRITE_OLD
                            _LOGGER.warning(
                                "%s: Using legacy BLE protocol (0xFD50)",
                                self.address,
                            )
                        except:
                            self._client = None
                            _LOGGER.error("%s: starting notifications failed",
                                          self.address, exc_info=True)
                            continue
                else:
                    continue

                if self._client and self._client.is_connected:
                    _LOGGER.debug(
                        "%s: Sending device info request", self.address)
                    try:
                        if not await self._send_packet_while_connected(
                            TuyaBLECode.FUN_SENDER_DEVICE_INFO,
                            bytes.fromhex("00f3"),
                            0,
                            True,
                        ):
                            self._client = None
                            _LOGGER.error(
                                "%s: Sending device info request failed",
                                self.address,
                            )
                            continue
                    except:  # [BLEAK_EXCEPTIONS, BleakNotFoundError]:
                        self._client = None
                        _LOGGER.error("%s: Sending device info request failed",
                                      self.address, exc_info=True)
                        continue
                else:
                    continue

                if self._client and self._client.is_connected:
                    _LOGGER.debug("%s: Sending pairing request", self.address)
                    try:
                        if not await self._send_packet_while_connected(
                            TuyaBLECode.FUN_SENDER_PAIR,
                            self._build_pairing_request(),
                            0,
                            True,
                        ):
                            self._client = None
                            _LOGGER.error(
                                "%s: Sending pairing request failed",
                                self.address,
                            )
                            continue
                    except:  # [BLEAK_EXCEPTIONS, BleakNotFoundError]:
                        self._client = None
                        _LOGGER.error("%s: Sending pairing request failed",
                                      self.address, exc_info=True)
                        continue
                else:
                    continue

                break

        if self._client:
            if self._client.is_connected:
                if self._is_paired:
                    _LOGGER.debug("%s: Successfully connected", self.address)
                    # v0.4.0: reset failure counter on successful connect
                    if self._consecutive_connect_failures > 0:
                        _LOGGER.debug(
                            "%s: clearing failure counter (was %d)",
                            self.address,
                            self._consecutive_connect_failures,
                        )
                        self._consecutive_connect_failures = 0
                    self._fire_connected_callbacks()
                else:
                    _LOGGER.error("%s: Connected but not paired", self.address)
            else:
                _LOGGER.error("%s: Not connected", self.address)
        else:
            _LOGGER.error("%s: No client device", self.address)

    async def _reconnect(self) -> None:
        """Attempt a reconnect"""
        _LOGGER.debug("%s: Reconnect, ensuring connection", self.address)
        async with self._seq_num_lock:
            self._current_seq_num = 1
        self._outbound_idx = 0
        try:
            if self._expected_disconnect:
                return
            await self._ensure_connected()
            if self._expected_disconnect:
                return
            _LOGGER.debug("%s: Reconnect, connection ensured", self.address)
        except BLEAK_EXCEPTIONS:  # BleakNotFoundError:
            _LOGGER.debug(
                "%s: Reconnect, failed to ensure connection - backing off",
                self.address,
                exc_info=True,
            )
            await asyncio.sleep(BLEAK_BACKOFF_TIME)
            _LOGGER.debug("%s: Reconnecting again", self.address)
            asyncio.create_task(self._reconnect())

    @staticmethod
    def _calc_crc16(data: bytes) -> int:
        crc = 0xFFFF
        for byte in data:
            crc ^= byte & 255
            for _ in range(8):
                tmp = crc & 1
                crc >>= 1
                if tmp != 0:
                    crc ^= 0xA001
        return crc

    @staticmethod
    def _pack_int(value: int) -> bytearray:
        curr_byte: int
        result = bytearray()
        while True:
            curr_byte = value & 0x7F
            value >>= 7
            if value != 0:
                curr_byte |= 0x80
            result += pack(">B", curr_byte)
            if value == 0:
                break
        return result

    @staticmethod
    def _unpack_int(data: bytes, start_pos: int) -> tuple(int, int):
        result: int = 0
        offset: int = 0
        while offset < 5:
            pos: int = start_pos + offset
            if pos >= len(data):
                raise TuyaBLEDataFormatError()
            curr_byte: int = data[pos]
            result |= (curr_byte & 0x7F) << (offset * 7)
            offset += 1
            if (curr_byte & 0x80) == 0:
                break
        if offset > 4:
            raise TuyaBLEDataFormatError()
        else:
            return (result, start_pos + offset)

    def _build_packets(
        self,
        seq_num: int,
        code: TuyaBLECode,
        data: bytes,
        response_to: int = 0,
    ) -> list[bytes]:
        key: bytes
        iv = secrets.token_bytes(16)
        security_flag: bytes
        if code == TuyaBLECode.FUN_SENDER_DEVICE_INFO:
            key = self._login_key
            security_flag = b"\x04"
        else:
            key = self._session_key
            security_flag = b"\x05"

        raw = bytearray()
        raw += pack(">IIHH", seq_num, response_to, code.value, len(data))
        raw += data
        crc = self._calc_crc16(raw)
        raw += pack(">H", crc)
        while len(raw) % 16 != 0:
            raw += b"\x00"

        cipher = AES.new(key, AES.MODE_CBC, iv)
        encrypted = security_flag + iv + cipher.encrypt(raw)

        command = []
        packet_num = 0
        pos = 0
        length = len(encrypted)
        while pos < length:
            packet = bytearray()
            packet += self._pack_int(packet_num)

            if packet_num == 0:
                packet += self._pack_int(length)
                packet += pack(">B", self._protocol_version << 4)

            data_part = encrypted[
                pos:pos + GATT_MTU - len(packet)  # fmt: skip
            ]
            packet += data_part
            command.append(packet)

            pos += len(data_part)
            packet_num += 1

        return command

    async def _get_seq_num(self) -> int:
        async with self._seq_num_lock:
            result = self._current_seq_num
            self._current_seq_num += 1
        return result

    async def _send_packet(
        self,
        code: TuyaBLECode,
        data: bytes,
        wait_for_response: bool = True,
        # retry: int | None = None,
    ) -> None:
        """Send packet to device and optional read response."""
        if self._expected_disconnect:
            return
        await self._ensure_connected()
        if self._expected_disconnect:
            return
        await self._send_packet_while_connected(code, data, 0, wait_for_response)
        # v0.4.0: For connect-on-demand categories, schedule a delayed
        # disconnect so the lock can return to its low-power advertising
        # state. We schedule rather than disconnect immediately so a
        # follow-up operation arriving within the idle window does not pay
        # the reconnect cost.
        self._maybe_schedule_idle_disconnect()

    async def _send_response(
        self,
        code: TuyaBLECode,
        data: bytes,
        response_to: int,
    ) -> None:
        """Send response to received packet."""
        if self._client and self._client.is_connected:
            await self._send_packet_while_connected(code, data, response_to, False)

    async def _send_packet_while_connected(
        self,
        code: TuyaBLECode,
        data: bytes,
        response_to: int,
        wait_for_response: bool,
        # retry: int | None = None
    ) -> bool:
        """Send packet to device and optional read response."""
        result = True
        future: asyncio.Future | None = None
        seq_num = await self._get_seq_num()
        if wait_for_response:
            future = asyncio.Future()
            self._input_expected_responses[seq_num] = future

        if response_to > 0:
            _LOGGER.debug(
                "%s: Sending packet: #%s %s in response to #%s",
                self.address,
                seq_num,
                code.name,
                response_to,
            )
        else:
            _LOGGER.debug(
                "%s: Sending packet: #%s %s",
                self.address,
                seq_num,
                code.name,
            )
        packets: list[bytes] = self._build_packets(
            seq_num, code, data, response_to)
        await self._int_send_packet_while_connected(packets)
        if future:
            try:
                await asyncio.wait_for(future, RESPONSE_WAIT_TIMEOUT)
            except asyncio.TimeoutError:
                _LOGGER.error(
                    "%s: timeout receiving response, RSSI: %s",
                    self.address,
                    self.rssi,
                )
                result = False
            self._input_expected_responses.pop(seq_num, None)

        return result

    async def _int_send_packet_while_connected(
        self,
        packets: list[bytes],
    ) -> None:
        if self._operation_lock.locked():
            _LOGGER.debug(
                "%s: Operation already in progress, "
                "waiting for it to complete; RSSI: %s",
                self.address,
                self.rssi,
            )
        async with self._operation_lock:
            try:
                await self._send_packets_locked(packets)
            except BleakNotFoundError:
                _LOGGER.error(
                    "%s: device not found, no longer in range, or poor RSSI: %s",
                    self.address,
                    self.rssi,
                    exc_info=True,
                )
                raise
            except BLEAK_EXCEPTIONS:
                _LOGGER.error(
                    "%s: communication failed",
                    self.address,
                    exc_info=True,
                )
                raise

    async def _resend_packets(self, packets: list[bytes]) -> None:
        if self._expected_disconnect:
            return
        await self._ensure_connected()
        if self._expected_disconnect:
            return
        await self._int_send_packet_while_connected(packets)

    async def _send_packets_locked(self, packets: list[bytes]) -> None:
        """Send command to device and read response."""
        try:
            await self._int_send_packets_locked(packets)
        except BleakDBusError as ex:
            # Disconnect so we can reset state and try again
            await asyncio.sleep(BLEAK_BACKOFF_TIME)
            _LOGGER.debug(
                "%s: RSSI: %s; Backing off %ss; Disconnecting due to error: %s",
                self.address,
                self.rssi,
                BLEAK_BACKOFF_TIME,
                ex,
            )
            if self._is_paired:
                asyncio.create_task(self._resend_packets(packets))
            else:
                asyncio.create_task(self._reconnect())
            raise BleakError from ex
        except BleakError as ex:
            # Disconnect so we can reset state and try again
            _LOGGER.debug(
                "%s: RSSI: %s; Disconnecting due to error: %s",
                self.address,
                self.rssi,
                ex,
            )
            if self._is_paired:
                asyncio.create_task(self._resend_packets(packets))
            else:
                asyncio.create_task(self._reconnect())
            raise

    async def _int_send_packets_locked(self, packets: list[bytes]) -> None:
        """Execute command and read response."""
        for packet in packets:
            if self._client:
                try:
                    # _LOGGER.debug("%s: Sending packet: %s", self.address, packet.hex())
                    _LOGGER.warning("%s: Sending raw packet: %s", self.address, packet.hex())
                    await self._client.write_gatt_char(
                        getattr(self, "_char_write", CHARACTERISTIC_WRITE),
                        packet,
                        False,
                    )
                except:
                    _LOGGER.error(
                        "%s: Error during sending packet",
                        self.address,
                        exc_info=True,
                    )
                    if self._client and self._client.is_connected:
                        self._disconnected(self._client)
                    raise BleakError()
            else:
                _LOGGER.error(
                    "%s: Client disconnected during sending packet",
                    self.address,
                    exc_info=True,
                )
                raise BleakError()

    def _get_key(self, security_flag: int) -> bytes:
        if security_flag == 1:
            return self._auth_key
        if security_flag == 4:
            return self._login_key
        elif security_flag == 5:
            return self._session_key
        else:
            pass

    def _parse_timestamp(self, data: bytes, start_pos: int) -> tuple(float, int):
        timestamp: float
        pos = start_pos
        if pos >= len(data):
            raise TuyaBLEDataLengthError()
        time_type = data[pos]
        pos += 1
        end_pos = pos
        match time_type:
            case 0:
                end_pos += 13
                if end_pos > len(data):
                    raise TuyaBLEDataLengthError()
                timestamp = int(data[pos:end_pos].decode()) / 1000
                pass
            case 1:
                end_pos += 4
                if end_pos > len(data):
                    raise TuyaBLEDataLengthError()
                timestamp = int.from_bytes(data[pos:end_pos], "big") * 1.0
                pass
            case _:
                raise TuyaBLEDataFormatError()

        _LOGGER.debug(
            "%s: Received timestamp: %s",
            self.address,
            time.ctime(timestamp),
        )
        return (timestamp, end_pos)

    def _parse_datapoints_v3(
        self, timestamp: float, flags: int, data: bytes, start_pos: int
    ) -> int:
        datapoints: list[TuyaBLEDataPoint] = []

        pos = start_pos
        while len(data) - pos >= 4:
            id: int = data[pos]
            pos += 1
            _type: int = data[pos]
            if _type > TuyaBLEDataPointType.DT_BITMAP.value:
                _LOGGER.warning(
                    "%s: DP parse: invalid type %s at pos %s, "
                    "remaining=%s. Stopping parse but keeping connection.",
                    self.address, _type, pos - 1, data[pos-1:].hex(),
                )
                break
            type: TuyaBLEDataPointType = TuyaBLEDataPointType(_type)
            pos += 1
            data_len: int = data[pos]
            pos += 1
            next_pos = pos + data_len
            if next_pos > len(data):
                _LOGGER.warning(
                    "%s: DP parse: length %s exceeds buffer at pos %s. "
                    "Stopping parse.",
                    self.address, data_len, pos - 1,
                )
                break
            raw_value = data[pos:next_pos]
            match type:
                case (TuyaBLEDataPointType.DT_RAW | TuyaBLEDataPointType.DT_BITMAP):
                    value = raw_value
                case TuyaBLEDataPointType.DT_BOOL:
                    value = int.from_bytes(raw_value, "big") != 0
                case (TuyaBLEDataPointType.DT_VALUE | TuyaBLEDataPointType.DT_ENUM):
                    value = int.from_bytes(raw_value, "big", signed=True)
                case TuyaBLEDataPointType.DT_STRING:
                    value = raw_value.decode()

            _LOGGER.debug(
                "%s: Received datapoint update, id: %s, type: %s: value: %s",
                self.address,
                id,
                type.name,
                value,
            )
            self._datapoints._update_from_device(
                id, timestamp, flags, type, value)
            datapoints.append(self._datapoints[id])
            pos = next_pos

        self._fire_callbacks(datapoints)

    def _parse_datapoints_v4(
        self, timestamp: float, flags: int, data: bytes, start_pos: int
    ) -> None:
        """Parse V4 DP records: [id:1][type:1][reserved:1][len:1][val:len].

        V4 packets start with a 4-byte dp_seq prefix; call this with
        start_pos=4 to skip it.

        DP 9 is a session-summary wrapper. Its 8-byte val carries a
        nested V3-format DP (id=2 raw, 4-byte BE int) that holds the
        battery percentage. We extract that and synthesize a DP 8
        VALUE update so the existing sensor.py battery mapping fires.
        """
        datapoints: list[TuyaBLEDataPoint] = []
        pos = start_pos

        while len(data) - pos >= 4:
            dp_id: int = data[pos]
            _type: int = data[pos + 1]
            reserved: int = data[pos + 2]
            data_len: int = data[pos + 3]

            if _type > TuyaBLEDataPointType.DT_BITMAP.value:
                _LOGGER.warning(
                    "%s: V4 DP parse: invalid type %s at pos %s, "
                    "remaining=%s",
                    self.address, _type, pos + 1, data[pos:].hex(),
                )
                break

            next_pos = pos + 4 + data_len
            if next_pos > len(data):
                _LOGGER.warning(
                    "%s: V4 DP parse: len %s overflows buffer at pos %s",
                    self.address, data_len, pos,
                )
                break

            dp_type = TuyaBLEDataPointType(_type)
            raw_value = data[pos + 4:next_pos]

            # Special case: session-summary wrapper carries battery % as a
            # nested V3-format DP: [id=02][type=00 RAW][len=04][val=4 BE bytes]
            # plus trailing byte 2f (lock_motor_state reference). Total 8 bytes.
            # The value (battery %) is at bytes 3-6 of the val, NOT 4-7 — the
            # 3-byte nested header is [02 00 04], then the 4-byte int, then 2f.
            # Observed wrapper dp_id varies (seen 9 and 13) so we signature-
            # match on the header bytes 0-2 plus trailing 2f instead.
            if (data_len == 8
                    and raw_value[:3] == b"\x02\x00\x04"
                    and raw_value[-1:] == b"\x2f"):
                battery_percent = int.from_bytes(raw_value[3:7], "big")
                _LOGGER.info(
                    "%s: battery wrapper (dp=%d) -> DP 8 battery = %d%%",
                    self.address, dp_id, battery_percent,
                )
                self._datapoints._update_from_device(
                    8, timestamp, flags,
                    TuyaBLEDataPointType.DT_VALUE,
                    battery_percent,
                )
                datapoints.append(self._datapoints[8])
                pos = next_pos
                continue

            # Decode outer DP value per type
            if dp_type in (TuyaBLEDataPointType.DT_RAW,
                           TuyaBLEDataPointType.DT_BITMAP):
                value = raw_value
            elif dp_type == TuyaBLEDataPointType.DT_BOOL:
                value = int.from_bytes(raw_value, "big") != 0
            elif dp_type in (TuyaBLEDataPointType.DT_VALUE,
                             TuyaBLEDataPointType.DT_ENUM):
                value = int.from_bytes(raw_value, "big", signed=True) \
                    if data_len > 0 else 0
            elif dp_type == TuyaBLEDataPointType.DT_STRING:
                try:
                    value = raw_value.decode()
                except UnicodeDecodeError:
                    value = raw_value.hex()
            else:
                value = raw_value

            _LOGGER.debug(
                "%s: V4 DP id=%s type=%s reserved=%s len=%s value=%s",
                self.address, dp_id, dp_type.name, reserved, data_len, value,
            )

            self._datapoints._update_from_device(
                dp_id, timestamp, flags, dp_type, value,
            )
            datapoints.append(self._datapoints[dp_id])
            pos = next_pos

        self._fire_callbacks(datapoints)

    def _handle_command_or_response(
        self, seq_num: int, response_to: int, code: TuyaBLECode, data: bytes
    ) -> None:
        result: int = 0

        match code:
            case TuyaBLECode.FUN_SENDER_DEVICE_INFO:
                if len(data) < 46:
                    raise TuyaBLEDataLengthError()

                self._device_version = ("%s.%s") % (data[0], data[1])
                self._protocol_version_str = ("%s.%s") % (data[2], data[3])
                self._hardware_version = ("%s.%s") % (data[12], data[13])

                self._protocol_version = data[2]
                self._flags = data[4]
                self._is_bound = data[5] != 0

                srand = data[6:12]
                self._session_key = hashlib.md5(
                    self._local_key + srand).digest()
                self._auth_key = data[14:46]

            case TuyaBLECode.FUN_SENDER_PAIR:
                if len(data) != 1:
                    raise TuyaBLEDataLengthError()
                result = data[0]
                if result == 2:
                    _LOGGER.debug(
                        "%s: Device is already paired",
                        self.address,
                    )
                    result = 0
                self._is_paired = result == 0

            case TuyaBLECode.FUN_SENDER_DEVICE_STATUS:
                if len(data) != 1:
                    raise TuyaBLEDataLengthError()
                result = data[0]

            case TuyaBLECode.FUN_RECEIVE_DP_V4:
                _LOGGER.debug(
                    "%s: FUN_RECEIVE_DP_V4 payload (%d bytes): %s",
                    self.address, len(data), data.hex(),
                )
                # Detect the lock's "please re-authenticate" greeting.
                # Structure (14 bytes):
                #   00 00 00 00 [idx:1] 00 00 45 00 00 03 00 02 01
                # The 0x45 at offset 7 tells us to send subcmd 0x45 user
                # registration. Without it, the lock refuses to send bulk
                # status and ignores unlock commands (though it still ACKs
                # them at the frame level).
                is_auth_prompt = (
                    len(data) == 14
                    and data[:4] == b"\x00\x00\x00\x00"
                    and len(data) > 10
                    and data[7] == 0x45
                    and data[10] == 0x03
                )
                if is_auth_prompt and self.category == "jtmspro":
                    _LOGGER.info(
                        "%s: jtmspro auth prompt received; sending subcmd 0x45",
                        self.address,
                    )
                    asyncio.create_task(self._send_jtmspro_user_auth())
                elif len(data) >= 4:
                    # Normal V4 DP data packet
                    self._parse_datapoints_v4(time.time(), 0, data, 4)
                asyncio.create_task(
                    self._send_response(code, bytes(0), seq_num))

            case TuyaBLECode.FUN_RECEIVE_TIME_DP_V4:
                _LOGGER.debug(
                    "%s: FUN_RECEIVE_TIME_DP_V4 payload (%d bytes): %s",
                    self.address, len(data), data.hex(),
                )
                try:
                    timestamp, pos = self._parse_timestamp(data, 4)
                    self._parse_datapoints_v4(timestamp, 0, data, pos)
                except Exception as exc:
                    _LOGGER.warning(
                        "%s: FUN_RECEIVE_TIME_DP_V4 parse failed: %s",
                        self.address, exc,
                    )
                asyncio.create_task(
                    self._send_response(code, bytes(0), seq_num))

            case TuyaBLECode.FUN_RECEIVE_TIME1_REQ:
                if len(data) != 0:
                    raise TuyaBLEDataLengthError()

                timestamp = int(time.time_ns() / 1000000)
                timezone = -int(time.timezone / 36)
                data = str(timestamp).encode() + pack(">h", timezone)
                asyncio.create_task(self._send_response(code, data, seq_num))

            case TuyaBLECode.FUN_RECEIVE_TIME2_REQ:
                if len(data) != 0:
                    raise TuyaBLEDataLengthError()

                time_str: time.struct_time = time.localtime()
                timezone = -int(time.timezone / 36)
                data = pack(
                    ">BBBBBBBh",
                    time_str.tm_year % 100,
                    time_str.tm_mon,
                    time_str.tm_mday,
                    time_str.tm_hour,
                    time_str.tm_min,
                    time_str.tm_sec,
                    time_str.tm_wday,
                    timezone,
                )
                asyncio.create_task(self._send_response(code, data, seq_num))

            case TuyaBLECode.FUN_RECEIVE_DP:
                self._parse_datapoints_v3(time.time(), 0, data, 0)
                asyncio.create_task(
                    self._send_response(code, bytes(0), seq_num))

            case TuyaBLECode.FUN_RECEIVE_SIGN_DP:
                dp_seq_num = int.from_bytes(data[:2], "big")
                flags = data[2]
                self._parse_datapoints_v3(time.time(), flags, data, 2)
                data = pack(">HBB", dp_seq_num, flags, 0)
                asyncio.create_task(self._send_response(code, data, seq_num))

            case TuyaBLECode.FUN_RECEIVE_TIME_DP:
                timestamp: float
                pos: int
                timestamp, pos = self._parse_timestamp(data, 0)
                self._parse_datapoints_v3(timestamp, 0, data, pos)
                asyncio.create_task(
                    self._send_response(code, bytes(0), seq_num))

            case TuyaBLECode.FUN_RECEIVE_SIGN_TIME_DP:
                timestamp: float
                pos: int
                dp_seq_num = int.from_bytes(data[:2], "big")
                flags = data[2]
                timestamp, pos = self._parse_timestamp(data, 3)
                self._parse_datapoints_v3(time.time(), flags, data, pos)
                data = pack(">HBB", dp_seq_num, flags, 0)
                asyncio.create_task(self._send_response(code, data, seq_num))

        if response_to != 0:
            future = self._input_expected_responses.pop(response_to, None)
            if future:
                _LOGGER.debug(
                    "%s: Received expected response to #%s, result: %s",
                    self.address,
                    response_to,
                    result,
                )
                if result == 0:
                    future.set_result(result)
                else:
                    future.set_exception(TuyaBLEDeviceError(result))

    def _clean_input(self) -> None:
        self._input_buffer = None
        self._input_expected_packet_num = 0
        self._input_expected_length = 0

    def _parse_input(self) -> None:
        security_flag = self._input_buffer[0]
        key = self._get_key(security_flag)
        iv = self._input_buffer[1:17]
        encrypted = self._input_buffer[17:]

        self._clean_input()

        cipher = AES.new(key, AES.MODE_CBC, iv)
        raw = cipher.decrypt(encrypted)

        seq_num: int
        response_to: int
        _code: int
        length: int
        seq_num, response_to, _code, length = unpack(">IIHH", raw[:12])

        data_end_pos = length + 12
        raw_length = len(raw)
        if raw_length < data_end_pos:
            raise TuyaBLEDataLengthError()
        if raw_length > data_end_pos:
            calc_crc = self._calc_crc16(raw[:data_end_pos])
            (data_crc,) = unpack(
                ">H",
                raw[data_end_pos:data_end_pos + 2]  # fmt: skip
            )
            if calc_crc != data_crc:
                raise TuyaBLEDataCRCError()
        data = raw[12:data_end_pos]

        code: TuyaBLECode
        try:
            code = TuyaBLECode(_code)
        except ValueError:
            _LOGGER.debug(
                "%s: Received unknown message: #%s %x, response to #%s, data %s",
                self.address,
                seq_num,
                _code,
                response_to,
                data.hex(),
            )
            return

        if response_to != 0:
            _LOGGER.debug(
                "%s: Received: #%s %s, response to #%s",
                self.address,
                seq_num,
                code.name,
                response_to,
            )
        else:
            _LOGGER.debug(
                "%s: Received: #%s %s",
                self.address,
                seq_num,
                code.name,
            )

        self._handle_command_or_response(seq_num, response_to, code, data)

    def _notification_handler(self, _sender: int, data: bytearray) -> None:
        """Handle notification responses."""
        _LOGGER.warning("%s: RAW packet received: %s", self.address, data.hex())
        _LOGGER.debug("%s: Packet received: %s", self.address, data.hex())

        pos: int = 0
        packet_num: int

        packet_num, pos = self._unpack_int(data, pos)

        if packet_num < self._input_expected_packet_num:
            _LOGGER.error(
                "%s: Unexpcted packet (number %s) in notifications, " "expected %s",
                self.address,
                packet_num,
                self._input_expected_packet_num,
            )
            self._clean_input()

        if packet_num == self._input_expected_packet_num:
            if packet_num == 0:
                self._input_buffer = bytearray()
                self._input_expected_length, pos = self._unpack_int(data, pos)
                pos += 1
            self._input_buffer += data[pos:]
            self._input_expected_packet_num += 1
        else:
            _LOGGER.error(
                "%s: Missing packet (number %s) in notifications, received %s",
                self.address,
                self._input_expected_packet_num,
                packet_num,
            )
            self._clean_input()
            return

        if len(self._input_buffer) > self._input_expected_length:
            _LOGGER.error(
                "%s: Unexpcted length of data in notifications, "
                "received %s expected %s",
                self.address,
                len(self._input_buffer),
                self._input_expected_length,
            )
            self._clean_input()
            return
        elif len(self._input_buffer) == self._input_expected_length:
            self._parse_input()

    async def _send_datapoints_v3(self, datapoint_ids: list[int]) -> None:
        """Send new values of datapoints to the device."""
        data = bytearray()
        for dp_id in datapoint_ids:
            dp = self._datapoints[dp_id]
            value = dp._get_value()
            _LOGGER.debug(
                "%s: Sending datapoint update, id: %s, type: %s: value: %s",
                self.address,
                dp.id,
                dp.type.name,
                dp.value,
            )
            data += pack(">BBB", dp.id, int(dp.type.value), len(value))
            data += value

        await self._send_packet(TuyaBLECode.FUN_SENDER_DPS, data)

    async def _send_datapoints_v4(self, datapoint_ids: list[int]) -> None:
        """Send new values of datapoints to a V4 protocol device.

        Format verified against HCI decryption of Smart Life writing
        DP 33 (auto_lock) to a CTL20H lock:
          opcode:  FUN_SENDER_DPS_V4 (0x0027)
          payload: [dp_seq:4 BE = 0][idx:1][records...]
          record:  [id:1][type:1][reserved:1][len:1][val:len]

        dp_seq is always 0. The `idx` byte is a session-wide command
        counter that increments with each outbound command (DP writes
        AND subcommand packets share the same counter). Starts at 1
        on reconnect.
        """
        self._outbound_idx = (self._outbound_idx + 1) & 0xFF
        data = bytearray(b"\x00\x00\x00\x00")
        data += pack(">B", self._outbound_idx)

        for dp_id in datapoint_ids:
            dp = self._datapoints[dp_id]
            value = dp._get_value()
            _LOGGER.debug(
                "%s: V4 DP SEND idx=%d dp_id=%s type=%s value=%s bytes=%s",
                self.address,
                self._outbound_idx,
                dp.id,
                dp.type.name,
                dp.value,
                value.hex(),
            )
            data += pack(">BBBB", dp.id, int(dp.type.value), 0, len(value))
            data += value

        await self._send_packet(TuyaBLECode.FUN_SENDER_DPS_V4, data)

    async def send_raw_command_v4(
        self, subcmd: int, payload: bytes
    ) -> None:
        """Send a raw V4 subcommand packet (for jtmspro lock unlock etc).

        Format derived from HCI capture of Smart Life unlock:
          opcode:  FUN_SENDER_DPS_V4 (0x0027)
          payload: [dp_seq:4 BE = 0][idx:1][subcmd:1][reserved:2 = 0][length:1][payload]

        The idx shares the same session counter as DP writes.
        """
        self._outbound_idx = (self._outbound_idx + 1) & 0xFF
        data = bytearray(b"\x00\x00\x00\x00")
        data += pack(">B", self._outbound_idx)
        data += pack(">BBBB", subcmd, 0, 0, len(payload))
        data += payload
        _LOGGER.warning(
            "%s: V4 SUBCMD SEND idx=%d subcmd=%#x payload=%s",
            self.address, self._outbound_idx, subcmd, payload.hex(),
        )
        await self._send_packet(TuyaBLECode.FUN_SENDER_DPS_V4, data)

    def _get_jtmspro_user_id(self) -> bytes:
        """Return the BLE user_id for jtmspro unlock/auth commands.

        Priority: cloud-derived value from ble_unlock_check DP, else the
        known-registered fallback captured from the original HCI session.
        The fallback lets production lock #1 continue to operate if cloud
        fetch fails at integration load.
        """
        if self._device_info is not None:
            cloud_id = getattr(self._device_info, "ble_user_id", None)
            if cloud_id:
                # Stored as str to keep config-entry JSON-serializable; BLE
                # payload needs bytes.
                return cloud_id.encode("ascii")
        _LOGGER.warning(
            "%s: jtmspro BLE user_id unavailable from cloud; using fallback "
            "84042128. If this is a newly-paired lock, unlock it once via "
            "Smart Life then reload the integration.",
            self.address,
        )
        return b"84042128"

    async def _send_jtmspro_user_auth(self) -> None:
        """Send subcmd 0x45 BLE user authentication for jtmspro locks.

        The lock refuses to report DP status or accept unlock commands
        until the phone identifies itself via subcmd 0x45 with a registered
        user_id. Smart Life generates a per-account user_id during initial
        pairing (captured value below). Re-sending this on every BLE
        reconnect re-authorizes the session — idempotent, doesn't create
        new users.

        Payload format (13 bytes, verified from HCI):
          ff ff 00 02              4-byte constant prefix
          [user_id_ascii:8]        registered BLE user
          00                       trailer
        """
        user_id = self._get_jtmspro_user_id()
        payload = bytearray(b"\xff\xff\x00\x02")
        payload += user_id
        payload += b"\x00"
        try:
            await self.send_raw_command_v4(0x45, bytes(payload))
        except Exception as exc:
            _LOGGER.warning(
                "%s: jtmspro auth send failed: %s", self.address, exc,
            )

    async def send_jtmspro_unlock(self, payload: bytes) -> None:
        """Authenticate then send a jtmspro unlock command (subcmd 0x47).

        The CTL20H lock rejects subcmd 0x47 (status code 0x03) unless the
        session has a recent subcmd 0x45 user authentication. Some firmware
        builds prompt for the auth on a timer (medicine lock behavior);
        others — typically locks that have been factory-reset and re-paired
        recently — do not prompt at all (knives lock behavior). Sending
        0x45 proactively before each 0x47 makes the unlock work either way,
        and matches what Smart Life does internally.

        The auth is idempotent — re-sending it does NOT register a new
        user_id; the lock just refreshes the session's authorization for
        the already-registered user.
        """
        try:
            await self._send_jtmspro_user_auth()
        except Exception as exc:
            _LOGGER.warning(
                "%s: jtmspro pre-unlock auth failed, attempting unlock anyway: %s",
                self.address, exc,
            )
        try:
            await self.send_raw_command_v4(0x47, payload)
        except Exception as exc:
            _LOGGER.error(
                "%s: jtmspro unlock send failed: %s", self.address, exc,
            )

    async def _send_datapoints(self, datapoint_ids: list[int]) -> None:
        """Send new values of datapoints to the device."""
        if self._protocol_version == 3:
            await self._send_datapoints_v3(datapoint_ids)
        elif self._protocol_version == 4:
            await self._send_datapoints_v4(datapoint_ids)
        else:
            _LOGGER.error(
                "%s: no send implementation for protocol version %s",
                self.address, self._protocol_version,
            )
            raise TuyaBLEDeviceError(0)
