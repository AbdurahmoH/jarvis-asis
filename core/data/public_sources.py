"""Честно бесплатные публичные источники без API-ключей.

Это чистый слой данных: без импорта core.actions, чтобы его можно было
тестировать и использовать отдельно от реестра инструментов.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

import requests

from core.network_guard import SSRFBlocked, safe_http_get

_TIMEOUT = 10
_USER_AGENT = "Jarvis/2.4 public-data client"


def news_search(query: str, max_results: int = 5) -> List[Dict[str, str]]:
    if not (query or "").strip():
        return []
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=ru&gl=RU&ceid=RU:ru"
    try:
        response = safe_http_get(url, headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT)
        response.raise_for_status()
        text = response.text
    except (requests.RequestException, SSRFBlocked):
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    out: List[Dict[str, str]] = []
    limit = max(1, min(int(max_results), 10))
    for item in root.iter("item"):
        if len(out) >= limit:
            break
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if title and link:
            out.append({"title": title, "url": link})
    return out


def wiki_summary(topic: str, lang: str = "ru") -> Optional[str]:
    if not (topic or "").strip():
        return None
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote_plus(topic)}"
    try:
        response = safe_http_get(url, headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT)
        response.raise_for_status()
        extract = (response.json().get("extract") or "").strip()
        return extract or None
    except (requests.RequestException, SSRFBlocked, ValueError):
        return None


def currency_rates(base: str = "RUB") -> Optional[Dict[str, float]]:
    url = f"https://open.er-api.com/v6/latest/{(base or 'RUB').upper()}"
    try:
        response = safe_http_get(url, timeout=_TIMEOUT)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, SSRFBlocked, ValueError):
        return None
    if not isinstance(data, dict) or data.get("result") != "success":
        return None
    return {str(k): float(v) for k, v in (data.get("rates") or {}).items()}


def cbr_currency_rates() -> Optional[Dict[str, Any]]:
    """Курсы валют ЦБ РФ (cbr-xml-daily.ru).

    Возвращает dict с курсами к рублю (RUB за 1 единицу валюты), дату и данные по валютам.
    """
    url = "https://www.cbr-xml-daily.ru/daily_json.js"
    try:
        response = safe_http_get(url, headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, SSRFBlocked, ValueError):
        return None
    if not isinstance(data, dict) or "Valute" not in data:
        return None
    rates = {}
    valute = data.get("Valute") or {}
    for code, info in valute.items():
        if isinstance(info, dict) and "Value" in info and "Nominal" in info:
            nominal = float(info["Nominal"]) or 1.0
            val = float(info["Value"]) / nominal
            rates[str(code).upper()] = {
                "rate": round(val, 4),
                "name": str(info.get("Name") or code),
                "nominal": nominal,
                "raw_value": float(info["Value"]),
            }
    return {
        "date": data.get("Date"),
        "rates": rates,
    }
