"""Погода через open-meteo.com (бесплатно, без ключа).

Инструмент:
- ``WeatherTool`` — текущая погода и прогноз.

Геолокация: если location не задан, пытаемся определить по IP (ipapi.co, тоже без ключа)
или берём дефолт из настроек.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from datetime import datetime

import requests

from config.settings import Settings
from core.actions.base import ActionResult, Tool, ToolContext
from core.actions.registry import DEFAULT_REGISTRY
from core.network_guard import safe_http_get
from core.utils.logger import get_logger

__all__ = ["WeatherTool", "get_weather", "geocode_location", "get_ip_location"]

log = get_logger(__name__)

#: Open-Meteo API
_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

#: ipapi.co для геолокации по IP
_IP_API_URL = "https://ipapi.co/json/"

#: Таймаут запросов
_REQUEST_TIMEOUT = 10

#: Коды погоды open-meteo (WMO)
_WEATHER_CODES = {
    0: "ясно",
    1: "преимущественно ясно",
    2: "частично облачно",
    3: "пасмурно",
    45: "туман",
    48: "изморозь",
    51: "лёгкая морось",
    53: "умеренная морось",
    55: "сильная морось",
    56: "лёгкий замерзающий морось",
    57: "сильный замерзающий морось",
    61: "лёгкий дождь",
    63: "умеренный дождь",
    65: "сильный дождь",
    66: "лёгкий замерзающий дождь",
    67: "сильный замерзающий дождь",
    71: "лёгкий снег",
    73: "умеренный снег",
    75: "сильный снег",
    77: "снежные зерна",
    80: "лёгкие ливни",
    81: "умеренные ливни",
    82: "сильные ливни",
    85: "лёгкие снежные ливни",
    86: "сильные снежные ливни",
    95: "гроза",
    96: "гроза с градом",
    99: "сильная гроза с градом",
}


def get_ip_location() -> Optional[Dict[str, float]]:
    """Определяет координаты по IP через ipapi.co.

    Returns:
        {"lat": ..., "lon": ..., "city": ..., "country": ...} или None.
    """
    try:
        resp = safe_http_get(_IP_API_URL, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if "latitude" in data and "longitude" in data:
            return {
                "lat": float(data["latitude"]),
                "lon": float(data["longitude"]),
                "city": data.get("city", ""),
                "country": data.get("country_name", ""),
            }
    except Exception as exc:
        log.debug("Не удалось определить местоположение по IP: %s", exc)
    return None


def geocode_location(location: str) -> Optional[Dict[str, Any]]:
    """Геокодирует название места через Open-Meteo geocoding API.

    Args:
        location: название города/места (например, "Москва", "London").

    Returns:
        {"lat": ..., "lon": ..., "name": ..., "country": ...} или None.
    """
    try:
        # Open-Meteo geocoding API
        url = "https://geocoding-api.open-meteo.com/v1/search"
        params = {"name": location, "count": 1, "language": "ru", "format": "json"}
        resp = safe_http_get(url, params=params, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results")
        if results:
            r = results[0]
            return {
                "lat": r["latitude"],
                "lon": r["longitude"],
                "name": r.get("name", location),
                "country": r.get("country", ""),
                "admin1": r.get("admin1", ""),
            }
    except Exception as exc:
        log.debug("Геокодирование '%s' не удалось: %s", location, exc)
    return None


def get_weather(
    location: Optional[str] = None,
    settings: Optional[Settings] = None,
    forecast_days: int = 1,
) -> Dict[str, Any]:
    """Получает текущую погоду и прогноз.

    Args:
        location: название города (например, "Москва"). None = авто по IP или дефолт.
        settings: конфигурация (для дефолтного местоположения).
        forecast_days: дней прогноза (1-7).

    Returns:
        Словарь с погодой:
        {
            "location": {"name": ..., "lat": ..., "lon": ...},
            "current": {"temp": ..., "weather": ..., "wind": ..., "humidity": ...},
            "forecast": [{"date": ..., "temp_min": ..., "temp_max": ..., "weather": ...}, ...]
        }
        При ошибке: {"error": "..."}
    """
    # 1. Определяем координаты
    coords: Optional[Dict[str, Any]] = None

    if location:
        coords = geocode_location(location)
    elif settings is not None:
        # Пробуем дефолт из настроек
        default_loc = getattr(getattr(settings, "weather", None), "default_location", None)
        if default_loc:
            coords = geocode_location(default_loc)

    if coords is None:
        # Фоллбэк: IP
        coords = get_ip_location()
        if coords:
            coords["name"] = coords.get("city", "ваше местоположение")

    if coords is None:
        return {"error": "Не удалось определить местоположение (нет IP/геокодинга)"}

    lat = coords["lat"]
    lon = coords["lon"]
    name = coords.get("name", f"{lat:.2f}, {lon:.2f}")

    # 2. Запрос погоды
    params = {
        "latitude": lat,
        "longitude": lon,
        "current_weather": "true",
        "daily": "weathercode,temperature_2m_max,temperature_2m_min",
        "timezone": "auto",
        "forecast_days": min(max(1, forecast_days), 7),
    }

    try:
        resp = safe_http_get(_OPEN_METEO_URL, params=params, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.error("Ошибка запроса погоды: %s", exc)
        return {"error": f"Ошибка получения погоды: {exc}"}

    # 3. Парсинг
    current = data.get("current_weather", {})
    daily = data.get("daily", {})

    weather_code = current.get("weathercode", 0)
    weather_desc = _WEATHER_CODES.get(weather_code, f"код {weather_code}")

    result = {
        "location": {"name": name, "lat": lat, "lon": lon},
        "current": {
            "temp": current.get("temperature"),
            "weather": weather_desc,
            "weather_code": weather_code,
            "wind": current.get("windspeed"),
            "wind_dir": current.get("winddirection"),
            "time": current.get("time"),
        },
        "forecast": [],
    }

    if daily:
        dates = daily.get("time", [])
        tmax = daily.get("temperature_2m_max", [])
        tmin = daily.get("temperature_2m_min", [])
        codes = daily.get("weathercode", [])
        for i, date in enumerate(dates):
            code = codes[i] if i < len(codes) else 0
            result["forecast"].append(
                {
                    "date": date,
                    "temp_max": tmax[i] if i < len(tmax) else None,
                    "temp_min": tmin[i] if i < len(tmin) else None,
                    "weather": _WEATHER_CODES.get(code, f"код {code}"),
                    "weather_code": code,
                }
            )

    return result


# --------------------------------------------------------------------------- #
# Tool-обёртка
# --------------------------------------------------------------------------- #


class WeatherTool(Tool):
    """Инструмент: текущая погода и прогноз."""

    @property
    def name(self) -> str:
        return "weather"

    @property
    def description(self) -> str:
        return (
            "Возвращает текущую погоду и прогноз на несколько дней через open-meteo.com "
            "(бесплатно, без API-ключа). Местоположение: по названию города, по IP или "
            "дефолт из настроек."
        )

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "Название города/места (например, 'Москва', 'London'). Пусто = авто.",
                    "default": "",
                },
                "forecast_days": {
                    "type": "integer",
                    "description": "Дней прогноза (1-7).",
                    "default": 1,
                    "minimum": 1,
                    "maximum": 7,
                },
            },
            "additionalProperties": False,
        }

    def run(self, args: Dict[str, Any], context: ToolContext) -> ActionResult:
        location = args.get("location", "").strip() or None
        forecast_days = args.get("forecast_days", 1)

        weather = get_weather(location, context.settings, forecast_days)

        if "error" in weather:
            return ActionResult(
                tool=self.name,
                args=args,
                ok=False,
                error=weather["error"],
            )

        loc = weather["location"]
        cur = weather["current"]
        fc = weather["forecast"]

        lines = [
            f"🌤 Погода в {loc['name']} ({loc['lat']:.2f}, {loc['lon']:.2f}):",
            f"  Сейчас: {cur['temp']}°C, {cur['weather']}, ветер {cur['wind']} км/ч",
        ]
        if cur.get("humidity") is not None:
            lines.append(f"  Влажность: {cur['humidity']}%")

        if fc:
            lines.append("  Прогноз:")
            for day in fc:
                lines.append(
                    f"    {day['date']}: {day['temp_min']}…{day['temp_max']}°C, {day['weather']}"
                )

        text_summary = "\n".join(lines)
        now_str = datetime.now().strftime("%H:%M")
        output_data = {
            "summary": text_summary,
            "text": text_summary,
            "value": cur.get("temp"),
            "unit": "°C",
            "subject": f"погода в {loc.get('name', 'городе')}",
            "source": "open-meteo.com",
            "fetched_at": now_str,
            "location": loc,
            "current": cur,
            "forecast": fc,
        }

        return ActionResult(
            tool=self.name,
            args=args,
            ok=True,
            output=output_data,
        )


# Авто-регистрация
DEFAULT_REGISTRY.register(WeatherTool())