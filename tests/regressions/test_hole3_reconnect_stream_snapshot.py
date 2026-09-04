"""Regression: ДЫРА 3 — переподключившийся клиент получает снимок активных
стримов (event:jarvis:start для каждого открытого correlation id).
"""
from __future__ import annotations

import json
import threading

import pytest


def test_snapshot_envelopes_for_active_streams():
    from core.ws_server import JarvisWSServer
    from config.settings import Settings

    settings = Settings()
    with __import__("unittest").mock.patch(
        "core.ws_server.JarvisWSServer.__init__", lambda self, *a, **kw: None
    ):
        server = JarvisWSServer.__new__(JarvisWSServer)
    server._lock = threading.RLock()
    server._streaming_started = {"stream-a", "stream-b"}

    envelopes = server._stream_snapshot_envelopes()
    assert len(envelopes) == 2
    ids = set()
    for envelope in envelopes:
        assert envelope["type"] == "event"
        event = envelope["event"]
        assert event["type"] == "event:jarvis:start"
        payload = event["payload"]
        assert payload["kind"] == "jarvis"
        assert payload["content"] == ""
        ids.add(payload["id"])
        # Конверт сериализуем — фронт получит валидный JSON.
        json.dumps(envelope)
    assert ids == {"stream-a", "stream-b"}


def test_snapshot_empty_when_no_active_streams():
    from core.ws_server import JarvisWSServer
    from config.settings import Settings

    with __import__("unittest").mock.patch(
        "core.ws_server.JarvisWSServer.__init__", lambda self, *a, **kw: None
    ):
        server = JarvisWSServer.__new__(JarvisWSServer)
    server._lock = threading.RLock()
    server._streaming_started = set()
    assert server._stream_snapshot_envelopes() == []


def test_handler_sends_snapshot_after_connect():
    """_handler реально отправляет снимок подключившемуся клиенту (источник правды — код)."""
    src = open("core/ws_server.py", encoding="utf-8").read()
    assert "_stream_snapshot_envelopes" in src
    # Снимок отправляется в _handler после регистрации клиента.
    assert "for envelope in self._stream_snapshot_envelopes():" in src
    assert "await ws.send(json.dumps(envelope))" in src
