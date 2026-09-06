"""Mission-aware model orchestration with bounded failover."""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import replace

from core.security.redaction import redact_text

from .health import BrainHealthManager, ProviderProbe
from .context import ContextComposer
from .models import (
    BrainRequest, BrainResult, BrainRoute, NoRouteAvailable,
    ProviderResponseError, ProviderUnavailable, RouteCandidate,
)
from .registry import BrainProviderRegistry
from .routing import SemanticBrainRouter


class BrainFabric:
    def __init__(self, registry: BrainProviderRegistry, *,
                 router: SemanticBrainRouter | None = None,
                 health: BrainHealthManager | None = None,
                 identity_contract: str = "") -> None:
        self.registry = registry
        self.health = health or BrainHealthManager()
        self.router = router or SemanticBrainRouter(registry, self.health)
        self.identity_contract = " ".join((identity_contract or "").split())
        self.context_composer = ContextComposer()
        self._bindings: dict[tuple[str, str], BrainRoute] = {}
        self._lock = threading.RLock()
        self._last_route: BrainRoute | None = None
        self._last_result: BrainResult | None = None
        self._config_store = None
        self._provider_factory = None
        self._dynamic_provider_names: set[str] = set()
        self._applied_config_generation = -1
        self._route_providers: OrderedDict[int, dict[str, object]] = OrderedDict()
        self._retired_providers: list[object] = []
        # C3: provider_effective — время последней пробы по провайдеру и
        # фоновый probe; время активности пользователя для probe-if-stale.
        self.last_probe_at: dict[str, str] = {}
        self._last_activity: float = time.monotonic()
        self._probe: ProviderProbe | None = None

    def mark_activity(self) -> None:
        """C3: пользователь что-то попросил — сброс idle-таймера пробы."""
        self._last_activity = time.monotonic()

    def probe_if_stale(self, *, max_idle_sec: float = 300.0) -> bool:
        """C3: первая проба после долгого простоя (idle > 5 мин по наряду)."""
        if time.monotonic() - self._last_activity < max_idle_sec:
            return False
        self.refresh_health()
        return True

    def start_probe(self, *, interval_sec: float = 60.0) -> None:
        """C3: фоновая проба провайдера раз в interval_sec (наряд: 60 с)."""
        if self._probe is None:
            self._probe = ProviderProbe(self.refresh_health, interval_sec=interval_sec)
        self._probe.start()

    def stop_probe(self) -> None:
        if self._probe is not None:
            self._probe.stop()

    def circuit_state(self, provider: str, model: str = "") -> str:
        """C3: состояние цепи breaker'а для ключа provider:model."""
        return self.health.circuit_state(f"{provider}:{model}" if model else provider)

    def attach_config(self, store, provider_factory) -> None:
        self._config_store = store
        self._provider_factory = provider_factory

    def reload_provider_config(self) -> tuple[str, ...]:
        if self._config_store is None or self._provider_factory is None:
            return ()
        configs = self._config_store.reload_if_changed()
        if self._config_store.generation == self._applied_config_generation:
            return tuple(sorted(self._dynamic_provider_names))
        incoming = {config.name for config in configs if config.enabled}
        static_names = set(self.registry.names()) - self._dynamic_provider_names
        collision = incoming & static_names
        if collision:
            raise ValueError(f"custom provider name collides with runtime provider: {sorted(collision)[0]}")
        for name in self._dynamic_provider_names - incoming:
            removed = self.registry.remove(name, close=False)
            if removed is not None:
                self._retired_providers.append(removed)
        for config in configs:
            if not config.enabled:
                continue
            provider = self._provider_factory(config)
            if self.registry.get(config.name) is not None:
                removed = self.registry.remove(config.name, close=False)
                if removed is not None:
                    self._retired_providers.append(removed)
            self.registry.register(provider)
        self._dynamic_provider_names = incoming
        self._applied_config_generation = self._config_store.generation
        return tuple(sorted(incoming))

    def _with_identity(self, request: BrainRequest) -> BrainRequest:
        if not self.identity_contract or self.identity_contract in request.system:
            return request
        system = f"{self.identity_contract}\n{request.system}".strip()
        return replace(request, system=system)

    def compose_context(self, *, role, identity: str, user_request: str,
                        mission: str = "", **sections):
        """Expose one bounded context path to Cognitive Core and providers."""
        return self.context_composer.compose(
            role=role, identity=identity, user_request=user_request,
            mission=mission, **sections,
        )

    def refresh_health(self) -> dict[str, object]:
        statuses: dict[str, object] = {}
        for entry in self.registry.providers():
            try:
                snapshot = entry.provider.health()
                statuses[entry.provider.name] = snapshot
                # C3: время последней пробы уходит в provider_effective.
                self.last_probe_at[entry.provider.name] = ProviderProbe.now_iso()
                for model in entry.provider.models():
                    key = f"{entry.provider.name}:{model}"
                    self.health.set_status(key, snapshot.status)
            except Exception:
                statuses[entry.provider.name] = None
        return statuses

    def select_route(self, request: BrainRequest, *, reselect: bool = False) -> BrainRoute:
        key = (request.mission_id, request.stage)
        with self._lock:
            if not reselect and request.mission_id and key in self._bindings:
                return self._bindings[key]
        route = self.router.route(request)
        with self._lock:
            if request.mission_id:
                self._bindings[key] = route
            self._last_route = route
            self._route_providers[id(route)] = {
                candidate.provider: self.registry.get(candidate.provider)
                for candidate in self._candidates(route)
            }
            self._route_providers.move_to_end(id(route))
            while len(self._route_providers) > 256:
                self._route_providers.popitem(last=False)
        return route

    @staticmethod
    def _candidates(route: BrainRoute) -> tuple[RouteCandidate, ...]:
        return (route.primary,) + route.fallback_chain

    def _provider_for(self, route: BrainRoute, candidate: RouteCandidate):
        with self._lock:
            bound = self._route_providers.get(id(route), {})
            provider = bound.get(candidate.provider)
        return provider or self.registry.get(candidate.provider)

    def generate(self, request: BrainRequest, *, route: BrainRoute | None = None) -> BrainResult:
        selected_route = route or self.select_route(request)
        effective_request = self._with_identity(request)
        failures: list[str] = []
        for candidate in self._candidates(selected_route):
            key = f"{candidate.provider}:{candidate.model}"
            if not self.health.allow(key):
                continue
            provider = self._provider_for(selected_route, candidate)
            if provider is None:
                failures.append(f"{candidate.provider}:removed")
                continue
            started = time.perf_counter()
            try:
                result = provider.generate(effective_request, model=candidate.model)
            except (ProviderUnavailable, ProviderResponseError, TimeoutError) as exc:
                latency = (time.perf_counter() - started) * 1000
                message = redact_text(str(exc))[:240]
                self.health.record_failure(
                    key, latency_ms=latency,
                    timeout=isinstance(exc, TimeoutError) or "timed out" in message.casefold(),
                    error=message,
                )
                failures.append(f"{candidate.provider}:{type(exc).__name__}")
                continue
            except Exception as exc:
                latency = (time.perf_counter() - started) * 1000
                self.health.record_failure(key, latency_ms=latency,
                                           error=redact_text(str(exc))[:240])
                failures.append(f"{candidate.provider}:{type(exc).__name__}")
                continue
            self.health.record_success(key, latency_ms=result.latency_ms)
            with self._lock:
                self._last_result = result
            return result
        raise ProviderUnavailable(
            "all routed providers unavailable" + (f" ({', '.join(failures)})" if failures else "")
        )

    def stream(self, request: BrainRequest, *, route: BrainRoute | None = None) -> Iterator[str]:
        selected_route = route or self.select_route(request)
        effective_request = self._with_identity(request)
        for candidate in self._candidates(selected_route):
            key = f"{candidate.provider}:{candidate.model}"
            if not self.health.allow(key):
                continue
            provider = self._provider_for(selected_route, candidate)
            if provider is None:
                continue
            started = time.perf_counter()
            emitted = False
            try:
                for piece in provider.stream(effective_request, model=candidate.model):
                    emitted = True
                    yield piece
                if emitted:
                    self.health.record_success(key, latency_ms=(time.perf_counter() - started) * 1000)
                    return
                raise ProviderResponseError("empty stream")
            except (ProviderUnavailable, ProviderResponseError, TimeoutError) as exc:
                self.health.record_failure(
                    key, latency_ms=(time.perf_counter() - started) * 1000,
                    timeout=isinstance(exc, TimeoutError), error=redact_text(str(exc))[:240],
                )
                if emitted:
                    raise ProviderUnavailable("stream interrupted after output") from exc
                continue
        raise ProviderUnavailable("all routed streams unavailable")

    def cancel(self, mission_id: str = "", stage: str = "") -> bool:
        route = self._bindings.get((mission_id, stage)) if mission_id else self._last_route
        if route is None:
            return False
        changed = False
        for candidate in self._candidates(route):
            provider = self._provider_for(route, candidate)
            if provider is not None:
                changed = provider.cancel(f"{mission_id}:{stage}") or changed
        return changed

    def release_mission(self, mission_id: str) -> int:
        with self._lock:
            keys = [key for key in self._bindings if key[0] == mission_id]
            for key in keys:
                route = self._bindings.pop(key, None)
                if route is not None:
                    self._route_providers.pop(id(route), None)
            return len(keys)

    def close(self) -> None:
        self.stop_probe()
        self.registry.close()
        for provider in self._retired_providers:
            try:
                provider.close()
            except Exception:
                pass
        self._retired_providers.clear()

    @property
    def last_result(self) -> BrainResult | None:
        return self._last_result

    @property
    def last_route(self) -> BrainRoute | None:
        return self._last_route


class BrainFabricBackend:
    """Compatibility facade exposing the existing synchronous LLMBackend API."""

    supports_embeddings = False

    def __init__(self, fabric: BrainFabric, route: BrainRoute, *, template: BrainRequest) -> None:
        self.fabric = fabric
        self.route = route
        self.template = template
        self.name = f"brain:{route.primary.provider}:{route.primary.model}"
        self.model = route.primary.model
        provider = fabric._provider_for(route, route.primary)
        try:
            profile = provider.capabilities(route.primary.model) if provider else None
        except Exception:
            profile = None
        self.supports_tools = bool(profile and profile.tool_calling)

    def _request(self, messages, system=None, max_tokens=None, temperature=None) -> BrainRequest:
        normalized = tuple(dict(item) for item in (messages or ()))
        user = self.template.user_request
        for item in reversed(normalized):
            if str(item.get("role", "")) == "user":
                user = str(item.get("content", ""))
                break
        return replace(
            self.template, user_request=user, messages=normalized,
            system=system or self.template.system, max_tokens=max_tokens,
            temperature=temperature,
        )

    def direct(self, prompt: str, system: str | None = None,
               max_tokens: int | None = None, temperature: float | None = None) -> str:
        return self.chat([{"role": "user", "content": prompt}], system=system,
                         max_tokens=max_tokens, temperature=temperature)

    def chat(self, messages, system: str | None = None,
             max_tokens: int | None = None, temperature: float | None = None) -> str:
        try:
            return self.fabric.generate(
                self._request(messages, system, max_tokens, temperature), route=self.route,
            ).text
        except Exception as exc:
            from core.llm.backend import BackendUnavailable
            raise BackendUnavailable(str(exc)) from exc

    def chat_with_tools(self, messages, tools, system: str | None = None,
                        tool_choice: str | dict | None = "auto",
                        max_tokens: int | None = None,
                        temperature: float | None = None):
        provider = self.fabric._provider_for(self.route, self.route.primary)
        call = getattr(provider, "chat_with_tools", None)
        if not callable(call):
            from core.llm.backend import ToolsNotSupportedError
            raise ToolsNotSupportedError("selected brain provider has no native tool calling")
        try:
            request = self._request(messages, system, max_tokens, temperature)
            return call(
                request,
                tools,
                model=self.route.primary.model,
                tool_choice=tool_choice or "auto",
            )
        except Exception as exc:
            from core.llm.backend import BackendUnavailable
            raise BackendUnavailable(str(exc)) from exc

    def streaming(self, messages, system: str | None = None,
                  max_tokens: int | None = None, temperature: float | None = None):
        try:
            yield from self.fabric.stream(
                self._request(messages, system, max_tokens, temperature), route=self.route,
            )
        except Exception as exc:
            from core.llm.backend import BackendUnavailable
            raise BackendUnavailable(str(exc)) from exc

    def list_models(self) -> list[str]:
        return [self.model]

    def warm_up(self) -> None:
        provider = self.fabric._provider_for(self.route, self.route.primary)
        if provider is None or provider.health().status.value not in {"available", "degraded"}:
            from core.llm.backend import BackendUnavailable
            raise BackendUnavailable(f"brain provider {self.route.primary.provider} unavailable")

    def is_available(self) -> bool:
        key = f"{self.route.primary.provider}:{self.route.primary.model}"
        return self.fabric._provider_for(self.route, self.route.primary) is not None and self.fabric.health.allow(key)

    def close(self) -> None:
        return None


__all__ = ["BrainFabric", "BrainFabricBackend"]
