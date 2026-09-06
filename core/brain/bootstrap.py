"""Build one BrainFabric from current Settings without loading model weights."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import BrainConfigStore
from .fabric import BrainFabric
from .health import BrainHealthManager
from .models import (
    BrainPolicy, BrainPolicyMode, BrainRole, ModelCapabilityProfile, ProviderConfig,
)
from .providers import (
    AnthropicProvider, BackendProviderAdapter, LocalGGUFProvider,
    OpenAICompatibleProvider, OpenAIProvider, OpenRouterProvider,
)
from .registry import BrainProviderRegistry
from .routing import SemanticBrainRouter
from .secrets import (
    CompositeSecretStore, DPAPISecretStore, EnvironmentSecretStore,
    MemorySecretStore, SecretStore,
)


def provider_from_config(config: ProviderConfig, *, secret_store: SecretStore):
    protocol = config.protocol.casefold()
    if protocol in {"openai", "openai_api"}:
        return OpenAIProvider(config, secret_store=secret_store)
    if protocol == "openrouter":
        return OpenRouterProvider(config, secret_store=secret_store)
    if protocol in {"anthropic", "anthropic_messages"}:
        return AnthropicProvider(config, secret_store=secret_store)
    if protocol in {"openai_compatible", "custom"}:
        return OpenAICompatibleProvider(config, secret_store=secret_store)
    raise ValueError(f"unsupported provider protocol: {config.protocol}")


def _policy(settings: Any) -> BrainPolicy:
    value = settings.brain_policy
    return BrainPolicy(
        mode=BrainPolicyMode(str(value.mode).upper()),
        prefer_local=bool(value.prefer_local), allow_cloud=bool(value.allow_cloud),
        allow_sensitive_cloud=bool(value.allow_sensitive_cloud),
        max_fallbacks=int(value.max_fallbacks),
        failure_timeout_seconds=float(value.failure_timeout_seconds),
        background_allow_cloud=bool(value.background_allow_cloud),
        max_cost_tier=int(value.max_cost_tier),
    )


def _identity_contract() -> str:
    try:
        from core.personality import PersonalityEngine
        identity = PersonalityEngine().identity
        values = ", ".join(identity.values)
        return f"Identity: {identity.name}; role: {identity.role}; mission: {identity.mission}; values: {values}."
    except Exception:
        return "Identity: ATLAS; role: personal AI operator; values: accuracy, privacy, initiative, reliability."


def build_brain_fabric(settings: Any, *, secret_store: SecretStore | None = None) -> BrainFabric:
    registry = BrainProviderRegistry()
    # C3: оконный breaker — 3 отказа/таймаута за 60 с открывают цепь на 90 с,
    # затем одна half-open проба.
    health = BrainHealthManager(failure_threshold=3, cooldown_seconds=90.0,
                                failure_window_seconds=60.0)
    policy = _policy(settings)
    credential_store = getattr(settings, "credential_store", None)
    credential_path = str(
        getattr(credential_store, "path", "data/brain/provider-secrets.dpapi")
        or "data/brain/provider-secrets.dpapi"
    )
    secret_path = Path(credential_path)
    if not secret_path.is_absolute():
        secret_path = Path(settings.data_dir).parent / secret_path
    credential_reference = str(
        getattr(credential_store, "reference", "DEEPINFRA_API_KEY")
        or "DEEPINFRA_API_KEY"
    ).strip()
    deepseek_mode = bool(getattr(settings, "deepseek_brain_mode", False))
    deepseek_provider = str(getattr(settings, "deepseek_provider", "deepinfra") or "deepinfra").strip().lower()
    deepseek_model = str(
        getattr(settings, "deepseek_model", "deepseek-ai/DeepSeek-V4-Flash-0731")
        or "deepseek-ai/DeepSeek-V4-Flash-0731"
    ).strip()
    configured_key = settings.get_api_key(deepseek_provider) if deepseek_mode else None
    secrets = secret_store or CompositeSecretStore(
        DPAPISecretStore(secret_path), EnvironmentSecretStore(),
        MemorySecretStore({credential_reference: configured_key or ""}),
    )

    if deepseek_mode:
        # One provider, one model, every cognitive role. No local provider is
        # registered here, so route failure cannot silently load GGUF.
        profile = ModelCapabilityProfile(
            model=deepseek_model,
            roles=frozenset({
                BrainRole.CHAT, BrainRole.FAST, BrainRole.REASONING,
                BrainRole.CODER, BrainRole.PLANNER, BrainRole.RESEARCH,
                BrainRole.CRITIC, BrainRole.SUMMARIZER, BrainRole.FALLBACK,
            }),
            streaming=True, structured_output=True, tool_calling=True,
            vision=False, context_window=131072, local=False, cost_tier=1,
            tested=frozenset({"chat", "streaming", "tool_calling"}),
            metadata={"source": "deepinfra", "migration": "deepseek-brain"},
        )
        endpoint = settings.get_endpoint(deepseek_provider) or ""
        registry.register(
            OpenAICompatibleProvider(ProviderConfig(
                name=deepseek_provider,
                protocol="openai_compatible",
                base_url=endpoint,
                api_key_ref=credential_reference,
                models=(profile,),
                external=True,
                timeout_seconds=float(getattr(settings.limits, "response_timeout_sec", 60.0)),
            ), secret_store=secrets),
            priority=100,
        )
    elif (bool(getattr(settings, "offline_mode", False))
          and bool(getattr(settings, "local_llm_enabled", False))):
        # C1: legacy local mode remains available to library tests and explicit
        # offline profiles, but is unreachable in the cloud-only build and from
        # production migration mode.
        try:
            from core.llm import Tier, get_llm_backend
            local_backend = get_llm_backend(settings, Tier.FAST)
            local_profile = ModelCapabilityProfile(
                model=str(local_backend.model),
                roles=frozenset({
                    BrainRole.CHAT, BrainRole.FAST, BrainRole.REASONING, BrainRole.CODER,
                    BrainRole.PLANNER, BrainRole.RESEARCH, BrainRole.CRITIC,
                    BrainRole.SUMMARIZER, BrainRole.FALLBACK,
                }),
                streaming=True, structured_output=False,
                tool_calling=bool(getattr(local_backend, "supports_tools", False)),
                vision=False, context_window=int(settings.local_model.n_ctx),
                local=True, cost_tier=0, tested=frozenset({"chat", "streaming"}),
                metadata={"source": "existing_local_runtime"},
            )
            registry.register(LocalGGUFProvider("local", local_backend, (local_profile,)), priority=20)
        except Exception:
            pass

    # Existing remote tiers remain usable through their proven backend code.
    if policy.allow_cloud and not deepseek_mode:
        try:
            from core.llm import Tier, get_llm_backend
            role_map = {
                Tier.FAST: frozenset({BrainRole.CHAT, BrainRole.FAST, BrainRole.SUMMARIZER}),
                Tier.ANALYST: frozenset({BrainRole.REASONING, BrainRole.RESEARCH, BrainRole.CRITIC}),
                Tier.CODER: frozenset({BrainRole.CODER}),
                Tier.ARCHITECT: frozenset({BrainRole.PLANNER, BrainRole.REASONING, BrainRole.CRITIC}),
            }
            for tier, roles in role_map.items():
                if settings.get_provider(tier) == "local" or not settings.is_tier_available(tier):
                    continue
                backend = get_llm_backend(settings, tier)
                provider_name = f"{settings.get_provider(tier)}:{tier.value}"
                profile = ModelCapabilityProfile(
                    model=str(backend.model), roles=roles,
                    streaming=True, structured_output=False,
                    tool_calling=bool(getattr(backend, "supports_tools", False)),
                    vision=False, context_window=32768, local=False, cost_tier=2,
                    metadata={"source": "existing_tier", "tier": tier.value},
                )
                registry.register(BackendProviderAdapter(
                    provider_name, backend, (profile,), external=True,
                ))
        except Exception:
            pass

    router = SemanticBrainRouter(registry, health, policy)
    fabric = BrainFabric(
        registry, router=router, health=health, identity_contract=_identity_contract(),
    )
    fabric.secret_store = secrets
    configured_path = getattr(settings.brain_policy, "resolved_providers_path", None)
    path = configured_path or (Path(settings.data_dir) / "brain" / "providers.json")
    store = BrainConfigStore(path)
    fabric.attach_config(store, lambda config: provider_from_config(config, secret_store=secrets))
    fabric.reload_provider_config()
    return fabric


__all__ = ["build_brain_fabric", "provider_from_config"]
