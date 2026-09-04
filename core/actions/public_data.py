"""Честно бесплатные публичные API (без ключа) для живого Джарвиса.

Набор источников, которые работают БЕЗ api-ключей и без подписок — то, о
чём говорил пользователь («погода, новости и что-то такое»):

    * Новости по запросу — через Google News RSS (свободный, без ключа).
    * Краткая справка/факт — Wikipedia (REST + русская выжимка).
    * Курсы валют — open.er-api.com (бесплатно, без ключа).
    * Геолокация по IP — ipapi.co (как уже в weather.py).

Инструменты регистрируются как обычные ``Tool`` (name/description/
input_schema/run) в ``DEFAULT_REGISTRY`` — тот же паттерн, что weather.py.

Сетевые сбои НЕ роняют процесс: возвращают ActionResult(ok=False) с честной
ошибкой (фикс A3 — никаких canned-«сохранено для повторной попытки»).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from datetime import datetime

import requests

from core.actions.base import ActionResult, Tool, ToolContext
from core.actions.registry import DEFAULT_REGISTRY
from core.utils.logger import get_logger

__all__ = [
    "PublicDataTool",
    "news_search",
    "wiki_summary",
    "currency_rates",
    "cbr_currency_rates",
]

log = get_logger(__name__)

_TIMEOUT = 10
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Jarvis/2.4"


# --------------------------------------------------------------------------- #
#  Источники (без ключей)
# --------------------------------------------------------------------------- #


from core.data.public_sources import cbr_currency_rates, currency_rates, news_search, wiki_summary


# --------------------------------------------------------------------------- #
#  Tool
# --------------------------------------------------------------------------- #


class PublicDataTool(Tool):
    """Инструмент: быстрые факты из бесплатных публичных источников."""

    @property
    def name(self) -> str:
        return "public_data"

    @property
    def description(self) -> str:
        return (
            "Быстрые факты из бесплатных публичных источников (без api-ключа): "
            "новости (news_search), справка (wiki), курсы валют (currency). "
            "usage: {'kind': 'news'|'wiki'|'currency', ...}."
        )

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["news", "wiki", "currency"],
                         "description": "Тип запроса"},
                "query": {"type": "string", "description": "Для news/wiki — тема"},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["kind"],
            "additionalProperties": False,
        }

    def run(self, args: Dict[str, Any], context: ToolContext) -> ActionResult:
        kind = str(args.get("kind") or "").lower()
        query = str(args.get("query") or "").strip()
        max_results = max(1, min(int(args.get("max_results", 5)), 10))

        try:
            if kind == "news":
                return self._news(query, max_results)
            if kind == "wiki":
                return self._wiki(query)
            if kind == "currency":
                return self._currency(query)
        except Exception as exc:  # noqa: BLE001
            return ActionResult(self.name, args, False, error=f"public_data: {exc}")
        return ActionResult(self.name, args, False, error=f"unknown kind: {kind}")

    @staticmethod
    def _news(query: str, max_results: int) -> ActionResult:
        if not query:
            return ActionResult("public_data", {"kind": "news"}, False,
                                error="query обязателен для news")
        items = news_search(query, max_results)
        if not items:
            return ActionResult("public_data", {"kind": "news", "query": query},
                                False, error="новости недоступны (сеть/пусто)")
        lines = [f"- {it['title']}\n  {it['url']}" for it in items]
        return ActionResult("public_data", {"kind": "news", "query": query}, True,
                            {"items": items, "text": "\n".join(lines)})

    @staticmethod
    def _wiki(query: str) -> ActionResult:
        if not query:
            return ActionResult("public_data", {"kind": "wiki"}, False,
                                error="query обязателен для wiki")
        text = wiki_summary(query)
        if not text:
            return ActionResult("public_data", {"kind": "wiki", "query": query},
                                False, error="статья не найдена/сеть")
        return ActionResult("public_data", {"kind": "wiki", "query": query}, True,
                            {"text": text})

    @staticmethod
    def _currency(query: str = "") -> ActionResult:
        q = (query or "").lower()
        if "eur" in q or "евро" in q:
            target, name = "EUR", "Евро"
        elif "cny" in q or "юан" in q:
            target, name = "CNY", "Юань"
        elif "kzt" in q or "тенге" in q:
            target, name = "KZT", "Тенге"
        else:
            target, name = "USD", "Доллар"

        # Первичный источник для рублёвых пар — ЦБ РФ (cbr-xml-daily.ru)
        cbr_data = cbr_currency_rates()
        if cbr_data and cbr_data.get("rates"):
            cbr_rates = cbr_data["rates"]
            source = "cbr.ru"
            fetched_at = str(cbr_data.get("date") or datetime.now().strftime("%Y-%m-%d %H:%M"))
            target_entry = cbr_rates.get(target) or cbr_rates.get("USD")
            val = round(float(target_entry["rate"]), 2) if target_entry else None

            rates_pick = {}
            for k in ("USD", "EUR", "KZT", "CNY"):
                if k in cbr_rates and cbr_rates[k]["rate"] > 0:
                    rates_pick[k] = round(1.0 / cbr_rates[k]["rate"], 6)

            summary_parts = []
            if val is not None:
                summary_parts.append(f"Курс {name} ({target}): {val:.2f} ₽")
            else:
                summary_parts.append(f"Курс валют ({target}): данные получены")

            other_items = []
            for code, c_name in (("USD", "USD"), ("EUR", "EUR"), ("CNY", "CNY")):
                if code != target and code in cbr_rates:
                    other_items.append(f"{c_name}: {cbr_rates[code]['rate']:.2f} ₽")
            if other_items:
                summary_parts.append(f"({' | '.join(other_items)})")

            summary = f"{' '.join(summary_parts)} (источник: {source}, {fetched_at})."

            output_data = {
                "rates": rates_pick,
                "target": target,
                "value": val,
                "unit": "₽",
                "currency": target,
                "subject": f"курс {target}",
                "source": source,
                "fetched_at": fetched_at,
                "summary": summary,
            }
            return ActionResult("public_data", {"kind": "currency"}, True, output_data)

        # Резервный источник (open.er-api.com)
        rates = currency_rates("RUB")
        if not rates:
            return ActionResult("public_data", {"kind": "currency"}, False,
                                error="курсы недоступны (сеть)")
        pick = {k: rates[k] for k in ("USD", "EUR", "KZT", "CNY") if k in rates}
        if not pick:
            return ActionResult("public_data", {"kind": "currency"}, False,
                                error="курсы валют не найдены")

        val = None
        if target in pick and pick[target]:
            val = round(1.0 / pick[target], 2)
        elif "USD" in pick and pick["USD"]:
            target, name = "USD", "Доллар"
            val = round(1.0 / pick["USD"], 2)

        source = "open.er-api.com"
        fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        summary_parts = []
        if val is not None:
            summary_parts.append(f"Курс {name} ({target}): {val:.2f} ₽")
        else:
            summary_parts.append(f"Курс валют ({target}): данные получены")

        other_items = []
        for code, c_name in (("USD", "USD"), ("EUR", "EUR"), ("CNY", "CNY")):
            if code != target and code in pick and pick[code]:
                other_items.append(f"{c_name}: {round(1.0 / pick[code], 2):.2f} ₽")
        if other_items:
            summary_parts.append(f"({' | '.join(other_items)})")

        summary = f"{' '.join(summary_parts)} (источник: {source}, {fetched_at})."

        output_data = {
            "rates": pick,
            "target": target,
            "value": val,
            "unit": "₽",
            "currency": target,
            "subject": f"курс {target}",
            "source": source,
            "fetched_at": fetched_at,
            "summary": summary,
        }
        return ActionResult("public_data", {"kind": "currency"}, True, output_data)


DEFAULT_REGISTRY.register(PublicDataTool())
