"""Regression: ДЫРА 4b — safe_http_* оборачивает requests-пути инструментов.

Тот же SSRF-guard, что у safe_urlopen: assert_safe_url до запроса и на
каждом редиректе, allow_redirects=False + ручной follow с лимитом hops.

Все сетевые вызовы через requests мокаются на уровне core.network_guard.requests.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class _FakeResponse:
    def __init__(self, status_code=200, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text


def _patch_requests(monkeypatch, responses):
    """Подменяет requests.request в core.network_guard последовательностью ответов."""
    calls: list[tuple] = []

    def _request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        assert kwargs.get("allow_redirects") is False, (
            "safe_http_request обязан ходить с allow_redirects=False"
        )
        return responses.pop(0)

    import core.network_guard as ng

    monkeypatch.setattr(ng.requests, "request", _request)
    return calls


def test_safe_http_get_blocks_private_target():
    import core.network_guard as ng

    with pytest.raises(ng.SSRFBlocked):
        ng.safe_http_get("http://127.0.0.1:8080/admin")


def test_safe_http_get_blocks_redirect_to_private(monkeypatch):
    calls = _patch_requests(monkeypatch, [
        _FakeResponse(302, {"Location": "http://192.168.1.5/steal"}),
    ])
    import core.network_guard as ng

    with pytest.raises(ng.SSRFBlocked):
        ng.safe_http_get("https://example.com/start")
    assert len(calls) == 1, "переход на приватный адрес не должен выполняться"


def test_safe_http_get_follows_public_redirect_chain(monkeypatch):
    calls = _patch_requests(monkeypatch, [
        _FakeResponse(301, {"Location": "https://example.com/step2"}),
        _FakeResponse(200, {}, text="done"),
    ])
    import core.network_guard as ng

    resp = ng.safe_http_get("https://example.com/start")
    assert resp.status_code == 200 and resp.text == "done"
    assert [c[1] for c in calls] == [
        "https://example.com/start", "https://example.com/step2",
    ]


def test_safe_http_get_hop_limit(monkeypatch):
    responses = [
        _FakeResponse(302, {"Location": f"https://example.com/hop{i}"})
        for i in range(10)
    ]
    _patch_requests(monkeypatch, responses)
    import core.network_guard as ng

    with pytest.raises(ng.SSRFBlocked):
        ng.safe_http_get("https://example.com/start", max_hops=2)


def test_web_search_uses_guarded_post():
    """duckduckgo_search ходит через safe_http_post, а не сырой requests."""
    src = open("core/actions/web_search.py", encoding="utf-8").read()
    assert "safe_http_post(" in src
    assert "SSRFBlocked" in src


def test_weather_media_public_sources_use_guarded_get():
    for path in (
        "core/actions/weather.py",
        "core/actions/media.py",
        "core/data/public_sources.py",
    ):
        src = open(path, encoding="utf-8").read()
        assert "safe_http_get(" in src, f"{path} не использует SSRF-guard"
    # В media.py и weather.py не осталось сырых requests.get по http-URL.
    media_src = open("core/actions/media.py", encoding="utf-8").read()
    assert "requests.get(" not in media_src
    weather_src = open("core/actions/weather.py", encoding="utf-8").read()
    assert "requests.get(" not in weather_src
    ps_src = open("core/data/public_sources.py", encoding="utf-8").read()
    assert "requests.get(" not in ps_src
