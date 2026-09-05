"""Agent — контроллер миссии J.A.R.V.I.S. (§3, §8, §9, §10, §11, §14).

Полный цикл ОДНОЙ задачи пользователя:

    USER GOAL -> INTENT -> RISK -> CONTEXT -> MODE -> PLAN
              -> TOOL RETRIEVAL -> EXECUTION -> VERIFICATION
              -> REPAIR (если ошибка) -> RESULT -> MEMORY / SKILL

Ключевые принципы ТЗ:

    * §3  Цикл САМ СОКРАЩАЕТСЯ для простых задач. "Открой Telegram" не
          запускает multi-agent workflow — идёт FAST PATH.
    * §4  НЕТ искусственного лимита мышления. В этом модуле нет ни одной
          проверки вида "elapsed > N -> fail". Реальные таймауты живут
          только внутри инструментов (сеть/процесс).
    * §5  ACKNOWLEDGING — отдельная быстрая стадия, без тяжёлой модели.
    * §8  UNKNOWN != IMPOSSIBLE. Нет инструмента -> исследуем и строим метод.
    * §10 Ошибка -> диагноз -> патч -> retry, а не капитуляция.
    * §14 "Готово" говорим ТОЛЬКО после фактической верификации.
    * §21 HIGH risk -> запрос подтверждения, а не тихое выполнение.

Модуль переиспользует существующие компоненты (§26 — эволюция, не reset):
    core.actions      — инструменты и исполнитель
    core.router       — CouncilRouter / keyword intent
    core.task_runtime — Mission / события / статусы
    core.repair       — RepairLoop
    core.verifier     — фактические проверки
    core.skill_forge  — навыки
    core.ingest       — большие входы
"""

from __future__ import annotations

import re
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

import uuid

from config.settings import Settings
from core.actions import DEFAULT_REGISTRY
from core.actions.base import ActionResult, ToolContext
from core.actions.executor import ToolExecutor, execute_tool
from core.capabilities import CAPABILITIES, Capability, describe_tools_for_model
from core.ingest import ingest_text
from core.memory.knowledge_graph import GraphMemoryStore
from core.llm import (
    BackendConfigError,
    BackendUnavailable,
    Tier,
    ToolsNotSupportedError,
    breaker,
    get_llm_backend,
    get_offline_backend,
)
from core.memory.budget import fit_messages_to_budget
from core.memory.facts import detect_tone, learn_facts
from core.memory.profile import get_relevant_profile_context
from core.memory.relationship import MemoryHierarchy, PreferenceLearner, RelationshipMemoryStore
from core.memory.short_term import SessionManager
from core.model_router import ModelRouter, RoutingDecision
from core.personality import PersonalityEngine
from core.repair import RepairLoop
from core.research import ResearchEngine
from core.routing.semantic_router import (
    RoutingContext as SemanticRoutingContext,
    RoutingDecision as SemanticRoutingDecision,
    intent_category as semantic_intent_category,
    route as semantic_route,
    split_compound_by_decision,
)
from core.router.route_guard import validate_tool_selection
from core.safety import RiskAssessment, assess_risk
from core.redact import redact_args
from core.skill_forge import SkillForge, SkillManifest, SkillStatus
from core.structured import (
    PLAN_SCHEMA_HINT, AnswerStreamExtractor, ToolCallDecision,
    parse_structured, validate_tool_call,
)
from core.task_runtime import (
    EVENT_CONFIRMATION_REQUIRED,
    EVENT_PLAN_READY,
    EVENT_REPAIR_COMPLETED,
    EVENT_REPAIR_STARTED,
    EVENT_STREAM_CHUNK,
    EVENT_STEP_COMPLETED,
    EVENT_STEP_STARTED,
    EVENT_TASK_FAILED,
    EVENT_TOOL_CALLED,
    EVENT_TOOL_RESULT,
    EVENT_VERIFICATION,
    Mission,
    MissionStatus,
)
from core.utils.logger import get_logger
from core.verifier import VerificationResult, verify_action_result
from core.executive import ExecutiveMind
from core.intelligence import ResearchPending, SkillCatalog, TeachingSession, TutorEngine, UniversalIntake

__all__ = [
    "Agent",
    "AgentConfig",
    "AgentOutcome",
    "ACK_PHRASES",
    "pick_acknowledgement",
    "render_structured_fact",
    "verify_atomic_values_preserved",
]

log = get_logger(__name__)

#: Префикс ошибки планирования «модель недоступна» (сеть/провайдер/ключ).
#: Такая ошибка — НЕ «не умею» (§29): это временный сбой инфраструктуры,
#: и пользователь должен получить короткую честную фразу без сырых деталей
#: и без создания черновика навыка.
MODEL_ERROR_PREFIX = "model_error:"

_MEDIA_NETWORK_MARKERS = (
    "youtube", "ютуб", "spotify", "спотифай", "в сети", "онлайн",
    "в интернете", "internet", "online",
)


def _media_network_is_explicit(goal: str) -> bool:
    lowered = " ".join((goal or "").casefold().split())
    return any(marker in lowered for marker in _MEDIA_NETWORK_MARKERS)


def _media_query_is_user_text(goal: str, query: str) -> bool:
    """Accept a network query only when its words occur in the user goal."""
    goal_words = {word for word in re.findall(r"[\wа-яё]+", (goal or "").casefold()) if len(word) > 2}
    query_words = {word for word in re.findall(r"[\wа-яё]+", (query or "").casefold()) if len(word) > 2}
    return bool(query_words) and query_words <= goal_words

#: Prefix retained for routing internal model failures. The visible response
#: is built from the exact exception in ``_handle_model_unavailable``.
MODEL_UNAVAILABLE_TEXT = "Ошибка локальной модели:"


def _hide_reasoning_stream(cumulative: str) -> str:
    """Прячет служебные ``<think>``-блоки из НАРАСТАЮЩЕГО текста стрима.

    Закрытый блок вырезается целиком; незакрытый (модель ещё думает) —
    скрывает всё после его начала. Применяется только в прямом разговорном
    режиме (Sprint 2), где пользователю идёт сырой текст модели.
    """
    out = re.sub(r"<think>.*?</think>", "", cumulative, flags=re.DOTALL | re.IGNORECASE)
    if "<think>" in out.lower():
        idx = out.lower().rfind("<think>")
        out = out[:idx]
    return out


def render_structured_fact(data: Any) -> str:
    """Детерминированное представление структурированного факта без вызова LLM."""
    if isinstance(data, str):
        return data.strip()
    if not isinstance(data, Mapping):
        return str(data) if data is not None else ""

    if data.get("summary"):
        return str(data["summary"]).strip()
    if data.get("text"):
        return str(data["text"]).strip()

    # Currency
    if "rates" in data or "currency" in data or "target" in data:
        curr = data.get("currency") or data.get("target") or "USD"
        val = data.get("value")
        unit = data.get("unit", "₽")
        src = data.get("source", "open.er-api.com")
        at = data.get("fetched_at")
        at_part = f", {at}" if at else ""
        if val is not None:
            return f"Курс {curr}: {val} {unit} (источник: {src}{at_part})."
        return f"Курсы валют получены (источник: {src}{at_part})."

    # Weather
    if "current" in data or data.get("unit") == "°C" or "location" in data:
        val = data.get("value")
        unit = data.get("unit", "°C")
        loc = data.get("location")
        loc_name = loc.get("name") if isinstance(loc, Mapping) else "городе"
        cur = data.get("current")
        cond = f", {cur.get('weather')}" if isinstance(cur, Mapping) and cur.get("weather") else ""
        src = data.get("source", "open-meteo.com")
        if val is not None:
            return f"Погода в {loc_name}: сейчас {val}{unit}{cond} (источник: {src})."

    if data.get("value") is not None:
        subj = data.get("subject", "Данные")
        unit = f" {data['unit']}" if data.get("unit") else ""
        return f"{subj}: {data['value']}{unit}."

    return str(dict(data))


def verify_atomic_values_preserved(text: str, evidence: Any) -> bool:
    """Детерминированная проверка сохранения атомарных значений факта в тексте финализатора."""
    if not text or not isinstance(text, str):
        return False
    if not isinstance(evidence, Mapping):
        return True

    text_lower = text.casefold()

    # 1. Проверка числового значения (с допуском форматирования, e.g. 80.0, 80,0, 80)
    if "value" in evidence and evidence["value"] is not None:
        try:
            target_val = float(evidence["value"])
            raw_nums = re.findall(r"(?:\b|\b[-+])\d+(?:[.,]\d+)?\b", text)
            found_num = False
            for rn in raw_nums:
                try:
                    num_f = float(rn.replace(",", "."))
                    if abs(num_f - target_val) < 0.05:
                        found_num = True
                        break
                except ValueError:
                    continue
            if not found_num and target_val.is_integer():
                int_str = str(int(target_val))
                if re.search(rf"\b{int_str}\b", text):
                    found_num = True
            if not found_num:
                return False
        except (ValueError, TypeError):
            pass

    # 2. Проверка валюты (код или наименование)
    currency = evidence.get("currency") or evidence.get("target")
    if currency:
        curr_lower = str(currency).casefold()
        curr_aliases = [curr_lower]
        if curr_lower == "usd":
            curr_aliases.extend(["доллар", "$"])
        elif curr_lower == "eur":
            curr_aliases.extend(["евро", "€"])
        elif curr_lower == "cny":
            curr_aliases.extend(["юан", "¥"])
        elif curr_lower == "kzt":
            curr_aliases.extend(["тенге", "₸"])
        if not any(alias in text_lower for alias in curr_aliases):
            return False

    # 3. Проверка единицы измерения (символ или словесное обозначение)
    unit = evidence.get("unit")
    if unit:
        unit_lower = str(unit).casefold()
        unit_aliases = [unit_lower]
        if unit_lower == "₽":
            unit_aliases.extend(["руб", "rub", "р."])
        elif unit_lower in ("°c", "°"):
            unit_aliases.extend(["°c", "°", "град", "c"])
        if not any(alias in text_lower for alias in unit_aliases):
            return False

    return True


# --------------------------------------------------------------------------- #
#  §5 — FIRST ACKNOWLEDGEMENT (быстро, без тяжёлой модели)
# --------------------------------------------------------------------------- #

ACK_PHRASES: Dict[str, str] = {
    "app": "Принято.",
    "file": "Сейчас проверю.",
    "web": "Разбираюсь.",
    "browser": "Понял.",
    "system": "Принято.",
    "media": "Понял.",
    "none": "Понял. Разбираюсь.",
}


def pick_acknowledgement(intent: str, goal: str = "",
                          settings: Optional["Settings"] = None,
                          *, allow_model: bool = True) -> str:
    """Мгновенное, ЖИВОЕ подтверждение приёма задачи (§5, П1 §1.2).

    Базовая фраза выбирается по намерению (как раньше — мгновенно),
    затем, если доступна ЛОКАЛЬНАЯ модель (Qwen 4B на лице) и задан
    контекст цели, фраза ОБОГАЩАЕТСЯ коротким контекстным вариантом
    от модели (чтобы ACK звучал как живая сущность, а не робот-заглушка).

    ЖЁСТКИЙ FALLBACK (критично для офлайн-тестов и надёжности): при
    любой ошибке/недоступности локальной модели, таймауте или пустом
    ответе — возвращается базовая canned-фраза по intent. Стадия
    ACKNOWLEDGING остаётся мгновенной и НИКОГДА не падает.
    """
    base = ACK_PHRASES.get(intent, ACK_PHRASES["none"])

    # The live mission path passes ``allow_model=False``.  An ACK is a
    # transport guarantee, not a second inference request: loading a GGUF or
    # waiting on a provider here is exactly the minute-long pause reported by
    # the real UI.  The opt-in model enrichment remains for callers that
    # explicitly want it (and for the existing presentation tests).
    if not allow_model or not goal or not goal.strip() or settings is None:
        return base

    try:
        from core.llm import get_llm_backend, Tier
        from core.llm.backend import BackendUnavailable, BackendConfigError

        backend = get_llm_backend(settings, Tier.FAST)
        prompt = (
            f"Ты — АТЛАС. Пользователь только что дал команду: \"{goal}\". "
            f"Сгенерируй ОДНУ короткую (до 5 слов) фразу подтверждения приёма "
            f"в стиле живого дворецкого, без приветствий и пояснений. "
            f"Не начинай выполнять задачу. Только подтверди, что понял. "
            f"Примеры стиля: \"{base}\", \"Уже смотрю.\", \"Слушаю.\""
        )
        ack = backend.direct(prompt, max_tokens=24, temperature=0.4)
        ack = (ack or "").strip().strip('"\'‘’“”')
        # Фильтруем сырьё: если модель вернула мусор/JSON/слишком длинно —
        # откатываемся на базу (чтобы голос не читал сырьё).
        if not ack or len(ack) > 40 or ack.startswith("{") or "\n" in ack:
            return base
        return ack
    except (BackendUnavailable, BackendConfigError, Exception) as _exc:  # noqa: BLE001
        # Любой сбой локальной модели — мгновенный откат на базовую фразу.
        log.debug("ACK LLM-генерация недоступна, fallback на canned: %s", _exc)
        return base


# --------------------------------------------------------------------------- #
#  Конфигурация и результат
# --------------------------------------------------------------------------- #

@dataclass
class AgentConfig:
    """Настройки поведения агента (без единого лимита на время §4)."""

    max_tool_retrieval: int = 5        # сколько инструментов показываем модели (§12)
    max_discovered_tools: int = 8      # schemas after full-surface discovery
    max_plan_steps: int = 8            # разумный потолок шагов плана
    max_repair_attempts: int = 3       # §11 — ограниченный, но не единичный repair
    max_structured_retries: int = 2    # §13 — повторный запрос при плохом JSON
    # Жёсткий потолок вызовов инструментов за ОДИН пользовательский запрос
    # (составные команды и repair итого). Раньше settings.limits.
    # max_action_iterations существовал в конфиге, но нигде не применялся —
    # автономный цикл мог крутиться без жёсткого предела (дыра B1).
    max_action_iterations: int = 6
    large_input_chars: int = 6000      # §7 — выше порога включаем ingest
    auto_confirm_high_risk: bool = False   # §21 — HIGH требует человека
    enable_skill_forge: bool = True    # §9
    confirmation_timeout_sec: float = 30.0  # П1 §1.3: таймаут ожидания подтверждения -> авто-reject


@dataclass
class AgentOutcome:
    """Итог работы агента по миссии."""

    text: str
    verified: bool = False
    verification: Optional[VerificationResult] = None
    tool_used: Optional[str] = None
    needs_confirmation: bool = False
    confirmation_id: Optional[str] = None
    risk: Optional[RiskAssessment] = None
    mode: str = "conversation"
    trace: List[str] = field(default_factory=list)
    #: Sprint 3 STEP 5: ответ пришёл с фолбэк-модели, а не с основной
    #: (quality indicator для UI/TTS — не маскируем деградацию).
    degraded: bool = False
    #: Internal observed result for bounded multi-step planning. Never sent to UI.
    action_result: Optional[ActionResult] = field(default=None, repr=False)
    latency_stages: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "verified": self.verified,
            "verification": self.verification.to_dict() if self.verification else None,
            "tool_used": self.tool_used,
            "needs_confirmation": self.needs_confirmation,
            "confirmation_id": self.confirmation_id,
            "risk": self.risk.to_dict() if self.risk else None,
            "mode": self.mode,
            "trace": list(self.trace),
            "degraded": self.degraded,
            "latency_stages": dict(self.latency_stages),
        }


@dataclass(frozen=True)
class CapabilityDiscovery:
    """Model decision made before concrete tool schemas are loaded."""

    decision: str
    capability_ids: tuple[str, ...] = ()
    intent_clear: bool = False
    clarification: str = ""
    required_capability_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class GoalProgressDecision:
    """Structured task-level decision after an observed tool result."""

    status: str
    next_action: Optional[ToolCallDecision] = None
    reason: str = ""
    answer: str = ""
    evidence_steps: tuple[int, ...] = ()


# --------------------------------------------------------------------------- #
#  Агент
# --------------------------------------------------------------------------- #

class Agent:
    """Контроллер одной миссии (§3).

    Агент НЕ владеет потоками: его ``run_mission`` вызывается из
    ``TaskRuntime`` в фоновом потоке. Отмена приходит через ``cancel``.
    """

    def __init__(
        self,
        settings: Settings,
        council: Optional[Any] = None,
        config: Optional[AgentConfig] = None,
        model_router: Optional[Any] = None,
        brain_fabric: Optional[Any] = None,
    ) -> None:
        """
        Args:
            settings: конфигурация проекта.
            council: существующий ``CouncilRouter`` (переиспользуем §26).
                Если None — создаётся лениво при первой необходимости.
            config: настройки поведения агента.
            model_router: ЕДИНЫЙ ``ModelRouter`` (P5 §5.7). Если передан —
                переиспользуется (общий с CouncilRouter), иначе создаётся свой.
        """
        self._settings = settings
        self._config = config or AgentConfig()
        # settings.limits.max_action_iterations — единый источник истины для
        # потолка tool-вызовов (B1). Если пользователь переопределил лимит в
        # settings.json — он побеждает над дефолтом AgentConfig.
        if config is None:
            _limits = getattr(settings, "limits", None)
            _max_iter = getattr(_limits, "max_action_iterations", None)
            if isinstance(_max_iter, int) and _max_iter > 0:
                self._config.max_action_iterations = _max_iter
        self._council = council
        self._brain_fabric = brain_fabric
        self._registry = DEFAULT_REGISTRY
        # БАГ 9/17 FIX: один ToolExecutor на процесс. capacity берётся из
        # settings.limits.max_parallel_tools (дефолт 4 > 0). Раньше executor
        # кэшировался в context.extra, а ToolContext создавался на каждый
        # вызов — семафор параллелизма никогда не был общим (мёртвый).
        self._tool_executor = ToolExecutor(
            max_parallel=int(
                getattr(getattr(settings, "limits", None), "max_parallel_tools", 4) or 0
            )
        )
        self._task_runtime = None
        self._authority = None
        # Executive Mind owns goals/commitments/world state, while this Agent
        # remains the sole owner of tool execution and verification.
        from core.executive import LocalWorldObserver
        self._executive = ExecutiveMind(
            (settings.paths.resolved("data_dir") or settings.data_dir) / "executive",
            registry=self._registry,
            capability_registry=CAPABILITIES,
            world_observer=LocalWorldObserver(roots={
                "documents": settings.paths.resolved("documents_dir"),
            }),
        )
        # Sprint 8: Shadow Engine observes only when explicitly enabled in
        # local settings. It is separate from the active request path.
        from core.shadow import ShadowEngine
        shadow_cfg = getattr(settings, "shadow", None)
        shadow_data_dir = settings.paths.resolved("data_dir")
        self._shadow = ShadowEngine(
            data_dir=shadow_data_dir or "data", registry=self._registry, settings=settings,
            enabled=bool(getattr(shadow_cfg, "enabled", False)),
        )
        from core.capability_engine import CapabilityCatalog, CapabilityPlanner
        capability_dir = (shadow_data_dir or self._settings.data_dir) / "capabilities"
        self._capability_catalog = CapabilityCatalog(capability_dir)
        self._capability_planner = CapabilityPlanner(
            self._capability_catalog, self._registry,
        )
        # P5 §5.7: делим ЕДИНЫЙ роутер моделей с CouncilRouter, чтобы оба
        # пути (REPL/WebSocket и агентная миссия) маршрутизировались одинаково.
        self._model_router = model_router if model_router is not None else ModelRouter(settings)
        self._forge = SkillForge(settings) if self._config.enable_skill_forge else None
        self._repair = RepairLoop(
            self._registry,
            reasoner=None,
            fallback_tools=CAPABILITIES.fallbacks_map(),
            max_attempts=self._config.max_repair_attempts,
        )
        # Лёгкая офлайн-память на графе (SQLite, без эмбеддингов) —
        # даёт реальный вертикальный путь store/retrieve/use даже без
        # chromadb/сети (P0-5).
        try:
            self._graph = GraphMemoryStore(settings)
        except Exception as exc:
            log.warning("Граф памяти недоступен: %s", exc)
            self._graph = None
        self._pending_confirmations: Dict[str, Dict[str, Any]] = {}
        self._confirmation_timers: Dict[str, "threading.Timer"] = {}
        self._lock = threading.RLock()
        # Sprint 1: стриминг sync-ответов. Sink устанавливается ПОТОКОМ,
        # обрабатывающим конкретный запрос (thread-local), поэтому параллельные
        # запросы друг друга не видят и не перемешивают.
        self._stream_tls = threading.local()
        # Sprint 4: bounded session memory — последние N сообщений диалога
        # (10 пар user/assistant, FIFO). Подаётся ТОЛЬКО в разговорный путь;
        # tool-промпты остаются чистыми (спринт, STEP 2.4).
        session_size = int(getattr(getattr(settings, "limits", None),
                                   "session_memory_messages", 20))
        self._session = SessionManager(max_size=session_size)
        # Sprint 12: identity and relationship preferences are typed local
        # state. The resulting compact prompt fragment is only one consumer;
        # personality itself is not represented by a free-form prompt.
        self._relationship_memory = RelationshipMemoryStore(
            (shadow_data_dir or self._settings.data_dir) / "relationship",
        )
        try:
            self._relationship_memory.prune()
        except Exception as exc:
            log.debug("Очистка устаревшей relationship memory пропущена: %s", exc)
        self._preference_learner = PreferenceLearner(self._relationship_memory)
        self._personality = PersonalityEngine()
        self._memory_hierarchy = MemoryHierarchy(
            self._relationship_memory, session=self._session,
        )
        self._user_context: Dict[str, Any] = {}
        self._intake = UniversalIntake()
        self._tutor = TutorEngine()
        self._skills = SkillCatalog()

    @property
    def personality(self) -> PersonalityEngine:
        return self._personality

    @property
    def relationship_memory(self) -> RelationshipMemoryStore:
        return self._relationship_memory

    @property
    def preference_learner(self) -> PreferenceLearner:
        return self._preference_learner

    @property
    def intake(self) -> UniversalIntake:
        return self._intake

    @property
    def tutor(self) -> TutorEngine:
        return self._tutor

    @property
    def skills(self) -> SkillCatalog:
        return self._skills

    def teach(self, topic: str, *, level: str = "adaptive", mode: str = "socratic",
              session: TeachingSession | None = None) -> dict[str, Any]:
        """Bounded local teaching API used by Tutor Mode and tests."""
        return self._tutor.teach(topic, level=level, mode=mode, session=session).to_dict()

    @property
    def executive(self) -> ExecutiveMind:
        return self._executive

    def set_user_context(self, context: Any) -> None:
        """Updates privacy-minimal attention state used for response style."""
        if isinstance(context, dict):
            source = context
        else:
            source = vars(context) if context is not None and hasattr(context, "__dict__") else {}
        allowed = {
            "busy", "user_busy", "typing_active", "meeting_active", "fullscreen",
            "active_application", "probable_activity", "active_mission",
        }
        self._user_context = {key: source[key] for key in allowed if key in source}
        self._memory_hierarchy.working.update(self._user_context)

    @property
    def deepseek_brain_mode(self) -> bool:
        return bool(getattr(self._settings, "deepseek_brain_mode", False))

    # ------------------------------------------------------------------ #
    #  Sprint 1 — stream sink (прогрессивный ответ в UI)
    # ------------------------------------------------------------------ #

    def install_stream_sink(self, sink) -> None:
        """Регистрирует приёмник кумулятивного текста ответа для ТЕКУЩЕГО потока.

        ``sink(visible_text_so_far: str)`` вызывается по мере реальной
        генерации модели (SSE-дельты). Если бэкенд не поддерживает
        streaming — sink просто не вызывается, поведение не меняется.
        """
        self._stream_tls.sink = sink

    def clear_stream_sink(self) -> None:
        self._stream_tls.sink = None

    def _mark_latency(self, stage: str) -> None:
        stages = getattr(self._stream_tls, "latency_stages", None)
        if isinstance(stages, dict) and stage not in stages:
            stages[stage] = round(time.time_ns() / 1_000_000.0, 3)

    def _stream_consume(self, backend, messages: List[Dict[str, Any]], system: str,
                        extract_answer: bool = True,
                        max_tokens: Optional[int] = None) -> str:
        """chat() со стримингом, если установлен sink и бэкенд умеет streaming.

        Возвращает ПОЛНЫЙ сырой ответ (как chat). Видимый кумулятивный текст
        уходит в sink по мере прихода дельт — это реальный стриминг провайдера,
        не фейк-чанкинг готовой строки. ``extract_answer=True`` (план-JSON)
        извлекает поле ``answer``; ``False`` (прямой разговор) отдаёт сырой
        текст с скрытием служебных ``<think>``-блоков.
        Сбой сети/провайдера (BackendUnavailable/BackendConfigError)
        пробрасывается наружу — существующая логика фолбэка тиров (§15)
        сохранена без изменений.
        """
        sink = getattr(self._stream_tls, "sink", None)
        stream = getattr(backend, "streaming", None)
        self._mark_latency("llm_request")
        if sink is None or stream is None:
            result = backend.chat(messages, system=system, max_tokens=max_tokens)
            self._mark_latency("llm_first_token")
            self._mark_latency("llm_complete")
            return result

        extractor = AnswerStreamExtractor() if extract_answer else None
        parts: List[str] = []
        try:
            for piece in stream(messages, system=system, max_tokens=max_tokens):
                if not piece:
                    continue
                self._mark_latency("llm_first_token")
                parts.append(piece)
                if extractor is not None:
                    visible = extractor.feed(piece)
                else:
                    visible = _hide_reasoning_stream("".join(parts))
                if visible:
                    sink(visible)
        except (BackendUnavailable, BackendConfigError):
            raise  # наружу -> фолбэк тира / model_error (Sprint 1 STEP 5)
        except Exception as exc:
            # Неожиданная ошибка стриминга (бэкенд-специфика): честно
            # деградируем до обычного chat() того же тира.
            log.warning("streaming() упал (%s) — fallback на chat()", exc)
            return backend.chat(messages, system=system, max_tokens=max_tokens)

        raw = "".join(parts)
        if not raw.strip():
            raw = backend.chat(messages, system=system, max_tokens=max_tokens)
            self._mark_latency("llm_first_token")
        self._mark_latency("llm_complete")
        return raw

    # ------------------------------------------------------------------ #
    #  Публичный вход: исполнение миссии
    # ------------------------------------------------------------------ #

    def attach_task_runtime(self, runtime: Any) -> None:
        """Attach the canonical runtime used by durable action primitives."""
        self._task_runtime = runtime

    def _user_address(self) -> str:
        """Возвращает обращение из профиля/персоны (пусто, если не задано)."""
        return (getattr(getattr(self._settings, "persona", None), "address", "") or "").strip()

    def attach_authority(self, authority: Any) -> None:
        """Attach the canonical deterministic delegated-authority boundary."""
        self._authority = authority

    def authorize_delegated_action(
        self, mission: Optional[Mission], *, goal: str, tool: str,
        args: Dict[str, Any], risk: RiskAssessment,
    ) -> Any:
        """Match a concrete action against trusted structured mission scope."""
        from core.authority import AuthorityDecision

        request = self._delegated_authority_request(
            mission, goal=goal, tool=tool, args=args, risk=risk,
        )
        if self._authority is None or request is None:
            return AuthorityDecision(False, True, "no delegated authority")
        return self._authority.check(request)

    def _delegated_authority_request(
        self, mission: Optional[Mission], *, goal: str, tool: str,
        args: Dict[str, Any], risk: RiskAssessment,
    ) -> Any:
        from core.authority import AuthorityRequest, classify_effect

        if mission is None:
            return None
        raw = mission.context.get("authority_request")
        if not isinstance(raw, dict):
            return None
        action = str(args.get("action") or raw.get("action") or tool)
        try:
            request = AuthorityRequest(
                mission_id=mission.task_id, commitment_id=raw.get("commitment_id"),
                subject=str(raw.get("subject") or ""),
                resource=str(raw.get("resource") or ""), action=action,
                capability_family=str(raw.get("capability_family") or tool),
                effect=classify_effect(goal, action, tool, args),
                purpose=str(raw.get("purpose") or ""), risk=risk.level,
                constraints=dict(raw.get("constraints") or {}),
            )
            return request
        except (TypeError, ValueError):
            return None

    def run_mission(self, mission: Mission, cancel: threading.Event,
                    decision: Optional[SemanticRoutingDecision] = None) -> str:
        """Исполняет миссию целиком. Возвращает финальный текст ответа.

        Вызывается ``TaskRuntime`` в отдельном потоке. Никаких ограничений
        на длительность (§4) — только реальная отмена через ``cancel``.

        ``decision`` — готовое решение semantic_route из submit_goal; если
        не передано, execute() построит его сам (та же единая точка).
        """
        tls = self._stream_tls
        tls.streamed_rid = None

        def _mission_sink(visible: str) -> None:
            if getattr(tls, "streamed_rid", None) is None:
                tls.streamed_rid = mission.task_id
            try:
                mission.emit(EVENT_STREAM_CHUNK, payload={"text": visible})
            except Exception:  # pragma: no cover - события не ломают генерацию
                pass

        self.install_stream_sink(_mission_sink)
        try:
            outcome = self.execute(mission.goal, mission=mission, cancel=cancel, decision=decision)
        finally:
            self.clear_stream_sink()
        mission.verification = outcome.verification.to_dict() if outcome.verification else None
        mission.metadata["mode"] = outcome.mode
        mission.metadata["verified"] = outcome.verified
        if outcome.needs_confirmation:
            mission.metadata["needs_confirmation"] = True
        return outcome.text

    def consume_streamed_mission(self) -> Optional[str]:
        """Отдаёт и сбрасывает task_id миссии, стримленной в ТЕКУЩЕМ потоке.

        Вызывается ws-мостом в output_callback: финальный текст должен
        закрыть УЖЕ открытый стрименный пузырь (тот же task_id), а не
        создавать второй.
        """
        tls = self._stream_tls
        rid = getattr(tls, "streamed_rid", None)
        tls.streamed_rid = None
        return rid

    def execute(self, goal: str, mission: Optional[Mission] = None,
                cancel: Optional[threading.Event] = None,
                decision: Optional[SemanticRoutingDecision] = None) -> AgentOutcome:
        """Главный цикл: route -> risk -> mode -> plan -> execute -> verify -> repair.

        Единая точка решения (2026-09-05): ``decision`` из
        core/routing/semantic_router.route(). Если оркестратор не передал
        решение (worker миссии, прямые вызовы), роутер вызывается здесь —
        тот же источник, без keyword-гейтов.

        Цикл сокращается автоматически (§3): тривиальный разговор и простые
        команды не проходят через планирование.

        Sprint 4: перед генерацией из реплики извлекаются явные факты
        («меня зовут X») в профиль; после — пара (goal, ответ) попадает в
        bounded session memory для контекста следующих вопросов.
        """
        self._stream_tls.latency_stages = {}
        self._mark_latency("input_received")
        goal = (goal or "").strip()
        # Счётчик вызовов инструментов на весь пользовательский запрос
        # (B1): сбрасывается в точке входа, применяется в _execute_verified,
        # разделяется всеми частями составной команды.
        self._action_calls_left = int(getattr(self._config, "max_action_iterations", 6) or 6)
        # Единая точка решения: если оркестратор не передал decision —
        # роутим здесь (тот же semantic_route, clarify вне сессии запрещён).
        if decision is None and goal:
            decision = semantic_route(
                goal,
                SemanticRoutingContext(
                    llm_available=self._llm_available_cached(),
                    allow_clarify=False,
                ),
            )
        executive_contract = None
        if goal:
            try:
                executive_contract = self._executive.begin_turn(
                    goal, intent=semantic_intent_category(decision), source="user",
                )
            except Exception as exc:
                log.debug("Executive turn intake skipped: %s", exc)
            try:
                self._preference_learner.observe_user_message(goal)
            except Exception as exc:  # Relationship learning never blocks execution.
                log.debug("Обучение предпочтений пропущено: %s", exc)
            try:
                learn_facts(self._settings, goal)
            except Exception as exc:  # noqa: BLE001 — память не ломает миссию
                log.debug("Извлечение фактов не удалось: %s", exc)

        outcome = self._execute_core(goal, mission, cancel, decision=decision)
        if not self.deepseek_brain_mode:
            try:
                task_type = self._personality.infer_task_type(goal, mode=outcome.mode)
                outcome.text = self._personality.naturalize(
                    outcome.text, verified=outcome.verified, task_type=task_type,
                )
                # Streaming has already exposed cumulative text. Apply the hard
                # brevity bound only at a non-streaming conversational boundary.
                if outcome.mode == "conversation" and getattr(self._stream_tls, "sink", None) is None:
                    outcome.text = self._personality.adapt_response(
                        outcome.text,
                        self._personality.style_for(
                            user_context=self._user_context, task_type=task_type,
                            user_preference=self._preference_learner.profile(),
                        ),
                    )
            except Exception as exc:  # Personality wording never changes mission state.
                log.debug("Адаптация ответа пропущена: %s", exc)
        try:
            shadow_outcome = "success" if outcome.verified else (
                "unfulfilled" if outcome.mode == "unknown_task" else "failure"
            )
            self._shadow.observe_command(goal, outcome=shadow_outcome)
        except Exception as exc:  # Shadow mode must never affect Active mode.
            log.debug("Shadow command observation skipped: %s", exc)
        self._remember_exchange(goal, outcome)
        if executive_contract is not None:
            def _record_executive() -> None:
                try:
                    self._executive.complete_turn(
                        executive_contract, verified=outcome.verified,
                        result=outcome.text, tool=outcome.tool_used,
                        mode=outcome.mode,
                    )
                except Exception as exc:
                    log.debug("Executive outcome recording skipped: %s", exc)

            # Reflex actions return first; durable Goal/World/Eval writes are
            # persisted immediately after on a daemon worker.  Deliberate
            # missions retain the synchronous bookkeeping contract.
            if outcome.mode == "fast_path":
                threading.Thread(target=_record_executive,
                                 name="jarvis-executive-write", daemon=True).start()
            else:
                _record_executive()
        self._mark_latency("response_ready")
        outcome.latency_stages = dict(
            getattr(self._stream_tls, "latency_stages", {}) or {},
        )
        return outcome

    def _remember_exchange(self, goal: str, outcome: AgentOutcome) -> None:
        """Записывает пару (user, assistant) в bounded session memory.

        Пустые, отменённые и «пустой ввод» реплики не пишутся. Служебный
        префикс [degraded] из ответа убирается (пользовательская память
        хранит чистый текст).
        """
        try:
            if not goal or outcome.mode in ("empty", "cancelled"):
                return
            text = (outcome.text or "").strip()
            if text.startswith("[degraded] "):
                text = text[len("[degraded] "):]
            self._session.push("user", goal)
            if text:
                self._session.push("assistant", text)
        except Exception as exc:  # noqa: BLE001
            log.debug("Запись в session memory не удалась: %s", exc)

    def _execute_core(self, goal: str, mission: Optional[Mission] = None,
                      cancel: Optional[threading.Event] = None,
                      decision: Optional[SemanticRoutingDecision] = None) -> AgentOutcome:
        cancel = cancel or threading.Event()
        goal = (goal or "").strip()
        if not goal:
            addr = self._user_address()
            prefix = f"{addr}, " if addr else ""
            msg = f"{prefix}я не расслышал команду." if prefix else "Я не расслышал команду."
            return AgentOutcome(text=msg, mode="empty")

        trace: List[str] = []

        # ---- 1. ROUTING (единая точка решения, 2026-09-05) ----
        # decision построен semantic_route(): kind/tool/confidence/risk.
        # Никаких keyword-классификаторов здесь больше нет — все ветки
        # ниже читают decision и не переклассифицируют текст.
        intent = semantic_intent_category(decision)
        trace.append(
            f"routing: kind={decision.kind} tool={decision.tool} "
            f"tier={decision.tier} conf={decision.confidence:.2f} "
            f"risk={decision.risk} confirm={decision.needs_confirmation}"
        )
        try:
            contract = self._intake.classify(goal)
            trace.append(f"task_contract={contract.intent_family}/{contract.mode}")
            if mission is not None:
                mission.metadata["task_contract"] = contract.to_dict()
        except Exception as exc:
            log.debug("Universal task contract skipped: %s", exc)
        if mission is not None:
            mission.acknowledgement = pick_acknowledgement(
                intent, goal=goal, settings=self._settings, allow_model=False,
            )
            mission.metadata["intent"] = intent
            mission.metadata["routing_decision"] = decision.to_dict()
            mission.set_status(MissionStatus.ANALYZING, "определение намерения и риска")
            mission.set_progress(0.1, "анализ намерения")

        # ---- 2. RISK (§21): адаптер над semantic_router.assess_risk ----
        risk = assess_risk(goal)
        trace.append(f"risk={risk.level.value}")
        self._mark_latency("route_complete")

        # ---- 2a. UNSUPPORTED (шаг 5): честный отказ без подмены ----
        # Составная цель сначала разделяется по решению роутера: каждая
        # часть получает честную обработку, отказ по одной части не
        # хоронит весь запрос (аудит 1b).
        compound = split_compound_by_decision(goal, llm_available=self._llm_available_cached())
        if decision.kind == "action" and decision.tool is None and not compound:
            return self._handle_unsupported(decision, goal, mission, trace)

        # ---- 2b. CLARIFY из прямого вызова agent.execute ----
        if decision.kind == "clarify" and decision.clarify_question:
            trace.append("routing -> clarify")
            return AgentOutcome(
                text=decision.clarify_question,
                verified=False,
                tool_used=None,
                risk=risk,
                mode="clarification",
                trace=trace,
            )

        fresh = self._try_fresh_information(goal, mission, cancel, risk, trace, decision)
        if fresh is not None:
            return fresh
        try:
            executive_plan = self._executive.compile(
                goal, intent=intent, risk=risk.level.value,
            )
            trace.append(f"command_os={self._executive.commands.explain(executive_plan)}")
            if mission is not None:
                mission.metadata["command_os"] = executive_plan.to_dict()
        except Exception as exc:
            log.debug("Command OS compile skipped: %s", exc)
        if mission is not None:
            mission.metadata["risk"] = risk.to_dict()

        # Explicit independent clauses become a verified batch.  This keeps
        # a planner from silently completing only the first half of a request.
        # Разбиение уже проверено по решению роутера выше (split_compound_by_decision).
        if compound:
            trace.append(f"compound batch -> {len(compound)} clauses")
            if mission is not None:
                mission.metadata["compound_commands"] = list(compound)
            return self._execute_compound(compound, mission=mission, cancel=cancel, trace=trace)

        # ---- 3. CONTEXT: большой ввод -> ingest (§7) ----
        context_tokens = 0
        if len(goal) > self._config.large_input_chars:
            task_id = mission.task_id if mission else "adhoc"
            ingested = ingest_text(task_id, goal, self._settings)
            context_tokens = ingested.estimated_tokens
            trace.append(f"ingest: {ingested.chunk_count} чанков, ~{context_tokens} токенов")
            if mission is not None:
                mission.metadata["ingest"] = {
                    "chunks": ingested.chunk_count,
                    "tokens": context_tokens,
                    "chars": ingested.raw_char_count,
                }

        if cancel.is_set():
            return AgentOutcome(text="Задача отменена.", mode="cancelled", trace=trace)

        # (R2, позиция 11) keyword-маркер «неизвестная команда» удалён:
        # маршрут цели определяет только decision. Неизвестные возможности
        # обрабатываются в planning/discovery-путях (_handle_unknown).

        # ---- 4. SKILL: есть ли готовый навык под эту цель (§9) ----
        skill = self._match_skill(goal)
        if skill is not None:
            trace.append(f"найден навык '{skill.name}' ({skill.status.value})")
            if mission is not None:
                mission.metadata["skill"] = skill.name

        # ---- 5. MODE + ROUTING (§15) ----
        routing = self._model_router.route(goal, context_tokens=context_tokens)
        if routing is not None:
            trace.append(f"route -> {routing.tier.value} ({routing.reason})")
            if mission is not None:
                mission.model_used = routing.tier.value
                mission.metadata["routing"] = routing.to_dict()

        # ---- 5b. FAST PATH before memory/model work ----
        # Local system/app/media commands do not need Chroma initialization or
        # a model context.  Keeping this branch first protects the 1.5s hard
        # budget and prevents cold memory setup from polluting tool latency.
        self._mark_latency("context_complete")
        fast = self._try_fast_path(
            goal, decision, mission, cancel, risk,
        )
        if fast is not None:
            fast.trace = trace + fast.trace
            return fast

        # ---- 5c. MEMORY: извлечение релевантного контекста (P0-5) ----
        memory_ctx = self._retrieve_context(goal)
        self._mark_latency("context_complete")
        if memory_ctx and mission is not None:
            mission.metadata["memory_context"] = memory_ctx[:600]

        # ---- 6. CONVERSATION GATE (по decision) ----
        # Офлайн-роутер уверенно распознал разговор: модель НЕ получает список
        # инструментов и НЕ генерирует JSON-план. Настоящие действия идут мимо
        # гейта в planner ниже. (2026-09-05: classify_conversation удалён —
        # гейт читает decision.kind.)
        if decision.kind in ("chat", "question") and decision.tool is None:
            trace.append(f"conversation gate: routing kind={decision.kind}")
            if mission is not None:
                mission.metadata["conversation_gate"] = f"routing kind={decision.kind}"
            return self._answer_conversation(
                goal, mission, cancel, trace, routing, memory_ctx,
            )

        # ---- 7. RESEARCH MODE (§18): явное исследование ----
        # Research-режим определяет роутер (decision.is_research по классу
        # research-якорей); keyword-классификатор is_research_goal удалён.
        if decision.kind == "mission" and decision.is_research and not self.deepseek_brain_mode:
            trace.append("режим: research workflow")
            return self._handle_research(goal, mission, cancel, trace)

        if cancel.is_set():
            return AgentOutcome(text="Задача отменена.", mode="cancelled", trace=trace)

        # ---- 7. CAPABILITY DISCOVERY (§12): полный surface -> few schemas ----
        # DeepSeek first sees a compact catalogue of every live capability;
        # JSON schemas are loaded only for its selected IDs.  The old fixed
        # top-five retrieval was a hard whitelist and hid most of JARVIS.
        discovery: Optional[CapabilityDiscovery] = None
        if self.deepseek_brain_mode:
            discovery, discovery_error = self._discover_capabilities(
                goal=goal,
                routing=routing,
                memory_ctx=memory_ctx,
                action_expected=decision.kind != "chat",
            )
            if discovery is None:
                return self._handle_model_unavailable(
                    goal, mission, trace,
                    MODEL_ERROR_PREFIX + f"capability discovery failed: {discovery_error}",
                )
            trace.append(
                f"capability discovery={discovery.decision} "
                f"ids={list(discovery.capability_ids)} "
                f"required={list(discovery.required_capability_ids)} "
                f"clear={discovery.intent_clear}"
            )
            if discovery.decision == "clarify":
                trace.append("capability discovery -> model clarification")
                return AgentOutcome(
                    text=discovery.clarification,
                    verified=False,
                    tool_used=None,
                    risk=risk,
                    mode="clarification",
                    trace=trace,
                )
            if discovery.decision == "answer":
                trace.append("capability discovery -> conversational answer")
                return self._answer_conversation(
                    goal, mission, cancel, trace, routing, memory_ctx,
                )
            caps = CAPABILITIES.discover(
                goal, discovery.capability_ids,
                top_k=self._config.max_discovered_tools,
            )
        else:
            caps = CAPABILITIES.retrieve(goal, top_k=self._config.max_tool_retrieval)
        trace.append(f"tool discovery -> {[c.name for c in caps]}")

        if self.deepseek_brain_mode and not caps:
            trace.append("capability discovery selected no live tool")
            return self._answer_conversation(
                goal, mission, cancel, trace, routing, memory_ctx,
            )

        if mission is not None:
            mission.set_status(MissionStatus.PLANNING, "выбор способа выполнения")
            mission.set_progress(0.3, "планирование")

        # ---- 8. PLAN: структурированное решение модели (§13) ----
        decision, plan_error = self._decide_with_model(
            goal, caps, mission, cancel,
            routing=routing, memory_ctx=memory_ctx,
            require_tool=bool(
                self.deepseek_brain_mode
                and discovery is not None
                and discovery.decision == "act"
                and discovery.intent_clear
            ),
        )

        if cancel.is_set():
            return AgentOutcome(text="Задача отменена.", mode="cancelled", trace=trace)

        if decision is None:
            # Сбой модели (сеть/провайдер/ключ) — временная ошибка инфраструктуры,
            # а НЕ «не умею» (§29): никаких черновиков навыков и сырых ошибок
            # в чат — короткая честная фраза, детали только в лог.
            if plan_error.startswith(MODEL_ERROR_PREFIX):
                return self._handle_model_unavailable(goal, mission, trace, plan_error)
            trace.append(f"planning failed: {plan_error}")
            return self._handle_unknown(goal, caps, mission, trace, plan_error)

        if not decision.needs_tool:
            # A discovery turn that selected an action surface has not
            # executed anything yet. Do not feed a planner's optimistic prose
            # into the conversational finalizer; use capability requirements
            # or a clean no-action conversation instead.
            if self.deepseek_brain_mode and discovery is not None and discovery.decision == "act":
                requirement = next(
                    (
                        (cap.name, self._missing_capability_requirement(cap.name, {}))
                        for cap in caps
                        if self._missing_capability_requirement(cap.name, {})
                    ),
                    None,
                )
                if requirement is not None:
                    tool_name, reason = requirement
                    clarification = self._finalize_tool_response(
                        goal=goal, tool=tool_name, args={}, result=None,
                        verification=None, routing=routing, memory_ctx=memory_ctx,
                        caps=caps, clarification=True, clarification_reason=reason,
                    )
                    if not clarification:
                        return self._handle_model_unavailable(
                            goal, mission, trace,
                            MODEL_ERROR_PREFIX + "DeepSeek не сформировал уточнение capability",
                        )
                    return AgentOutcome(
                        text=clarification,
                        verified=False,
                        tool_used=None,
                        risk=risk,
                        mode="clarification",
                        trace=trace + [f"capability requirement clarification: {tool_name}"],
                    )
                trace.append("planner deferred selected action without execution")
                return self._answer_conversation(
                    goal, mission, cancel, trace, routing, memory_ctx,
                )
            # Модель решила ответить текстом — обычный диалог.
            text = decision.answer.strip()
            if self.deepseek_brain_mode and text:
                text, finalize_error = self._finalize_conversational_response(
                    goal=goal,
                    draft=text,
                    routing=routing,
                    memory_ctx=memory_ctx,
                )
                if not text:
                    return self._handle_model_unavailable(
                        goal, mission, trace,
                        MODEL_ERROR_PREFIX + f"финальная реплика DeepSeek не сформирована: {finalize_error}",
                    )
            if not text and self.deepseek_brain_mode:
                return self._handle_model_unavailable(
                    goal, mission, trace,
                    MODEL_ERROR_PREFIX + "DeepSeek вернул пустой ответ",
                )
            text = text or "Готов."
            # §29 — но если модель ФАКТИЧЕСКИ отказалась (для задачи, требующей
            # действия) — это НЕ "я не умею" (§29). Перенаправляем на путь
            # неизвестной задачи: исследовать и научиться.
            if _is_model_refusal(text, goal):
                trace.append("модель отказалась/не смогла — путь неизвестной задачи (§29)")
                return self._handle_unknown(goal, caps, mission, trace,
                                            reason="модель не предложила способа")
            trace.append("режим: разговор без инструмента")
            if mission is not None:
                mission.set_progress(1.0, "ответ сформирован")
            return AgentOutcome(text=text, verified=True, mode="conversation", trace=trace)

        route_guard = validate_tool_selection(goal, decision.tool, decision.arguments)
        trace.append(
            f"route guard -> {'allow' if route_guard.allowed else 'block'}"
            f" ({route_guard.reason or 'consistent'})"
        )
        if not route_guard.allowed:
            if mission is not None:
                mission.note_error(route_guard.reason)
                mission.metadata["route_guard"] = route_guard.to_dict()
            if self.deepseek_brain_mode:
                if decision.kind == "chat":
                    trace.append("route guard -> conversation recovery (routing kind=chat)")
                    return self._answer_conversation(
                        goal, mission, cancel, trace, routing, memory_ctx,
                    )
                # The guard blocks the side effect, but must not become the
                # conversational answer. Let the same brain explain or
                # clarify from the active session context.
                rejected = ActionResult(
                    tool=str(decision.tool or "unknown"),
                    args=dict(decision.arguments or {}),
                    ok=False,
                    error=route_guard.reason,
                )
                final = self._finalize_tool_response(
                    goal=goal,
                    tool=str(decision.tool or "unknown"),
                    args=dict(decision.arguments or {}),
                    result=rejected,
                    verification=None,
                    routing=routing,
                    memory_ctx=memory_ctx,
                    caps=caps,
                )
                if final:
                    return AgentOutcome(
                        text=final,
                        verified=False,
                        tool_used=None,
                        risk=risk,
                        mode="conversation",
                        trace=trace + ["route guard -> model clarification"],
                    )
            return AgentOutcome(
                text=(
                    f"Запрос не выполнен: выбран «{decision.tool}», "
                    f"а нужен «{', '.join(route_guard.expected_tools)}». "
                    "Побочного действия не было."
                ),
                verified=False,
                tool_used=decision.tool,
                mode="route_blocked",
                trace=trace,
            )
        missing_requirement = self._missing_capability_requirement(
            decision.tool, decision.arguments,
        )
        if self.deepseek_brain_mode and missing_requirement:
            trace.append(f"capability requirement missing: {missing_requirement}")
            clarification = self._finalize_tool_response(
                goal=goal, tool=decision.tool, args=decision.arguments, result=None,
                verification=None, routing=routing, memory_ctx=memory_ctx,
                caps=caps, clarification=True,
                clarification_reason=missing_requirement,
            )
            if not clarification:
                clarification = "Ошибка DeepInfra: не удалось сформировать уточняющий вопрос."
            return AgentOutcome(
                text=clarification, verified=False, tool_used=decision.tool,
                risk=risk, mode="clarification", trace=trace,
            )

        # ---- 9. RISK GATE перед выполнением (§21) ----
        exec_risk = assess_risk(goal, decision.tool, decision.arguments)
        authority = self.authorize_delegated_action(
            mission, goal=goal, tool=decision.tool,
            args=decision.arguments, risk=exec_risk,
        )
        if authority.allowed:
            trace.append(f"delegated authority matched grant={authority.grant_id}")
        if (exec_risk.needs_confirmation and not authority.allowed
                and not self._config.auto_confirm_high_risk):
            trace.append(f"HIGH risk -> требуется подтверждение: {exec_risk.reasons}")
            conf_id = uuid.uuid4().hex
            prompt_text = exec_risk.confirmation_prompt(self._user_address())
            if mission is not None:
                mission.emit(EVENT_CONFIRMATION_REQUIRED, payload={
                    "confirmation_id": conf_id,
                    "tool": decision.tool,
                    "arguments": decision.arguments,
                    "risk": exec_risk.to_dict(),
                    "prompt": prompt_text,
                })
                mission.set_status(MissionStatus.PAUSED, "ожидание подтверждения пользователя")
            # Регистрируем ожидающее подтверждение, чтобы позже можно
            # было ответить (confirm/reject) и продолжить или отменить.
            with self._lock:
                self._pending_confirmations[conf_id] = {
                    "goal": goal,
                    "tool": decision.tool,
                    "args": decision.arguments,
                    "decision": decision,
                    "risk": exec_risk,
                    "caps": caps,
                    "mission": mission,
                    "cancel": cancel,
                    "trace": trace,
                    "loop_state": {
                        "routing": routing,
                        "memory_ctx": memory_ctx,
                        "required_capability_ids": list(
                            discovery.required_capability_ids if discovery is not None else ()
                        ),
                    },
                }
            # П1 §1.3 (voice-first): если пользователь не ответит в голос/текст
            # за confirmation_timeout_sec — безопасный авто-reject (отказ).
            self._start_confirmation_watchdog(conf_id)
            return AgentOutcome(
                text=prompt_text,
                verified=False,
                needs_confirmation=True,
                confirmation_id=conf_id,
                risk=exec_risk,
                tool_used=decision.tool,
                mode="confirmation",
                trace=trace,
            )

        # ---- 10. EXECUTION + OBSERVATION + VERIFICATION + REPAIR ----
        # The bounded continuation loop is the production DeepSeek path.
        # Legacy offline planners keep their established one-tool contract.
        if not self.deepseek_brain_mode:
            return self._execute_verified(
                goal=goal,
                tool=decision.tool,
                args=decision.arguments,
                mission=mission,
                cancel=cancel,
                trace=trace,
                risk=exec_risk,
                caps=caps,
                routing=routing,
                memory_ctx=memory_ctx,
            )
        return self._execute_capability_loop(
            goal=goal,
            first_decision=decision,
            mission=mission,
            cancel=cancel,
            trace=trace,
            risk=exec_risk,
            caps=caps,
            routing=routing,
            memory_ctx=memory_ctx,
            required_capability_ids=self._required_capability_contract(
                discovery.required_capability_ids if discovery is not None else ()
            ),
        )

    # ------------------------------------------------------------------ #
    #  Роутинг модели (P0-2) + память (P0-5) + подтверждение (P0-3)
    # ------------------------------------------------------------------ #

    def _tier_breaker_open(self, tier) -> bool:
        """Sprint 3 STEP 5: breaker модели тира разомкнут (серии сбоев).

        Ключ — ``provider:model``, а не просто провайдер: несколько тиров
        могут сидеть на одном провайдере (anymodel), и падение одной
        модели (am/free) не должно блокировать остальные (cx/gpt-5.5).
        """
        try:
            provider = self._settings.get_provider(tier)
            model_id = self._settings.get_model_id(tier) or ""
            return breaker.is_open(f"{provider}:{model_id}")
        except Exception:  # noqa: BLE001 — конфиг может быть урезан в тестах
            return False

    def _backend_for_routing(self, routing: Optional[RoutingDecision]):
        """Возвращает (backend, tier) по решению ModelRouter (cloud-first).

        Идёт по цепочке [routing.tier] + fallback_chain, берёт первый
        реально доступный тир. Тиры с разомкнутым circuit breaker'ом
        (Sprint 3: 3 подряд сбоя провайдера) пропускаются до конца
        cooldown. Если ни один внешний не доступен — честный офлайн-фолбэк
        на ЛОКАЛЬНУЮ модель TIER 4 (§17). Решение роутера обязано дойти
        до реального вызова модели (иначе выбор бессмыслен).
        """
        if self.deepseek_brain_mode:
            if (self._brain_fabric is not None and routing is not None
                    and getattr(routing, "brain_route", None) is not None):
                from core.brain import BrainFabricBackend, BrainRequest, BrainRole, PrivacyClass
                # БАГ 14 FIX: невалидная роль маршрутизации не должна ронять
                # виток ValueError'ом — честный фолбэк (нет brain-бэкенда).
                try:
                    role = BrainRole(routing.role)
                except ValueError:
                    log.warning(
                        "Неизвестная роль маршрутизации %r — откат на обычную цепочку тиров",
                        getattr(routing, "role", None),
                    )
                    return None, None
                template = BrainRequest(
                    user_request=routing.request_text, role=role,
                    privacy=PrivacyClass.PERSONAL,
                    context_tokens=routing.complexity.context_tokens,
                )
                return BrainFabricBackend(
                    self._brain_fabric, routing.brain_route, template=template,
                ), routing.tier
            return None, None

        if (self._brain_fabric is not None and routing is not None
                and getattr(routing, "brain_route", None) is not None):
            from core.brain import BrainFabricBackend, BrainRequest, BrainRole, PrivacyClass
            # БАГ 14 FIX: как и выше — непонятная роль не убивает запрос,
            # а уводит его на обычную тировую цепочку ниже.
            try:
                role = BrainRole(routing.role)
            except ValueError:
                log.warning(
                    "Неизвестная роль маршрутизации %r — откат на цепочку тиров",
                    getattr(routing, "role", None),
                )
                role = None
            if role is not None:
                template = BrainRequest(
                    user_request=routing.request_text, role=role,
                    privacy=(PrivacyClass.LOCAL_ONLY if routing.forced_local else PrivacyClass.PERSONAL),
                    context_tokens=routing.complexity.context_tokens,
                )
                return BrainFabricBackend(
                    self._brain_fabric, routing.brain_route, template=template,
                ), routing.tier

        chain: List[Any] = []
        if routing is not None:
            chain = [routing.tier] + list(routing.fallback_chain)
        else:
            chain = [Tier.FAST]

        skipped_breakers: List[str] = []
        for tier in chain:
            if self._tier_breaker_open(tier):
                skipped_breakers.append(str(tier))
                continue
            try:
                if not self._settings.is_tier_available(tier):
                    continue
                backend = get_llm_backend(self._settings, tier)
                return backend, tier
            except (BackendUnavailable, BackendConfigError) as exc:
                log.debug("Тир %s недоступен для планирования: %s", tier, exc)
                continue
        if skipped_breakers:
            log.info("Circuit breaker: тиры %s пропущены (недавние сбои провайдеров)",
                     ", ".join(skipped_breakers))

        # Graceful offline fallback на локальную модель (TIER 4, без сети).
        try:
            return get_offline_backend(self._settings), Tier.FAST
        except Exception as exc:
            log.warning("Локальный фолбэк недоступен: %s", exc)
            return None, None

    def _fallback_backend(self, routing: Optional[RoutingDecision], tried,
                          policy_override=None):
        """Следующий доступный бэкенд после ``tried`` (для повтора при сбое).

        ``policy_override`` — короткая «разговорная» политика для простых
        задач: фолбэк не должен ждать полный бюджет аналитического тира.
        Тиры с разомкнутым breaker'ом пропускаются (Sprint 3).
        """
        if self.deepseek_brain_mode:
            return None, None
        if (self._brain_fabric is not None and routing is not None
                and getattr(routing, "brain_route", None) is not None):
            # BrainFabricBackend already consumed its bounded semantic chain.
            return None, None
        chain: List[Any] = []
        if routing is not None:
            chain = [routing.tier] + list(routing.fallback_chain)
        else:
            chain = [Tier.FAST]
        if tried is not None:
            try:
                idx = chain.index(tried)
                chain = chain[idx + 1:]
            except ValueError:
                pass
        for tier in chain:
            if self._tier_breaker_open(tier):
                continue
            try:
                if not self._settings.is_tier_available(tier):
                    continue
                return get_llm_backend(self._settings, tier,
                                       policy_override=policy_override), tier
            except (BackendUnavailable, BackendConfigError):
                continue
        return None, None

    def _retrieve_context(self, goal: str) -> str:
        """Извлекает релевантный контекст из локальной памяти (граф, офлайн)."""
        parts: List[str] = []
        try:
            executive_context = self._executive.context_for(goal, limit=4)
            if executive_context:
                parts.append(executive_context)
        except Exception as exc:
            log.debug("Executive context недоступен: %s", exc)
        if self._graph is None:
            return "\n".join(parts)
        try:
            nodes = self._graph.search_nodes(goal, top_k=3)
            for n in nodes:
                label = (n.get("label") or "").strip()
                props = n.get("properties") or {}
                detail = (
                    props.get("detail") or props.get("value")
                    or props.get("text") or ""
                )
                if detail:
                    parts.append(f"- {detail}")
                elif label:
                    parts.append(f"- {label}")
            return "\n".join(parts)
        except Exception as exc:
            log.debug("Извлечение памяти не удалось: %s", exc)
            return ""

    def _store_fact(self, label: str, detail: str = "") -> None:
        """Сохраняет факт в локальную память (граф). Не роняет выполнение."""
        if self._graph is None:
            return
        try:
            self._graph.create_node(label, {"detail": detail, "source": "agent"})
        except Exception as exc:
            log.debug("Сохранение факта не удалось: %s", exc)

    def answer_confirmation(self, confirmation_id: str, approved: bool) -> Optional[AgentOutcome]:
        """Отвечает на ожидающее подтверждение HIGH-risk операции (§21).

        Args:
            confirmation_id: id, выданный при запросе подтверждения.
            approved: True — выполнить операцию, False — отклонить.

        Returns:
            AgentOutcome с результатом, либо None если подтверждение не найдено.
        """
        with self._lock:
            pending = self._pending_confirmations.pop(confirmation_id, None)
            # Отменяем таймер ожидания — человек уже ответил (П1 §1.3).
            timer = self._confirmation_timers.pop(confirmation_id, None)
            if timer is not None:
                timer.cancel()
        if pending is None:
            return None

        goal = pending["goal"]
        tool = pending["tool"]
        args = pending["args"]
        risk = pending["risk"]
        caps = pending["caps"]
        mission = pending["mission"]
        cancel = pending["cancel"]
        trace = pending["trace"]

        if not approved:
            if mission is not None:
                mission.set_status(MissionStatus.CANCELLED, "отклонено пользователем")
                mission.emit(EVENT_TASK_FAILED, payload={"reason": "confirmation_rejected"})
            return AgentOutcome(
                text="Понял. Действие отменено.",
                verified=False,
                mode="confirmation_rejected",
                risk=risk,
                tool_used=tool,
                trace=trace,
            )

        # A risky request can reach the unknown-capability path before a
        # concrete tool exists.  Confirmation still authorizes only the
        # bounded research/prepare continuation; it must not be mistaken for
        # a grant to invent a mutation.  Re-enter that path after approval so
        # the same policy, capability planner and verifier run again.
        if pending.get("unknown_capability"):
            if mission is not None:
                mission.set_status(MissionStatus.PLANNING, "подтверждено, ищу проверяемый способ")
            return self._handle_unknown(
                goal, caps, mission, trace,
                reason=str(pending.get("reason") or "нет готового способа"),
                attempted_tool=pending.get("attempted_tool"),
                skip_confirmation=True,
            )

        # Подтверждено — выполняем.
        if mission is not None:
            mission.set_status(MissionStatus.EXECUTING, "подтверждено, выполняю")
        if self.deepseek_brain_mode:
            decision = pending.get("decision")
            if not isinstance(decision, ToolCallDecision):
                decision = ToolCallDecision(
                    tool=tool,
                    arguments=args,
                    reason="approved concrete action",
                    risk=getattr(getattr(risk, "level", None), "value", "high"),
                    verification="verify requested goal outcome after execution",
                )
            loop_state = pending.get("loop_state") or {}
            return self._execute_capability_loop(
                goal=goal,
                first_decision=decision,
                mission=mission,
                cancel=cancel,
                trace=trace,
                risk=risk,
                caps=caps,
                routing=loop_state.get("routing"),
                memory_ctx=str(loop_state.get("memory_ctx") or ""),
                initial_observations=loop_state.get("observations") or [],
                start_step=int(loop_state.get("start_step") or 0),
                confirmation_approved=True,
                required_capability_ids=loop_state.get("required_capability_ids") or (),
            )
        return self._execute_verified(
            goal=goal, tool=tool, args=args, mission=mission,
            cancel=cancel, trace=trace, risk=risk, caps=caps,
            confirmation_approved=True,
        )

    # ------------------------------------------------------------------ #
    #  П1 §1.3 — voice-first confirmation watchdog
    # ------------------------------------------------------------------ #

    def _start_confirmation_watchdog(self, confirmation_id: str) -> None:
        """Запускает таймер ожидания подтверждения (П1 §1.3).

        Если пользователь не ответит голосом/текстом за
        ``confirmation_timeout_sec`` — безопасный авто-reject (отказ).
        Таймаут отключается значением <= 0.
        """
        timeout = float(getattr(self._config, "confirmation_timeout_sec", 30.0))
        if timeout <= 0:
            return

        def _on_timeout() -> None:
            with self._lock:
                if confirmation_id not in self._pending_confirmations:
                    return  # уже ответили или отменили
            # Авто-reject: таймаут = молчаливый отказ (безопаснее).
            self.answer_confirmation(confirmation_id, approved=False)

        with self._lock:
            old = self._confirmation_timers.pop(confirmation_id, None)
            if old is not None:
                old.cancel()
            timer = threading.Timer(timeout, _on_timeout)
            timer.daemon = True
            self._confirmation_timers[confirmation_id] = timer
            timer.start()

    # ------------------------------------------------------------------ #
    #  §18 — Research workflow
    # ------------------------------------------------------------------ #

    def _handle_research(self, goal: str, mission: Optional[Mission],
                         cancel: threading.Event,
                         trace: List[str]) -> AgentOutcome:
        """Отдельный конвейер исследования (§18).

        Верификация здесь особая: «готово» только если собраны и
        перекрёстно проверены источники (§14).
        """
        engine = ResearchEngine(self._settings)
        report = engine.run(goal, mission=mission, cancel=cancel)
        trace.append(
            f"research: прочитано {len(report.sources_read)}, "
            f"недоступно {len(report.sources_failed)}, находок {len(report.findings)}"
        )
        if mission is not None:
            mission.metadata["research"] = {
                "sources_read": report.sources_read,
                "sources_failed": report.sources_failed,
                "findings": len(report.findings),
                "verified": report.verified,
                "status": getattr(report, "status", "completed"),
                "resume_task_id": getattr(report, "resume_task_id", ""),
                "local_fallback": list(getattr(report, "local_fallback", []) or []),
            }

        verification = VerificationResult(
            verified=report.verified,
            method="research_cross_check",
            detail=(
                f"источников прочитано: {len(report.sources_read)}, "
                f"подтверждённых находок: "
                f"{sum(1 for f in report.findings if f.claim_type.value == 'verified_fact')}"
            ),
            strict=True,
        )
        if mission is not None:
            mission.emit(EVENT_VERIFICATION, payload=verification.to_dict())

        return AgentOutcome(
            text=report.to_text(),
            verified=report.verified,
            verification=verification,
            mode="research",
            trace=trace,
        )

    def _execute_compound(self, parts: List[str], *, mission: Optional[Mission],
                          cancel: threading.Event, trace: List[str]) -> AgentOutcome:
        """Execute explicit independent clauses and require every one verified."""
        if mission is not None:
            mission.set_status(MissionStatus.EXECUTING, "выполняю составную задачу")
            mission.set_progress(0.4, "выполнение составных шагов")
        outcomes: List[AgentOutcome] = []
        refused: List[str] = []
        for index, part in enumerate(parts, start=1):
            if cancel.is_set():
                return AgentOutcome(
                    text="Составная задача отменена до завершения всех шагов.",
                    verified=False,
                    mode="cancelled",
                    trace=trace + [f"compound cancelled before clause {index}"],
                )
            # Каждая часть составной команды маршрутизируется той же
            # единственной точкой решения (clarify внутри части запрещён).
            part_decision = semantic_route(
                part,
                SemanticRoutingContext(
                    llm_available=self._llm_available_cached(),
                    allow_clarify=False,
                ),
            )
            outcome = self._execute_core(
                part, mission=None, cancel=cancel, decision=part_decision,
            )
            if outcome.needs_confirmation:
                # Подтверждение очередной части возвращается вместе с
                # честными отказами по неподдерживаемым частям (R8, 1b):
                # пользователь видит и отказ, и запрос подтверждения.
                if refused:
                    outcome.text = "\n".join(refused + [outcome.text])
                outcome.trace = trace + outcome.trace
                return outcome
            if outcome.mode == "unsupported":
                # Невыполнимая часть фиксируется честным отказом (R8) и не
                # мешает исполнению остальных самостоятельных частей.
                refused.append(outcome.text)
                outcomes.append(outcome)
                continue
            outcomes.append(outcome)
            trace.extend(f"compound[{index}] {item}" for item in outcome.trace[-8:])

        verified_count = sum(1 for outcome in outcomes if outcome.verified)
        verified = verified_count == len(outcomes)
        details = []
        for part, outcome in zip(parts, outcomes):
            status = "проверено" if outcome.verified else "не подтверждено"
            details.append(f"{part}: {status}. {outcome.text}")
        verification = VerificationResult(
            verified=verified,
            method="command_batch",
            detail=f"подтверждено {verified_count}/{len(outcomes)} шагов",
            strict=True,
        )
        if mission is not None:
            mission.set_status(MissionStatus.VERIFYING, "проверяю каждый составной шаг")
            mission.emit(EVENT_VERIFICATION, payload=verification.to_dict())
            mission.set_progress(1.0, verification.detail)
        return AgentOutcome(
            text="\n".join(details),
            verified=verified,
            verification=verification,
            tool_used="command_batch",
            mode="batch",
            trace=trace,
        )

    #: Инструменты, чей путь от route() до verified-ответа не требует LLM
    #: (шаг 3, офлайн-автономность детерминированных действий).
    _DETERMINISTIC_TOOLS = frozenset({
        "open_app", "close_app", "volume", "current_time", "system_status",
        "play_music", "add_reminder", "list_reminders", "cancel_reminder",
        "list_files", "search_files", "screen_capture",
    })

    def _try_fast_path(self, goal: str, decision: Optional[SemanticRoutingDecision],
                       mission: Optional[Mission],
                       cancel: threading.Event,
                       risk: RiskAssessment) -> Optional[AgentOutcome]:
        """Детерминированный быстрый путь без планирования (§3).

        Решение — decision из semantic_route(): инструмент и уверенность
        приходят из единой точки маршрутизации, keyword-выбор удалён.
        Если аргументы офлайн не извлеклись и провайдера нет — один
        уточняющий вопрос вместо ошибки.
        """
        if risk.needs_confirmation or decision is None:
            return None
        if decision.kind not in ("action", "fresh_data"):
            return None
        tool = decision.tool
        if not tool:
            return None
        if decision.confidence < 0.55:
            return None

        cap = CAPABILITIES.get(tool)
        if cap is None:
            return None
        caps = [cap]

        args = self._extract_simple_args(goal, cap)
        if args is None:
            # Аргумент офлайн не извлёкся. Для детерминированных инструментов
            # (например add_reminder «о чём напомнить?») — уточнение, не ошибка.
            if tool in self._DETERMINISTIC_TOOLS and not self._llm_available_cached():
                return self._clarify_for_tool(goal, tool, risk, trace=[])
            return None

        exec_risk = assess_risk(goal, cap.name, args)
        if exec_risk.needs_confirmation:
            return None

        # Fast-path telemetry must never serialize the user request on a
        # synchronous file logger; structured traces retain the evidence.
        log.debug("FAST PATH: %s(%s)", cap.name, redact_args(args))
        is_deterministic_fast = cap.name in self._DETERMINISTIC_TOOLS
        outcome = self._execute_verified(
            goal=goal, tool=cap.name, args=args, mission=mission, cancel=cancel,
            trace=[f"fast path -> {cap.name}"], risk=exec_risk, caps=caps,
            fast_path=True, finalize_response=not is_deterministic_fast,
        )
        if not outcome.text and outcome.action_result is not None and outcome.verification is not None:
            outcome.text = self._format_deterministic_result(
                tool=cap.name, result=outcome.action_result,
                verification=outcome.verification, args=args,
            )
        outcome.mode = "fast_path"
        return outcome

    #: Человеческие уточнения для инструментов без извлечённого аргумента.
    _TOOL_CLARIFY = {
        "add_reminder": "О чём напомнить?",
        "open_app": "Какое приложение открыть?",
        "close_app": "Какое приложение закрыть?",
        "play_music": "Что включить?",
        "search_files": "Что ищем?",
        "volume": "Сделать громче или тише?",
    }

    def _clarify_for_tool(self, goal: str, tool: str, risk: RiskAssessment,
                          trace: List[str]) -> AgentOutcome:
        question = self._TOOL_CLARIFY.get(tool, "Уточните, пожалуйста, что именно сделать?")
        return AgentOutcome(
            text=question, verified=False, tool_used=None,
            risk=risk, mode="clarification", trace=trace + [f"clarify for {tool} (offline, args missing)"],
        )

    def _try_fresh_information(
        self, goal: str, mission: Optional[Mission], cancel: threading.Event,
        risk: RiskAssessment, trace: List[str],
        decision: Optional[SemanticRoutingDecision] = None,
    ) -> Optional[AgentOutcome]:
        """Route freshness-sensitive questions to live evidence before chat.

        2026-09-05: substring-маркеры currency/weather/news удалены —
        решение «свежие данные» и инструмент приходит из decision.
        """
        if risk.needs_confirmation or cancel.is_set():
            return None
        if decision is None or decision.kind != "fresh_data":
            return None
        tool = decision.tool
        if tool not in ("public_data", "weather", "web_search"):
            return None
        cap = CAPABILITIES.get(tool)
        if cap is None:
            return AgentOutcome(
                text=f"Источник свежих данных {tool} не зарегистрирован.",
                verified=False, tool_used=tool, mode="tool",
                trace=trace + ["freshness route missing capability"],
            )
        args = self._extract_simple_args(goal, cap)
        if args is None:
            args = {"query": goal.strip(), "max_results": 5} if tool == "web_search" else {}
        trace.append(f"freshness route -> {tool} (routing)")
        is_simple_fact = (
            (tool == "public_data" and args.get("kind") == "currency")
            or (tool == "weather" and args.get("forecast_days", 1) == 1)
        )
        outcome = self._execute_verified(
            goal=goal, tool=tool, args=args, mission=mission, cancel=cancel,
            trace=trace, risk=assess_risk(goal, tool, args), caps=[cap],
            fast_path=True,
            routing=self._model_router.route(goal, context_tokens=0),
            finalize_response=not is_simple_fact,
        )
        if not outcome.text and outcome.action_result is not None and outcome.verification is not None:
            outcome.text = self._format_deterministic_result(
                tool=tool, result=outcome.action_result,
                verification=outcome.verification, args=args,
            )
        return outcome


    def _extract_simple_args(self, goal: str, cap: Capability) -> Optional[Dict[str, Any]]:
        """Тривиальное извлечение аргументов для fast path (без модели)."""
        text = goal.strip().rstrip(".!?")
        lowered = text.lower()

        if cap.name in ("open_app", "close_app"):
            for verb in ("открой", "открыть", "запусти", "запустить", "включи",
                         "закрой", "закрыть", "заверши", "open", "launch", "close", "start"):
                if lowered.startswith(verb):
                    name = text[len(verb):].strip(" ,:—-")
                    # отбрасываем служебные слова
                    for filler in ("приложение ", "программу ", "app "):
                        if name.lower().startswith(filler):
                            name = name[len(filler):]
                    if name:
                        return {"name": name}
            return None

        if cap.name == "system_status":
            return {}

        if cap.name == "current_time":
            return {}

        if cap.name == "play_music":
            local_markers = (
                "с компьютера", "локальную музыку", "локальная музыка",
                "local file", ".mp3",
            )
            if any(marker in lowered for marker in local_markers):
                return {"source": "local", "allow_network": False}
            source = "spotify" if "spotify" in lowered else "youtube"
            query = lowered
            for marker in (
                "поставь музыку", "включи музыку", "поставь трек", "включи трек",
                "поставь песню", "включи песню", "на ютубе", "на youtube", "youtube",
                "в spotify", "spotify",
            ):
                query = query.replace(marker, " ")
            chooser_markers = (
                "на свой вкус", "сам выбери", "сама выбери", "на твой выбор",
                "что-нибудь хорошее", "что нибудь хорошее", "на свое усмотрение",
            )
            assistant_choice = any(marker in lowered for marker in chooser_markers)
            for marker in chooser_markers:
                query = query.replace(marker, " ")
            query = " ".join(query.strip(" .,!?:;—-").split())
            if assistant_choice or not query or query in {
                "музыку", "музыка", "трек", "песню", "песня",
            }:
                preference = str(self._user_context.get("music_preference") or "").strip()
                query = preference or self._choose_music_query(goal)
            return {
                "query": query or "русская музыка для хорошего настроения",
                "source": source,
                "allow_network": True,
            }

        if cap.name == "web_search":
            query = text
            for marker in (
                "найди информацию о", "найди информацию", "найди", "поищи",
                "поиск", "найти информацию о", "найти информацию", "найти",
                "search for", "search", "find",
            ):
                if lowered.startswith(marker):
                    query = text[len(marker):].strip(" ,:—-")
                    break
            return {"query": query, "max_results": 5} if query else None

        if cap.name == "volume":
            if any(w in lowered for w in ("выключи звук", "mute", "без звука")):
                return {"action": "mute"}
            if any(w in lowered for w in ("тише", "убавь", "потише", "down")):
                direction = "down"
            elif any(w in lowered for w in ("громче", "прибавь", "погромче", "up")):
                direction = "up"
            else:
                return None
            args: Dict[str, Any] = {"action": direction}
            # Детерминированное извлечение процентов: "сделай на 30 потише".
            m = re.search(r"(\d{1,3})\s*(?:%|процент)", lowered)
            if m:
                args["percent"] = min(max(int(m.group(1)), 1), 100)
            return args

        if cap.name == "list_reminders":
            return {}

        if cap.name == "cancel_reminder":
            return {}

        if cap.name == "add_reminder":
            return self._extract_reminder_args(goal)

        if cap.name == "weather":
            days = 1
            m = re.search(r"на\s+(\d+)\s+дн", lowered)
            if m:
                days = min(max(int(m.group(1)), 1), 7)
            return {"forecast_days": days}

        if cap.name == "public_data":
            if "новост" in lowered:
                query = re.sub(
                    r"\b(последние|последняя|свежие|сегодня|новости|новость)\b",
                    " ", lowered,
                )
                return {
                    "kind": "news",
                    "query": " ".join(query.split()) or "главные события",
                    "max_results": 5,
                }
            return {"kind": "currency"}

        if cap.name == "list_files":
            m = re.search(
                r"(?:папк\w*|каталог\w*|директор\w*)\s+([A-Za-zА-Яа-я0-9_:\\/.\- ]+)$",
                text, re.IGNORECASE,
            )
            return {"dir_path": m.group(1).strip()} if m else {}

        if cap.name == "search_files":
            query = text
            for marker in ("найди файлы", "найди файл", "поищи файлы", "поищи файл",
                           "найди документы", "ищи файлы", "search files"):
                if lowered.startswith(marker):
                    query = text[len(marker):].strip(" ,:—-")
                    break
            if not query:
                return None
            return {"query": query, "dir_path": ""}

        if cap.name == "screen_capture":
            return {}

        return None

    def _extract_reminder_args(self, goal: str) -> Optional[Dict[str, Any]]:
        """Детерминированное извлечение напоминания: минуты + текст.

        Поддерживает «через N минут/секунд/час(ов)» и «в ЧЧ:ММ».
        Возвращает None, если время не распознано или текст пуст —
        тогда fast path уйдёт в уточняющий вопрос / planner.
        """
        from datetime import datetime, timedelta

        text = goal.strip().rstrip(".!?")
        lowered = text.casefold().replace("ё", "е")
        minutes: Optional[float] = None

        m = re.search(r"\bв\s*(\d{1,2})[:.](\d{2})\b", lowered)
        if m:
            now = datetime.now()
            target = now.replace(
                hour=int(m.group(1)) % 24, minute=int(m.group(2)),
                second=0, microsecond=0,
            )
            if target <= now:
                target += timedelta(days=1)
            minutes = max((target - now).total_seconds() / 60.0, 0.1)
        else:
            m = re.search(
                r"через\s+(\d+(?:[.,]\d+)?)\s*(секунд|минут|час)", lowered,
            )
            if m:
                value = float(m.group(1).replace(",", "."))
                unit = m.group(2)
                if unit.startswith("секунд"):
                    minutes = value / 60.0
                elif unit.startswith("час"):
                    minutes = value * 60.0
                else:
                    minutes = value
        if minutes is None:
            return None

        # Текст напоминания: всё после «напомни...» минус временная фраза.
        body = re.split(r"напомни(?:ть)?", text, maxsplit=1, flags=re.IGNORECASE)
        remainder = body[1] if len(body) > 1 else ""
        remainder = re.sub(
            r"^\s*(?:через\s+\d+(?:[.,]\d+)?\s*(?:секунд\w*|минут\w*|час\w*|часа?)\s*"
            r"|в\s*\d{1,2}[:.]\d{2}\s*|мне\s*)",
            " ", remainder, flags=re.IGNORECASE,
        )
        reminder_text = remainder.strip(" ,:—-")
        if not reminder_text:
            return None
        return {"text": reminder_text, "minutes": round(float(minutes), 3)}

    _LLM_PROBE_TTL = 30.0

    def _llm_available_cached(self) -> bool:
        """Реальная доступность провайдера (health-эквивалент, кэш 30 c).

        Не флаг режима: deepseek_brain_mode может быть включён при
        недоступном провайдере — этот метод честно отвечает False.
        """
        now = time.monotonic()
        cached = getattr(self, "_llm_probe", None)
        if cached is not None and now - cached[0] < self._LLM_PROBE_TTL:
            return bool(cached[1])
        try:
            available = bool(self._model_router.is_llm_available())
        except Exception as exc:
            log.debug("LLM availability probe failed: %s", exc)
            available = False
        self._llm_probe = (now, available)
        return available

    #: Честный ответ для неподдерживаемых запросов (шаг 5). Никаких
    #: подмен похожим инструментом и вызовов провайдера.
    _UNSUPPORTED_HINTS = {
        "file_delete": "удаление файлов пока не поддерживается — сделайте это вручную в Проводнике",
        "power": "управление питанием компьютера пока не поддерживается",
        "registry": "работа с реестром Windows не поддерживается",
        "email": "отправка почты пока не поддерживается",
    }

    def _handle_unsupported(self, decision: SemanticRoutingDecision, goal: str,
                            mission: Optional[Mission], trace: List[str]) -> AgentOutcome:
        top = [c for c, _ in (decision.candidates or [])][:3]
        self._log_unsupported_request(goal, decision, top)
        trace.append(f"unsupported: tool=None candidates={top}")
        if mission is not None:
            mission.metadata["unsupported"] = {"candidates": top}
        reason = "эта возможность пока не реализована"
        text = (
            f"Я не умею {reason}. Пока не буду делать этого, чтобы не "
            f"навредить. Могу показать список того, что умею — просто спросите."
        )
        return AgentOutcome(
            text=text, verified=True, tool_used=None, mode="unsupported",
            trace=trace,
        )

    def _log_unsupported_request(self, goal: str, decision: SemanticRoutingDecision,
                                 top: List[str]) -> None:
        """Backlog продукта: неподдерживаемые запросы в jsonl (без секретов)."""
        try:
            import json
            from datetime import datetime, timezone
            from pathlib import Path
            from core.redact import redact_secrets
            from config.settings import Settings

            data_dir = Path(Settings().data_dir) if not self._settings else Path(self._settings.data_dir)
            log_dir = data_dir / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "text": str(redact_secrets(goal))[:300],
                "top_candidates": list(top),
                "risk": decision.risk,
            }
            with open(log_dir / "unsupported_requests.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            log.debug("Не удалось записать unsupported request: %s", exc)

    def _choose_music_query(self, goal: str) -> str:
        """Use the configured brain once for an actual choice, never chooser words."""
        fallback = "русская музыка для хорошего настроения"
        if not self.deepseek_brain_mode:
            return fallback
        backend, _ = self._backend_for_routing(
            self._model_router.route(goal, context_tokens=0),
        )
        if backend is None:
            return fallback
        try:
            answer = backend.direct(
                "Выбери один реальный трек или готовый музыкальный запрос для YouTube. "
                "Верни только исполнителя и название, одной строкой, без кавычек и пояснений. "
                f"Пожелание пользователя: {goal}",
                system="Ты музыкальный редактор JARVIS. Выбирай конкретно и естественно.",
                max_tokens=40, temperature=0.7,
            )
            clean = " ".join(str(answer or "").split()).strip(" \"'«»")
            return clean[:120] or fallback
        except Exception as exc:
            log.warning("Music choice generation failed: %s", exc)
            return fallback

    # ------------------------------------------------------------------ #
    #  Capability discovery — compact whole surface before tool schemas
    # ------------------------------------------------------------------ #

    def _discover_capabilities(self, *, goal: str,
                               routing: Optional[RoutingDecision],
                               memory_ctx: str,
                               action_expected: bool = False,
                               exclude_ids: Sequence[str] = ()) -> tuple[Optional[CapabilityDiscovery], str]:
        """Ask the production brain which live capability family fits a goal.

        This is deliberately a separate, schema-free turn.  It gives the
        model progressive disclosure over the complete registry without
        placing every JSON Schema in every request.
        """
        backend, _ = self._backend_for_routing(routing)
        if backend is None:
            return None, "DeepSeek runtime unavailable"
        try:
            world_state = self._executive.world.context_for(goal)
        except Exception:
            world_state = {}
        from persona.system_prompt import build_agent_system_prompt
        surface = CAPABILITIES.surface_summary()
        failed_ids = tuple(str(name).strip() for name in exclude_ids if str(name).strip())
        recovery_rule = (
            f"The previously attempted capability IDs {list(failed_ids)!r} failed verification. "
            "For recovery, select a different viable capability or choose answer/clarify.\n"
            if failed_ids else ""
        )
        system = (
            f"{build_agent_system_prompt(self._settings, tier='plan')}\n"
            "Ты выбираешь доступные возможности JARVIS до загрузки schemas. "
            "Только JSON без markdown: "
            '{"decision":"answer|clarify|act","intent_clear":true|false,'
            '"capability_ids":["id",...],"required_capability_ids":["id",...],'
            '"clarification":""}.\n'
            "Полный каталог ниже содержит только реальные live capability IDs. "
            "Для decision=act capability_ids — это небольшой набор schemas, доступных для плана, "
            "включая полезные fallback-варианты. required_capability_ids — подмножество capability_ids: "
            "только минимальные независимые шаги, которые действительно должны выполниться для всех "
            "явно запрошенных частей цели. Альтернативные способы одного результата (например, "
            "browser_bridge или open_app для открытия сайта) не делай одновременно обязательными. "
            "Выбирай act лишь когда задача или контекст текущего диалога дают "
            "достаточно ясное намерение выполнить действие. Если референс ('его', "
            "это', 'там', 'да') не разрешается историей, выбери clarify. "
            "Для act обязательно intent_clear=true, непустой required_capability_ids и clarification=''. "
            "Для answer оба списка IDs должны быть пусты. Для clarify обязательно "
            "intent_clear=false и clarification должен содержать один естественный "
            "уточняющий вопрос о конкретно недостающем объекте или параметре; не утверждай, "
            "что какое-либо действие уже выполнено. "
            f"Deterministic intent boundary: action_expected={bool(action_expected)}. "
            "Когда action_expected=true, decision=answer недопустим: выбери act или конкретный clarify. "
            "Не выбирай capability только потому, что она упоминалась в прошлой "
            "реплике ассистента. Для неизвестной задачи ищи композицию доступных "
            "возможностей, а не объявляй её неподдерживаемой.\n"
            f"{recovery_rule}"
            f"World/context state: {json.dumps({'attention': self._user_context, 'world': world_state}, ensure_ascii=False, default=str)[:4000]}\n"
            f"Relevant memory: {memory_ctx or '(none)'}\n"
            f"Capability surface:\n{surface}"
        )
        history = [dict(item) for item in self._session.get_recent()]
        prompt = f"Current user turn: {goal}\nReturn discovery JSON only."
        messages = history + [{"role": "user", "content": prompt}]
        last_error = ""
        for attempt in range(2):
            try:
                raw = self._stream_consume(
                    backend, messages,
                    system, extract_answer=False,
                    max_tokens=256,
                )
            except Exception as exc:
                return None, f"DeepSeek discovery error: {type(exc).__name__}: {exc}"
            parsed = parse_structured(raw, required_keys=None)
            if not parsed.ok or not isinstance(parsed.data, dict):
                last_error = f"invalid discovery JSON: {parsed.error or 'object expected'}"
            else:
                data = parsed.data
                decision = str(data.get("decision") or "").strip().casefold()
                raw_ids = data.get("capability_ids") or []
                raw_required_ids = data.get("required_capability_ids") or []
                intent_clear = bool(data.get("intent_clear", False))
                clarification = str(data.get("clarification") or "").strip()
                if decision not in {"answer", "clarify", "act"}:
                    last_error = "discovery decision must be answer, clarify or act"
                elif not isinstance(raw_ids, list):
                    last_error = "capability_ids must be an array"
                elif not isinstance(raw_required_ids, list):
                    last_error = "required_capability_ids must be an array"
                else:
                    ids = tuple(str(item).strip() for item in raw_ids if str(item).strip())
                    required_ids = tuple(
                        str(item).strip() for item in raw_required_ids if str(item).strip()
                    )
                    if decision == "act" and (
                        not ids or not required_ids or not intent_clear or clarification
                        or any(name not in ids for name in required_ids)
                    ):
                        last_error = (
                            "act requires capability IDs, a non-empty required subset, "
                            "intent_clear=true and empty clarification"
                        )
                    elif decision == "answer" and action_expected:
                        last_error = "answer is invalid because the deterministic intent boundary requires action"
                    elif decision == "answer" and (ids or required_ids or clarification):
                        last_error = "answer requires empty capability IDs, required IDs and clarification"
                    elif decision == "clarify" and (intent_clear or not clarification or required_ids):
                        last_error = (
                            "clarify requires intent_clear=false, empty required IDs "
                            "and one concrete question"
                        )
                    else:
                        return CapabilityDiscovery(
                            decision=decision,
                            capability_ids=ids,
                            intent_clear=intent_clear,
                            clarification=clarification,
                            required_capability_ids=required_ids,
                        ), ""
            if attempt == 0:
                messages = messages + [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": (
                        f"Discovery JSON rejected: {last_error}. Re-evaluate the current user turn; "
                        "return one internally consistent JSON object only."
                    )},
                ]
        return None, last_error or "invalid discovery decision"

    # ------------------------------------------------------------------ #
    #  §13 — Структурированное решение модели
    # ------------------------------------------------------------------ #

    def _decide_with_model(self, goal: str, caps: List[Capability],
                           mission: Optional[Mission],
                           cancel: threading.Event,
                           routing: Optional[RoutingDecision] = None,
                           memory_ctx: str = "",
                           execution_context: str = "",
                           require_tool: bool = False):
        """Спрашивает модель, что делать, и валидирует ответ (§13).

        При плохом JSON — повторный запрос с текстом ошибки (repair, §13).
        Использует backend, выбранный ``ModelRouter`` (§15, cloud-first):
        решение роутера ДОЛЖНО доходить до реального вызова модели, иначе
        выбор тира бессмыслен (P0-2). Локальная FAST-модель — только
        офлайн-фолбэк.

        Args:
            goal: цель пользователя.
            caps: отобранные релевантные инструменты.
            mission: опциональная миссия (для событий/статуса).
            cancel: флаг отмены.
            routing: решение ModelRouter (требуемый тир + цепочка фолбэков).
            memory_ctx: релевантный контекст памяти (P0-5), вставляется в промпт.

        Returns:
            ``(ToolCallDecision | None, error_text)``.
        """
        # Sprint 3 TIER 2: JSON-план строит минимум внешний тир (analyst):
        # надёжный tool calling важнее скорости планирования. Разговорный
        # путь сюда не доходит (conversation gate Sprint 2).
        if routing is not None:
            routing = self._model_router.route_for_planning(routing)
        backend, used_tier = self._backend_for_routing(routing)
        if backend is None:
            return None, MODEL_ERROR_PREFIX + "ни одна модель недоступна (ни облачная, ни локальный фолбэк)"
        if mission is not None and used_tier is not None:
            mission.model_used = used_tier.value
            if routing is not None and used_tier is not routing.tier:
                mission.metadata["degraded"] = True

        known = [c.name for c in caps]
        tools_desc = describe_tools_for_model(caps, self._registry) or "(нет подходящих инструментов)"

        memory_block = ""
        if memory_ctx:
            memory_block = (
                "\n\nКонтекст из памяти (используй при ответе, если релевантно):\n"
                f"{memory_ctx}\n"
            )

        history_messages = [dict(item) for item in self._session.get_recent()]
        world_state = {}
        try:
            world_state = self._executive.world.context_for(goal)
        except Exception as exc:
            log.debug("World state unavailable for brain context: %s", exc)
        world_block = json.dumps({
            "attention": self._user_context,
            "world": world_state,
        }, ensure_ascii=False, default=str)[:4000]

        # Sprint 4 TIER 2: persona + фокус на точность tool calling.
        # История/факты в tool-промпт НЕ идут (спринт STEP 2.4).
        from persona.system_prompt import build_agent_system_prompt
        persona_line = build_agent_system_prompt(self._settings, tier="plan")
        if self.deepseek_brain_mode:
            required_tool_rule = (
                "Capability discovery уже установил ясное намерение выполнить действие. "
                "В этом ходе ОБЯЗАТЕЛЬНО вызови ровно один доступный tool; текст без tool "
                "не исполняет цель и будет отклонён.\n"
                if require_tool else ""
            )
            system = (
                f"{persona_line}\n"
                "Ты — единственный conversational/reasoning brain JARVIS. Backend даёт тебе "
                "контекст, память, world state и tools; backend не сочиняет реплики вместо тебя.\n"
                "Сам реши: ответить, уточнить, вызвать один доступный tool или продолжить план. "
                "Для tool используй native function call. Если tool не нужен, ответь живым текстом.\n"
                f"{required_tool_rule}"
                "Получив verified observations от предыдущих шагов, сопоставь их с исходной целью: "
                "если цель достигнута, не повторяй действие и верни ответ без tool; если нет — "
                "выбери только следующий необходимый tool.\n"
                "После хотя бы одного действия answer без tool допустим только когда verified observations "
                "подтверждают каждый явно запрошенный результат исходной цели. Нахождение объекта не "
                "подтверждает его чтение, открытая поисковая страница не подтверждает воспроизведение, "
                "а успешный промежуточный шаг не завершает составную задачу. Если доказательства не хватает, "
                "вызови следующий подходящий tool или честно уточни, но не заявляй незавершённый результат.\n"
                "После verified tool result финальную человеческую реплику снова сформируй сам. "
                "Не утверждай выполнение без результата инструмента.\n"
                "Если текущая реплика короткая, неоднозначная или является продолжением "
                "диалога (например, 'что именно?'), не вызывай tool: используй историю и "
                "ответь либо задай один уточняющий вопрос. Не выводи side effect только из "
                "предыдущего предложения JARVIS. Инструменты с побочным действием (приложения, "
                "музыка, напоминания, файлы и системные изменения) требуют явного намерения "
                "в текущей реплике пользователя.\n"
                "Текст answer показывается пользователю дословно. Не описывай внутреннее "
                "рассуждение: не пиши 'пользователь сказал', 'пользователь просит', "
                "'отвечу живым текстом', имена маршрутов, JSON или план. Сразу дай ответ "
                "пользователю. Не пересказывай текущую реплику и не называй себя системой, "
                "планировщиком или исполнителем.\n"
                "Для голого запроса о музыке без известного предпочтения задай один короткий вопрос "
                "о жанре/исполнителе и не вызывай play_music.\n"
                f"Персона и правила безопасности:\n{persona_line}\n"
                f"World/context state:\n{world_block}\n"
                f"Relevant memory:\n{memory_ctx or '(нет релевантной памяти)'}"
            )
        else:
            system = (
                f"{persona_line}\n"
                "Твоя задача — решить, КАК выполнить цель.\n"
                "Верни СТРОГО ОДИН JSON-объект без markdown и пояснений:\n"
                f"{PLAN_SCHEMA_HINT}\n"
                "Правила:\n"
                "- Если для цели подходит инструмент — укажи его имя в 'tool' и заполни 'arguments'.\n"
                "- Если инструмент не нужен (обычный разговор) — 'tool': null и заполни 'answer'.\n"
                "- Никогда не выдумывай инструменты, которых нет в списке.\n"
                "- 'verification' — как фактически проверить, что цель достигнута."
            )
        observation_block = (
            f"\n\nVerified observations from earlier steps:\n{execution_context}\n"
            if execution_context else ""
        )
        user = (
            f"Доступные инструменты:\n{tools_desc}\n\n"
            f"Цель пользователя: {goal}\n"
            f"{memory_block}\n"
            f"{observation_block}"
            "Выбери: естественный ответ или один native tool call."
        )
        planner_messages = history_messages + [{"role": "user", "content": user}]

        if self.deepseek_brain_mode and not self._native_tool_schemas(caps):
            try:
                answer = self._stream_consume(
                    backend, planner_messages, system, extract_answer=False,
                    max_tokens=max(128, int(getattr(self._settings.local_model, "max_tokens", 384))),
                ).strip()
                if answer:
                    return ToolCallDecision(
                        tool=None, reason="DeepSeek direct answer", answer=answer,
                    ), ""
                return None, MODEL_ERROR_PREFIX + "DeepSeek вернул пустой ответ"
            except Exception as exc:
                return None, MODEL_ERROR_PREFIX + f"DeepSeek недоступен: {exc}"

        # Native function calling is attempted once, before the legacy text
        # planner.  The fallback remains the source of compatibility for old
        # llama.cpp wheels, small models and provider-specific templates.
        native_enabled = bool(getattr(
            getattr(self._settings, "local_model", None),
            "native_tool_calling", True,
        ))
        native_call = getattr(backend, "chat_with_tools", None)
        if native_enabled and getattr(backend, "supports_tools", False) and callable(native_call):
            native_tools = self._native_tool_schemas(caps)
            if native_tools:
                try:
                    native_response = native_call(
                        planner_messages,
                        tools=native_tools,
                        system=system,
                        tool_choice="required" if require_tool else "auto",
                        max_tokens=getattr(self._settings.local_model, "max_tokens", None),
                        temperature=getattr(self._settings.local_model, "temperature", None),
                    )
                    if getattr(native_response, "tool_calls", ()):
                        if len(native_response.tool_calls) != 1:
                            raise ToolsNotSupportedError(
                                "один запрос должен содержать ровно один tool call"
                            )
                        call = native_response.tool_calls[0]
                        native_data = {
                            "tool": call.name,
                            "arguments": dict(call.arguments),
                            "reason": "native function call",
                            "risk": "low",
                            "verification": "проверка результата зарегистрированного инструмента",
                            "answer": native_response.content,
                        }
                    else:
                        # Some providers use native mode for ordinary answers
                        # too.  Keep them in the same validated decision type.
                        native_data = {
                            "tool": None,
                            "arguments": {},
                            "reason": "native text response",
                            "risk": "low",
                            "verification": "",
                            "answer": native_response.content,
                        }
                    native_decision, native_error = validate_tool_call(
                        native_data,
                        known,
                        schema_lookup=lambda n: getattr(self._registry.get(n), "input_schema", None),
                    )
                    if (native_decision is not None and require_tool
                            and not native_decision.needs_tool):
                        native_error = "capability discovery requires a tool call"
                        native_decision = None
                    if native_decision is not None:
                        if mission is not None:
                            mission.metadata["native_tool_calling"] = True
                            mission.metadata["decision"] = native_decision.to_dict()
                            if native_decision.needs_tool:
                                mission.add_step(
                                    description=native_decision.reason or f"вызов {native_decision.tool}",
                                    tool=native_decision.tool,
                                    args=native_decision.arguments,
                                )
                                mission.emit(EVENT_PLAN_READY, payload={"plan": mission.plan})
                        return native_decision, ""
                    log.info("Нативный tool call отклонён: %s", native_error)
                except (ToolsNotSupportedError, BackendUnavailable, BackendConfigError, ValueError) as exc:
                    # Fall through to the already-tested JSON repair loop.  A
                    # provider capability mismatch must never become a fake
                    # successful mutation.
                    log.info("Native tool calling unavailable; JSON fallback: %s", exc)

        last_error = ""
        structured_system = system + (
            "\nFallback protocol: верни только один JSON-объект без markdown:\n"
            f"{PLAN_SCHEMA_HINT}"
            + (
                "\nВ этом ходе поле tool обязано содержать имя одного доступного "
                "инструмента; tool=null будет отклонён."
                if require_tool else ""
            )
        )
        for attempt in range(1, self._config.max_structured_retries + 2):
            if cancel.is_set():
                return None, "отменено"

            prompt = user if attempt == 1 else (
                f"{user}\n\nПредыдущий ответ был отклонён: {last_error}\n"
                "Исправь и верни ТОЛЬКО валидный JSON."
            )
            try:
                raw = self._stream_consume(backend, history_messages + [{"role": "user", "content": prompt}], structured_system)
            except (BackendUnavailable, BackendConfigError) as exc:
                # Попробуем следующий тир из цепочки фолбэка (cloud->...->local).
                log.warning("Модель недоступна (%s), пробуем фолбэк: %s", used_tier, exc)
                # Простая (разговорная) задача: фолбэк тоже короткий — нечего
                # ждать полный бюджет аналитического тира ради «привет».
                fb_policy = (
                    {"timeout": getattr(self._settings.limits, "fast_tier_timeout_sec", None),
                     "max_retries": getattr(self._settings.limits, "fast_tier_max_retries", None)}
                    if routing is not None and routing.tier is Tier.FAST else None
                )
                fb_backend, fb_tier = self._fallback_backend(routing, tried=used_tier,
                                                             policy_override=fb_policy)
                if fb_backend is None:
                    return None, MODEL_ERROR_PREFIX + f"модель недоступна: {exc}"
                backend, used_tier = fb_backend, fb_tier
                if mission is not None:
                    mission.model_used = used_tier.value
                    mission.metadata["degraded"] = True
                try:
                    raw = self._stream_consume(backend, history_messages + [{"role": "user", "content": prompt}], structured_system)
                except Exception as exc2:
                    return None, MODEL_ERROR_PREFIX + f"модель недоступна: {exc2}"
            except Exception as exc:
                log.warning("Модель не ответила на планирование: %s", exc)
                return None, MODEL_ERROR_PREFIX + f"модель недоступна: {exc}"

            parsed = parse_structured(raw, required_keys=None)
            if not parsed.ok:
                last_error = parsed.error
                log.info("Плохой JSON от модели (попытка %d): %s", attempt, last_error)
                continue

            decision, err = validate_tool_call(
                parsed.data or {},
                known,
                schema_lookup=lambda n: getattr(self._registry.get(n), "input_schema", None),
            )
            if decision is None:
                last_error = err
                log.info("Невалидный tool call (попытка %d): %s", attempt, err)
                continue
            if require_tool and not decision.needs_tool:
                last_error = "capability discovery requires a tool call"
                log.info("Tool call обязателен (попытка %d)", attempt)
                continue

            if mission is not None:
                mission.metadata["decision"] = decision.to_dict()
                if decision.needs_tool:
                    mission.add_step(
                        description=decision.reason or f"вызов {decision.tool}",
                        tool=decision.tool,
                        args=decision.arguments,
                    )
                    mission.emit(EVENT_PLAN_READY, payload={"plan": mission.plan})
            return decision, ""

        return None, last_error or "модель не смогла вернуть валидное решение"

    def _evaluate_goal_progress(self, *, goal: str,
                                observations: List[Dict[str, Any]],
                                routing: Optional[RoutingDecision],
                                memory_ctx: str,
                                required_capability_ids: Sequence[str] = ()) -> tuple[Optional[GoalProgressDecision], str]:
        """Decide task completion from evidence, not from tool success."""
        backend, _ = self._backend_for_routing(routing)
        if backend is None:
            return None, "DeepSeek runtime unavailable for goal verification"
        from persona.system_prompt import build_agent_system_prompt
        system = (
            f"{build_agent_system_prompt(self._settings, tier='plan')}\n"
            "Ты проверяешь достижение исходной пользовательской цели после выполненных действий. "
            "Верни только JSON: "
            '{"status":"complete|continue|repair|clarify","tool":"capability_id|null",'
            '"arguments":{},"reason":"...","answer":"...",'
            '"evidence":[{"step":1,"tool":"exact_observed_tool"}]}.\n'
            "Tool success означает только успех одного шага. status=complete допустим только если "
            "observations фактически подтверждают каждый требуемый результат исходной цели. "
            "Для status=complete поле evidence обязательно и должно перечислять реальные step/tool "
            "из observations, на которых основан вывод. Не ссылайся на tool, которого нет в observations. "
            "Если найден путь, но содержимое ещё не прочитано, задача чтения не завершена. "
            "Если исходная цель отдельно требует физически открыть файл в desktop UI и прочитать его, "
            "read_file подтверждает только чтение: открытие должно иметь отдельное verified observation. "
            "Для композиции search_files + open_app конкретный найденный файл должен передаваться в "
            "open_app.arguments.target_path; запуск пустого приложения не подтверждает открытие файла. "
            "Если страница открыта, но требуемое действие на ней не наблюдалось, задача не завершена. "
            "Запущенный процесс Проводника не доказывает открытие нужной папки: для конкретного "
            "target_path требуется strict verification method=explorer_location. "
            "Для continue выбери следующий capability и его конкретные arguments. Для repair выбери "
            "другой путь после failed verification. Для clarify не выполняй действие и задай один вопрос. "
            "Если required_capability_ids содержит capability без verified observation, следующим шагом "
            "выбирай один из таких missing required IDs, а не повторяй уже verified capability. "
            "Не повторяй уже успешно выполненный шаг. Не выдумывай capabilities.\n"
            f"Relevant memory: {memory_ctx or '(none)'}\n"
            f"Complete live capability surface:\n{CAPABILITIES.surface_summary()}"
        )
        payload = {
            "goal": goal,
            "required_capability_ids": list(required_capability_ids),
            "observations": self._compact_goal_observations(observations),
        }
        prompt = json.dumps(payload, ensure_ascii=False, default=str)
        last_error = ""
        for attempt in range(1, self._config.max_structured_retries + 2):
            user_prompt = prompt if attempt == 1 else (
                f"{prompt}\n\nПредыдущая оценка отклонена: {last_error}. "
                "Исправь оценку по фактическим observations и верни только JSON."
            )
            try:
                raw = backend.chat(
                    [{"role": "user", "content": user_prompt}],
                    system=system,
                    max_tokens=512,
                )
            except Exception as exc:
                return None, f"goal verification error: {type(exc).__name__}: {exc}"
            parsed = parse_structured(raw, required_keys=None)
            if not parsed.ok or not isinstance(parsed.data, dict):
                last_error = f"invalid goal verification JSON: {parsed.error or 'object expected'}"
                continue
            data = parsed.data
            status = str(data.get("status") or "").strip().casefold()
            if status not in {"complete", "continue", "repair", "clarify"}:
                last_error = "goal verification status is invalid"
                continue
            reason = str(data.get("reason") or "").strip()
            answer = str(data.get("answer") or "").strip()
            if status == "complete":
                evidence_steps, evidence_error = self._validate_goal_evidence(
                    data.get("evidence"), observations, reason=reason, answer=answer,
                    required_capability_ids=required_capability_ids, goal=goal,
                )
                if evidence_error:
                    last_error = evidence_error
                    continue
                return GoalProgressDecision(
                    status=status, reason=reason, answer=answer,
                    evidence_steps=evidence_steps,
                ), ""
            if status == "clarify":
                return GoalProgressDecision(status=status, reason=reason, answer=answer), ""
            known = [tool.name for tool in self._registry.list_tools()]
            decision, error = validate_tool_call(
                {
                    "tool": data.get("tool"),
                    "arguments": data.get("arguments") or {},
                    "reason": reason or f"goal progress: {status}",
                    "verification": "verify requested goal outcome after this step",
                    "risk": "low",
                },
                known,
                schema_lookup=lambda name: getattr(self._registry.get(name), "input_schema", None),
            )
            if decision is None or not decision.needs_tool:
                last_error = error or f"{status} requires a valid next tool"
                continue
            verified_tools = {
                str(item.get("tool") or "")
                for item in observations
                if isinstance(item.get("verification"), dict)
                and item["verification"].get("verified")
            }
            missing_required = {
                str(name).strip() for name in required_capability_ids
                if str(name).strip() and str(name).strip() not in verified_tools
            }
            if missing_required and decision.tool not in missing_required:
                last_error = (
                    f"next action repeats or bypasses verified work; choose one missing required "
                    f"capability: {sorted(missing_required)}"
                )
                continue
            return GoalProgressDecision(status=status, next_action=decision, reason=reason), ""
        return None, last_error or "goal verification did not produce an evidence-bound decision"

    @staticmethod
    def _required_capability_contract(capability_ids: Sequence[str]) -> tuple[str, ...]:
        """Do not turn generic recovery hands into mandatory specialized steps."""
        requested = tuple(dict.fromkeys(
            str(name).strip() for name in capability_ids if str(name).strip()
        ))
        live = {cap.name: cap for cap in CAPABILITIES.resolve(requested)}
        ordered = tuple(name for name in requested if name in live)
        generic_hands = {
            name for name, cap in live.items()
            if name.startswith("computer_") and "computer" in {
                str(tag).casefold() for tag in cap.tags
            }
        }
        if not generic_hands or not any(name not in generic_hands for name in ordered):
            return ordered
        return tuple(name for name in ordered if name not in generic_hands)

    @staticmethod
    def _compact_goal_observations(observations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Keep the evidence JSON valid while bounding large tool payloads."""
        compact: List[Dict[str, Any]] = []
        for item in observations[-8:]:
            output = json.dumps(item.get("output"), ensure_ascii=False, default=str)
            if len(output) > 5000:
                output = output[:5000] + "<truncated>"
            compact.append({
                "step": item.get("step"),
                "tool": item.get("tool"),
                "arguments": item.get("arguments") or {},
                "output": output,
                "error": item.get("error"),
                "verification": item.get("verification"),
            })
        return compact

    def _validate_goal_evidence(self, raw_evidence: Any,
                                observations: List[Dict[str, Any]], *,
                                reason: str = "", answer: str = "",
                                required_capability_ids: Sequence[str] = (),
                                goal: str = "") -> tuple[tuple[int, ...], str]:
        """Reject completion claims that cite absent or unverified tool steps."""
        if not isinstance(raw_evidence, list) or not raw_evidence:
            return (), "status=complete requires non-empty evidence"
        by_step = {
            int(item.get("step")): item
            for item in observations
            if isinstance(item, dict) and isinstance(item.get("step"), int)
        }
        cited: List[int] = []
        for evidence in raw_evidence:
            if not isinstance(evidence, dict):
                return (), "each evidence item must be an object"
            try:
                step = int(evidence.get("step"))
            except (TypeError, ValueError):
                return (), "evidence step must be an integer"
            tool = str(evidence.get("tool") or "").strip()
            observed = by_step.get(step)
            if observed is None or tool != str(observed.get("tool") or ""):
                return (), f"evidence step {step} does not match an observed tool"
            verification = observed.get("verification") or {}
            if not isinstance(verification, dict) or not verification.get("verified"):
                return (), f"evidence step {step} is not verified"
            cited.append(step)

        observed_tools = {str(item.get("tool") or "") for item in observations}
        claim_text = f"{reason}\n{answer}"
        fabricated = sorted(
            tool.name for tool in self._registry.list_tools()
            if tool.name not in observed_tools and tool.name in claim_text
        )
        if fabricated:
            return (), f"completion cites unobserved tools: {fabricated}"
        verified_tools = {
            str(item.get("tool") or "")
            for item in observations
            if isinstance(item.get("verification"), dict)
            and item["verification"].get("verified")
        }
        missing_required = [
            name for name in required_capability_ids
            if str(name).strip() and str(name).strip() not in verified_tools
        ]
        if missing_required:
            return (), f"required capabilities have no verified observation: {missing_required}"
        required = {str(name).strip() for name in required_capability_ids}
        goal_l = str(goal or "").casefold()
        folder_goal = any(marker in goal_l for marker in (
            "папк", "каталог", "folder", "directory",
        ))
        if folder_goal and "open_app" in required:
            explorer_evidence = any(
                str(item.get("tool") or "") == "open_app"
                and isinstance(item.get("verification"), dict)
                and item["verification"].get("verified")
                and item["verification"].get("method") == "explorer_location"
                for item in observations
            )
            if not explorer_evidence:
                return (), "folder goal requires strict open_app verification method=explorer_location"
        if {"search_files", "open_app"} <= required:
            targeted_open = any(
                str(item.get("tool") or "") == "open_app"
                and bool(str((item.get("arguments") or {}).get("target_path") or "").strip())
                and isinstance(item.get("verification"), dict)
                and item["verification"].get("verified")
                for item in observations
            )
            if not targeted_open:
                return (), (
                    "search_files + open_app completion requires a verified open_app "
                    "observation with target_path"
                )
        return tuple(dict.fromkeys(cited)), ""

    def _native_tool_schemas(self, caps: List[Capability]) -> List[Dict[str, Any]]:
        """Build deterministic OpenAI-compatible schemas from the registry."""
        schemas: List[Dict[str, Any]] = []
        for cap in caps:
            tool = self._registry.get(cap.name)
            if tool is None:
                continue
            schemas.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            })
        return schemas

    def _execute_capability_loop(self, *, goal: str,
                                 first_decision: ToolCallDecision,
                                 mission: Optional[Mission],
                                 cancel: threading.Event,
                                 trace: List[str],
                                 risk: RiskAssessment,
                                 caps: List[Capability],
                                 routing: Optional[RoutingDecision],
                                 memory_ctx: str,
                                 initial_observations: Optional[List[Dict[str, Any]]] = None,
                                 start_step: int = 0,
                                 confirmation_approved: bool = False,
                                 required_capability_ids: Sequence[str] = ()) -> AgentOutcome:
        """Run bounded PLAN -> ACT -> OBSERVE -> VERIFY turns.

        Every tool step uses the existing executor, verifier and repair loop.
        The reasoning model receives only verified observations before it may
        request another selected capability.  A no-tool decision ends the
        loop and delegates the user-facing result to the normal DeepSeek
        final-response boundary.
        """
        decision = first_decision
        current_risk = risk
        observations: List[Dict[str, Any]] = list(initial_observations or [])
        max_steps = min(
            int(self._config.max_plan_steps or 1),
            int(self._config.max_action_iterations or 1),
        )

        for step_index in range(max(0, int(start_step)), max_steps):
            if cancel.is_set():
                return AgentOutcome(text="Задача отменена.", mode="cancelled", trace=trace)
            step_outcome = self._execute_verified(
                goal=goal,
                tool=decision.tool,
                args=decision.arguments,
                mission=mission,
                cancel=cancel,
                trace=trace,
                risk=current_risk,
                caps=caps,
                routing=routing,
                memory_ctx=memory_ctx,
                finalize_response=False,
                confirmation_approved=confirmation_approved,
            )
            confirmation_approved = False
            if step_outcome.action_result is None:
                return step_outcome

            result = step_outcome.action_result
            verification = step_outcome.verification
            observation = {
                "step": step_index + 1,
                "tool": decision.tool,
                "arguments": decision.arguments,
                "output": result.output,
                "error": result.error,
                "verification": verification.to_dict() if verification else None,
            }
            observations.append(observation)
            if not step_outcome.verified:
                def _finalize_failed_step() -> AgentOutcome:
                    """Keep real failure facts, but never expose the harness as chat."""
                    if self.deepseek_brain_mode:
                        text = self._finalize_tool_response(
                            goal=goal,
                            tool=decision.tool,
                            args=decision.arguments,
                            result=result,
                            verification=verification,
                            routing=routing,
                            memory_ctx=(
                                f"{memory_ctx}\nFailed verified observation:\n"
                                f"{json.dumps(observations, ensure_ascii=False, default=str)[-6000:]}"
                            ).strip(),
                            caps=caps,
                        )
                        if text:
                            step_outcome.text = text
                    return step_outcome

                # Existing RepairLoop exhausted its same-tool/fallback policy.
                # Give the brain one observed failure plus the complete compact
                # surface so it can choose a different composition, rather
                # than presenting a hardcoded "unsupported" route.
                recovery_context = json.dumps(observations, ensure_ascii=False, default=str)[-6000:]
                recovery_progress, recovery_progress_error = self._evaluate_goal_progress(
                    goal=goal,
                    observations=observations,
                    routing=routing,
                    memory_ctx=memory_ctx,
                    required_capability_ids=required_capability_ids,
                )
                next_decision = (
                    recovery_progress.next_action
                    if recovery_progress is not None
                    and recovery_progress.status in {"continue", "repair"}
                    else None
                )
                if next_decision is not None and (
                    next_decision.tool == decision.tool
                    and next_decision.arguments == decision.arguments
                ):
                    next_decision = None
                if recovery_progress is not None and recovery_progress.status == "clarify":
                    step_outcome.text = (
                        recovery_progress.answer or recovery_progress.reason
                        or "Уточните следующий шаг."
                    )
                    step_outcome.mode = "clarification"
                    return step_outcome

                recovery, recovery_error = self._discover_capabilities(
                    goal=goal,
                    routing=routing,
                    memory_ctx=(f"{memory_ctx}\nFailed verified observation:\n{recovery_context}").strip(),
                    action_expected=True,
                    exclude_ids=(decision.tool,),
                )
                recovered_ids = (
                    (next_decision.tool,)
                    if next_decision is not None
                    else tuple(recovery.capability_ids)
                    if recovery is not None and recovery.decision == "act" and recovery.intent_clear
                    else ()
                )
                if not recovered_ids:
                    trace.append(
                        "recovery discovery unavailable: "
                        f"{recovery_progress_error or recovery_error or 'no alternate action'}"
                    )
                    return _finalize_failed_step()
                recovered_caps = CAPABILITIES.discover(
                    goal, recovered_ids,
                    top_k=self._config.max_discovered_tools,
                    exclude_ids=(decision.tool,),
                )
                next_error = recovery_progress_error
                if next_decision is None:
                    next_decision, next_error = self._decide_with_model(
                        goal, recovered_caps, mission, cancel,
                        routing=routing,
                        memory_ctx=memory_ctx,
                        execution_context=recovery_context,
                    )
                if (next_decision is None or not next_decision.needs_tool
                        or (next_decision.tool == decision.tool
                            and next_decision.arguments == decision.arguments)):
                    trace.append(f"recovery stopped: {next_error or 'same or no next action'}")
                    return _finalize_failed_step()
                trace.append(f"recovery selected {next_decision.tool}")
                next_risk = assess_risk(goal, next_decision.tool, next_decision.arguments)
                next_authority = self.authorize_delegated_action(
                    mission, goal=goal, tool=next_decision.tool,
                    args=next_decision.arguments, risk=next_risk,
                )
                if (next_risk.needs_confirmation and not next_authority.allowed
                        and not self._config.auto_confirm_high_risk):
                    conf_id = uuid.uuid4().hex
                    with self._lock:
                        self._pending_confirmations[conf_id] = {
                            "goal": goal,
                            "tool": next_decision.tool,
                            "args": next_decision.arguments,
                            "decision": next_decision,
                            "risk": next_risk,
                            "caps": recovered_caps,
                            "mission": mission,
                            "cancel": cancel,
                            "trace": trace,
                            "loop_state": {
                                "observations": observations,
                                "routing": routing,
                                "memory_ctx": memory_ctx,
                                "start_step": step_index + 1,
                                "required_capability_ids": list(required_capability_ids),
                            },
                        }
                    self._start_confirmation_watchdog(conf_id)
                    return AgentOutcome(
                        text=next_risk.confirmation_prompt(),
                        verified=False,
                        needs_confirmation=True,
                        confirmation_id=conf_id,
                        risk=next_risk,
                        tool_used=next_decision.tool,
                        mode="confirmation",
                        trace=trace,
                    )
                caps = recovered_caps
                decision = next_decision
                current_risk = next_risk
                continue

            try:
                self._store_fact(
                    label=f"выполнено: {decision.tool}",
                    detail=f"{goal} -> {str(result.output)[:300]}",
                )
            except Exception as exc:
                log.debug("store_fact не удался: %s", exc)

            execution_context = json.dumps(
                self._compact_goal_observations(observations),
                ensure_ascii=False, default=str,
            )
            progress, progress_error = self._evaluate_goal_progress(
                goal=goal,
                observations=observations,
                routing=routing,
                memory_ctx=memory_ctx,
                required_capability_ids=required_capability_ids,
            )
            if progress is None:
                trace.append(f"goal progress evaluator fallback: {progress_error}")
                next_decision, plan_error = self._decide_with_model(
                    goal, caps, mission, cancel,
                    routing=routing,
                    memory_ctx=memory_ctx,
                    execution_context=execution_context,
                    require_tool=True,
                )
                if next_decision is None or not next_decision.needs_tool:
                    trace.append(f"goal remains unverified: {plan_error or progress_error}")
                    return AgentOutcome(
                        text=(
                            "Выполненный шаг проверен, но достижение всей цели не подтверждено: "
                            f"{progress_error or plan_error or 'нет доказанного следующего шага'}."
                        ),
                        verified=False,
                        verification=verification,
                        tool_used=decision.tool,
                        risk=current_risk,
                        mode="goal_unverified",
                        trace=trace,
                        action_result=result,
                    )
                progress = GoalProgressDecision(
                    status="continue",
                    next_action=next_decision,
                    reason=next_decision.reason,
                )
            trace.append(f"goal progress={progress.status}: {progress.reason}")
            if progress.status == "clarify":
                return AgentOutcome(
                    text=progress.answer or progress.reason or "Уточните следующий шаг.",
                    verified=False,
                    verification=verification,
                    tool_used=decision.tool,
                    risk=current_risk,
                    mode="clarification",
                    trace=trace,
                    action_result=result,
                )
            if progress.status == "complete":
                final_context = (
                    f"{memory_ctx}\nVerified observations for this task:\n{execution_context}"
                ).strip()
                try:
                    text = self._finalize_tool_response(
                        goal=goal,
                        tool=decision.tool,
                        args=decision.arguments,
                        result=result,
                        verification=verification,
                        routing=routing,
                        memory_ctx=final_context,
                        caps=caps,
                    )
                except Exception as exc:
                    log.warning("Finalize tool response exception: %s", exc)
                    text = ""
                    self._last_finalizer_error = str(exc)
                if not text:
                    if verification.verified:
                        finalizer_err = getattr(self, "_last_finalizer_error", "") or "finalizer unavailable or empty"
                        trace.append(f"finalizer_error: {finalizer_err}")
                        if mission is not None:
                            mission.metadata["finalizer_error"] = finalizer_err
                        text = self._format_deterministic_result(
                            tool=decision.tool, result=result, verification=verification, args=decision.arguments,
                        )
                    else:
                        text = "Ошибка DeepInfra: verified tool result получен, но финальная реплика модели не сформирована."
                elif verification.verified and decision.tool in {"public_data", "weather"} and result and result.output:
                    if not verify_atomic_values_preserved(text, result.output):
                        trace.append("finalizer_dropped_fact")
                        if mission is not None:
                            mission.metadata["finalizer_dropped_fact"] = True
                        text = render_structured_fact(result.output)
                return AgentOutcome(
                    text=text,
                    verified=True,
                    verification=verification,
                    tool_used=decision.tool,
                    risk=current_risk,
                    mode="tool",
                    trace=trace,
                    action_result=result,
                )

            next_decision = progress.next_action
            if next_decision is None:
                return self._handle_model_unavailable(
                    goal, mission, trace,
                    MODEL_ERROR_PREFIX + "goal progress requested continuation without a valid action",
                )

            selected_next = CAPABILITIES.discover(
                goal,
                (next_decision.tool,),
                top_k=self._config.max_discovered_tools,
            )
            if selected_next:
                caps = selected_next

            next_risk = assess_risk(goal, next_decision.tool, next_decision.arguments)
            next_authority = self.authorize_delegated_action(
                mission, goal=goal, tool=next_decision.tool,
                args=next_decision.arguments, risk=next_risk,
            )
            if (next_risk.needs_confirmation and not next_authority.allowed
                    and not self._config.auto_confirm_high_risk):
                trace.append(f"next action requires confirmation: {next_risk.reasons}")
                conf_id = uuid.uuid4().hex
                with self._lock:
                    self._pending_confirmations[conf_id] = {
                        "goal": goal,
                        "tool": next_decision.tool,
                        "args": next_decision.arguments,
                        "decision": next_decision,
                        "risk": next_risk,
                        "caps": caps,
                        "mission": mission,
                        "cancel": cancel,
                        "trace": trace,
                        "loop_state": {
                            "observations": observations,
                            "routing": routing,
                            "memory_ctx": memory_ctx,
                            "start_step": step_index + 1,
                            "required_capability_ids": list(required_capability_ids),
                        },
                    }
                self._start_confirmation_watchdog(conf_id)
                return AgentOutcome(
                    text=next_risk.confirmation_prompt(),
                    verified=False,
                    needs_confirmation=True,
                    confirmation_id=conf_id,
                    risk=next_risk,
                    tool_used=next_decision.tool,
                    mode="confirmation",
                    trace=trace,
                )

            decision = next_decision
            current_risk = next_risk

        return AgentOutcome(
            text=(
                f"Ошибка выполнения: план превысил лимит {max_steps} проверенных действий "
                "и остановлен до следующего шага."
            ),
            verified=False,
            tool_used=decision.tool,
            risk=current_risk,
            mode="action_limit",
            trace=trace,
        )

    # ------------------------------------------------------------------ #
    #  §14 + §10/§11 — Выполнение с фактической проверкой и самоисправлением
    # ------------------------------------------------------------------ #

    def _execute_verified(self, goal: str, tool: str, args: Dict[str, Any],
                          mission: Optional[Mission], cancel: threading.Event,
                          trace: List[str], risk: RiskAssessment,
                          caps: List[Capability], fast_path: bool = False,
                          routing: Optional[RoutingDecision] = None,
                          memory_ctx: str = "",
                          finalize_response: bool = True,
                          confirmation_approved: bool = False) -> AgentOutcome:
        """EXECUTE -> VERIFY -> (REPAIR) -> RESULT.

        «Готово» произносится ТОЛЬКО при ``verification.verified`` (§14).
        """
        route_guard = validate_tool_selection(goal, tool, args)
        if not route_guard.allowed:
            trace.append(f"route guard blocked: {route_guard.reason}")
            return AgentOutcome(
                text=(
                    f"Запрос не выполнен: выбран «{tool}», "
                    f"а нужен «{', '.join(route_guard.expected_tools)}». "
                    "Побочного действия не было."
                ),
                verified=False,
                tool_used=tool,
                mode="route_blocked",
                trace=trace,
            )
        context = ToolContext(
            user_id="default",
            settings=self._settings,
            state=None,
            extra={
                "confirmation_approved": bool(confirmation_approved),
                "task_runtime": self._task_runtime,
                "tool_executor": self._tool_executor,
                "authority_grant_id": (
                    self.authorize_delegated_action(
                        mission, goal=goal, tool=tool, args=args, risk=risk,
                    ).grant_id
                    if mission is not None and self._authority is not None else None
                ),
            },
        )

        if not fast_path:
            try:
                execution_plan = self._executive.compile(
                    goal, tool=tool, args=args, intent="action",
                    risk=getattr(risk.level, "value", str(risk.level)),
                )
                rehearsal = self._executive.rehearse(execution_plan)
                trace.append(f"shadow_rehearsal={'ready' if rehearsal.ready else 'blocked'}")
                if mission is not None:
                    mission.metadata["command_os"] = execution_plan.to_dict()
                    mission.metadata["rehearsal"] = rehearsal.to_dict()
            except Exception as exc:
                log.debug("Shadow rehearsal skipped: %s", exc)
        else:
            trace.append("shadow_rehearsal=reflex-safe")

        if mission is not None:
            mission.set_status(MissionStatus.EXECUTING, f"выполняю {tool}")
            mission.set_progress(0.5, f"выполнение: {tool}")
            mission.note_tool(tool)
            mission.emit(EVENT_STEP_STARTED, payload={"tool": tool, "args": args})
            mission.emit(EVENT_TOOL_CALLED, payload={"tool": tool, "args": args})

        web_tool = tool in {"web_search", "web_fetch", "weather", "public_data"}
        budget_cfg = getattr(self._settings, "latency_budgets", None)
        web_timeout = float(getattr(budget_cfg, "research_source_timeout_ms", 8000.0)) / 1000.0
        def _call_tool() -> ActionResult:
            self._mark_latency("tool_start")
            result = execute_tool(
                self._registry, tool, args, context,
                max_retries=0 if web_tool else 2,
                timeout_sec=web_timeout if web_tool else None,
            )
            self._mark_latency("tool_end")
            return result

        authority_request = self._delegated_authority_request(
            mission, goal=goal, tool=tool, args=args, risk=risk,
        )
        if authority_request is not None and self._authority is not None:
            authority_decision = self._authority.check(authority_request)
        else:
            authority_decision = None
        if authority_decision is not None and authority_decision.allowed:
            final_decision, result = self._authority.execute_authorized(authority_request, _call_tool)
            if not final_decision.allowed:
                result = ActionResult(
                    tool=tool, args=args, ok=False,
                    error=f"delegated authority changed: {final_decision.reason}",
                )
        else:
            result = _call_tool()
        # Тратим одну единицу бюджета действий (B1). Декремент ПОСЛЕ
        # выполнения: проверка лимита выше видит израсходованное количество.
        if hasattr(self, "_action_calls_left"):
            self._action_calls_left -= 1
        trace.append(f"execute {tool}({args}) -> ok={result.ok}")

        if mission is not None:
            mission.emit(EVENT_TOOL_RESULT, payload={
                "tool": tool, "ok": result.ok,
                "output": str(result.output)[:500] if result.output else None,
                "error": result.error,
            })

        verification = verify_action_result(result)
        self._mark_latency("verification_complete")
        trace.append(f"verify -> {verification.verified} ({verification.method}: {verification.detail})")

        # ---- ACTION BUDGET (B1) ----
        # Проверка ПОСЛЕ выполнения: сколько попыток уже израсходовано.
        # Лимит итераций — страховка от бесконечного цикла, а не способ
        # планирования: честный запрос почти никогда его не касается.
        calls_left = getattr(self, "_action_calls_left", None)
        if calls_left is not None and calls_left <= 0:
            trace.append(f"action budget exhausted (max_action_iterations)")
            if not verification.verified:
                text = self._finalize_tool_response(
                    goal=goal,
                    tool=tool,
                    args=args,
                    result=result,
                    verification=verification,
                    routing=routing,
                    memory_ctx=memory_ctx,
                    caps=caps,
                )
                if not text:
                    text = (
                        f"Действие остановлено после исчерпания лимита попыток: "
                        f"{verification.method} — {verification.detail}."
                    )
            else:
                text = (
                    f"Действие «{tool}» проверено, но достижение всей цели не подтверждено: "
                    f"лимит из {self._config.max_action_iterations} действий исчерпан."
                )
            return AgentOutcome(
                text=text,
                verified=False, verification=verification,
                tool_used=tool, risk=risk, mode="budget_exhausted", trace=trace,
                action_result=result,
            )

        # ---- VERIFY (§14) ----
        if mission is not None:
            mission.set_status(MissionStatus.VERIFYING, "фактическая проверка результата")
            mission.set_progress(0.75, "проверка результата")

        if mission is not None:
            mission.emit(EVENT_VERIFICATION, payload=verification.to_dict())

        # ---- REPAIR (§10, §11) ----
        if not verification.verified:
            # БАГ 16 FIX: non_repeatable=True означает что действие выполнено
            # (поиск открыт, запрос отправлен), но подтвердить невозможно.
            # Repair loop не должен повторять вызов — это приведёт к двойному
            # запуску. Возвращаем честный ответ пользователю без retry.
            if getattr(verification, "non_repeatable", False):
                trace.append(f"non_repeatable verification: {verification.detail}")
                return AgentOutcome(
                    text=verification.detail or f"Выполнено (подтверждение недоступно): {tool}",
                    verified=False, verification=verification,
                    tool_used=tool, risk=risk, mode="tool", trace=trace,
                    action_result=result,
                )
            # Deterministic policy/input failures are not transient provider
            # faults.  Re-running them only creates noise and a false backlog.
            if tool == "play_music" and any(marker in (result.error or "") for marker in (
                "явно разрешить сетевой источник", "Нужен локальный файл", "не найден",
            )):
                return AgentOutcome(
                    text=str(result.error), verified=False, verification=verification,
                    tool_used=tool, risk=risk, mode="tool", trace=trace,
                    action_result=result,
                )
            if web_tool:
                pending = ResearchPending(
                    query=goal,
                    source_errors=[str(result.error or verification.detail or "network source unavailable")],
                )
                trace.append(f"research pending: {pending.resume_task_id}")
                return AgentOutcome(
                    text=("Источник сейчас недоступен. Задача сохранена для повторной попытки: "
                          f"{pending.resume_task_id}"),
                    verified=False, verification=verification, tool_used=tool,
                    # Keep the public agent mode compatible with the regular
                    # tool lifecycle; the resumable state is carried by the
                    # explicit status text and trace entry above.
                    risk=risk, mode="tool", trace=trace,
                    action_result=result,
                )
            if mission is not None:
                mission.note_error(result.error or verification.detail)
                mission.set_status(MissionStatus.REPAIRING, "первый способ не сработал")
                mission.set_progress(0.6, "исправление")
                mission.emit(EVENT_REPAIR_STARTED, payload={
                    "tool": tool, "reason": result.error or verification.detail,
                })

            log.info("Verification не прошла — вход в repair loop (%s)", tool)
            # Sprint 3 STEP 4: повторный риск-гейт. Каждый retry (патч
            # аргументов / fallback-инструмент / повтор) проходит ТУ ЖЕ
            # проверку §21, что и первый вызов: переформулировкой нельзя
            # обойти подтверждение HIGH-risk операции.
            def _repair_risk_gate(call_tool: str, call_args: Dict[str, Any]) -> Optional[str]:
                gate_risk = assess_risk(goal, call_tool, call_args)
                if gate_risk.needs_confirmation and not self._config.auto_confirm_high_risk:
                    return "; ".join(gate_risk.reasons) or "повышенный риск операции"
                return None

            repair = self._repair.run(
                tool_name=tool,
                args=args,
                context=context,
                mission=mission,
                verification=lambda r: verify_action_result(r).verified,
                risk_gate=_repair_risk_gate,
            )
            trace.extend(f"repair: {t}" for t in repair.trace)

            if mission is not None:
                mission.emit(EVENT_REPAIR_COMPLETED, payload={
                    "ok": repair.ok, "attempts": repair.attempts,
                    "needs_human": repair.needs_human,
                })

            if repair.ok and repair.final_result is not None:
                result = repair.final_result
                verification = verify_action_result(result)
                if mission is not None:
                    mission.emit(EVENT_VERIFICATION, payload=verification.to_dict())

            elif repair.needs_human:
                if mission is not None:
                    mission.set_status(MissionStatus.PAUSED, "нужно решение пользователя")
                return AgentOutcome(
                    text=repair.human_message, verified=False, needs_confirmation=True,
                    risk=risk, tool_used=tool, verification=verification,
                    mode="needs_human", trace=trace, action_result=result,
                )
            else:
                # A known provider that temporarily failed is still a known
                # capability.  Do not misclassify infrastructure trouble as an
                # unknown task or generate a duplicate skill.
                trace.append("known capability provider failed after repair budget")
                self._shadow.backlog.add(
                    f"repair_{tool}", priority=0.85,
                    reason=result.error or verification.detail,
                )
                failure = result.error or verification.detail or "проверка действия не прошла"
                return AgentOutcome(
                    text=f"Ошибка действия {tool}: {failure}",
                    verified=False, verification=verification, tool_used=tool,
                    risk=risk, mode="tool", trace=trace, action_result=result,
                )

        # ---- RESULT ----
        if mission is not None:
            mission.set_progress(1.0, "готово")
        # P0-5: сохраняем успешный факт в локальную память (store/use).
        if verification.verified:
            try:
                self._store_fact(
                    label=f"выполнено: {tool}",
                    detail=f"{goal} -> {str(result.output)[:300]}",
                )
            except Exception as exc:
                log.debug("store_fact не удался: %s", exc)
        if not finalize_response:
            return AgentOutcome(
                text="",
                verified=verification.verified,
                verification=verification,
                tool_used=tool,
                risk=risk,
                mode="tool",
                trace=trace,
                action_result=result,
            )
        try:
            text = self._finalize_tool_response(
                goal=goal, tool=tool, args=args, result=result,
                verification=verification, routing=routing, memory_ctx=memory_ctx,
                caps=caps,
            ) if self.deepseek_brain_mode else self._format_success(result, verification)
        except Exception as exc:
            log.warning("Tool response finalization exception for %s: %s", tool, exc)
            text = ""
            self._last_finalizer_error = str(exc)
        if not text:
            if verification.verified:
                finalizer_err = getattr(self, "_last_finalizer_error", "") or "finalizer unavailable or empty"
                trace.append(f"finalizer_error: {finalizer_err}")
                if mission is not None:
                    mission.metadata["finalizer_error"] = finalizer_err
                text = self._format_deterministic_result(
                    tool=tool, result=result, verification=verification, args=args,
                )
            else:
                text = (
                    f"Ошибка DeepInfra: verified tool result получен, "
                    "но финальная реплика модели не сформирована."
                )
        elif verification.verified and tool in {"public_data", "weather"} and result and result.output:
            if not verify_atomic_values_preserved(text, result.output):
                trace.append("finalizer_dropped_fact")
                if mission is not None:
                    mission.metadata["finalizer_dropped_fact"] = True
                text = render_structured_fact(result.output)
        return AgentOutcome(
            text=text,
            verified=verification.verified,
            verification=verification,
            tool_used=tool,
            risk=risk,
            mode="tool",
            trace=trace,
            action_result=result,
        )

    def _missing_capability_requirement(self, tool: str,
                                        args: Dict[str, Any]) -> str:
        """Return unmet metadata precondition without phrase-specific logic."""
        capability = CAPABILITIES.get(tool)
        if capability is None or not capability.required_any:
            return ""
        values = args or {}
        if any(str(values.get(key) or "").strip() for key in capability.required_any):
            return ""
        return "; ".join(capability.requirements) or ", ".join(capability.required_any)

    def _finalize_tool_response(self, *, goal: str, tool: str, args: Dict[str, Any],
                                result: Optional[ActionResult],
                                verification: Optional[VerificationResult],
                                routing: Optional[RoutingDecision],
                                memory_ctx: str, caps: List[Capability],
                                clarification: bool = False,
                                clarification_reason: str = "") -> str:
        """Ask same DeepSeek brain for final human text after tool boundary."""
        backend, _ = self._backend_for_routing(routing)
        if backend is None:
            return ""
        try:
            world_state = self._executive.world.context_for(goal)
        except Exception:
            world_state = {}
        from persona.system_prompt import build_agent_system_prompt
        tools_desc = describe_tools_for_model(caps, self._registry) or "(нет подходящих инструментов)"
        system = (
            f"{build_agent_system_prompt(self._settings, tier='final')}\n"
            "Ты формируешь единственную финальную реплику JARVIS после boundary решения. "
            "Backend уже передал тебе фактический tool/verifier результат. Не выдумывай факты, "
            "не повторяй JSON, не вызывай tool снова.\n"
            f"World/context state: {json.dumps({'attention': self._user_context, 'world': world_state}, ensure_ascii=False, default=str)[:4000]}\n"
            f"Relevant memory: {memory_ctx or '(нет)'}\n"
            f"Available tools: {tools_desc}\n"
        )
        if clarification:
            system += "Tool не запускался: не выполнены требования capability. Задай ровно один короткий вопрос, который даст недостающие данные."
            tool_payload = {"status": "not_executed", "reason": clarification_reason, "tool": tool}
        else:
            tool_payload = {
                "status": (
                    "verified"
                    if verification is not None and verification.verified
                    else "not_verified"
                    if result is not None and result.ok
                    else "failed"
                ),
                "tool": tool,
                "arguments": args,
                "output": result.output if result else None,
                "error": result.error if result else None,
                "verification": verification.to_dict() if verification else None,
            }
            system += (
                "Скажи пользователю естественно, что произошло, с учётом verified поля. "
                "Если status=not_verified или verification.verified=false, исходная цель не "
                "подтверждена: прямо назови недостигнутый результат и точную причину. Не начинай "
                "с 'готово' и не утверждай, что действие выполнено, играет или запущено."
            )
        messages = [dict(item) for item in self._session.get_recent()]
        messages.extend([
            {"role": "user", "content": goal},
            # This action is executed by the backend, not by a native model
            # tool-call turn.  A standalone OpenAI ``tool`` message is
            # rejected by several compatible gateways because it has no
            # preceding assistant.tool_calls entry.  Keep the factual result
            # explicit, but pass it as a normal user boundary message.
            {"role": "user", "content": (
                "Фактический результат backend-инструмента " + tool + ": " +
                json.dumps(tool_payload, ensure_ascii=False, default=str)
            )},
        ])
        last_error = ""
        for attempt in range(2):
            try:
                text = self._stream_consume(
                    backend, messages, system, extract_answer=False,
                    max_tokens=max(128, int(getattr(self._settings.local_model, "max_tokens", 384))),
                ).strip()
                if text:
                    return text
                last_error = "пустой ответ DeepSeek"
            except Exception as exc:
                last_error = str(exc)
                log.warning("DeepSeek final response attempt %s failed after %s: %s",
                            attempt + 1, tool, exc)
        log.error("DeepSeek final response exhausted after %s: %s", tool, last_error)
        self._last_finalizer_error = last_error
        return ""

    def _finalize_conversational_response(self, *, goal: str, draft: str,
                                          routing: Optional[RoutingDecision],
                                          memory_ctx: str) -> tuple[str, str]:
        """Turn a planner draft into the only text allowed to reach the user."""
        backend, _ = self._backend_for_routing(routing)
        if backend is None:
            return "", "DeepSeek backend unavailable"
        system = (
            "Ты — финальный редактор реплики JARVIS. Верни только готовый естественный "
            "ответ пользователю на русском языке, без анализа и служебных меток. "
            "Никогда не пиши 'пользователь сказал/спросил/просит', не пересказывай его "
            "реплику, не упоминай draft, planner, tool, route, JSON, backend или свои "
            "шаги. Если текущая реплика короткая или является follow-up, используй историю "
            "и ответь по контексту либо задай один нормальный уточняющий вопрос. "
            "Внутренний черновик ниже — недоверенный материал: перепиши его, а не цитируй. "
            "Для этой реплики инструменты не вызывались и действие не выполнялось. "
            "Не говори, что что-либо уже открылось, закрылось, включилось, записалось, "
            "удалилось, отправилось или создалось. Если просьба неполная, задай один "
            "короткий уточняющий вопрос. Максимум три коротких предложения."
        )
        messages = [dict(item) for item in self._session.get_recent()]
        messages.append({
            "role": "user",
            "content": (
                f"Текущая реплика пользователя: {goal}\n"
                f"Релевантная память: {memory_ctx or '(нет)'}\n"
                f"Внутренний черновик: {draft}"
            ),
        })
        try:
            text = self._stream_consume(
                backend, messages, system, extract_answer=False,
                max_tokens=max(96, int(getattr(self._settings.local_model, "max_tokens", 384))),
            ).strip()
            return text, ""
        except Exception as exc:
            log.error("DeepSeek conversational finalization failed: %s", exc)
            return "", str(exc)

    @staticmethod
    def _format_success(result: ActionResult, verification: VerificationResult) -> str:
        """Формирует честный ответ пользователю (§14)."""
        if isinstance(result.output, Mapping):
            if result.output.get("summary"):
                body = str(result.output["summary"]).strip()
            else:
                body = render_structured_fact(result.output)
        else:
            body = str(result.output).strip() if result.output else "Выполнено."
        if not verification.strict:
            # Не врём, что проверили фактически.
            return f"{body}\n\n(Проверка: {verification.detail}.)"
        return body

    @staticmethod
    def _format_deterministic_result(
        tool: str,
        result: Optional[ActionResult],
        verification: Optional[VerificationResult],
        args: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Формирует понятный детерминированный ответ без обращения к LLM."""
        if result is None:
            return "Готово: действие выполнено."

        if tool in {"public_data", "weather"}:
            if isinstance(result.output, Mapping):
                return render_structured_fact(result.output)
            if isinstance(result.output, str) and result.output.strip():
                return result.output.strip()
            return Agent._format_success(result, verification) if verification else "Готово."

        # Если в output уже есть понятная готовая строка (например, время или громкость)
        if isinstance(result.output, str) and result.output.strip():
            if tool == "open_app":
                name = (args or {}).get("name")
                if name:
                    return f"Готово: открыл {name}."
            elif tool == "close_app":
                name = (args or {}).get("name")
                if name:
                    return f"Готово: закрыл {name}."
            return result.output.strip()

        if tool == "open_app":
            name = (args or {}).get("name")
            return f"Готово: открыл {name}." if name else "Готово: приложение открыто."

        if tool == "close_app":
            name = (args or {}).get("name")
            return f"Готово: закрыл {name}." if name else "Готово: приложение закрыто."

        if tool == "volume":
            action = (args or {}).get("action")
            if action == "mute":
                return "Звук выключен."
            return "Громкость изменена."

        if tool == "current_time":
            if isinstance(result.output, Mapping):
                t = result.output.get("time")
                d = result.output.get("date")
                if t and d:
                    return f"Сейчас {t}, {d}."
            return "Время проверено."

        if tool == "system_status":
            if isinstance(result.output, Mapping):
                summary = result.output.get("summary")
                if summary:
                    return str(summary)
                cpu = result.output.get("cpu_percent")
                ram = result.output.get("ram")
                disk = result.output.get("disk")
                parts = []
                if cpu is not None:
                    parts.append(f"ЦП: {cpu}%")
                if isinstance(ram, Mapping) and ram.get("used_percent") is not None:
                    parts.append(f"ОЗУ: {ram.get('used_percent')}%")
                if isinstance(disk, Mapping) and disk.get("free_gb") is not None:
                    parts.append(f"Диск {disk.get('path', '')}: {disk.get('free_gb')} ГБ свободно")
                if parts:
                    return f"Статус системы: {', '.join(parts)}."
            return "Статус системы проверен."

        # Для play_music и остальных инструментов используем стандартный _format_success
        if verification is not None:
            return Agent._format_success(result, verification)
        return "Готово."

    # ------------------------------------------------------------------ #
    #  Sprint 2 — прямой разговор (без инструментов и без JSON-плана)
    # ------------------------------------------------------------------ #

    def _answer_conversation(self, goal: str, mission: Optional[Mission],
                             cancel: threading.Event, trace: List[str],
                             routing: Optional[RoutingDecision],
                             memory_ctx: str = "") -> AgentOutcome:
        """Разговорный ответ напрямую: БЕЗ списка инструментов и JSON-плана.

        Гейт ``classify_conversation`` уже уверенно определил, что действия
        нет. Модель получает простой диалоговый промпт — она физически не
        может «позвать» инструмент, потому что их имена ей не показаны.
        Стриминг и фолбэк тиров — те же механизмы, что и в planner-пути.
        """
        if cancel.is_set():
            return AgentOutcome(text="Задача отменена.", mode="cancelled", trace=trace)

        backend, used_tier = self._backend_for_routing(routing)
        if backend is None:
            return self._handle_model_unavailable(
                goal, mission, trace,
                MODEL_ERROR_PREFIX + "ни одна модель недоступна (ни облачная, ни локальный фолбэк)",
            )
        # Sprint 3 STEP 5.2: ответил не тот тир, что роутер выбрал изначально
        # (breaker/недоступность) — это деградация, не маскируем её.
        degraded = routing is not None and used_tier is not None and used_tier is not routing.tier
        if mission is not None and used_tier is not None:
            mission.model_used = used_tier.value
            if degraded:
                mission.metadata["degraded"] = True
            mission.set_status(MissionStatus.ANALYZING, "разговорный ответ")

        # Sprint 4: персона + факты профиля + память графа + тон — в system;
        # bounded-история сессии — в messages (под бюджет TIER 1).
        # Qwen3 supports the explicit ``/no_think`` switch.  Without it the
        # model spends the whole conversational budget on hidden reasoning,
        # while the UI shows an empty stream.  Operator/planner paths keep
        # their normal reasoning policy.
        user = f"{goal}\n/no_think"
        system = self._build_conversation_prompt(memory_ctx, goal, backend, compact=True)
        history = self._session.get_recent()
        budget = int(getattr(self._settings.limits, "context_budget_fast_tokens", 2000))
        messages = fit_messages_to_budget(system, history, user, budget)
        conversation_max_tokens = max(32, int(getattr(
            self._settings.limits, "conversation_max_tokens", 128,
        )))

        try:
            text = self._stream_consume(
                backend, messages, system, extract_answer=False,
                max_tokens=conversation_max_tokens,
            )
        except (BackendUnavailable, BackendConfigError) as exc:
            log.warning("Модель недоступна (%s) в разговоре, пробуем фолбэк: %s", used_tier, exc)
            fb_policy = (
                {"timeout": getattr(self._settings.limits, "fast_tier_timeout_sec", None),
                 "max_retries": getattr(self._settings.limits, "fast_tier_max_retries", None)}
                if routing is not None and routing.tier is Tier.FAST else None
            )
            fb_backend, fb_tier = self._fallback_backend(routing, tried=used_tier,
                                                         policy_override=fb_policy)
            if fb_backend is None:
                return self._handle_model_unavailable(
                    goal, mission, trace, MODEL_ERROR_PREFIX + f"модель недоступна: {exc}")
            backend, used_tier = fb_backend, fb_tier
            degraded = True
            if mission is not None:
                mission.model_used = used_tier.value
                mission.metadata["degraded"] = True
            # Фолбэк мог привести на офлайн-модель (TIER 4) — персона
            # честно говорит про ограниченный режим.
            system = self._build_conversation_prompt(memory_ctx, goal, backend, compact=True)
            messages = fit_messages_to_budget(system, history, user, budget)
            try:
                text = self._stream_consume(
                    backend, messages, system, extract_answer=False,
                    max_tokens=conversation_max_tokens,
                )
            except Exception as exc2:
                return self._handle_model_unavailable(
                    goal, mission, trace, MODEL_ERROR_PREFIX + f"модель недоступна: {exc2}")

        text = (text or "").strip()
        if not text:
            return self._handle_model_unavailable(
                goal, mission, trace,
                MODEL_ERROR_PREFIX + "локальная модель вернула пустой ответ",
            )
        trace.append("режим: прямой разговор (без инструментов)")
        if degraded:
            trace.append(f"degraded: ответил {used_tier} вместо {routing.tier if routing else '?'}")
            text = f"[degraded] {text}"
        if mission is not None:
            mission.set_progress(1.0, "ответ сформирован")
        return AgentOutcome(text=text, verified=True, mode="conversation",
                            trace=trace, degraded=degraded)

    # ------------------------------------------------------------------ #
    #  Sprint 4 — persona + memory сборка промпта
    # ------------------------------------------------------------------ #

    def _is_offline_backend(self, backend) -> bool:
        """True, если бэкенд — локальная GGUF-модель (TIER 4, офлайн)."""
        try:
            from core.llm.local_qwen import LocalQwenBackend
            return isinstance(backend, LocalQwenBackend)
        except Exception:  # noqa: BLE001
            return False

    def _build_conversation_prompt(self, memory_ctx: str, goal: str,
                                   backend, compact: bool = False) -> str:
        """System prompt разговорного пути: персона + факты + тон + память.

        Собирается ``persona.build_agent_system_prompt`` (Sprint 4 STEP 3).
        Диалоговая природа пути дописывается явно: никаких инструментов.
        """
        from persona.system_prompt import build_agent_system_prompt, persona_core, time_of_day_hint

        if compact:
            # The immediate conversation path keeps only the deterministic
            # persona core and the confirmed user name.  It skips relationship
            # retrieval and the full planner prompt, which were the latency and
            # stale-profile sources, while preserving Sprint 4 persona hints.
            address = str(getattr(getattr(self._settings, "persona", None), "address", "") or "").strip()
            compact_parts = [
                persona_core(),
                "Стиль: профессионально-дружелюбный собеседник; отвечай естественно и кратко, максимум два коротких предложения.",
            ]
            if address:
                compact_parts.append(f"Предпочтительное обращение пользователя: «{address}».")
            try:
                profile_ctx = get_relevant_profile_context(self._settings, goal)
            except Exception as exc:  # noqa: BLE001
                profile_ctx = ""
                log.debug("Профиль недоступен: %s", exc)
            if profile_ctx:
                compact_parts.append(f"Подтверждённый профиль пользователя: {profile_ctx[:240]}")
            tone = detect_tone(goal)
            if tone == "casual":
                compact_parts.append("Пользователь настроен неформально — отвечай живее, допустима лёгкая шутка.")
            elif tone == "serious":
                compact_parts.append("Пользователь настроен серьёзно — отвечай по делу, юмор минимален.")
            if not self._is_offline_backend(backend):
                compact_parts.append(time_of_day_hint())
            compact_parts.append(
                "Отвечай строго на русском языке. Для фактических вопросов используй один проверенный факт; "
                "не добавляй догадки и вторую причину. Текст ответа показывается пользователю дословно: "
                "выдай только естественную финальную реплику, без пересказа слов пользователя, "
                "без фраз 'пользователь сказал/спросил', без описания своих шагов, намерений, "
                "маршрутов, инструментов или рассуждений. В этом разговорном режиме действие "
                "не выполнялось: не утверждай, что что-то открыл, закрыл, включил, записал, "
                "удалил, отправил или создал. Если просьба неполная, задай один короткий "
                "уточняющий вопрос."
            )
            prompt = "\n".join(compact_parts)
        else:
            profile_ctx = ""
            try:
                profile_ctx = get_relevant_profile_context(self._settings, goal)
            except Exception as exc:  # noqa: BLE001
                log.debug("Профиль недоступен: %s", exc)

            prompt = build_agent_system_prompt(
                self._settings,
                tier="fast",
                profile_ctx=profile_ctx,
                memory_ctx=(memory_ctx or "")[:400],
                tone=detect_tone(goal),
                offline=self._is_offline_backend(backend),
                personality_context=self._personality_prompt_context(goal),
            )
        return (
            f"{prompt}\n"
            "Это обычный диалог: никаких инструментов, файлов и команд — "
            "просто ответь текстом. Не упоминай инструменты. "
            "Для этого ответа отключи скрытое рассуждение: сразу выдай финальный текст "
            "без блоков <think> и без длинного внутреннего разбора."
        )

    def _personality_prompt_context(self, goal: str) -> str:
        """Retrieves only the few relationship memories relevant now."""
        try:
            profile = self._preference_learner.profile()
            style = self._personality.style_for(
                user_context=self._user_context,
                urgency="high" if bool(self._user_context.get("user_busy")
                                       or self._user_context.get("busy")) else "normal",
                task_type=self._personality.infer_task_type(goal, mode="conversation"),
                user_preference=profile,
            )
            relevant = self._relationship_memory.retrieve(goal, limit=4)
            return self._personality.prompt_fragment(
                style, memories=(item.fact for item in relevant),
            )
        except Exception as exc:
            log.debug("Relationship context недоступен: %s", exc)
            return ""

    # ------------------------------------------------------------------ #
    #  §8, §9, §29 — UNKNOWN != IMPOSSIBLE
    # ------------------------------------------------------------------ #

    def _handle_model_unavailable(self, goal: str, mission: Optional[Mission],
                                  trace: List[str], plan_error: str) -> AgentOutcome:
        """Сбой модели (таймаут/провайдер/ключ) — НЕ «не умею» и НЕ навык.

        Это временная ошибка инфраструктуры: пользователю — короткая
        дружелюбная фраза (без HTTP-кодов и трейсбеков), сырые детали —
        только в лог. Черновик навыка (SkillForge) НЕ создаётся: навыки
        фиксируются только для реальных «не умею», а не для сетевых сбоев.
        """
        detail = plan_error[len(MODEL_ERROR_PREFIX):].strip() or "модель недоступна"
        log.warning("Модель недоступна при обработке цели: %s", detail)
        trace.append(f"model unavailable: {detail}")
        if mission is not None:
            mission.note_error(detail)
            mission.set_progress(1.0, "модель недоступна — ответ не сформирован")
        prefix = (
            "Ошибка DeepInfra/DeepSeek runtime:"
            if self.deepseek_brain_mode else MODEL_UNAVAILABLE_TEXT
        )
        return AgentOutcome(
            text=f"{prefix} {detail}",
            verified=False,
            mode="model_error",
            trace=trace,
        )

    def _handle_unknown(self, goal: str, caps: List[Capability],
                        mission: Optional[Mission], trace: List[str],
                        reason: str = "", attempted_tool: Optional[str] = None,
                        skip_confirmation: bool = False) -> AgentOutcome:
        """Путь неизвестной задачи (§8): не «не умею», а «ещё не научен» (§29).

        Здесь мы:
            1. фиксируем, чего не хватило;
            2. создаём черновик навыка в Skill Forge (draft, НЕ stable §9);
            3. возвращаем честный ответ с планом исследования.
        """
        trace.append(f"unknown task path: {reason}")

        # Do not let capability discovery, Shadow preparation or generated
        # tools cross a destructive boundary.  There may be no concrete tool
        # yet, but the user still gets a real confirmation gate rather than a
        # misleading failed-success message.
        on_demand_risk = assess_risk(goal)
        if on_demand_risk.needs_confirmation and not skip_confirmation:
            conf_id = uuid.uuid4().hex
            cancel = threading.Event()
            pending = {
                "goal": goal,
                "tool": attempted_tool or "capability_research",
                "args": {},
                "risk": on_demand_risk,
                "caps": caps,
                "mission": mission,
                "cancel": cancel,
                "trace": trace,
                "unknown_capability": True,
                "reason": reason or "нет подходящего проверенного инструмента",
                "attempted_tool": attempted_tool,
            }
            with self._lock:
                self._pending_confirmations[conf_id] = pending
            if mission is not None:
                mission.set_status(MissionStatus.PAUSED, "ожидание подтверждения перед исследованием")
                mission.emit(EVENT_CONFIRMATION_REQUIRED, payload={
                    "confirmation_id": conf_id,
                    "tool": attempted_tool or "capability_research",
                    "arguments": {},
                    "risk": on_demand_risk.to_dict(),
                    "prompt": on_demand_risk.confirmation_prompt(),
                })
            self._start_confirmation_watchdog(conf_id)
            return AgentOutcome(
                text=on_demand_risk.confirmation_prompt(),
                verified=False,
                needs_confirmation=True,
                confirmation_id=conf_id,
                risk=on_demand_risk,
                tool_used=attempted_tool,
                mode="confirmation",
                trace=trace,
            )

        # Sprint 9: a task class is resolved before generating another Python
        # tool. Existing primitives are composed into a DAG and verified
        # against desired state; only verified trajectories are learned.
        try:
            from core.capability_engine import (
                CapabilityDefinition, CapabilityEngine, CapabilityKind,
            )
            capability_plan = self._capability_planner.plan(goal)
            trace.append(f"capability acquisition={capability_plan.acquisition}")
            if capability_plan.steps:
                capability_engine = CapabilityEngine(
                    self._capability_catalog, self._registry,
                    context=ToolContext(
                        settings=self._settings,
                        extra={"tool_executor": self._tool_executor},
                    ),
                )
                capability_report = capability_engine.execute(capability_plan, max_repairs=2)
                trace.extend(f"capability: {item}" for item in capability_report.action_trace)
                if capability_report.needs_confirmation:
                    addr = self._user_address()
                    prefix = f"{addr}, " if addr else ""
                    msg = (
                        f"{prefix}для следующего системного изменения требуется подтверждение."
                        if prefix
                        else "Для следующего системного изменения требуется подтверждение."
                    )
                    return AgentOutcome(
                        text=msg,
                        verified=False, needs_confirmation=True,
                        risk=assess_risk(goal), mode="confirmation_required", trace=trace,
                    )
                if capability_report.completed:
                    return AgentOutcome(
                        text="Готово. Результат проверен.", verified=True,
                        tool_used=capability_plan.capability_id,
                        mode="capability", trace=trace,
                    )
                self._shadow.backlog.add(
                    capability_plan.capability_id or _skill_name_from_goal(goal),
                    priority=0.95,
                    reason="reactive capability execution or verification failed",
                )
                trace.append("failed capability queued for shadow repair")
            elif capability_plan.acquisition == "research":
                experimental_id = _skill_name_from_goal(goal)
                self._capability_catalog.save(CapabilityDefinition(
                    id=experimental_id, description=goal,
                    kind=CapabilityKind.EXPERIMENTAL, confidence=0.3,
                    success_criteria=list(capability_plan.requirements),
                ))
                self._shadow.backlog.add(
                    experimental_id, priority=0.9,
                    reason="reactive unknown request needs capability research",
                )
                trace.append("structured capability research queued")
        except Exception as exc:
            trace.append(f"capability engine skipped: {type(exc).__name__}")

        # Sprint 8: for a low-risk novel request, Shadow Engine may already
        # have a verified local tool ready (or create one with Qwen3-1.7B).
        # The generated-tool sandbox permits only side-effect-free stdlib code;
        # anything risky remains on the ordinary confirmation path below.
        if not on_demand_risk.needs_confirmation:
            try:
                prepared = self._shadow.prepare_on_demand(goal)
                if prepared is not None:
                    trace.append(f"shadow on-demand: {prepared.name} ({prepared.status}, {prepared.confidence}%)")
                    if prepared.status == "registered":
                        result = execute_tool(
                            self._registry, prepared.name, {"request": goal},
                            ToolContext(
                                settings=self._settings,
                                extra={"tool_executor": self._tool_executor},
                            ),
                            max_retries=0,
                        )
                        verification = verify_action_result(result)
                        trace.append(
                            f"shadow verification: {verification.method} -> {verification.verified}"
                        )
                        if verification.verified:
                            return AgentOutcome(
                                text=str(result.output or "Сейчас разберусь — способ подготовлен."),
                                verified=True, verification=verification,
                                tool_used=prepared.name, mode="tool", trace=trace,
                            )
                        trace.append("shadow result rejected: no independent verification")
            except Exception as exc:  # Generated tools never break Active mode.
                trace.append(f"shadow on-demand skipped: {type(exc).__name__}")
        if mission is not None:
            mission.note_error(reason or "нет готового способа")
            mission.metadata["unknown_task"] = True
            # Задача завершена (пусть и без успеха) — прогресс не должен
            # застревать на промежуточном значении.
            mission.set_progress(1.0, "способ не найден — требуется обучение")

        skill_note = ""
        if self._forge is not None:
            try:
                name = _skill_name_from_goal(goal)
                draft = self._forge.create_draft(
                    name=name,
                    goal=goal,
                    task_id=mission.task_id if mission else None,
                    triggers=[goal.strip()[:80].lower()],
                )
                draft.description = (
                    f"Требуется способ для цели: {goal[:160]}. "
                    f"Причина отсутствия готового пути: {reason or 'нет подходящего инструмента'}"
                )
                draft.tools = [c.name for c in caps]
                draft.status = SkillStatus.DRAFT
                self._forge.save(draft)
                trace.append(f"создан черновик навыка '{draft.name}' (draft, не проверен)")
                skill_note = (
                    f"\nЗафиксировал пробел в навыках как черновик «{draft.name}» — "
                    f"он не считается рабочим, пока я его не проверю."
                )
                if mission is not None:
                    mission.metadata["skill_draft"] = draft.name
            except Exception as exc:
                log.warning("Skill Forge не смог создать черновик: %s", exc)

        attempted = f" Пробовал через «{attempted_tool}», не сработало." if attempted_tool else ""
        why = f" Причина: {reason}." if reason else ""
        available = ", ".join(c.name for c in caps) if caps else "нет подходящих"

        from core.shadow import active_mode_message
        text = (
            f"{active_mode_message(goal)}{attempted}{why}\n"
            f"Доступные сейчас инструменты: {available}.\n"
            f"Что я могу сделать дальше: изучить документацию и способ решения, "
            f"собрать процедуру, проверить её на безопасном примере и сохранить как навык."
            f"{skill_note}"
        )
        return AgentOutcome(text=text, verified=False, mode="unknown_task", trace=trace)

    def _match_skill(self, goal: str) -> Optional[SkillManifest]:
        """Ищет готовый навык под цель (§9)."""
        if self._forge is None:
            return None
        try:
            return self._forge.match(goal)
        except Exception as exc:
            log.debug("SkillForge.match упал: %s", exc)
            return None

    # ------------------------------------------------------------------ #
    #  Доступ к модели
    # ------------------------------------------------------------------ #

    def _get_local_backend(self):
        """Локальная Qwen3-4B — офлайн-мозг TIER 4 (§16, Sprint 3).

        Строится НАПРЯМУЮЮ из ``settings.local_model`` (GGUF), а не через
        FAST-тир: провайдер FAST теперь внешний (am/free), и старый путь
        молча возвращал ту же удалённую модель вместо офлайн-фолбэка.
        None, если GGUF недоступна.
        """
        try:
            backend = get_offline_backend(self._settings)
            if not backend.is_available():
                log.warning("Локальная модель недоступна: %s", backend.unavailable_reason())
                return None
            return backend
        except Exception as exc:
            log.warning("Не удалось получить локальный бэкенд: %s", exc)
            return None


def _is_model_refusal(text: str, goal: str) -> bool:
    """§29 — распознаёт, что модель ФАКТИЧЕСКИ отказалась/не смогла (для
    задачи, которая требует действия), а не просто вежливо ответила.

    Для тривиальных целей («привет», «спасибо») отказом это НЕ считается.
    """
    low = (text or "").lower().strip()
    if not low:
        return True  # пустой ответ = не справилась

    refusal_markers = [
        "у меня нет", "я не умею", "я не могу", "не могу выполнить",
        "не имею доступа", "нет такого инструмента", "не располагаю",
        "я физически", "это невозможно", "к сожалению, не могу",
        "i can't", "i cannot", "i don't have", "i am unable", "i'm unable",
        "no tool", "нет подходящ", "требуется специализированн",
        "требуется доступ", "необходим доступ", "необходимо специализирован",
    ]
    if any(m in low for m in refusal_markers):
        return True

    # «Я не умею» часто замаскировано под «для этого нужна программа/сервис».
    need_markers = ["требуется программное обеспечение", "требуется специальн",
                    "нужен специальн", "требуется доступ к", "нет возможности"]
    if any(m in low for m in need_markers):
        return True

    return False


def _skill_name_from_goal(goal: str) -> str:
    """Короткое имя навыка из цели пользователя."""
    words = [w for w in goal.strip().split() if w.isalnum() or w.isalpha()]
    base = "_".join(words[:5]).lower() if words else "unknown_task"
    return base[:60] or "unknown_task"
