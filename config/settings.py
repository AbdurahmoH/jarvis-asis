"""Конфигурация проекта: загрузка, валидация, атомарное сохранение.

Ключевой принцип: **отсутствие API-ключа не должно ронять приложение**.
Тир без ключа помечается недоступным (``is_tier_available()`` вернёт False),
и «совет мудрецов» просто пропустит его при эскалации, оставшись на
локальной Qwen 4B.

Использование::

    from config.settings import load_config

    settings = load_config()                 # config/settings.json
    settings.ensure_directories()
    key = settings.get_api_key("deepseek")   # None, если не задан
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

log = logging.getLogger(__name__)

#: C2: DPAPI-ключ читается один раз на (путь, ссылку, mtime) — маршрут
#: дергает ``get_api_key`` на горячем пути, DPAPI-расшифровка каждый раз
#: недопустима. Значение кэша — строка (может быть пустой: «записи нет»).
_DPAPI_KEY_CACHE: Dict[Any, str] = {}
#: C2: проблема хранилища логируется ровно один раз за процесс.
_DPAPI_KEY_WARNING_SHOWN: bool = False

# ВАЖНО: config — лист зависимостей, он НЕ импортирует core.llm на верхнем
# уровне, иначе возникает цикл импортов:
#   config.settings -> core.llm.tiers (запускает core.llm.__init__
#       -> core.llm.factory -> config.settings) -> ImportError.
# Поэтому Tier/resolver импортируются лениво внутри тех методов, где нужны.
from core.utils.logger import get_logger
from core.utils.paths import (
    PROJECT_ROOT,
    ensure_dirs,
    ensure_parent,
    resolve_path,
)

__all__ = [
    "ApiKeys",
    "ApiEndpoints",
    "ModelTiers",
    "TierProviders",
    "LocalModelConfig",
    "LocalCoderModelConfig",
    "VoiceConfig",
    "PathsConfig",
    "PersonaConfig",
    "LimitsConfig",
    "LoggingConfig",
    "ProxyConfig",
    "LauncherConfig",
    "STTConfig",
    "WakeWordConfig",
    "ShadowConfig",
    "BrainPolicyConfig",
    "CredentialStoreConfig",
    "Settings",
    "ConfigError",
    "load_config",
    "default_config_path",
    "example_config_path",
]

log = get_logger(__name__)

#: Имя рабочего файла конфигурации и шаблона рядом с ним.
CONFIG_FILENAME = "settings.json"
EXAMPLE_FILENAME = "settings.example.json"

#: Провайдер локальной модели — единственный, которому не нужен API-ключ.
LOCAL_PROVIDER = "local"

#: Мультиязычная модель эмбеддингов по умолчанию (Шаг 0).
DEFAULT_EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"


class ConfigError(RuntimeError):
    """Ошибка конфигурации: файл не найден, битый JSON, не прошла валидация."""


# --------------------------------------------------------------------------- #
#  Секции конфигурации
# --------------------------------------------------------------------------- #

class _Section(BaseModel):
    """База для секций: разрешает ключи с префиксом ``model_`` (например,
    ``model_tiers``) и не запрещает неизвестные ключи."""

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        protected_namespaces=(),  # убираем резерв 'model_', чтобы model_tiers не конфликтовал
    )


class ApiKeys(_Section):
    """API-ключи провайдеров. Пустая строка == ключ не задан."""

    deepseek: str = ""
    deepinfra: str = ""
    kimi: str = ""
    claude: str = ""
    openrouter: str = ""

    def get(self, provider: str) -> Optional[str]:
        """Ключ провайдера или None, если он пуст/не задан."""
        raw = getattr(self, provider.strip().lower(), None)
        if raw is None:
            extra = self.model_extra or {}
            raw = extra.get(provider.strip().lower())
        if not isinstance(raw, str):
            return None
        value = raw.strip()
        return value or None


class ApiEndpoints(_Section):
    """OpenAI-совместимые базовые URL провайдеров."""

    deepseek: str = "https://api.deepseek.com/v1"
    deepinfra: str = "https://api.deepinfra.com/v1/openai"
    kimi: str = "https://api.moonshot.ai/v1"
    claude: str = "https://api.anthropic.com/v1"
    openrouter: str = "https://openrouter.ai/api/v1"

    def get(self, provider: str) -> Optional[str]:
        """Базовый URL провайдера без завершающего слэша."""
        key = provider.strip().lower()
        raw = getattr(self, key, None)
        if raw is None:
            extra = self.model_extra or {}
            raw = extra.get(key)
        if not isinstance(raw, str):
            return None
        value = raw.strip().rstrip("/")
        return value or None


class ModelTiers(_Section):
    """Логические роли одной локальной модели.

    В production все роли намеренно указывают на один GGUF. Это не
    означает пять загрузок: фабрика разделяет физический backend по пути.
    """

    fast: str = "deepseek-ai/DeepSeek-V4-Flash-0731"
    analyst: str = "deepseek-ai/DeepSeek-V4-Flash-0731"
    coder: str = "deepseek-ai/DeepSeek-V4-Flash-0731"
    architect: str = "deepseek-ai/DeepSeek-V4-Flash-0731"
    research: str = "deepseek-ai/DeepSeek-V4-Flash-0731"

    def get(self, tier_key: str) -> Optional[str]:
        raw = getattr(self, tier_key, None)
        if raw is None:
            extra = self.model_extra or {}
            raw = extra.get(tier_key)
        if not isinstance(raw, str):
            return None
        return raw.strip() or None


class TierProviders(_Section):
    """Какой провайдер обслуживает каждый тир.

    ``fast`` по умолчанию = ``local`` (llama-cpp-python, без сети).
    """

    fast: str = "deepinfra"
    analyst: str = "deepinfra"
    coder: str = "deepinfra"
    architect: str = "deepinfra"
    research: str = "deepinfra"

    def get(self, tier_key: str) -> str:
        raw = getattr(self, tier_key, None)
        if raw is None:
            extra = self.model_extra or {}
            raw = extra.get(tier_key)
        if not isinstance(raw, str) or not raw.strip():
            return LOCAL_PROVIDER
        return raw.strip().lower()


class LocalModelConfig(_Section):
    """Параметры единственной локальной GGUF-модели (llama.cpp)."""

    gguf_path: str = "data/models/qwen3-4b-instruct-q5_k_m.gguf"
    # Автопрофиль выбирает самый сильный локальный GGUF, который уже есть на
    # диске и подходит текущему железу. Пользовательский путь сохраняется,
    # если этот флаг выключен.
    auto_profile: bool = True
    #: ``python`` keeps the embedded llama-cpp path; ``auto`` prefers the
    #: installed official llama.cpp server and falls back to Python locally.
    runtime_backend: str = "python"
    server_binary_path: str = ""
    server_host: str = "127.0.0.1"
    server_port: int = 8782
    server_gpu_layers: str = "all"
    server_start_timeout_sec: float = 30.0
    server_request_timeout_sec: float = 45.0
    draft_model_path: str = ""
    speculative_decoding: bool = False
    draft_max_tokens: int = 5
    # Prefer structured llama.cpp/OpenAI tool calls; the planner falls back to
    # its validated JSON contract when a provider or old wheel rejects them.
    native_tool_calling: bool = True
    embedding_gguf_path: str = ""
    embedding_model_name: str = "paraphrase-multilingual-MiniLM-L12-v2"
    n_gpu_layers: int = 0           # 0 = CPU, -1 = все слои на GPU
    n_ctx: int = 4096
    n_threads: int = 0              # 0 = автоопределение по числу ядер
    n_batch: int = 256
    quantization: str = "Q4_K_M"
    temperature: float = 0.25
    max_tokens: int = 384
    chat_format: Optional[str] = None   # None = взять шаблон из GGUF-метаданных
    verbose: bool = False

    @field_validator("n_ctx", "n_batch", "max_tokens", "draft_max_tokens")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("значение должно быть больше нуля")
        return value

    @field_validator("server_port")
    @classmethod
    def _valid_server_port(cls, value: int) -> int:
        if not 1 <= int(value) <= 65535:
            raise ValueError("server_port должен быть в диапазоне 1..65535")
        return int(value)

    @field_validator("server_start_timeout_sec", "server_request_timeout_sec")
    @classmethod
    def _valid_server_timeout(cls, value: float) -> float:
        if float(value) <= 0:
            raise ValueError("таймаут llama-server должен быть положительным")
        return float(value)

    @field_validator("temperature")
    @classmethod
    def _sane_temperature(cls, value: float) -> float:
        if not 0.0 <= value <= 2.0:
            raise ValueError("temperature должна быть в диапазоне 0.0..2.0")
        return value

    @property
    def resolved_gguf_path(self) -> Optional[Path]:
        """Абсолютный путь к GGUF-модели."""
        return resolve_path(self.gguf_path)

    @property
    def resolved_embedding_path(self) -> Optional[Path]:
        """Абсолютный путь к отдельной модели эмбеддингов (если задана)."""
        return resolve_path(self.embedding_gguf_path)

    @property
    def resolved_draft_model_path(self) -> Optional[Path]:
        """Абсолютный путь к speculative draft GGUF, если включён."""
        return resolve_path(self.draft_model_path)

    @property
    def effective_threads(self) -> Optional[int]:
        """Число потоков для llama.cpp (None = решает библиотека)."""
        if self.n_threads > 0:
            return self.n_threads
        return None


class LocalCoderModelConfig(_Section):
    """Параметры coder-роли той же локальной Qwen 4B.

    Отдельный объект сохраняется ради совместимости старых конфигов, но
    физическая модель не дублируется и не вызывает платный cloud fallback.
    """

    # Empty in a bare Settings() object so an explicitly configured
    # local_model path remains the single physical GGUF. Production
    # settings.json pins the same Qwen 4B file for this role.
    gguf_path: str = ""
    n_gpu_layers: int = 0            # 0 = CPU, -1 = все слои на GPU
    n_ctx: int = 4096
    n_threads: int = 0               # 0 = автоопределение по числу ядер
    n_batch: int = 256
    quantization: str = "Q5_K_M"
    temperature: float = 0.2         # ниже для кода — детерминированнее
    max_tokens: int = 768
    chat_format: Optional[str] = None
    verbose: bool = False

    @field_validator("n_ctx", "n_batch", "max_tokens")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("значение должно быть больше нуля")
        return value

    @field_validator("temperature")
    @classmethod
    def _sane_temperature(cls, value: float) -> float:
        if not 0.0 <= value <= 2.0:
            raise ValueError("temperature должна быть в диапазоне 0.0..2.0")
        return value

    @property
    def resolved_gguf_path(self) -> Optional[Path]:
        """Абсолютный путь к GGUF-модели."""
        return resolve_path(self.gguf_path)

    @property
    def effective_threads(self) -> Optional[int]:
        """Число потоков для llama.cpp (None = решает библиотека)."""
        if self.n_threads > 0:
            return self.n_threads
        return None


class VoiceModeConfig(_Section):
    rate: float = Field(default=1.0, ge=0.5, le=2.0)
    volume: float = Field(default=1.0, ge=0.0, le=1.0)


def _default_voice_modes() -> dict[str, VoiceModeConfig]:
    return {
        "normal": VoiceModeConfig(rate=1.00, volume=1.00),
        "focused": VoiceModeConfig(rate=1.03, volume=1.00),
        "quiet": VoiceModeConfig(rate=0.96, volume=0.72),
        "urgent": VoiceModeConfig(rate=1.04, volume=1.00),
        "amused": VoiceModeConfig(rate=1.02, volume=0.95),
        "background": VoiceModeConfig(rate=0.98, volume=0.85),
    }


class VoiceConfig(_Section):
    """Голосовой ввод-вывод. У пользователя нет микрофона -> stt выключен."""

    tts_enabled: bool = True
    #: Sprint 5: озвучивать КАЖДЫЙ ответ backend (любой канал ввода).
    tts_always_on: bool = True
    provider: str = "piper"
    language: str = "ru"
    voice: str = "ru_RU-dmitri-medium"
    fallback: str = "none"
    stt_enabled: bool = False
    # P5 §5.9: параметры STT-движка (faster-whisper, MIT).
    stt_model: str = "small"          # размер модели faster-whisper
    stt_device: str = "cpu"           # cpu / cuda
    # Индекс устройства PortAudio. Пустое значение включает авто-поиск
    # рабочего входа вместо слепого использования системного default.
    input_device_index: Optional[int] = None
    piper_model_path: str = "data/models/piper/ru_RU-dmitri-medium.onnx"
    # New production voice can be rolled out without invalidating older
    # manifests that still expose piper_model_path.  The primary path wins.
    primary_piper_model_path: str = ""
    piper_binary_path: str = "piper"
    speed: float = 1.0
    volume: float = 1.0
    piper_speaker_id: int = 0
    piper_length_scale: float = 1.0
    piper_noise_scale: float = 0.667
    piper_noise_w: float = 0.8
    piper_voices: list[dict] = Field(default_factory=list)
    modes: dict[str, VoiceModeConfig] = Field(default_factory=_default_voice_modes)

    @property
    def resolved_piper_model(self) -> Optional[Path]:
        # Legacy callers use this field as the compatibility/default path.
        return resolve_path(self.piper_model_path)

    @property
    def resolved_primary_piper_model(self) -> Optional[Path]:
        """Actual production voice path; falls back to the legacy field."""
        return resolve_path(self.primary_piper_model_path or self.piper_model_path)


class PathsConfig(_Section):
    """Каталоги данных проекта (относительные пути — от корня проекта)."""

    data_dir: str = "data"
    documents_dir: str = "data/documents"
    memory_dir: str = "data/memory"
    models_dir: str = "data/models"
    logs_dir: str = "data/logs"
    profile_dir: str = "data/profile"
    graph_dir: str = "data/graph"

    def as_dict(self) -> Dict[str, str]:
        """Плоский словарь «ключ -> путь» для ``ensure_dirs``."""
        data = self.model_dump()
        return {k: str(v) for k, v in data.items() if isinstance(v, str)}

    def resolved(self, key: str) -> Optional[Path]:
        """Абсолютный путь по ключу каталога."""
        raw = getattr(self, key, None)
        if raw is None:
            extra = self.model_extra or {}
            raw = extra.get(key)
        return resolve_path(raw) if isinstance(raw, str) else None


class PersonaConfig(_Section):
    """Личность ассистента."""

    name: str = "АТЛАС"
    address: str = ""
    persona_file: str = "persona/persona.md"
    language: str = "ru"

    @property
    def resolved_persona_file(self) -> Optional[Path]:
        return resolve_path(self.persona_file)


class LimitsConfig(_Section):
    """Лимиты и бюджеты времени."""

    short_memory_size: int = 20
    response_timeout_sec: float = 15.0
    proactive_cooldown_min: int = 30
    # P5 §5.8: реалистичный целевой бюджет локальной модели. Измеренное
    # время ответа Qwen3-4B на типовую команду ~3.2с; старый лимит 1.5с был
    # нереалистичен и ложно флагал телеметрию. Это SOFT TARGET / TELEMETRY
    # (ТЗ §4) — НЕ условие эскалации.
    local_latency_target_sec: float = 3.5
    # ПОЛЕ ОБРАТНОЙ СОВМЕСТИМОСТИ (deprecated alias): старое имя
    # ``local_latency_budget_sec`` читалось как HARD-бюджет эскалации.
    # По ТЗ §4 latency — это SOFT PERFORMANCE TARGET / TELEMETRY, а НЕ
    # условие маршрутизации. Поле оставлено для совместимости со старыми
    # settings.json; его значение НЕ влияет на выбор тира или эскалацию.
    local_latency_budget_sec: Optional[float] = None
    max_retries: int = 3
    rag_top_k: int = 3
    memory_top_k: int = 5
    max_action_iterations: int = 6
    # Разговорный FAST-тир: сбой провайдера должен признаваться БЫСТРО.
    # 3×15 c ожидания для простого «привет» неприемлемы — короткий таймаут
    # и минимум попыток, затем честный фолбэк/ошибка. Аналитические тиры
    # (analyst/coder/architect) остаются на общих response_timeout_sec/max_retries.
    fast_tier_timeout_sec: float = 7.0
    fast_tier_max_retries: int = 2
    # Sprint 3 TIER 3: глубокие тиры (coder/architect) — качество важнее
    # скорости, им нужен щедрый бюджет (30-60 c), а не общий 15-секундный.
    deep_tier_timeout_sec: float = 45.0
    # Sprint 3 STEP 3: executor safety — таймауты инструментов по категориям.
    tool_timeout_file_sec: float = 10.0
    tool_timeout_web_sec: float = 30.0
    tool_timeout_system_sec: float = 5.0
    # S4: браузер и CUA — отдельный класс. Раньше браузерные инструменты
    # делили веб-бюджет, а computer_* попадали в файловый (10 c): навигация
    # со скриптами и физический ввод не укладываются в такой потолок и
    # получали ложный таймаут. Класс явный, чтобы бюджет не выводился из
    # соседнего.
    tool_timeout_browser_sec: float = 60.0
    # S6: потолок одной записи write_file в байтах. Раньше инструмент
    # принимал произвольный объём; непригодное значение в настройках
    # деградирует к модулю-дефолту (2 МБ), а не к безлимитной записи.
    max_write_bytes: int = 2 * 1024 * 1024
    #: Максимум параллельных вызовов инструментов (изоляция ресурсов).
    max_parallel_tools: int = 4
    #: Потолок вывода одного инструмента, байты (50 KB; больше — усечение).
    tool_output_max_bytes: int = 50 * 1024
    # Sprint 4 — bounded memory:
    #: Сообщений в session memory (10 пар user/assistant; FIFO).
    session_memory_messages: int = 20
    #: Контекстные бюджеты, токены (оценка ~3 символа/токен).
    context_budget_fast_tokens: int = 2000    # TIER 1: разговор
    conversation_max_tokens: int = 32        # bounded CPU generation for chat
    context_budget_plan_tokens: int = 4000    # TIER 2: планирование
    context_budget_deep_tokens: int = 8000    # TIER 3: research/coding

    @field_validator("session_memory_messages")
    @classmethod
    def _non_negative_session(cls, value: int) -> int:
        if value < 0:
            raise ValueError("не может быть отрицательным")
        return value

    @field_validator("context_budget_fast_tokens", "context_budget_plan_tokens",
                     "context_budget_deep_tokens")
    @classmethod
    def _positive_budget(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("бюджет должен быть больше нуля")
        return value

    @field_validator("short_memory_size", "max_retries", "rag_top_k",
                     "memory_top_k", "max_action_iterations", "fast_tier_max_retries")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("значение не может быть отрицательным")
        return value

    @field_validator("response_timeout_sec", "fast_tier_timeout_sec",
                     "deep_tier_timeout_sec", "tool_timeout_file_sec",
                     "tool_timeout_web_sec", "tool_timeout_system_sec",
                     "tool_timeout_browser_sec")
    @classmethod
    def _positive_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("таймаут должен быть больше нуля")
        return value

    @field_validator("max_parallel_tools", "tool_output_max_bytes", "max_write_bytes")
    @classmethod
    def _positive_limit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("лимит должен быть больше нуля")
        return value


class LatencyBudgetConfig(_Section):
    """First-class performance budgets for the Wave 0 gates (milliseconds)."""

    fast_p50_ms: float = 600.0
    fast_p95_ms: float = 1000.0
    fast_hard_max_ms: float = 1500.0
    deliberate_first_progress_p95_ms: float = 2500.0
    deliberate_p50_ms: float = 8000.0
    deliberate_p95_ms: float = 15000.0
    research_first_progress_p95_ms: float = 3000.0
    research_source_timeout_ms: float = 8000.0
    background_enqueue_p95_ms: float = 100.0


class LoggingConfig(_Section):
    """Настройки логирования."""

    level: str = "INFO"
    console: bool = True
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 5

    @field_validator("level")
    @classmethod
    def _known_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = str(value).strip().upper()
        if upper not in allowed:
            raise ValueError(f"level должен быть одним из {sorted(allowed)}")
        return upper


class ProxyConfig(_Section):
    """Локальный proxy-режим LLM (П1 §1.4) — ключ автора НЕ в клиенте.

    В proxy-режиме клиент шлёт запрос на ЛОКАЛЬНЫЙ proxy-сервер
    (``endpoint``), а не напрямую провайдеру. Ключ автора добавляется
    СЕРВЕРОМ при форвардинге. В заголовках клиента — только локальный
    ``proxy_token`` (маркер доступа к proxy), НЕ ключ провайдера.
    Сам proxy-сервер — отдельная инфра-задача (docs/P1_BLOCKERS.md,
    BLOCKER-1); здесь только клиентская конфигурация.
    """

    enabled: bool = False
    endpoint: str = ""           # http://127.0.0.1:8787/v1
    proxy_token: str = ""       # локальный маркер доступа к proxy (НЕ ключ провайдера)


class LauncherConfig(_Section):
    """Sprint 5: запуск приложения — автостарт, hotkey, backend-процесс.

    Секция читается и Python'ом (приветствие/TTS), и Tauri/Rust
    (реестр автозапуска, глобальный hotkey, spawn python-backend).
    """

    #: Автозагрузка Windows (HKCU\\...\\Run) — включает фронтенд при старте.
    autostart: bool = False
    #: Глобальный hotkey (Tauri globalShortcut; работает и в полноэкранных играх).
    hotkey: str = "Ctrl+Space"
    #: Команда запуска python-backend (Tauri поднимает её при старте).
    backend_command: List[str] = Field(default_factory=lambda: [
        "python", "-m", "core.ws_server",
    ])
    #: Рабочая директория backend (корень проекта).
    backend_workdir: str = ""
    #: Приветствие при старте сессии (голос + текст).
    greeting_enabled: bool = False

class STTConfig(_Section):
    enabled: bool = False
    model: str = "faster-whisper-small"
    vad: str = "webrtc"
    language: str = "auto"
    hotkey_mode: str = "hold_ctrl_space"

class WakeWordConfig(_Section):
    enabled: bool = False
    phrase: str = "ATLAS"
    sensitivity: float = 0.5


class ShadowConfig(_Section):
    """Local, opt-in settings for Sprint 8 Shadow Engine.

    It is opt-in because command history and screen summaries are personal
    data. Enabling it never grants screen-capture permission by itself.
    """

    enabled: bool = False
    auto_generate: bool = True
    interval_sec: int = 300
    code_model_path: str = "data/models/qwen3-4b-instruct-q5_k_m.gguf"

    @field_validator("interval_sec")
    @classmethod
    def _valid_interval(cls, value: int) -> int:
        if value < 30:
            raise ValueError("interval_sec должен быть не меньше 30")
        return value

    @property
    def resolved_code_model_path(self) -> Optional[Path]:
        return resolve_path(self.code_model_path)


class BrainPolicyConfig(_Section):
    """Provider-independent model routing policy (safe/local by default)."""

    mode: str = "BALANCED"
    prefer_local: bool = True
    allow_cloud: bool = False
    allow_sensitive_cloud: bool = False
    background_allow_cloud: bool = False
    max_fallbacks: int = 2
    failure_timeout_seconds: float = 3.0
    max_cost_tier: int = 3
    providers_path: str = "data/brain/providers.json"

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, value: str) -> str:
        normalized = str(value).strip().upper()
        if normalized not in {"LOCAL_ONLY", "BALANCED", "QUALITY", "SPEED", "CUSTOM"}:
            raise ValueError("unknown brain policy mode")
        return normalized

    @field_validator("max_fallbacks", "max_cost_tier")
    @classmethod
    def _non_negative_brain_limit(cls, value: int) -> int:
        if value < 0:
            raise ValueError("brain policy limit must be non-negative")
        return value

    @field_validator("failure_timeout_seconds")
    @classmethod
    def _positive_brain_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("failure_timeout_seconds must be positive")
        return value

    @property
    def resolved_providers_path(self) -> Optional[Path]:
        return resolve_path(self.providers_path)


class CredentialStoreConfig(_Section):
    """Central credential indirection used by production and future backends."""

    provider: str = "deepinfra"
    reference: str = "DEEPINFRA_API_KEY"
    backend: str = "dpapi"
    path: str = "data/brain/provider-secrets.dpapi"
    required_in_production: bool = True


# --------------------------------------------------------------------------- #
#  Корневая модель
# --------------------------------------------------------------------------- #

class Settings(BaseModel):
    """Полная конфигурация Джарвиса."""

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        protected_namespaces=(),  # разрешаем поле model_tiers без warning
    )

    api_keys: ApiKeys = Field(default_factory=ApiKeys)
    api_endpoints: ApiEndpoints = Field(default_factory=ApiEndpoints)
    model_tiers: ModelTiers = Field(default_factory=ModelTiers)
    tier_providers: TierProviders = Field(default_factory=TierProviders)

    #: Главный логический тир для bare Settings()/library compatibility.
    #: Production config/settings.json overrides this to the local FAST path.
    primary_brain: str = "analyst"
    #: Physical local model family.  ``qwen`` keeps library compatibility;
    #: the JARVIS 4 production profile sets ``ministral``.
    model_family: str = "qwen"
    #: Production config enables local-only mode explicitly. Bare Settings()
    #: remains backwards-compatible for provider/router tests and integrations.
    offline_mode: bool = False
    #: Load the local FAST model in a daemon warmup thread at service start.
    #: False for bare Settings() keeps unit tests and library imports cheap.
    warmup_local_on_start: bool = False
    #: Download a missing, pinned local GGUF in the warmup thread.  The
    #: manifest is HTTPS-only and SHA-256 verified; an existing user model is
    #: never replaced just because a newer profile is recommended.
    auto_download_models: bool = True
    #: Cloud-only приватная сборка: локальная GGUF-LLM погашена флагом.
    #: Файлы core/llm/local_qwen.py и llama_server.py остаются в репозитории
    #: и возвращаются в pro-версии значением True. Голос (Piper/whisper),
    #: wake word, ChromaDB и sentence-transformer роутера — не LLM и живут
    #: локально независимо от этого флага.
    local_llm_enabled: bool = False
    #: Production migration switch: one DeepInfra brain owns dialogue,
    #: planning and tool decisions. Local GGUF is not a fallback in this mode.
    deepseek_brain_mode: bool = False
    deepseek_model: str = "deepseek-ai/DeepSeek-V4-Flash-0731"
    deepseek_provider: str = "deepinfra"

    local_model: LocalModelConfig = Field(default_factory=LocalModelConfig)
    local_coder_model: LocalCoderModelConfig = Field(default_factory=LocalCoderModelConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    latency_budgets: LatencyBudgetConfig = Field(default_factory=LatencyBudgetConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    launcher: LauncherConfig = Field(default_factory=LauncherConfig)
    stt: STTConfig = Field(default_factory=STTConfig)
    wake_word: WakeWordConfig = Field(default_factory=WakeWordConfig)
    shadow: ShadowConfig = Field(default_factory=ShadowConfig)
    brain_policy: BrainPolicyConfig = Field(default_factory=BrainPolicyConfig)
    credential_store: CredentialStoreConfig = Field(default_factory=CredentialStoreConfig)
    system_triggers: List[Dict[str, Any]] = Field(default_factory=list)

    #: Откуда конфиг был загружен (не сериализуется в JSON).
    source_path: Optional[Path] = Field(default=None, exclude=True)

    # ---------------------- доступ к параметрам ---------------------------- #

    def get_api_key(self, provider: str) -> Optional[str]:
        """API-ключ провайдера.

        Порядок поиска (C2, мост D1): переменная окружения
        ``JARVIS_<PROVIDER>_API_KEY``, затем ``<PROVIDER>_API_KEY``, затем
        settings.json, затем DPAPI-хранилище — тот же credential store, из
        которого ключ читают провайдеры мозга (``core/brain/providers.py``).
        До C2 разрыв этой цепочки делал ``is_tier_available`` слепым к
        установленному ключу, и роутер молча уводил всё на локальную модель.

        Отсутствие ключа — не ошибка: возвращается ``None``.
        """
        name = provider.strip().lower()
        if name == LOCAL_PROVIDER:
            return None  # локальной модели ключ не нужен

        for env_name in (f"JARVIS_{name.upper()}_API_KEY", f"{name.upper()}_API_KEY"):
            env_value = os.environ.get(env_name, "").strip()
            if env_value:
                return env_value

        from_settings = self.api_keys.get(name)
        if from_settings:
            return from_settings

        return self._dpapi_key(name)

    def api_key_source(self, provider: str) -> str:
        """C3: откуда реально берётся ключ — для provider_effective.

        Возвращает "env" | "settings.json" | "dpapi" | "missing". Та же
        цепочка приоритетов, что и в :meth:`get_api_key`, без значения.
        """
        name = provider.strip().lower()
        if name == LOCAL_PROVIDER:
            return "missing"
        for env_name in (f"JARVIS_{name.upper()}_API_KEY", f"{name.upper()}_API_KEY"):
            if os.environ.get(env_name, "").strip():
                return "env"
        if (self.api_keys.get(name) or "").strip():
            return "settings.json"
        if (self._dpapi_key(name) or "").strip():
            return "dpapi"
        return "missing"

    def _credential_store_path(self) -> Optional[Path]:
        """Абсолютный путь к DPAPI-хранилищу ключей (как его строит bootstrap)."""
        store = getattr(self, "credential_store", None)
        raw = str(getattr(store, "path", "") or "data/brain/provider-secrets.dpapi")
        path = Path(raw)
        if not path.is_absolute():
            path = Path(self.data_dir).parent / path
        return path

    def _dpapi_reference(self, provider: str) -> str:
        """Ссылка ключа провайдера внутри DPAPI-хранилища."""
        store = getattr(self, "credential_store", None)
        default_provider = str(getattr(store, "provider", "") or "").strip().lower()
        if provider == default_provider:
            return str(getattr(store, "reference", "") or "").strip()
        return f"{provider.upper()}_API_KEY"

    def _dpapi_key(self, provider: str) -> Optional[str]:
        """Читает ключ провайдера из DPAPI-хранилища, молча деградируя.

        Любая проблема (не Windows, файла нет, расшифровка не удалась) — это
        «ключа нет», а не сбой: ровно одно предупреждение за процесс, чтобы
        не шуметь на горячем пути маршрутизации.
        """
        global _DPAPI_KEY_WARNING_SHOWN
        reference = self._dpapi_reference(provider)
        if not reference:
            return None
        path = self._credential_store_path()
        if path is None or not path.is_file():
            return None
        cache_key = (str(path), reference, path.stat().st_mtime_ns)
        cached = _DPAPI_KEY_CACHE.get(cache_key)
        if cached is not None:
            return cached or None
        try:
            from core.brain.secrets import DPAPISecretStore
            value = (DPAPISecretStore(path).get(reference) or "").strip()
        except Exception as exc:
            if not _DPAPI_KEY_WARNING_SHOWN:
                _DPAPI_KEY_WARNING_SHOWN = True
                log.warning(
                    "DPAPI credential store недоступен (%s); ключ провайдера '%s' "
                    "не читается — считаем его отсутствующим.", type(exc).__name__, provider,
                )
            return None
        _DPAPI_KEY_CACHE[cache_key] = value
        if not value and not _DPAPI_KEY_WARNING_SHOWN:
            _DPAPI_KEY_WARNING_SHOWN = True
            log.warning(
                "В DPAPI-хранилище %s нет записи '%s' — провайдер '%s' без ключа.",
                path.name, reference, provider,
            )
        return value or None

    def get_endpoint(self, provider: str) -> Optional[str]:
        """Базовый URL провайдера (без завершающего слэша)."""
        name = provider.strip().lower()
        if name == LOCAL_PROVIDER:
            return None
        env_value = os.environ.get(f"JARVIS_{name.upper()}_BASE_URL", "").strip()
        if env_value:
            return env_value.rstrip("/")
        return self.api_endpoints.get(name)

    def get_model_id(self, tier: Union[str, "Tier"]) -> Optional[str]:
        """Реальный model-id для тира из ``model_tiers``."""
        from core.llm.tiers import resolve_tier, tier_to_backend_key
        key = tier_to_backend_key(resolve_tier(str(tier)))
        return self.model_tiers.get(key)

    def get_provider(self, tier: Union[str, "Tier"]) -> str:
        """Провайдер, обслуживающий тир ('local' / 'deepseek' / ...)."""
        if self.offline_mode:
            return LOCAL_PROVIDER
        from core.llm.tiers import resolve_tier, tier_to_backend_key
        key = tier_to_backend_key(resolve_tier(str(tier)))
        return self.tier_providers.get(key)

    def get_local_config(self, tier: Union[str, "Tier"]):
        """Возвращает конфиг локальной модели для указанного тира."""
        from core.llm.tiers import resolve_tier, tier_to_backend_key
        resolved = resolve_tier(str(tier))
        key = tier_to_backend_key(resolved)
        if key == "coder":
            return self.local_coder_model
        return self.local_model

    def is_tier_available(self, tier: Union[str, "Tier"]) -> bool:
        """Доступен ли тир прямо сейчас.

        * локальный тир — доступен, если существует файл GGUF;
        * удалённый тир — доступен, если заданы и ключ, и endpoint, и model-id.

        Совет мудрецов обязан вызывать этот метод перед эскалацией.
        """
        from core.llm.tiers import resolve_tier
        resolved = resolve_tier(str(tier))
        provider = self.get_provider(resolved)
        model_id = self.get_model_id(resolved)

        if provider == LOCAL_PROVIDER:
            # C1: в cloud-only сборке локальные тиры недоступны независимо
            # от наличия файла GGUF на диске.
            if not bool(getattr(self, "local_llm_enabled", False)):
                return False
            local_cfg = self.get_local_config(resolved)
            gguf = local_cfg.resolved_gguf_path
            return bool(gguf and gguf.is_file())

        if not model_id:
            log.debug("Тир %s недоступен: не задан model-id", resolved)
            return False
        if not self.get_endpoint(provider):
            log.debug("Тир %s недоступен: не задан endpoint провайдера %s", resolved, provider)
            return False
        if not self.get_api_key(provider):
            log.debug("Тир %s недоступен: нет API-ключа провайдера %s", resolved, provider)
            return False
        return True

    def available_tiers(self) -> List["Tier"]:
        """Список доступных тиров в порядке эскалации."""
        from core.llm.tiers import Tier
        return [tier for tier in Tier if self.is_tier_available(tier)]

    def warn_about_missing_keys(self) -> List[str]:
        """Логирует WARNING по каждому недоступному тиру.

        Вызывается один раз при старте. Возвращает список предупреждений,
        чтобы GUI мог показать их пользователю.
        """
        from core.llm.tiers import Tier
        warnings: List[str] = []
        for tier in Tier:
            if self.is_tier_available(tier):
                continue
            provider = self.get_provider(tier)
            if provider == LOCAL_PROVIDER:
                local_cfg = self.get_local_config(tier)
                gguf = local_cfg.resolved_gguf_path
                message = (
                    f"Тир '{tier.value}' (локальная модель) недоступен: "
                    f"файл GGUF не найден по пути {gguf}"
                )
            elif not self.get_api_key(provider):
                message = (
                    f"Тир '{tier.value}' недоступен: не задан API-ключ "
                    f"провайдера '{provider}' (api_keys.{provider})"
                )
            else:
                message = (
                    f"Тир '{tier.value}' недоступен: проверьте endpoint и model-id "
                    f"для провайдера '{provider}'"
                )
            warnings.append(message)
            log.warning(message)
        if not warnings:
            log.info("Все тиры совета мудрецов доступны")
        return warnings

    # ---------------------- пути и каталоги -------------------------------- #

    @property
    def data_dir(self) -> Path:
        return self.paths.resolved("data_dir") or (PROJECT_ROOT / "data")

    @property
    def documents_dir(self) -> Path:
        return self.paths.resolved("documents_dir") or (self.data_dir / "documents")

    @property
    def memory_dir(self) -> Path:
        return self.paths.resolved("memory_dir") or (self.data_dir / "memory")

    @property
    def models_dir(self) -> Path:
        return self.paths.resolved("models_dir") or (self.data_dir / "models")

    @property
    def logs_dir(self) -> Path:
        return self.paths.resolved("logs_dir") or (self.data_dir / "logs")

    @property
    def profile_dir(self) -> Path:
        return self.paths.resolved("profile_dir") or (self.data_dir / "profile")

    @property
    def graph_dir(self) -> Path:
        return self.paths.resolved("graph_dir") or (self.data_dir / "graph")

    def ensure_directories(self) -> Dict[str, Path]:
        """Создаёт все каталоги данных. Возвращает «ключ -> путь»."""
        return ensure_dirs(self.paths.as_dict())

    # ---------------------- сериализация ----------------------------------- #

    def to_dict(self) -> Dict[str, Any]:
        """Конфиг в виде словаря, готового к записи в JSON."""
        return self.model_dump(mode="json", exclude_none=False)

    def save_config(self, path: Optional[Union[str, Path]] = None) -> Path:
        """Атомарно сохраняет конфиг (tmp-файл в том же каталоге + rename).

        Args:
            path: куда писать. По умолчанию — файл, из которого конфиг был
                загружен, иначе ``config/settings.json``.

        Returns:
            Путь к сохранённому файлу.

        Raises:
            ConfigError: если запись не удалась.
        """
        target = Path(path) if path else (self.source_path or (PROJECT_ROOT / "config" / CONFIG_FILENAME))
        ensure_parent(target)
        tmp = target.with_suffix(target.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(target)
            log.info("Конфигурация сохранена: %s", target)
            return target
        except (OSError, json.JSONDecodeError) as exc:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            log.error("Не удалось сохранить конфиг: %s", exc)
            raise ConfigError(f"Ошибка сохранения конфигурации: {exc}") from exc

    @classmethod
    def load_config(cls, path: Optional[Union[str, Path]] = None) -> "Settings":
        """Загружает конфиг из JSON-файла."""
        target = Path(path) if path else (PROJECT_ROOT / "config" / CONFIG_FILENAME)
        if not target.exists():
            raise ConfigError(f"Файл конфигурации не найден: {target}")
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            # Миграция deprecated alias (ТЗ §4): latency — это SOFT TARGET,
            # а не HARD-бюджет эскалации. Старое имя local_latency_budget_sec
            # переносим в новое local_latency_target_sec, если новое не задано.
            if "local_latency_budget_sec" in data and "local_latency_target_sec" not in data:
                data["local_latency_target_sec"] = data.pop("local_latency_budget_sec")
            settings = cls.model_validate(data)
            settings.source_path = target
            log.info("Конфигурация загружена: %s", target)
            return settings
        except (json.JSONDecodeError, ValidationError) as exc:
            log.error("Ошибка валидации конфигурации: %s", exc)
            raise ConfigError(f"Неверный формат конфигурации: {exc}") from exc


def load_config(path: Optional[Union[str, Path]] = None) -> Settings:
    """Публичный хелпер: загрузить конфиг (создаст дефолт, если файла нет)."""
    target = Path(path) if path else (PROJECT_ROOT / "config" / CONFIG_FILENAME)
    if not target.exists():
        # Создаём дефолтный конфиг
        default = Settings()
        default.save_config(target)
        log.info("Создан дефолтный конфиг: %s", target)
        return default
    return Settings.load_config(target)


def default_config_path() -> Path:
    """Путь к рабочему файлу конфигурации."""
    return PROJECT_ROOT / "config" / CONFIG_FILENAME


def example_config_path() -> Path:
    """Путь к шаблону конфигурации."""
    return PROJECT_ROOT / "config" / EXAMPLE_FILENAME
