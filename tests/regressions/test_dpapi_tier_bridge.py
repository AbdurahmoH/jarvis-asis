"""Regression C2 — мост DPAPI ↔ tiers (корень аудита D1).

Аудит 2026-09-06: ``get_api_key`` смотрел только в env и settings.json, а
боевой ключ лежал в DPAPI-хранилище (``data/brain/provider-secrets.dpapi``),
которое читали лишь провайдеры мозга. Следствие: ``is_tier_available(FAST)``
был False при живом ключе — ``llm_available: false`` у роутера, весь трафик
молча уходил на локальную 3B (8 OK / 15 BAD в runtime default).

Теперь порядок: env → settings.json → DPAPI. Любая проблема хранилища —
«ключа нет» и ровно одно предупреждение за процесс.
"""
from __future__ import annotations

import logging

import pytest

import config.settings as settings_module
from config.settings import Settings
from core.brain.secrets import DPAPISecretStore

_REFERENCE = "DEEPINFRA_API_KEY"


@pytest.fixture(autouse=True)
def _fresh_dpapi_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Изоляция кэша и флага предупреждений между тестами."""
    monkeypatch.setattr(settings_module, "_DPAPI_KEY_CACHE", {})
    monkeypatch.setattr(settings_module, "_DPAPI_KEY_WARNING_SHOWN", False)


@pytest.fixture()
def dpapi_store(tmp_path):
    """Настоящее DPAPI-хранилище во временной папке."""
    return DPAPISecretStore(tmp_path / "provider-secrets.dpapi")


def _cloud_settings(tmp_path, dpapi_path) -> Settings:
    """Settings с облачным FAST-тиром и DPAPI-хранилищем в tmp."""
    return Settings(
        api_endpoints={"deepinfra": "https://api.infra.example/v1/openai"},
        models={"fast": "stub/model-id"},
        credential_store={
            "provider": "deepinfra",
            "reference": _REFERENCE,
            "path": str(dpapi_path),
        },
    )


def test_env_beats_settings_and_dpapi(tmp_path, dpapi_store,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    dpapi_store.set(_REFERENCE, "dpapi-key")
    s = _cloud_settings(tmp_path, tmp_path / "provider-secrets.dpapi")
    s.api_keys.deepinfra = "settings-key"
    monkeypatch.setenv("DEEPINFRA_API_KEY", "env-key")

    assert s.get_api_key("deepinfra") == "env-key"


def test_settings_json_beats_dpapi(tmp_path, dpapi_store) -> None:
    dpapi_store.set(_REFERENCE, "dpapi-key")
    s = _cloud_settings(tmp_path, tmp_path / "provider-secrets.dpapi")
    s.api_keys.deepinfra = "settings-key"

    assert s.get_api_key("deepinfra") == "settings-key"


def test_dpapi_used_when_env_and_settings_absent(tmp_path, dpapi_store,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPINFRA_API_KEY", raising=False)
    monkeypatch.delenv("JARVIS_DEEPINFRA_API_KEY", raising=False)
    dpapi_store.set(_REFERENCE, "dpapi-key-32-chars-long-xxxxx")
    s = _cloud_settings(tmp_path, tmp_path / "provider-secrets.dpapi")
    s.api_keys.deepinfra = ""

    assert s.get_api_key("deepinfra") == "dpapi-key-32-chars-long-xxxxx"


def test_is_tier_available_fast_true_with_only_dpapi(tmp_path, dpapi_store,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """Ключ только в DPAPI → тир доступен (это и есть починка D1)."""
    monkeypatch.delenv("DEEPINFRA_API_KEY", raising=False)
    monkeypatch.delenv("JARVIS_DEEPINFRA_API_KEY", raising=False)
    dpapi_store.set(_REFERENCE, "dpapi-key-32-chars-long-xxxxx")
    s = _cloud_settings(tmp_path, tmp_path / "provider-secrets.dpapi")
    s.api_keys.deepinfra = ""

    assert s.get_provider("fast") == "deepinfra"
    assert s.is_tier_available("fast") is True


def test_missing_dpapi_entry_means_no_key(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPINFRA_API_KEY", raising=False)
    monkeypatch.delenv("JARVIS_DEEPINFRA_API_KEY", raising=False)
    s = _cloud_settings(tmp_path, tmp_path / "provider-secrets.dpapi")
    s.api_keys.deepinfra = ""

    assert s.get_api_key("deepinfra") is None
    assert s.is_tier_available("fast") is False


def test_broken_store_warns_exactly_once(tmp_path, monkeypatch: pytest.MonkeyPatch,
                                         caplog: pytest.LogCaptureFixture) -> None:
    """Сбой хранилища — не падение: None и ОДНО предупреждение за процесс."""
    monkeypatch.delenv("DEEPINFRA_API_KEY", raising=False)
    monkeypatch.delenv("JARVIS_DEEPINFRA_API_KEY", raising=False)
    path = tmp_path / "provider-secrets.dpapi"
    path.write_bytes(b"not-a-real-blob")

    def _broken_get(self, reference):
        raise RuntimeError("dpapi decrypt failed")

    monkeypatch.setattr(settings_module, "_DPAPI_KEY_CACHE", {})
    monkeypatch.setattr("core.brain.secrets.DPAPISecretStore", _broken_get)
    s = _cloud_settings(tmp_path, path)
    s.api_keys.deepinfra = ""

    with caplog.at_level(logging.WARNING, logger="config.settings"):
        for _ in range(100):
            assert s.get_api_key("deepinfra") is None

    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "DPAPI" in r.getMessage()]
    assert len(warnings) == 1, "предупреждение о хранилище — ровно одно за процесс"


def test_empty_cache_result_is_not_refetched_per_call(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустая запись кэшируется: горячий путь не гоняет DPAPI на каждый вызов."""
    monkeypatch.delenv("DEEPINFRA_API_KEY", raising=False)
    monkeypatch.delenv("JARVIS_DEEPINFRA_API_KEY", raising=False)
    path = tmp_path / "provider-secrets.dpapi"
    path.write_bytes(b"blob")

    def _counting_store_factory(store_calls: dict):
        class _Store:
            def __init__(self, path) -> None:
                pass

            def get(self, reference):
                store_calls["n"] += 1
                return ""

        return _Store

    store_calls = {"n": 0}
    monkeypatch.setattr(settings_module, "_DPAPI_KEY_CACHE", {})
    monkeypatch.setattr("core.brain.secrets.DPAPISecretStore", _counting_store_factory(store_calls))
    s = _cloud_settings(tmp_path, path)
    s.api_keys.deepinfra = ""

    for _ in range(50):
        assert s.get_api_key("deepinfra") is None
    assert store_calls["n"] == 1
