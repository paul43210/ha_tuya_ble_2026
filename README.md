# Home Assistant support for Tuya BLE devices

## About This Fork

This is a compatibility fork of [PlusPlus-ua/ha_tuya_ble](https://github.com/PlusPlus-ua/ha_tuya_ble) maintained by [@paul43210](https://github.com/paul43210), updated to work with **Home Assistant 2025.x and 2026.x**.

### Compatibility Fixes Applied (April 2026)

The original repository became incompatible with modern Home Assistant versions due to several breaking changes. The following fixes were applied in collaboration with [Claude](https://claude.ai) (Anthropic):

| File | Fix |
|---|---|
| `manifest.json` | Changed `pycountry==22.3.5` → `pycountry>=22.3.5` to resolve dependency conflict with HA 2025+ |
| `const.py` | Replaced `from homeassistant.backports.enum import StrEnum` → `from enum import StrEnum` (module removed in HA 2024.x) |
| `config_flow.py` | Removed import of `CONF_ACCESS_ID` and other constants from `homeassistant.components.tuya.const` (removed in HA 2024.1); defined locally. Added `TuyaCountry` dataclass and full country list. Added `FlowResult` compatibility shim for HA 2025.1+ |
| `cloud.py` | Removed import of `CONF_ACCESS_ID` and other constants from `homeassistant.components.tuya.const`; defined locally |

### Use Case

This fork was tested with **MOES CTL20H SmartLock BLE cabinet locks** on a Beelink Mini S13 (Intel N150) running Home Assistant OS 2026.4, with Bluetooth 5.2 providing local BLE control — no hub, no cloud dependency after initial setup.

---

## Overview

This integration supports Tuya devices connected via BLE.

_Inspired by code of [@redphx](https://github.com/redphx/poc-tuya-ble-fingerbot)_

## Installation

Install via [HACS](https://hacs.xyz/) by adding this repository as a custom integration repository:

`https://github.com/paul43210/ha_tuya_ble_2026`

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=paul43210&repository=ha_tuya_ble_2026&category=integration)

## Usage

After adding to Home Assistant the integration should discover all supported Bluetooth devices, or you can add discoverable devices manually.

The integration works locally over BLE. Initial setup requires a one-time cloud credential fetch from the [Tuya IoT Platform](https://iot.tuya.com) to obtain device IDs and encryption keys. After setup, all communication is local Bluetooth — no internet connection required for day-to-day operation.

To obtain credentials, refer to the official Tuya integration [documentation](https://www.home-assistant.io/integrations/tuya/).

## Supported devices list

* Fingerbots (category_id 'szjqr')
  + Fingerbot (product_ids 'ltak7e1p', 'y6kttvd6', 'yrnk7mnn', 'nvr2rocq', 'bnt7wajf', 'rvdceqjh', '5xhbk964'), original device, first in category, powered by CR2 battery.
  + Adaprox Fingerbot (product_id 'y6kttvd6'), built-in battery with USB type C charging.
  + Fingerbot Plus (product_ids 'blliqpsj', 'ndvkgsrm', 'yiihr7zh', 'neq16kgd'), almost same as original, has sensor button for manual control.
  + CubeTouch 1s (product_id '3yqdo5yt'), built-in battery with USB type C charging.
  + CubeTouch II (product_id 'xhf790if'), built-in battery with USB type C charging.

  All features available in Home Assistant, programming (series of actions) is implemented for Fingerbot Plus.
  For programming exposed entities 'Program' (switch), 'Repeat forever', 'Repeats count', 'Idle position' and 'Program' (text). Format of program text is: 'position\[/time\];...' where position is in percents, optional time is in seconds (zero if missing).

* Temperature and humidity sensors (category_id 'wsdcg')
  + Soil moisture sensor (product_id 'ojzlzzsw').

* CO2 sensors (category_id 'co2bj')
  + CO2 Detector (product_id '59s19z5m').

* Smart Locks (category_id 'ms')
  + Smart Lock (product_id 'ludzroix', 'isk2p555').
  + CTL20H SmartLock cabinet/drawer lock (tested April 2026).

* Climate (category_id 'wk')
  + Thermostatic Radiator Valve (product_ids 'drlajpqc', 'nhj2j7su').

* Smart water bottle (category_id 'znhsb')
  + Smart water bottle (product_id 'cdlandip')

* Irrigation computer (category_id 'ggq')
  + Irrigation computer (product_id '6pahkcau')

## Support the original project

The original integration was written by PlusPlus-ua, who lives and works in Ukraine. If you find this integration useful, please consider supporting the original author:

<p align="center">
  <a href="https://www.buymeacoffee.com/3PaK6lXr4l"><img src="https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png" alt="Buy me an air defense"></a>
</p>
