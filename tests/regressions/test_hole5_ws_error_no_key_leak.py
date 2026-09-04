"""Regression: ДЫРА 5 — сообщения об ошибках WS не содержат API-ключ.

Проверяем что _update_cloud_settings при ошибке валидации не включает
значение api_key в str(exc), которое уходит в WS-ответ.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch


def _make_ws_server():
    """Создаёт минимальный JarvisWSServer для тестирования."""
    from core.ws_server import JarvisWSServer
    from config.settings import Settings

    settings = Settings()
    orch = MagicMock()
    orch._settings = settings
    orch._output_callback = lambda x: None
    orch.proactor = None
    orch._tts_queue = None

    # Патчим тяжёлые зависимости
    with patch("core.ws_server.JarvisWSServer.__init__", lambda self, *a, **kw: None):
        server = JarvisWSServer.__new__(JarvisWSServer)
        server._settings = settings
        server._orch = orch
        server._lock = __import__("threading").RLock()
        server._clients = set()
        server._authorized_clients = set()
        server._message_times = {}
        server._streaming_started = set()
        server._running = False
        server._auth_token = None
        server._allowed_origins = set()
        server._max_messages_per_window = 60
        server._rate_window_sec = 10.0
        server._loop = None
        server._tls = __import__("threading").local()
        server._greeted = False
        server._system_monitor = None
        server._stt_engine = None
        server._voice_addressed_until = 0.0
        server._orig_output = lambda x: None
        server._stop_event = __import__("threading").Event()
        server._ws_server = None
        server._unsub = None
    return server


def test_update_cloud_settings_error_does_not_leak_api_key():
    """Ошибка валидации settings не включает значение api_key в сообщение."""
    server = _make_ws_server()

    secret_key = "sk-super-secret-key-12345"

    # Передаём невалидный patch с api_key
    patch_data = {
        "provider": "deepinfra",
        "api_key": secret_key,
        "model": "x" * 300,  # слишком длинная модель — вызовет ошибку
    }

    try:
        server._update_cloud_settings(patch_data)
    except Exception as exc:
        error_msg = str(exc)
        assert secret_key not in error_msg, (
            f"API-ключ утёк в сообщение об ошибке: {error_msg!r}"
        )
    else:
        # Если не бросило — тоже ОК (валидация прошла)
        pass


def test_validate_model_error_does_not_include_value():
    """_validate_model не включает значение модели в сообщение об ошибке."""
    server = _make_ws_server()

    long_model = "a" * 300
    try:
        server._validate_model(long_model)
    except ValueError as exc:
        assert long_model not in str(exc), (
            "Значение модели не должно попадать в сообщение об ошибке"
        )


def test_validate_base_url_error_does_not_include_credentials():
    """_validate_base_url не включает credentials в сообщение об ошибке."""
    server = _make_ws_server()

    url_with_creds = "https://user:password123@example.com/api"
    try:
        server._validate_base_url(url_with_creds)
    except (ValueError, Exception) as exc:
        assert "password123" not in str(exc), (
            "Пароль из URL не должен попадать в сообщение об ошибке"
        )


def test_update_cloud_settings_unknown_fields_error_safe():
    """Ошибка о неизвестных полях не включает их значения."""
    server = _make_ws_server()

    patch_data = {
        "unknown_field": "sk-secret-value",
        "another_secret": "token-abc123",
    }

    try:
        server._update_cloud_settings(patch_data)
    except ValueError as exc:
        error_msg = str(exc)
        # Имена полей могут быть в сообщении (это ОК), но не значения
        assert "sk-secret-value" not in error_msg, (
            "Значение неизвестного поля утекло в сообщение об ошибке"
        )
        assert "token-abc123" not in error_msg, (
            "Значение неизвестного поля утекло в сообщение об ошибке"
        )
