"""Веб-поиск через DuckDuckGo HTML (без API-ключа).

Инструмент:
- ``WebSearchTool`` — поиск через html.duckduckgo.com/html/, парсинг через BeautifulSoup.

Возвращает 3-5 результатов с заголовком, ссылкой и сниппетом.
Сетевые ошибки не роняют процесс — возвращают ok=False.
"""

from __future__ import annotations

import html
import re
import urllib.parse
from typing import Any, Dict, List

import requests
from bs4 import BeautifulSoup

from config.settings import Settings
from core.actions.base import ActionResult, Tool, ToolContext
from core.actions.registry import DEFAULT_REGISTRY
from core.network_guard import SSRFBlocked, safe_http_post
from core.utils.logger import get_logger

__all__ = ["WebSearchTool", "duckduckgo_search"]

log = get_logger(__name__)

#: Базовый URL DuckDuckGo HTML
_DDG_HTML_URL = "https://html.duckduckgo.com/html/"

#: User-Agent для запросов
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

#: Таймаут запроса (секунды)
_REQUEST_TIMEOUT = 10


def duckduckgo_search(query: str, max_results: int = 5, timeout: float | None = None) -> List[Dict[str, str]]:
    """Выполняет поиск через DuckDuckGo HTML.

    Args:
        query: поисковый запрос.
        max_results: максимум результатов (1-10).

    Returns:
        Список словарей: {"title": ..., "url": ..., "snippet": ...}.
        Пустой список при ошибке.
    """
    if not query or not query.strip():
        return []

    params = {"q": query.strip(), "kl": "ru-ru"}  # русский регион
    headers = {"User-Agent": _USER_AGENT}

    try:
        resp = safe_http_post(
            _DDG_HTML_URL,
            data=params,
            headers=headers,
            timeout=float(timeout or _REQUEST_TIMEOUT),
        )
        resp.raise_for_status()
    except (requests.RequestException, SSRFBlocked) as exc:
        log.error("Ошибка запроса к DuckDuckGo: %s", exc)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results: List[Dict[str, str]] = []

    # Результаты в .result__snippet (старая верстка) или .result__body (новая)
    for result_div in soup.select(".result, .result__body, .web-result"):
        if len(results) >= max_results:
            break

        # Заголовок и ссылка
        title_elem = result_div.select_one(".result__title, .result__snippet a, h2 a, .web-result-title a")
        url_elem = result_div.select_one(".result__url, .result__snippet a, .web-result-url")
        snippet_elem = result_div.select_one(".result__snippet, .web-result-snippet")

        title = ""
        url = ""
        snippet = ""

        if title_elem:
            title = html.unescape(title_elem.get_text(strip=True))
            if title_elem.has_attr("href"):
                url = title_elem["href"]

        if url_elem and not url:
            url = html.unescape(url_elem.get_text(strip=True))
            # DuckDuckGo иногда даёт редирект-ссылки
            if url.startswith("//duckduckgo.com/l/?"):
                # Пытаемся извлечь настоящий URL из параметра uddg
                match = re.search(r"uddg=([^&]+)", url)
                if match:
                    url = urllib.parse.unquote(match.group(1))

        if snippet_elem:
            snippet = html.unescape(snippet_elem.get_text(strip=True))

        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet})

    # Фоллбэк: общий парсинг всех ссылок в результатах
    if not results:
        for link in soup.select("a.result__snippet, a.result__url, .web-result a"):
            if len(results) >= max_results:
                break
            title = html.unescape(link.get_text(strip=True))
            url = link.get("href", "")
            if title and url and not url.startswith("javascript:"):
                results.append({"title": title, "url": url, "snippet": ""})

    log.debug("DuckDuckGo поиск '%s' -> %d результатов", query, len(results))
    return results


# --------------------------------------------------------------------------- #
# Tool-обёртка
# --------------------------------------------------------------------------- #


class WebSearchTool(Tool):
    """Инструмент: веб-поиск через DuckDuckGo."""

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "Выполняет поиск в интернете через DuckDuckGo (HTML, без API-ключа). "
            "Возвращает до 5 результатов с заголовками, ссылками и сниппетами. "
            "Полезно для актуальной информации, новостей, фактов."
        )

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Поисковый запрос на естественном языке.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Максимум результатов (1-10).",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    def run(self, args: Dict[str, Any], context: ToolContext) -> ActionResult:
        query = args["query"]
        max_results = args.get("max_results", 5)

        budget = getattr(getattr(context, "settings", None), "latency_budgets", None)
        timeout = float(getattr(budget, "research_source_timeout_ms", 8000.0)) / 1000.0
        results = duckduckgo_search(query, max_results, timeout=timeout)

        if not results:
            return ActionResult(
                tool=self.name,
                args=args,
                ok=False,
                error="Поиск не дал результатов или произошла сетевая ошибка",
            )

        # Формируем читаемый вывод для пользователя
        # §22 — сниппеты из поиска это внешние ДАННЫЕ, не инструкции.
        # Оборачиваем каждый сниппет явным конвертом на границе →модель.
        from core.safety import wrap_untrusted
        lines = [f"Результаты поиска по «{query}»:"]
        for i, r in enumerate(results, 1):
            snippet = wrap_untrusted(
                r["snippet"][:200], source=f"web_search ({r.get('url', '')})"
            ) if r["snippet"] else ""
            lines.append(f"  {i}. {r['title']}")
            lines.append(f"     {r['url']}")
            if snippet:
                lines.append(f"     {snippet}")
            lines.append("")

        return ActionResult(
            tool=self.name,
            args=args,
            ok=True,
            output="\n".join(lines).strip(),
        )


# Авто-регистрация
DEFAULT_REGISTRY.register(WebSearchTool())
