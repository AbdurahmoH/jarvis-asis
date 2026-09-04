"""Regression: ДЫРА 4 — SSRF guard для сетевых инструментов.

web_fetch: уже защищён (assert_safe_url + safe_redirect_url + allow_redirects=False).
web_search: URL поиска захардкожен (_DDG_HTML_URL), пользователь не контролирует.
  Тест документирует текущее состояние и проверяет что web_fetch блокирует
  внутренние адреса.
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# web_fetch блокирует loopback/private адреса
# ---------------------------------------------------------------------------

def test_web_fetch_blocks_loopback():
    """web_fetch отклоняет запросы к loopback-адресам."""
    from core.actions.web_fetch import fetch_page
    from core.network_guard import SSRFBlocked

    with pytest.raises((SSRFBlocked, ValueError)):
        fetch_page("http://127.0.0.1/secret")


def test_web_fetch_blocks_private_network():
    """web_fetch отклоняет запросы к приватным сетям."""
    from core.actions.web_fetch import fetch_page
    from core.network_guard import SSRFBlocked

    with pytest.raises((SSRFBlocked, ValueError)):
        fetch_page("http://192.168.1.1/admin")


def test_web_fetch_blocks_cloud_metadata():
    """web_fetch отклоняет запросы к cloud metadata endpoint."""
    from core.actions.web_fetch import fetch_page
    from core.network_guard import SSRFBlocked

    with pytest.raises((SSRFBlocked, ValueError)):
        fetch_page("http://169.254.169.254/latest/meta-data/")


def test_web_fetch_blocks_file_scheme():
    """web_fetch отклоняет file:// схему."""
    from core.actions.web_fetch import fetch_page
    from core.network_guard import SSRFBlocked

    with pytest.raises((SSRFBlocked, ValueError)):
        fetch_page("file:///etc/passwd")


# ---------------------------------------------------------------------------
# web_search URL захардкожен — не контролируется пользователем
# ---------------------------------------------------------------------------

def test_web_search_uses_fixed_ddg_url():
    """web_search использует фиксированный URL DuckDuckGo, не пользовательский."""
    from core.actions.web_search import _DDG_HTML_URL

    assert _DDG_HTML_URL == "https://html.duckduckgo.com/html/", (
        "URL поиска изменился — проверьте что новый URL не контролируется пользователем"
    )
    # Проверяем что это публичный HTTPS URL
    from core.network_guard import is_ssrf_blocked
    assert not is_ssrf_blocked(_DDG_HTML_URL), (
        "DuckDuckGo URL заблокирован SSRF-защитой — это неожиданно"
    )


# ---------------------------------------------------------------------------
# network_guard корректно блокирует опасные адреса
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,should_block", [
    ("http://127.0.0.1/", True),
    ("http://localhost/", True),
    ("http://192.168.0.1/", True),
    ("http://10.0.0.1/", True),
    ("http://169.254.169.254/", True),
    ("file:///etc/passwd", True),
    ("ftp://example.com/", True),
    ("https://example.com/", False),
    ("https://html.duckduckgo.com/html/", False),
])
def test_ssrf_guard_classification(url, should_block):
    """network_guard корректно классифицирует URL."""
    from core.network_guard import is_ssrf_blocked
    result = is_ssrf_blocked(url)
    assert result == should_block, (
        f"URL {url!r}: ожидалось blocked={should_block}, получено {result}"
    )
