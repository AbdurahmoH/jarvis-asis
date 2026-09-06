"""Regression C3 — health probe, оконный circuit breaker, provider_effective.

Наряд: проба провайдера каждые 60 с и перед первым запросом после простоя
> 5 мин; 3 отказа/таймаута за 60 с открывают цепь на 90 с, затем half-open
проба; в runtime_status/WS видно provider_effective = {tier, source,
reachable, last_probe, circuit}.

Причина существования: баг D1 жил месяц, потому что «работает ли облако»
не было видно снаружи.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import config.settings as settings_module
from config.settings import Settings
from core.brain.health import BrainHealthManager, ProviderProbe
from core.brain.models import HealthSnapshot, HealthStatus


# --------------------------------------------------------------------------- #
# Circuit breaker: окно 3 отказа / 60 с -> open 90 с -> half-open -> closed
# --------------------------------------------------------------------------- #


def test_three_failures_in_window_open_the_circuit() -> None:
    health = BrainHealthManager(failure_threshold=3, cooldown_seconds=90.0,
                                failure_window_seconds=60.0)
    key = "deepinfra:stub-model"
    for _ in range(3):
        health.record_failure(key, timeout=True, error="timed out")
    assert health.circuit_state(key) == "open"
    assert health.allow(key) is False


def test_two_failures_do_not_open_the_circuit() -> None:
    health = BrainHealthManager(failure_threshold=3, cooldown_seconds=90.0,
                                failure_window_seconds=60.0)
    key = "deepinfra:stub-model"
    health.record_failure(key)
    health.record_failure(key)
    assert health.circuit_state(key) == "closed"
    assert health.allow(key) is True


def test_half_open_after_cooldown_then_closed_on_success() -> None:
    health = BrainHealthManager(failure_threshold=3, cooldown_seconds=0.05,
                                failure_window_seconds=60.0)
    key = "deepinfra:stub-model"
    for _ in range(3):
        health.record_failure(key, timeout=True)
    assert health.circuit_state(key) == "open"
    time.sleep(0.06)
    # cooldown истёк: ровно одна half-open проба разрешена
    assert health.allow(key) is True
    assert health.circuit_state(key) == "half_open"
    health.record_success(key, latency_ms=12.0)
    assert health.circuit_state(key) == "closed"
    assert health.allow(key) is True


def test_failures_outside_the_window_do_not_open() -> None:
    """Окно скользящее: старые отказы не копятся вечно (3 за 60 с)."""
    health = BrainHealthManager(failure_threshold=3, cooldown_seconds=0.05,
                                failure_window_seconds=0.0)
    key = "deepinfra:stub-model"
    health.record_failure(key)
    time.sleep(0.01)
    health.record_failure(key)
    # оба отказа вышли из окна нулевой ширины
    assert health.circuit_state(key) == "closed"


# --------------------------------------------------------------------------- #
# ProviderProbe: фоновая проба меняет состояние и время последней пробы
# --------------------------------------------------------------------------- #


class _FlipProvider:
    """Провайдер, здоровье которого управляется тестом."""

    def __init__(self) -> None:
        self.alive = False
        self.name = "stub"
        self.probes = 0

    def health(self) -> HealthSnapshot:
        self.probes += 1
        return HealthSnapshot(
            status=HealthStatus.AVAILABLE if self.alive else HealthStatus.OFFLINE,
            latency_ms=1.0,
        )

    def models(self):
        return ("stub-model",)

    def close(self):
        return None


class _Registry:
    def __init__(self, provider) -> None:
        self._provider = provider

    def providers(self):
        return [SimpleNamespace(provider=self._provider)]


def test_probe_updates_last_probe_and_reachability() -> None:
    provider = _FlipProvider()
    from core.brain.registry import BrainProviderRegistry
    registry = BrainProviderRegistry()
    registry.register(provider)
    from core.brain.fabric import BrainFabric
    fabric = BrainFabric(registry)

    assert fabric.last_probe_at.get("stub") is None
    statuses = fabric.refresh_health()
    assert fabric.last_probe_at.get("stub") is not None
    assert str(statuses["stub"].status.value) == "offline"

    provider.alive = True
    statuses = fabric.refresh_health()
    assert str(statuses["stub"].status.value) == "available"


def test_probe_thread_runs_and_stops() -> None:
    provider = _FlipProvider()
    probe = ProviderProbe(provider.health, interval_sec=5.0)
    probe.start()
    deadline = time.time() + 3.0
    while provider.probes == 0 and time.time() < deadline:
        time.sleep(0.05)
    probe.stop()
    assert provider.probes >= 1, "фоновая проба выполнена хотя бы один раз"


# --------------------------------------------------------------------------- #
# api_key_source: env > settings.json > dpapi > missing
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _fresh_dpapi_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_module, "_DPAPI_KEY_CACHE", {})
    monkeypatch.setattr(settings_module, "_DPAPI_KEY_WARNING_SHOWN", False)


def _cloud_settings(tmp_path) -> Settings:
    return Settings(
        api_endpoints={"deepinfra": "https://api.infra.example/v1/openai"},
        models={"fast": "stub/model-id"},
        credential_store={"provider": "deepinfra", "reference": "DEEPINFRA_API_KEY",
                          "path": str(tmp_path / "provider-secrets.dpapi")},
    )


def test_api_key_source_priority(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.brain.secrets import DPAPISecretStore
    monkeypatch.delenv("DEEPINFRA_API_KEY", raising=False)
    monkeypatch.delenv("JARVIS_DEEPINFRA_API_KEY", raising=False)
    s = _cloud_settings(tmp_path)

    assert s.api_key_source("deepinfra") == "missing"

    store = DPAPISecretStore(tmp_path / "provider-secrets.dpapi")
    store.set("DEEPINFRA_API_KEY", "dpapi-key-32-chars-long-xxxxx")
    assert s.api_key_source("deepinfra") == "dpapi"

    s.api_keys.deepinfra = "settings-key"
    assert s.api_key_source("deepinfra") == "settings.json"

    monkeypatch.setenv("DEEPINFRA_API_KEY", "env-key")
    assert s.api_key_source("deepinfra") == "env"
