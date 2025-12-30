import asyncio
import os
import ssl
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Tuple

import certifi
from aiohttp import ClientSession, TCPConnector
from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, url_for
from thinqconnect import (
    PROPERTY_READABLE,
    PROPERTY_WRITABLE,
    CooktopDevice,
    OvenDevice,
    ThinQApi,
    ThinQAPIException,
)
from thinqconnect.devices.const import Location, Property

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET") or os.urandom(16)
ENV_PATH = Path(__file__).with_name(".env")


@dataclass(frozen=True)
class ThinQConfig:
    access_token: str
    client_id: str
    country: str


@dataclass
class DeviceOption:
    device_id: str
    alias: str
    model_name: str
    device_type: str


@dataclass
class OvenSnapshot:
    devices: list[DeviceOption]
    selected: DeviceOption | None
    cook_modes: list[str]
    locations: list[str]
    selected_location: str | None
    unit: str
    status: dict[str, Any]
    temp_hint: str | None
    cooktop_zones: list[dict[str, Any]]
    raw_status: Any | None


def _get(mapping: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _extract_list(payload: Any) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("devices", "deviceList", "items", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _extract_profile(payload: Any) -> dict:
    if isinstance(payload, dict):
        for key in ("profile", "result", "modelJson", "modelJsonV2", "data"):
            value = payload.get(key)
            if isinstance(value, dict):
                return value
    if isinstance(payload, dict):
        return payload
    raise ValueError("Unexpected profile payload")


def _extract_status(payload: Any) -> dict | list:
    if isinstance(payload, dict):
        for key in ("state", "result", "data", "status"):
            value = payload.get(key)
            if value is not None:
                return value
    return payload


def load_config() -> Tuple[ThinQConfig | None, str | None]:
    load_dotenv(override=True)
    access_token = os.getenv("LG_THINQ_ACCESS_TOKEN", "").strip()
    client_id = os.getenv("LG_THINQ_CLIENT_ID", "").strip()
    country = os.getenv("LG_THINQ_COUNTRY", "US").strip() or "US"
    if not access_token or not client_id:
        return None, "Missing LG_THINQ_ACCESS_TOKEN or LG_THINQ_CLIENT_ID in your .env file."
    return ThinQConfig(access_token=access_token, client_id=client_id, country=country), None


def save_config(access_token: str, client_id: str, country: str, flask_secret: str | None = None) -> None:
    lines = [
        f"LG_THINQ_ACCESS_TOKEN={access_token.strip()}",
        f"LG_THINQ_CLIENT_ID={client_id.strip()}",
        f"LG_THINQ_COUNTRY={country.strip() or 'US'}",
    ]
    if flask_secret:
        lines.append(f"FLASK_SECRET={flask_secret.strip()}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _normalize_device_entry(entry: dict) -> dict:
    if "deviceInfo" in entry and isinstance(entry["deviceInfo"], dict):
        merged = dict(entry["deviceInfo"])
        for key in ("deviceId", "device_id", "id", "deviceID"):
            if key in entry and key not in merged:
                merged[key] = entry[key]
        entry = merged
    return entry


def _to_device_option(entry: dict) -> DeviceOption | None:
    entry = _normalize_device_entry(entry)
    device_id = _get(entry, "deviceId", "device_id", "id", "deviceID")
    device_type = _get(entry, "deviceType", "device_type", "type")
    if not device_id or not device_type:
        return None
    model_name = _get(entry, "modelName", "model_name", default="")
    alias = _get(entry, "alias", "name", default=model_name) or model_name or device_id
    return DeviceOption(
        device_id=str(device_id),
        alias=str(alias),
        model_name=str(model_name),
        device_type=str(device_type),
    )


def _pick_device(devices: list[DeviceOption], device_id: str | None) -> DeviceOption | None:
    if not devices:
        return None
    if device_id:
        for device in devices:
            if device.device_id == device_id:
                return device
    return devices[0]


def _get_location_enum(location_name: str | None) -> Location | None:
    if not location_name:
        return None
    key = location_name.upper()
    if key in Location.__members__:
        return Location.__members__[key]
    return None


def _pick_subdevice(device: OvenDevice, location: str | None):
    location_enum = _get_location_enum(location)
    if location_enum:
        sub = device.get_sub_device(location_enum)
        if sub:
            return sub, location_enum
    for fallback in (Location.OVEN, Location.UPPER, Location.LOWER):
        sub = device.get_sub_device(fallback)
        if sub:
            return sub, fallback
    if device._sub_devices:
        location_key = next(iter(device._sub_devices.keys()))
        return device._sub_devices[location_key], location_key
    return None, None


def _cook_modes(subdevice) -> list[str]:
    prop = subdevice.profiles.get_property(Property.COOK_MODE)
    modes = prop.get(PROPERTY_WRITABLE) or prop.get(PROPERTY_READABLE) or []
    return [str(mode) for mode in modes if mode]


def _temp_hint(subdevice, unit: str) -> str | None:
    prop = subdevice.profiles.get_property(
        Property.TARGET_TEMPERATURE_C if unit.upper() == "C" else Property.TARGET_TEMPERATURE_F
    )
    values = prop.get(PROPERTY_WRITABLE) or []
    if isinstance(values, dict):
        min_temp = values.get("min")
        max_temp = values.get("max")
        if min_temp is not None and max_temp is not None:
            return f"{min_temp}-{max_temp}{unit.upper()}"
    if isinstance(values, Iterable) and values:
        return ", ".join(str(v) for v in values)
    return None


def run_async(coro):
    return asyncio.run(coro)


def _device_label(device: DeviceOption) -> str:
    model = f" — {device.model_name}" if device.model_name else ""
    return f"{device.alias}{model} ({device.device_type.replace('DEVICE_', '')})"


def _is_oven(device: DeviceOption) -> bool:
    return "OVEN" in device.device_type.upper()


def _is_cooktop(device: DeviceOption) -> bool:
    return "COOKTOP" in device.device_type.upper()


def _cooktop_zone_status(cooktop: CooktopDevice) -> list[dict[str, Any]]:
    zones = []
    for location, sub in cooktop._sub_devices.items():
        zones.append(
            {
                "location": str(location),
                "state": sub.get_status(Property.CURRENT_STATE),
                "power": sub.get_status(Property.POWER_LEVEL),
                "remote_enabled": sub.get_status(Property.REMOTE_CONTROL_ENABLED),
                "timer": {
                    "hour": sub.get_status(Property.REMAIN_HOUR),
                    "minute": sub.get_status(Property.REMAIN_MINUTE),
                },
            }
        )
    return zones


async def async_get_snapshot(cfg: ThinQConfig, device_id: str | None, location: str | None) -> OvenSnapshot:
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    connector = TCPConnector(ssl=ssl_context)
    async with ClientSession(connector=connector) as session:
        api = ThinQApi(
            session=session,
            access_token=cfg.access_token,
            country_code=cfg.country,
            client_id=cfg.client_id,
        )
        devices_payload = await api.async_get_device_list()
        devices = _extract_list(devices_payload)
        options = [opt for entry in devices if (opt := _to_device_option(entry))]
        selected = _pick_device(options, device_id)
        if not selected:
            return OvenSnapshot(
                devices=options,
                selected=None,
                cook_modes=[],
                locations=[],
                selected_location=None,
                unit="F",
                status={},
                temp_hint=None,
                cooktop_zones=[],
                raw_status=None,
            )

        profile_payload = await api.async_get_device_profile(selected.device_id)
        profile = _extract_profile(profile_payload)
        status_payload = await api.async_get_device_status(selected.device_id)
        status = _extract_status(status_payload)

        if _is_oven(selected):
            device = OvenDevice(
                thinq_api=api,
                device_id=selected.device_id,
                device_type=selected.device_type,
                model_name=selected.model_name,
                alias=selected.alias,
                reportable=True,
                group_id="",
                profile=profile,
                profiles=None,
            )
            if status:
                device.set_status(status)

            subdevice, picked_location = _pick_subdevice(device, location)
            if not subdevice:
                return OvenSnapshot(
                    devices=options,
                    selected=selected,
                    cook_modes=[],
                    locations=[],
                    selected_location=None,
                    unit="F",
                    status={},
                    temp_hint=None,
                    cooktop_zones=[],
                    raw_status=status,
                )

            unit = subdevice.get_status(Property.TEMPERATURE_UNIT) or "F"
            locations = [str(loc) for loc in device._sub_devices.keys()]
            status_view = {
                "operation": subdevice.get_status(Property.OVEN_OPERATION_MODE),
                "cook_mode": subdevice.get_status(Property.COOK_MODE),
                "state": subdevice.get_status(Property.CURRENT_STATE),
                "target_f": subdevice.get_status(Property.TARGET_TEMPERATURE_F),
                "target_c": subdevice.get_status(Property.TARGET_TEMPERATURE_C),
                "remote_enabled": subdevice.get_status(Property.REMOTE_CONTROL_ENABLED),
                "location": str(picked_location) if picked_location else None,
            }
            return OvenSnapshot(
                devices=options,
                selected=selected,
                cook_modes=_cook_modes(subdevice),
                locations=locations,
                selected_location=str(picked_location) if picked_location else None,
                unit=str(unit),
                status=status_view,
                temp_hint=_temp_hint(subdevice, str(unit)),
                cooktop_zones=[],
                raw_status=status,
            )

        if _is_cooktop(selected):
            device = CooktopDevice(
                thinq_api=api,
                device_id=selected.device_id,
                device_type=selected.device_type,
                model_name=selected.model_name,
                alias=selected.alias,
                reportable=True,
                group_id="",
                profile=profile,
                profiles=None,
            )
            if status:
                device.set_status(status)
            status_view = {
                "operation": device.get_status(Property.OPERATION_MODE),
                "remote_enabled": device.get_status(Property.REMOTE_CONTROL_ENABLED),
            }
            return OvenSnapshot(
                devices=options,
                selected=selected,
                cook_modes=[],
                locations=[],
                selected_location=None,
                unit="F",
                status=status_view,
                temp_hint=None,
                cooktop_zones=_cooktop_zone_status(device),
                raw_status=status,
            )

        return OvenSnapshot(
            devices=options,
            selected=selected,
            cook_modes=[],
            locations=[],
            selected_location=None,
            unit="F",
            status={},
            temp_hint=None,
            cooktop_zones=[],
            raw_status=status,
        )


async def async_preheat(
    cfg: ThinQConfig,
    device_id: str,
    mode: str,
    temp: int,
    unit: str,
    location: str | None,
    refresh: bool = True,
) -> None:
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    connector = TCPConnector(ssl=ssl_context)
    async with ClientSession(connector=connector) as session:
        api = ThinQApi(
            session=session,
            access_token=cfg.access_token,
            country_code=cfg.country,
            client_id=cfg.client_id,
        )
        devices_payload = await api.async_get_device_list()
        devices = _extract_list(devices_payload)
        options = [opt for entry in devices if (opt := _to_device_option(entry))]
        selected = _pick_device(options, device_id)
        if not selected:
            raise ValueError("No oven device found for the provided device_id.")
        if not _is_oven(selected):
            raise ValueError("Selected device is not an oven.")

        profile_payload = await api.async_get_device_profile(selected.device_id)
        profile = _extract_profile(profile_payload)
        status_payload = await api.async_get_device_status(selected.device_id) if refresh else None
        status = _extract_status(status_payload) if status_payload else None

        device = OvenDevice(
            thinq_api=api,
            device_id=selected.device_id,
            device_type=selected.device_type,
            model_name=selected.model_name,
            alias=selected.alias,
            reportable=True,
            group_id="",
            profile=profile,
            profiles=None,
        )
        if status:
            device.set_status(status)
        subdevice, _location = _pick_subdevice(device, location)
        if not subdevice:
            raise ValueError("No oven sub-device found for this model.")
        if refresh:
            remote_enabled = subdevice.get_status(Property.REMOTE_CONTROL_ENABLED)
            if remote_enabled is False:
                loc_label = str(_location) if _location else "selected location"
                raise ValueError(f"Remote control is OFF for {loc_label}. Enable remote and try again.")

        unit = unit.upper()
        if unit == "C":
            await subdevice.set_cook_mode_with_temperature_c(mode, temp)
        else:
            await subdevice.set_cook_mode_with_temperature_f(mode, temp)


async def async_oven_action(cfg: ThinQConfig, device_id: str, location: str | None, action: str) -> None:
    async with ClientSession(
        connector=TCPConnector(ssl=ssl.create_default_context(cafile=certifi.where()))
    ) as session:
        api = ThinQApi(
            session=session,
            access_token=cfg.access_token,
            country_code=cfg.country,
            client_id=cfg.client_id,
        )
        devices_payload = await api.async_get_device_list()
        devices = _extract_list(devices_payload)
        options = [opt for entry in devices if (opt := _to_device_option(entry))]
        selected = _pick_device(options, device_id)
        if not selected or not _is_oven(selected):
            raise ValueError("Selected device is not an oven.")

        profile_payload = await api.async_get_device_profile(selected.device_id)
        profile = _extract_profile(profile_payload)
        device = OvenDevice(
            thinq_api=api,
            device_id=selected.device_id,
            device_type=selected.device_type,
            model_name=selected.model_name,
            alias=selected.alias,
            reportable=True,
            group_id="",
            profile=profile,
            profiles=None,
        )
        subdevice, _location = _pick_subdevice(device, location)
        if not subdevice:
            raise ValueError("No oven sub-device found for this model.")

        if action == "start":
            await subdevice.set_oven_operation_mode("START")
        elif action == "stop":
            await subdevice.set_oven_operation_mode("STOP")
        elif action == "remote_on":
            await subdevice.do_attribute_command(Property.REMOTE_CONTROL_ENABLED, True)
        elif action == "remote_off":
            await subdevice.do_attribute_command(Property.REMOTE_CONTROL_ENABLED, False)
        else:
            raise ValueError(f"Unknown action: {action}")


@app.get("/")
def index():
    cfg, error = load_config()
    if error:
        suggested_client_id = str(uuid.uuid4())
        return render_template(
            "index.html",
            config_error=error,
            snapshot=None,
            suggested_client_id=suggested_client_id,
        )

    device_id = request.args.get("device_id")
    location = request.args.get("location")
    try:
        snapshot = run_async(async_get_snapshot(cfg, device_id, location))
    except ThinQAPIException as exc:
        flash(str(exc), "error")
        snapshot = OvenSnapshot(
            devices=[],
            selected=None,
            cook_modes=[],
            locations=[],
            selected_location=None,
            unit="F",
            status={},
            temp_hint=None,
            cooktop_zones=[],
            raw_status=None,
        )
    except Exception as exc:
        flash(f"Unexpected error: {exc}", "error")
        snapshot = OvenSnapshot(
            devices=[],
            selected=None,
            cook_modes=[],
            locations=[],
            selected_location=None,
            unit="F",
            status={},
            temp_hint=None,
            cooktop_zones=[],
            raw_status=None,
        )

    return render_template("index.html", config_error=None, snapshot=snapshot)


@app.post("/save-config")
def save_config_route():
    access_token = request.form.get("access_token", "").strip()
    client_id = request.form.get("client_id", "").strip()
    country = request.form.get("country", "US").strip() or "US"
    if not access_token or not client_id:
        flash("Access token and client ID are required.", "error")
        return redirect(url_for("index"))

    save_config(access_token, client_id, country, os.getenv("FLASK_SECRET"))
    flash("Saved configuration to .env.", "success")
    return redirect(url_for("index"))


@app.post("/preheat")
def preheat():
    cfg, error = load_config()
    if error:
        flash(error, "error")
        return redirect(url_for("index"))

    device_id = request.form.get("device_id", "")
    mode = request.form.get("cook_mode", "")
    unit = request.form.get("unit", "F")
    temp_raw = request.form.get("temperature", "")
    location = request.form.get("location", None)
    action = request.form.get("action", "preheat")
    location_override = request.form.get("location_override", None)
    if location_override:
        location = location_override

    try:
        temp = int(temp_raw)
        refresh = action in {"refresh_preheat", "test_upper", "test_lower"}
        run_async(async_preheat(cfg, device_id, mode, temp, unit, location, refresh=refresh))
        flash("Refreshed and sent preheat." if refresh else "Preheat command sent.", "success")
    except ThinQAPIException as exc:
        flash(str(exc), "error")
    except Exception as exc:
        flash(f"Preheat failed: {exc}", "error")

    return redirect(url_for("index", device_id=device_id, location=location))


@app.post("/oven-action")
def oven_action():
    cfg, error = load_config()
    if error:
        flash(error, "error")
        return redirect(url_for("index"))

    device_id = request.form.get("device_id", "")
    location = request.form.get("location", None)
    action = request.form.get("action", "")

    try:
        run_async(async_oven_action(cfg, device_id, location, action))
        flash("Oven command sent.", "success")
    except ThinQAPIException as exc:
        flash(str(exc), "error")
    except Exception as exc:
        flash(f"Oven command failed: {exc}", "error")

    return redirect(url_for("index", device_id=device_id, location=location))


@app.post("/refresh")
def refresh():
    device_id = request.form.get("device_id", "")
    location = request.form.get("location", None)
    return redirect(url_for("index", device_id=device_id, location=location))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5055, debug=False)
