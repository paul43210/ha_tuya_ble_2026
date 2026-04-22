"""Config flow for Tuya BLE integration."""

from __future__ import annotations

import logging
import pycountry
from typing import Any

import voluptuous as vol
from tuya_iot import AuthType

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlowWithConfigEntry,
)
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowHandler
try:
    from homeassistant.data_entry_flow import FlowResult
except ImportError:
    from typing import Any as FlowResult

CONF_ACCESS_ID = "access_id"
CONF_ACCESS_SECRET = "access_secret"
CONF_APP_TYPE = "tuya_app_type"
CONF_AUTH_TYPE = "auth_type"
CONF_COUNTRY_CODE = "country_code"
CONF_ENDPOINT = "endpoint"
CONF_PASSWORD = "password"
CONF_USERNAME = "username"
SMARTLIFE_APP = "smartlife"
TUYA_SMART_APP = "tuyaSmart"
TUYA_RESPONSE_CODE = "code"
TUYA_RESPONSE_MSG = "msg"
TUYA_RESPONSE_SUCCESS = "success"
from dataclasses import dataclass
from tuya_iot import TuyaCloudOpenAPIEndpoint

@dataclass
class TuyaCountry:
    name: str
    country_code: str
    endpoint: TuyaCloudOpenAPIEndpoint

TUYA_COUNTRIES = [
    TuyaCountry("Afghanistan", "93", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Albania", "355", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Algeria", "213", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Angola", "244", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Argentina", "54", TuyaCloudOpenAPIEndpoint.AMERICA),
    TuyaCountry("Armenia", "374", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Australia", "61", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Austria", "43", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Azerbaijan", "994", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Bahrain", "973", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Bangladesh", "880", TuyaCloudOpenAPIEndpoint.INDIA),
    TuyaCountry("Belarus", "375", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Belgium", "32", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Bolivia", "591", TuyaCloudOpenAPIEndpoint.AMERICA),
    TuyaCountry("Bosnia and Herzegovina", "387", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Brazil", "55", TuyaCloudOpenAPIEndpoint.AMERICA),
    TuyaCountry("Brunei", "673", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Bulgaria", "359", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Cambodia", "855", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Cameroon", "237", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Canada", "1-CA", TuyaCloudOpenAPIEndpoint.AMERICA),
    TuyaCountry("Chile", "56", TuyaCloudOpenAPIEndpoint.AMERICA),
    TuyaCountry("China", "86", TuyaCloudOpenAPIEndpoint.CHINA),
    TuyaCountry("Colombia", "57", TuyaCloudOpenAPIEndpoint.AMERICA),
    TuyaCountry("Costa Rica", "506", TuyaCloudOpenAPIEndpoint.AMERICA),
    TuyaCountry("Croatia", "385", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Cyprus", "357", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Czech Republic", "420", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Denmark", "45", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Ecuador", "593", TuyaCloudOpenAPIEndpoint.AMERICA),
    TuyaCountry("Egypt", "20", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Estonia", "372", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Ethiopia", "251", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Finland", "358", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("France", "33", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Georgia", "995", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Germany", "49", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Ghana", "233", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Greece", "30", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Guatemala", "502", TuyaCloudOpenAPIEndpoint.AMERICA),
    TuyaCountry("Honduras", "504", TuyaCloudOpenAPIEndpoint.AMERICA),
    TuyaCountry("Hong Kong", "852", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Hungary", "36", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("India", "91", TuyaCloudOpenAPIEndpoint.INDIA),
    TuyaCountry("Indonesia", "62", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Iraq", "964", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Ireland", "353", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Israel", "972", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Italy", "39", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Japan", "81", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Jordan", "962", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Kazakhstan", "7", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Kenya", "254", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Kuwait", "965", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Latvia", "371", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Lebanon", "961", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Lithuania", "370", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Luxembourg", "352", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Malaysia", "60", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Malta", "356", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Mexico", "52", TuyaCloudOpenAPIEndpoint.AMERICA),
    TuyaCountry("Moldova", "373", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Morocco", "212", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Mozambique", "258", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Netherlands", "31", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("New Zealand", "64", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Nigeria", "234", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Norway", "47", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Oman", "968", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Pakistan", "92", TuyaCloudOpenAPIEndpoint.INDIA),
    TuyaCountry("Panama", "507", TuyaCloudOpenAPIEndpoint.AMERICA),
    TuyaCountry("Peru", "51", TuyaCloudOpenAPIEndpoint.AMERICA),
    TuyaCountry("Philippines", "63", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Poland", "48", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Portugal", "351", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Qatar", "974", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Romania", "40", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Russia", "7-RU", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Saudi Arabia", "966", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Senegal", "221", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Serbia", "381", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Singapore", "65", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Slovakia", "421", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Slovenia", "386", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("South Africa", "27", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("South Korea", "82", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Spain", "34", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Sri Lanka", "94", TuyaCloudOpenAPIEndpoint.INDIA),
    TuyaCountry("Sweden", "46", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Switzerland", "41", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Taiwan", "886", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Tanzania", "255", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Thailand", "66", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Tunisia", "216", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Turkey", "90", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Uganda", "256", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Ukraine", "380", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("United Arab Emirates", "971", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("United Kingdom", "44", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("United States", "1", TuyaCloudOpenAPIEndpoint.AMERICA),
    TuyaCountry("Uruguay", "598", TuyaCloudOpenAPIEndpoint.AMERICA),
    TuyaCountry("Uzbekistan", "998", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Venezuela", "58", TuyaCloudOpenAPIEndpoint.AMERICA),
    TuyaCountry("Vietnam", "84", TuyaCloudOpenAPIEndpoint.EUROPE),
    TuyaCountry("Zimbabwe", "263", TuyaCloudOpenAPIEndpoint.EUROPE),
]

from .tuya_ble import SERVICE_UUID, TuyaBLEDeviceCredentials

from .const import (
    DOMAIN,
)
from .devices import TuyaBLEData, get_device_readable_name
from .cloud import HASSTuyaBLEDeviceManager

_LOGGER = logging.getLogger(__name__)


async def _try_login(
    manager: HASSTuyaBLEDeviceManager,
    user_input: dict[str, Any],
    errors: dict[str, str],
    placeholders: dict[str, Any],
) -> dict[str, Any] | None:
    response: dict[Any, Any] | None
    data: dict[str, Any]

    country = [
        country
        for country in TUYA_COUNTRIES
        if country.name == user_input[CONF_COUNTRY_CODE]
    ][0]

    data = {
        CONF_ENDPOINT: country.endpoint,
        CONF_AUTH_TYPE: AuthType.CUSTOM,
        CONF_ACCESS_ID: user_input[CONF_ACCESS_ID],
        CONF_ACCESS_SECRET: user_input[CONF_ACCESS_SECRET],
        CONF_USERNAME: user_input[CONF_USERNAME],
        CONF_PASSWORD: user_input[CONF_PASSWORD],
        CONF_COUNTRY_CODE: country.country_code,
    }

    for app_type in (TUYA_SMART_APP, SMARTLIFE_APP, ""):
        data[CONF_APP_TYPE] = app_type
        if app_type == "":
            data[CONF_AUTH_TYPE] = AuthType.CUSTOM
        else:
            data[CONF_AUTH_TYPE] = AuthType.SMART_HOME

        response = await manager._login(data, True)

        if response.get(TUYA_RESPONSE_SUCCESS, False):
            return data

    errors["base"] = "login_error"
    if response:
        placeholders.update(
            {
                TUYA_RESPONSE_CODE: response.get(TUYA_RESPONSE_CODE),
                TUYA_RESPONSE_MSG: response.get(TUYA_RESPONSE_MSG),
            }
        )

    return None


def _show_login_form(
    flow: FlowHandler,
    user_input: dict[str, Any],
    errors: dict[str, str],
    placeholders: dict[str, Any],
) -> FlowResult:
    """Shows the Tuya IOT platform login form."""
    if user_input is not None and user_input.get(CONF_COUNTRY_CODE) is not None:
        for country in TUYA_COUNTRIES:
            if country.country_code == user_input[CONF_COUNTRY_CODE]:
                user_input[CONF_COUNTRY_CODE] = country.name
                break

    def_country_name: str | None = None
    try:
        def_country = pycountry.countries.get(alpha_2=flow.hass.config.country)
        if def_country:
            def_country_name = def_country.name
    except:
        pass

    return flow.async_show_form(
        step_id="login",
        data_schema=vol.Schema(
            {
                vol.Required(
                    CONF_COUNTRY_CODE,
                    default=user_input.get(CONF_COUNTRY_CODE, def_country_name),
                ): vol.In(
                    # We don't pass a dict {code:name} because country codes can be duplicate.
                    [country.name for country in TUYA_COUNTRIES]
                ),
                vol.Required(
                    CONF_ACCESS_ID, default=user_input.get(CONF_ACCESS_ID, "")
                ): str,
                vol.Required(
                    CONF_ACCESS_SECRET,
                    default=user_input.get(CONF_ACCESS_SECRET, ""),
                ): str,
                vol.Required(
                    CONF_USERNAME, default=user_input.get(CONF_USERNAME, "")
                ): str,
                vol.Required(
                    CONF_PASSWORD, default=user_input.get(CONF_PASSWORD, "")
                ): str,
            }
        ),
        errors=errors,
        description_placeholders=placeholders,
    )


class TuyaBLEOptionsFlow(OptionsFlowWithConfigEntry):
    """Handle a Tuya BLE options flow."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        super().__init__(config_entry)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        return await self.async_step_login(user_input)

    async def async_step_login(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the Tuya IOT platform login step."""
        errors: dict[str, str] = {}
        placeholders: dict[str, Any] = {}
        credentials: TuyaBLEDeviceCredentials | None = None
        address: str | None = self.config_entry.data.get(CONF_ADDRESS)

        if user_input is not None:
            entry: TuyaBLEData | None = None
            domain_data = self.hass.data.get(DOMAIN)
            if domain_data:
                entry = domain_data.get(self.config_entry.entry_id)
            if entry:
                login_data = await _try_login(
                    entry.manager,
                    user_input,
                    errors,
                    placeholders,
                )
                if login_data:
                    credentials = await entry.manager.get_device_credentials(
                        address, True, True
                    )
                    if credentials:
                        return self.async_create_entry(
                            title=self.config_entry.title,
                            data=entry.manager.data,
                        )
                    else:
                        errors["base"] = "device_not_registered"

        if user_input is None:
            user_input = {}
            user_input.update(self.config_entry.options)

        return _show_login_form(self, user_input, errors, placeholders)


class TuyaBLEConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tuya BLE."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        super().__init__()
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}
        self._data: dict[str, Any] = {}
        self._manager: HASSTuyaBLEDeviceManager | None = None
        self._get_device_info_error = False

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
        if self._manager is None:
            self._manager = HASSTuyaBLEDeviceManager(self.hass, self._data)
        await self._manager.build_cache()
        self.context["title_placeholders"] = {
            "name": await get_device_readable_name(
                discovery_info,
                self._manager,
            )
        }
        return await self.async_step_login()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the user step."""
        if self._manager is None:
            self._manager = HASSTuyaBLEDeviceManager(self.hass, self._data)
        await self._manager.build_cache()
        return await self.async_step_login()

    async def async_step_login(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the Tuya IOT platform login step."""
        data: dict[str, Any] | None = None
        errors: dict[str, str] = {}
        placeholders: dict[str, Any] = {}

        if user_input is not None:
            data = await _try_login(
                self._manager,
                user_input,
                errors,
                placeholders,
            )
            if data:
                self._data.update(data)
                return await self.async_step_device()

        if user_input is None:
            user_input = {}
            if self._discovery_info:
                await self._manager.get_device_credentials(
                    self._discovery_info.address,
                    False,
                    True,
                )
            if self._data is None or len(self._data) == 0:
                self._manager.get_login_from_cache()
            if self._data is not None and len(self._data) > 0:
                user_input.update(self._data)

        return _show_login_form(self, user_input, errors, placeholders)

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the user step to pick discovered device."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            discovery_info = self._discovered_devices[address]
            local_name = await get_device_readable_name(discovery_info, self._manager)
            await self.async_set_unique_id(
                discovery_info.address, raise_on_progress=False
            )
            self._abort_if_unique_id_configured()
            credentials = await self._manager.get_device_credentials(
                discovery_info.address, self._get_device_info_error, True
            )
            self._data[CONF_ADDRESS] = discovery_info.address
            if credentials is None:
                self._get_device_info_error = True
                errors["base"] = "device_not_registered"
            else:
                return self.async_create_entry(
                    title=local_name,
                    data={CONF_ADDRESS: discovery_info.address},
                    options=self._data,
                )

        if discovery := self._discovery_info:
            self._discovered_devices[discovery.address] = discovery
        else:
            current_addresses = self._async_current_ids()
            for discovery in async_discovered_service_info(self.hass):
                is_tuya_uuid = (
                    discovery.service_data is not None
                    and SERVICE_UUID in discovery.service_data.keys()
                )
                is_tuya_name = (
                    discovery.name is not None
                    and discovery.name.startswith("TY")
                )
                if (
                    discovery.address in current_addresses
                    or discovery.address in self._discovered_devices
                    or (not is_tuya_uuid and not is_tuya_name)
                ):
                    continue
                self._discovered_devices[discovery.address] = discovery

        if not self._discovered_devices:
            return self.async_abort(reason="no_unconfigured_devices")

        def_address: str
        if user_input:
            def_address = user_input.get(CONF_ADDRESS)
        else:
            def_address = list(self._discovered_devices)[0]

        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ADDRESS,
                        default=def_address,
                    ): vol.In(
                        {
                            service_info.address: await get_device_readable_name(
                                service_info,
                                self._manager,
                            )
                            for service_info in self._discovered_devices.values()
                        }
                    ),
                },
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> TuyaBLEOptionsFlow:
        """Get the options flow for this handler."""
        return TuyaBLEOptionsFlow(config_entry)
