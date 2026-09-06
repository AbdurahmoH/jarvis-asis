"""Regression C1 — cloud-only сборка: локальная LLM гасится флагом.

Дефолт ``local_llm_enabled=False`` в этой сборке означает:

* фабрика бэкендов отказывается строить локальный мозг (файлы
  ``local_qwen.py``/``llama_server.py`` остаются в репозитории и возвращаются
  флагом True — не удаляются);
* ``is_tier_available`` для локальных тиров всегда False, даже если GGUF
  лежит на диске;
* bootstrap не регистрирует ``LocalGGUFProvider`` в реестре мозга;
* warmup не греет локаль и не скачивает GGUF (покрыто гейтом оркестратора,
  здесь проверяется уровень настроек).

С флагом True (моки вместо реальных весов) всё возвращается.
"""
from __future__ import annotations



import pytest

from config.settings import Settings
from core.llm.backend import BackendConfigError
from core.llm.factory import get_llm_backend


@pytest.fixture()
def local_gguf(tmp_path):
    """Фейковый GGUF-файл: существование проверяется, веса не грузятся."""
    path = tmp_path / "models" / "fake-model.gguf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake gguf header")
    return path


def _settings(gguf, *, flag: bool) -> Settings:
    return Settings(
        offline_mode=True,
        local_llm_enabled=flag,
        local_model={"gguf_path": str(gguf)},
    )


def test_flag_default_is_false() -> None:
    assert Settings().local_llm_enabled is False


def test_factory_rejects_local_backend_when_flag_off(local_gguf) -> None:
    with pytest.raises(BackendConfigError) as exc:
        get_llm_backend(_settings(local_gguf, flag=False), "fast")
    assert "local_llm_enabled" in str(exc.value)


def test_factory_accepts_local_backend_when_flag_on(local_gguf,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """Флаг True возвращает локальный путь (мок вместо реальных весов)."""

    class _StubBackend:
        name = "stub:local"
        model = "fake-model"
        supports_tools = False

        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def is_available(self) -> bool:
            return True

        def close(self) -> None:
            return None

    monkeypatch.setattr("core.llm.factory.LocalQwenBackend", _StubBackend)
    backend = get_llm_backend(_settings(local_gguf, flag=True), "fast")
    assert isinstance(backend, _StubBackend)


def test_is_tier_available_local_tier_gated_by_flag(local_gguf) -> None:
    assert _settings(local_gguf, flag=False).is_tier_available("fast") is False
    assert _settings(local_gguf, flag=True).is_tier_available("fast") is True


def test_bootstrap_registers_no_local_provider_when_flag_off(local_gguf) -> None:
    from core.brain import build_brain_fabric

    fabric = build_brain_fabric(_settings(local_gguf, flag=False))
    assert "local" not in fabric.registry.names()


def test_bootstrap_registers_local_provider_when_flag_on(local_gguf,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    from core.brain import build_brain_fabric

    class _StubBackend:
        name = "stub:local"
        model = "fake-model"
        supports_tools = False

        def is_available(self) -> bool:
            return True

        def chat(self, *_a, **_kw):
            return "ok"

        def close(self) -> None:
            return None

    monkeypatch.setattr("core.llm.get_llm_backend", lambda *a, **kw: _StubBackend())
    fabric = build_brain_fabric(_settings(local_gguf, flag=True))
    assert "local" in fabric.registry.names()


def test_cloud_tier_without_key_is_unavailable_even_with_gguf(local_gguf) -> None:
    """Облачный FAST без ключа недоступен даже при лежащем GGUF.

    Гейт флага не должен создавать скрытый локальный фолбэк: если облачный
    тир недоступен, он недоступен — никакого тихого перехода на локаль.
    """
    s = Settings(
        offline_mode=True,
        local_llm_enabled=False,
        local_model={"gguf_path": str(local_gguf)},
        api_endpoints={"deepinfra": "https://api.infra.com/v1/openai"},
        models={"fast": "stub/model-id", "providers": {"fast": "deepinfra"}},
    )
    if s.get_provider("fast") == "local":
        pytest.skip("конфигурация направляет fast на локального провайдера")
    assert s.get_api_key("deepinfra") is None
    assert s.is_tier_available("fast") is False
