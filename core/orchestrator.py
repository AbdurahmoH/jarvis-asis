"""Лёгкий оркестратор (без langchain/langgraph).

``Orchestrator`` — полный цикл обработки одного витка:
1. Intake: new_state, push в short_term
2. Memory: retrieve (заполняет retrieved_context)
3. Council: route (выбирает тир, генерирует ответ, решает про tools)
4. Tools: если LLM вернула tool_call — execute_tool, результат в контекст, ре-спрос модели
5. Response: генерация финального ответа
6. Memory: remember_exchange (сохраняем в долгую память)
7. Short-term: push assistant
8. TTS: add_to_queue (если включено)

Простой шаблон tool calling: если в ответе модели есть специальный маркер
``TOOL_CALL:{"name": "...", "args": {...}}`` — парсим, выполняем, добавляем результат
в контекст и переспрашиваем модель ОДИН раз.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import replace as _dataclass_replace
from typing import Any, Callable, Dict, List, Mapping, Optional

from config.settings import Settings
from core.actions import DEFAULT_REGISTRY
from core.authority import AuthorityProposal, AuthorityRequest, AuthorityStore, ProvenanceKind
from core.agent import Agent, pick_acknowledgement
from core.capabilities import CAPABILITIES
from core.cognitive import CognitiveOrchestrator
from core.memory import MemoryRetriever
from core.memory.taste import TasteProfile
from core.model_router import ModelRouter, estimate_complexity
from core.memory.profile import load_profile
from core.routing.semantic_router import (
    RoutingContext as SemanticRoutingContext,
    RoutingDecision as SemanticRoutingDecision,
    assess_risk as semantic_assess_risk,
    get_router,
    intent_category as semantic_intent_category,
    route as semantic_route,
)
from core.router import CouncilRouter
from core.understanding import (
    NaturalMissionCoordinator, NaturalMissionInterpreter, QuickAnswerEngine,
    Route, SemanticMode, UnderstandingLayer,
)
from core.state import JarvisState, new_state, push_message, trim_short_memory
from core.task_runtime import (
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    EVENT_TASK_PROGRESS,
    Mission,
    MissionStatus,
    MissionTrigger,
    TaskEvent,
    TaskRuntime,
)
from core.voice import (
    AssistantOutput, ErrorCategory, ErrorInfo, PiperTTS, SpeechRenderer, TTSQueue,
    build_wake_word_detector, assistant_output_from_outcome, show_toast,
)
from core.proactive import Proactor, BackgroundScheduler
from core.actions.reminders import TaskManager, get_default_manager
from core.utils.logger import get_logger
from core.brain import build_brain_fabric
from core.intelligence import EvidenceRecord, LatencyBudget, TutorEngine, UniversalIntake
from core.cognitive_kernel import (
    CapabilityGraph,
    CapabilityManifest,
    CognitiveKernel,
    EvidenceRecordV2,
)

__all__ = ["Orchestrator"]

log = get_logger(__name__)


class Orchestrator:
    """Единый оркестратор витка обработки запроса."""

    def __init__(
        self,
        settings: Settings,
        output_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        """
        Args:
            settings: конфигурация.
            output_callback: функция для вывода ответа пользователю (текст -> None).
                По умолчанию: print + TTS queue + toast.
        """
        self._settings = settings
        self._output_callback = output_callback or self._default_output

        # ЕДИНЫЙ роутер моделей — его делят CouncilRouter и Agent (P5 §5.7),
        # чтобы любой вход (консоль/WebSocket) маршрутизировался одинаково.
        self._brain = build_brain_fabric(settings)
        self._model_router = ModelRouter(settings, brain_fabric=self._brain)

        # Инициализация компонентов
        self._council = CouncilRouter(
            settings, model_router=self._model_router, brain_fabric=self._brain,
        )
        self._session = None  # будет создан в start()
        self._memory = MemoryRetriever(settings)
        self._registry = DEFAULT_REGISTRY

        # Canonical mission authority. Existing TaskRuntime remains the
        # compatibility execution surface while every submitted goal gets an
        # idempotent ledger record for resume/undo/evidence.
        self._kernel = CognitiveKernel(
            settings.data_dir / "cognitive_kernel",
            capability_graph=CapabilityGraph(),
            intake=UniversalIntake(),
        )
        self._register_kernel_capabilities()

        # Understanding Layer (Фаза 1): единственная точка классификации
        # ввода. Создаётся ДО агента — лёгкий regex-конструктор, без моделей.
        self._understanding = UnderstandingLayer()
        # Quick-Answer путь (Фаза 2, без API-ключа): вопрос -> локальная
        # Qwen решает -> DuckDuckGo(+Bing fallback) -> сжатие -> ответ 1-3 сек.
        # Не требует внешних ключей; модель берётся из тира FAST лениво.
        # Память (дыры 1/3): ретривер — живой контекст (self._living.answer_context),
        # saver — долговременная память (self._memory.remember_exchange). Оба
        # резолвятся ЛЕНИВО (при вызове), т.к. _living создаётся позже.
        self._quick_answer = QuickAnswerEngine(
            settings,
            memory_retriever=lambda q: self._quick_memory_retrieve(q),
            memory_saver=lambda q, a: self._remember_exchange_background(q, a),
        )

        # Wake word (Фаза 3, MIT): детектирует слово-пробуждение и будит
        # слушателя. Создаётся лениво через фабрику — тяжёлые движки
        # подхватятся только при enabled=True и установленных зависимостях.
        self._wake_word = build_wake_word_detector(
            settings,
            on_detected=self._on_wake_word_detected,
        )

        # Профиль вкусов (память/вкусы, MIT-паттерн): структурно копит, что
        # пользователь любит/не любит слушать (жанры, исполнители, настроения).
        # Сценарий: «поставь музыку под настроение» -> score() ранжирует треки.
        # Store (RelationshipMemoryStore) подключается после создания агента.
        self._taste = TasteProfile(settings.data_dir / "taste")

        # --- Агентное ядро J.A.R.V.I.S. 3.0 (§3, §6) ---
        # Агент исполняет миссии, TaskRuntime даёт им асинхронную жизнь.
        # НЕТ watchdog по умолчанию: миссия живёт столько, сколько нужно (§4).
        self._agent = Agent(
            settings, council=self._council, model_router=self._model_router,
            brain_fabric=self._brain,
        )
        # Подключаем вкусовой профиль к сущностной памяти, чтобы устойчивые
        # предпочтения (жанры/исполнители) записывались и в relationship store.
        try:
            rel = getattr(self._agent, "relationship_memory", None)
            if rel is not None:
                self._taste._store = rel
        except Exception as exc:  # noqa: BLE001
            log.debug("Taste: store не подключён (%s)", exc)
        # Shadow Engine is owned by Agent but its cadence belongs to the
        # orchestrator lifecycle, alongside other background services.
        self._shadow = self._agent._shadow
        self._shadow.attach_brain_fabric(self._brain)
        self._runtime = TaskRuntime(
            default_watchdog_sec=None,
            persistence_dir=settings.data_dir / "missions",
            durable_runner=self._durable_mission_runner,
            world_state=self._agent.executive.world,
        )
        self._agent.attach_task_runtime(self._runtime)
        self._authority = AuthorityStore(
            settings.data_dir / "authority", mission_resolver=self._runtime.get,
        )
        self._authority_unsubscribe = self._authority.bind_runtime(self._runtime)
        self._agent.attach_authority(self._authority)
        self._natural_missions = NaturalMissionCoordinator(
            self._runtime, self._authority,
            NaturalMissionInterpreter(
                clock=lambda: self._runtime._clock(),
                integrations=self._configured_integrations(),
                structured_backend=(
                    self._semantic_brain_interpret
                    if bool(getattr(settings, "deepseek_brain_mode", False)) else None
                ),
            ),
        )
        from core.living import LivingIntelligence
        self._living = LivingIntelligence(
            settings.data_dir / "living",
            task_runtime=self._runtime,
            shadow_engine=self._shadow,
            relationship_learner=self._agent.preference_learner,
        )
        self._runtime.subscribe(self._living.observe_mission_event)

        # Sprint 13: one continuity/state coordinator above the existing
        # owners. It references their real registries and policies rather than
        # cloning tools, memory, personality, attention, or Shadow behavior.
        self._cognitive = CognitiveOrchestrator(
            settings.data_dir / "cognitive",
            registry=self._registry,
            capability_registry=CAPABILITIES,
            providers={"shadow": self._shadow, "brain": self._brain},
            task_runtime=self._runtime,
            living_context=self._living.context,
            memory_hierarchy=self._agent._memory_hierarchy,
            capability_planner=self._agent._capability_planner,
            personality=self._agent.personality,
            shadow_engine=self._shadow,
            attention_manager=self._living.decisions.attention,
            goal_tracker=self._living.context.goal_tracker,
            brain_fabric=self._brain,
        )

        # TTS
        self._tts = PiperTTS(settings)
        self._speech_renderer = SpeechRenderer(settings.voice)
        self._tts_queue = TTSQueue(self._tts, renderer=self._speech_renderer)

        # Reminders
        self._task_manager = get_default_manager()
        self._task_manager.set_runtime(self._runtime)

        # Proactive
        self._proactor = Proactor(
            settings=settings,
            council=self._council,
            output_callback=self._proactive_output,
            reminder_check_callback=self._check_reminders,
        )

        # БАГ 3 FIX: подключаем callback к TaskManager после создания proactor.
        # get_default_manager() создаёт менеджер без callback → напоминания
        # молча терялись. Теперь _fire() вызывает _on_reminder_fired, который
        # выводит текст через typed AssistantOutput (не raw str).
        def _on_reminder_fired(reminder_id: str, text: str) -> None:
            try:
                message = f"Напоминание: {text}"
                log.info("Напоминание сработало #%s: %s", reminder_id, text[:80])
                self._output_callback(message)
                self._queue_assistant_output(
                    AssistantOutput.natural(message, speech_mode="focused")
                )
            except Exception as exc:
                log.error("Ошибка вывода напоминания #%s: %s", reminder_id, exc)

        self._task_manager._callback = _on_reminder_fired
        self._scheduler = BackgroundScheduler(
            settings=settings,
            task_manager=self._task_manager,
            nightly_callback=self._nightly_consolidation,
        )

        self._running = False
        self._lock = threading.Lock()
        self._warmup_thread: Optional[threading.Thread] = None
        self._warmup_diagnostics: Dict[str, Any] = {
            "backend": "unknown",
            "model": (
                str(getattr(settings, "deepseek_model", ""))
                if bool(getattr(settings, "deepseek_brain_mode", False))
                else "local-runtime"
            ),
            "n_gpu_layers": 0,
            "warmup_ms": 0.0,
            "ready_before_first_request": False,
        }
        self._warmup_ready = threading.Event()
        # Шаг 6: прогрев semantic-роутера и его статус готовности.
        self._router_diagnostics: Dict[str, Any] = {"ready": False}
        self._intake = UniversalIntake()
        self._tutor = TutorEngine()

    def _configured_integrations(self) -> set[str]:
        """Return connector names configured in settings, without loading SDKs."""
        raw = getattr(self._settings, "integrations", None)
        if isinstance(raw, Mapping):
            return {
                str(name).casefold() for name, config in raw.items()
                if config is True or bool(getattr(config, "configured", False))
                or (isinstance(config, Mapping) and bool(config.get("configured")))
            }
        return set()

    def _semantic_brain_interpret(self, text: str, contract: Mapping[str, Any]) -> Mapping[str, Any]:
        """One bounded structured Brain call; runtime/policy validate its proposal."""
        from core.brain import BrainRequest, BrainRole, PrivacyClass

        prompt = (
            "Return one JSON object matching this interpretation contract. "
            "Do not add permissions or actions not stated by the user.\n"
            f"Contract: {json.dumps(dict(contract), ensure_ascii=False)}\n"
            f"User input: {text}"
        )
        result = self._brain.generate(BrainRequest(
            user_request=prompt, role=BrainRole.PLANNER,
            privacy=PrivacyClass.PERSONAL, stage="natural_mission_interpretation",
            max_tokens=300, temperature=0.0,
        ))
        raw = str(result.text or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("semantic interpretation must be an object")
        return value

    def _register_kernel_capabilities(self) -> None:
        """Project the real action registry into the canonical capability graph."""
        family_by_name = {
            "current_time": "system", "volume": "system", "system_status": "system",
            "open_app": "operate", "close_app": "operate", "play_music": "media",
            "web_search": "research", "web_fetch": "research", "add_reminder": "monitor",
            "browser_automation": "operate", "browser_bridge": "operate",
        }
        for tool in self._registry.list_tools():
            name = str(getattr(tool, "name", "") or "")
            if not name:
                continue
            family = family_by_name.get(name, "operate")
            self._kernel.capabilities.register(CapabilityManifest(
                name=name,
                intent_families=(family,),
                inputs=dict(getattr(tool, "input_schema", {}) or {}),
                outputs={"state": "verified_state"},
                preconditions=("runtime_ready",),
                postconditions=("observed", "verified"),
                risk="low" if family in {"system", "media", "research"} else "medium",
                confirmation_policy="policy",
                verification=("execute", "observe", "verify"),
                rollback=("available_when_declared",),
                reliability=0.8,
            ))

    # --------------------------------------------------------------------- #
    #  Публичный API
    # --------------------------------------------------------------------- #

    def install_stream_sink(self, sink) -> None:
        """Sprint 1: проброс stream-sink в агент для ТЕКУЩЕГО потока запроса."""
        install = getattr(self._agent, "install_stream_sink", None)
        if callable(install):
            install(sink)

    def clear_stream_sink(self) -> None:
        clear = getattr(self._agent, "clear_stream_sink", None)
        if callable(clear):
            clear()

    # ------------------------------------------------------------------ #
    #  Единая точка решения (2026-09-05): semantic_route()
    # ------------------------------------------------------------------ #

    _LLM_PROBE_TTL = 30.0

    def _llm_available_cached(self) -> bool:
        """Реальная доступность провайдера (health-эквивалент, кэш 30 c)."""
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

    def _route_cached(self, text: str) -> SemanticRoutingDecision:
        """semantic_route с memo последнего запроса (одна маршрутизация на виток)."""
        memo = getattr(self, "_route_memo", None)
        if memo is not None and memo[0] == text:
            return memo[1]
        llm_available = self._llm_available_cached()
        decision = semantic_route(
            text,
            SemanticRoutingContext(
                llm_available=llm_available,
                fast_llm_fn=self._fast_llm_route if llm_available else None,
                addressing=self._profile_addressing(),
            ),
        )
        self._route_memo = (text, decision)
        return decision

    _FAST_LLM_KINDS = {"action", "question", "chat", "mission", "fresh_data"}

    def _fast_llm_route(self, text: str,
                        candidates: List[Tuple[str, float]]) -> Dict[str, Any]:
        """FAST LLM tier semantic-роутера: разбор серой зоны провайдером.

        Возвращает {"kind", "tool", "confidence"} или {} — роутер тогда
        сам уходит в clarify/fallback. Никогда не бросает исключений.
        """
        try:
            from core.llm import Tier, get_llm_backend
            backend = get_llm_backend(self._settings, Tier.FAST)
            options = ", ".join(name for name, _score in (candidates or [])[:4])
            prompt = (
                "Классифицируй реплику пользователя. Ответь ТОЛЬКО валидным "
                'JSON без пояснений: {"kind": "...", "tool": null}, где kind — '
                "одно из action | question | chat | mission | fresh_data, "
                f"tool — имя инструмента из списка: {options or 'нет'} или null.\n"
                f"Реплика: «{text}»"
            )
            raw = backend.chat(
                [{"role": "user", "content": prompt}],
                max_tokens=64, temperature=0.0,
            )
            body = str(raw or "")
            start, end = body.find("{"), body.rfind("}")
            if start < 0 or end <= start:
                return {}
            payload = json.loads(body[start:end + 1])
            kind = str(payload.get("kind") or "")
            if kind not in self._FAST_LLM_KINDS:
                return {}
            tool = payload.get("tool")
            return {"kind": kind, "tool": tool, "confidence": 0.8}
        except Exception as exc:
            log.debug("fast_llm_route не удался: %s", exc)
            return {}

    def _profile_addressing(self) -> str:
        """Обращение из профиля пользователя; пустой профиль — без обращения."""
        try:
            profile = load_profile(self._settings)
            return str((profile.get("name") or "")).strip()
        except Exception as exc:
            log.debug("Адресация из профиля недоступна: %s", exc)
            return ""

    @staticmethod
    def _decision_from_metadata(mission: Mission) -> Optional[SemanticRoutingDecision]:
        payload = (getattr(mission, "metadata", None) or {}).get("routing_decision")
        if not payload:
            return None
        try:
            return SemanticRoutingDecision.from_dict(payload)
        except Exception as exc:
            log.debug("Не удалось восстановить routing decision миссии: %s", exc)
            return None

    # ------------------------------------------------------------------ #
    #  Clarify-сессия (шаг 4): awaiting clarification + TTL 60 c
    # ------------------------------------------------------------------ #

    _CLARIFY_TTL_SEC = 60.0

    def _remember_clarification(self, question: str, decision: SemanticRoutingDecision,
                                original_text: str = "") -> None:
        self._clarification = {
            "question": question,
            "candidates": list((decision.candidates or [])[:2]),
            "base": decision.to_dict(),
            "original": original_text,
            "ts": time.monotonic(),
        }

    def _pending_clarification(self) -> Optional[Dict[str, Any]]:
        pending = getattr(self, "_clarification", None)
        if not pending:
            return None
        if time.monotonic() - pending["ts"] > self._CLARIFY_TTL_SEC:
            self._clarification = None
            return None
        return pending

    def _clear_pending_clarification(self) -> None:
        self._clarification = None

    def _resolve_clarification(self, pending: Dict[str, Any],
                               reply_text: str) -> Optional[Tuple[str, str]]:
        """Резолвит короткий ответ пользователя по топ-2 кандидатам.

        Возвращает (candidate_id, цель_для_исполнения) или None, если
        реплика не похожа на выбор — тогда она идёт полным маршрутом.
        """
        import re as _re

        reply = (reply_text or "").strip().casefold()
        if not reply:
            return None
        candidates = pending.get("candidates") or []
        if not candidates:
            return None
        first = candidates[0][0]
        second = candidates[1][0] if len(candidates) > 1 else None

        verb_hints = {
            "open_app": ("открой", "открыть", "запусти", "включи"),
            "close_app": ("закрой", "закрыть", "выруби", "заверши"),
            "volume": ("громче", "тише", "потише", "погромче"),
            "current_time": ("время", "час"),
            "system_status": ("статус", "состояние"),
            "play_music": ("музык", "песн", "трек"),
            "list_files": ("список файлов", "покажи файлы", "файлы"),
            "search_files": ("найди", "поищи", "искать"),
            "weather": ("погод",),
            "public_data": ("курс", "валют", "новост"),
            "add_reminder": ("напомни",),
            "mission": ("поручение", "задание", "займись", "разбери"),
            "chat": ("поболтать", "разговор", "болтать"),
            "question": ("вопрос",),
        }
        for cid in (first, second):
            if cid is None:
                continue
            for hint in verb_hints.get(cid, ()):
                if hint in reply:
                    return cid, (reply_text or "").strip()
        negative = _re.match(r"^\s*(нет|не|нет,)\b", reply)
        if negative and second:
            return second, (reply_text or "").strip()
        if _re.search(r"\b(да|ага|ок|давай|угу|верно|перв\w+)\b", reply):
            return first, (reply_text or "").strip()
        return None

    def _decision_for_candidate(self, base: SemanticRoutingDecision,
                                candidate_id: str) -> SemanticRoutingDecision:
        """Синтезирует decision выбранного кандидата clarify-сессии (шаг 4).

        Риск пересчитывается по выбранному инструменту — правило без
        исключений: destructive/write подтверждается независимо от пути.
        """
        candidate_id = (candidate_id or "").strip()
        if candidate_id in {"chat", "question", "mission"}:
            kind: str = candidate_id
            tool: Optional[str] = None
        elif candidate_id in {"weather", "public_data"}:
            kind, tool = "fresh_data", candidate_id
        else:
            kind, tool = "action", candidate_id or None
        raw = str((base.trace or {}).get("raw_text") or "")
        try:
            risk_level, needs_conf = semantic_assess_risk(tool, None, raw)
        except Exception as exc:
            log.debug("Пересчёт риска clarify-кандидата не удался: %s", exc)
            risk_level, needs_conf = base.risk, base.needs_confirmation
        return _dataclass_replace(
            base,
            kind=kind,
            tool=tool,
            confidence=max(base.confidence, 0.6),
            tier="clarify_resolve",
            risk=risk_level,
            needs_confirmation=needs_conf,
            trace={**(base.trace or {}), "clarify_resolved": candidate_id},
        )

    def _emit_route_event(self, decision: SemanticRoutingDecision,
                          llm_available: bool) -> None:
        """Шаг 7: объяснимость — маршрут в WS-событие (если слушатели есть)."""
        try:
            payload = {
                "tier": decision.tier,
                "kind": decision.kind,
                "tool": decision.tool or "",
                "confidence": round(decision.confidence, 3),
                "margin": round(float((decision.trace or {}).get("margin_agg", 0.0)), 3),
                "top3": [c for c, _ in (decision.candidates or [])[:3]],
                "risk": decision.risk,
                "needs_confirmation": decision.needs_confirmation,
                "latency_ms": round(float((decision.trace or {}).get("latency_ms", 0.0)), 2),
                "llm_available": bool(llm_available),
            }
            log.info("route: %s", json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            log.debug("route event не отправлен: %s", exc)

    def _new_state(self, text: str, *, include_executive: bool = True) -> JarvisState:
        """Create a state with semantic routing context and bounded executive context."""
        state = new_state(text)
        decision = self._route_cached(text)
        state["intent"] = semantic_intent_category(decision)
        # Шаг 7: объяснимость — полное решение маршрутизации в состоянии
        # (WS-мост транслирует его в событие route).
        routing_dict = decision.to_dict()
        routing_dict["margin"] = round(
            float((decision.trace or {}).get("margin_agg", 0.0)), 3)
        routing_dict["latency_ms"] = round(
            float((decision.trace or {}).get("latency_ms", 0.0)), 2)
        routing_dict["llm_available"] = self._llm_available_cached()
        state["routing_decision"] = routing_dict
        try:
            state["task_contract"] = self._intake.classify(text).to_dict()
        except Exception as exc:
            log.debug("Universal intake skipped: %s", exc)
            state["task_contract"] = {}
        state["latency_budget"] = self._latency_budget_for(state.get("intent"))
        if include_executive:
            try:
                state["executive"] = self._agent.executive.snapshot()
            except Exception as exc:
                log.debug("Executive snapshot unavailable: %s", exc)
                state["executive"] = {}
        return state

    @staticmethod
    def _latency_budget_for(intent: Optional[str]) -> Dict[str, Any]:
        fast = intent in {"app", "system", "media"}
        if fast:
            budget = LatencyBudget("fast", 600.0, 1000.0, 1500.0)
        elif intent == "web":
            budget = LatencyBudget("research", 8000.0, 15000.0, 30000.0, 3000.0)
        else:
            budget = LatencyBudget("deliberate", 8000.0, 15000.0, 30000.0, 2500.0)
        return {
            "path": budget.path,
            "p50_ms": budget.p50_ms,
            "p95_ms": budget.p95_ms,
            "hard_max_ms": budget.hard_max_ms,
            "first_progress_p95_ms": budget.first_progress_p95_ms,
        }

    @staticmethod
    def _stamp_latency(state: JarvisState, started: float, path: str) -> JarvisState:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        state.setdefault("latency", {})["total_ms"] = round(elapsed_ms, 3)
        state["latency"]["path"] = path
        state.setdefault("evidence", []).append(EvidenceRecord(
            claim="request reached verified state" if state.get("verified") else "request completed without verified state",
            source="orchestrator",
            confidence=1.0 if state.get("verified") else 0.0,
            expected_state={"verified": True},
            observed_state={"verified": bool(state.get("verified")), "tool": str(state.get("tool", ""))},
            latency_ms=elapsed_ms, path=path,
        ).to_dict())
        return state

    def _warmup_router(self) -> None:
        """Шаг 6: прогрев semantic-роутера до объявления готовности (<= 40 мс)."""
        try:
            started = time.perf_counter()
            router = get_router()
            warm_ms = float(router.warmup() or 0.0)
            probe_start = time.perf_counter()
            semantic_route("статус системы", SemanticRoutingContext(llm_available=False))
            probe_ms = (time.perf_counter() - probe_start) * 1000.0
            self._router_diagnostics = {
                "ready": probe_ms <= 40.0,
                "warmup_ms": round(warm_ms, 2),
                "probe_ms": round(probe_ms, 2),
            }
            log.info("Semantic router warmup: %s", self._router_diagnostics)
        except Exception as exc:
            log.error("Semantic router warmup failed: %s", exc)
            self._router_diagnostics = {"ready": False, "error": str(exc)}

    def provider_status(self) -> str:
        """Шаг 6: статус провайдера отдельно от режима deepseek_brain_mode.

        configured — ключ задан, health-проба ещё не выполнялась;
        ready / unavailable — по результату пробы.
        """
        try:
            from core.llm import Tier
            key_configured = bool(self._settings.is_tier_available(Tier.FAST))
        except Exception as exc:
            log.debug("Проверка конфигурации провайдера не удалась: %s", exc)
            return "unavailable"
        if not key_configured:
            return "unavailable"
        probed = getattr(self, "_llm_probe", None)
        if probed is None:
            return "configured"
        return "ready" if bool(probed[1]) else "unavailable"

    def _start_local_warmup(self) -> None:
        """Warm local backend before the first user request and record diagnostics."""
        if (not bool(getattr(self._settings, "warmup_local_on_start", False))
                and not bool(getattr(self._settings, "deepseek_brain_mode", False))):
            return
        if self._warmup_thread is not None and self._warmup_thread.is_alive():
            return

        def _warm() -> None:
            started = time.perf_counter()
            if bool(getattr(self._settings, "deepseek_brain_mode", False)):
                try:
                    from core.brain import BrainRequest, BrainRole
                    provider_name = str(getattr(self._settings, "deepseek_provider", "deepinfra"))
                    provider = self._brain.registry.get(provider_name)
                    if provider is None:
                        raise RuntimeError(f"provider {provider_name} не зарегистрирован")
                    statuses = self._brain.refresh_health()
                    snapshot = statuses.get(provider_name)
                    if snapshot is None or str(getattr(snapshot.status, "value", snapshot.status)) != "available":
                        detail = str(getattr(snapshot, "last_error", "provider health check failed") or "provider health check failed")
                        raise RuntimeError(detail)
                    probe = provider.generate(BrainRequest(
                        user_request="Ответь одним словом: готов",
                        role=BrainRole.FAST,
                        max_tokens=1,
                        temperature=0.0,
                    ), model=str(getattr(self._settings, "deepseek_model", "")))
                    if not str(probe.text or "").strip():
                        raise RuntimeError("DeepSeek readiness probe вернул пустой ответ")
                    self._warmup_diagnostics.update({
                        "backend": provider_name,
                        "model": str(getattr(self._settings, "deepseek_model", "")),
                        "warmup_ms": round((time.perf_counter() - started) * 1000.0, 3),
                        "ready_before_first_request": True,
                        "state": "ready",
                        "runtime": {
                            "provider": provider_name,
                            "endpoint": str(getattr(self._settings, "api_endpoints", {}).get(provider_name) or ""),
                            "model_probe": True,
                            "probe_latency_ms": round(float(probe.latency_ms), 3),
                            "error": None,
                        },
                    })
                except Exception as exc:
                    self._warmup_diagnostics.update({
                        "backend": "unavailable",
                        "warmup_ms": round((time.perf_counter() - started) * 1000.0, 3),
                        "error": f"{type(exc).__name__}: {exc}",
                        "ready_before_first_request": False,
                        "state": "unavailable",
                    })
                    log.error("DeepInfra readiness probe failed: %s", exc)
                finally:
                    self._warmup_ready.set()
                return
            if not bool(getattr(self._settings, "local_llm_enabled", False)):
                # C1: cloud-only сборка — локальный мозг не греется и GGUF
                # не скачивается; readiness закрывается, чтобы wait_for_warmup
                # не ждал того, что отключено.
                self._warmup_diagnostics["local_llm"] = "disabled (cloud-only build)"
                self._warmup_ready.set()
                return
            try:
                # A clean install may have no GGUF yet.  Prepare exactly one
                # hardware-selected artifact in the background; an existing
                # configured file remains the safe offline fallback.
                try:
                    from core.llm.hardware_profile import apply_profile, recommend_profile
                    from core.utils.model_manager import ModelManager

                    profile = recommend_profile(
                        models_dir=self._settings.models_dir,
                        model_family=str(getattr(self._settings, "model_family", "qwen") or "qwen"),
                    )
                    configured = getattr(self._settings.local_model, "resolved_gguf_path", None)
                    configured_exists = bool(configured is not None and configured.is_file())
                    if (profile.download_required
                            and bool(getattr(self._settings, "auto_download_models", True))
                            and not configured_exists):
                        self._warmup_diagnostics["state"] = "downloading_model"
                        manager = ModelManager(self._settings)

                        def _progress(event: Dict[str, Any]) -> None:
                            self._warmup_diagnostics["download"] = dict(event)

                        manager.ensure_model(profile, progress=_progress)
                        apply_profile(self._settings, logger=log)
                    elif profile.download_required:
                        self._warmup_diagnostics["model_download"] = "skipped_existing_fallback"
                except Exception as exc:
                    # A missing network/model is reported in diagnostics; it
                    # must not prevent the local service from binding its WS.
                    self._warmup_diagnostics["model_download"] = f"unavailable: {type(exc).__name__}"
                    log.warning("Local model preparation skipped: %s", exc)

                from core.llm import Tier, get_llm_backend
                backend = get_llm_backend(self._settings, Tier.FAST)
                warm_up = getattr(backend, "warm_up", None)
                if callable(warm_up):
                    warm_up()
                    log.info("Local FAST backend warmed before first request")
                else:
                    log.debug("FAST backend has no warm_up hook: %s", type(backend).__name__)
                info = dict(getattr(backend, "runtime_info", lambda: {})() or {})
                raw_layers = info.get("n_gpu_layers", 0)
                if isinstance(raw_layers, str):
                    configured_layers = -1 if raw_layers.casefold() in {"all", "auto", "-1"} else int(raw_layers or 0)
                else:
                    configured_layers = int(raw_layers or 0)
                runtime_backend = str(info.get("runtime_backend", "") or "").casefold()
                safe_runtime = {
                    str(key): value for key, value in info.items()
                    if str(key).casefold() not in {"model", "model_path", "gguf_path", "filename"}
                }
                self._warmup_diagnostics.update({
                    "backend": "cuda" if configured_layers != 0 or runtime_backend in {"cuda", "vulkan", "metal"} else "cpu",
                    "model": "local-runtime",
                    "n_gpu_layers": configured_layers,
                    "warmup_ms": round((time.perf_counter() - started) * 1000.0, 3),
                    "ready_before_first_request": True,
                    "runtime": safe_runtime,
                    "state": "ready",
                })
            except Exception as exc:
                # Warmup is an optimisation; normal lazy loading remains valid.
                log.warning("Local backend warmup skipped: %s", exc)
                self._warmup_diagnostics.update({
                    "backend": "unavailable",
                    "warmup_ms": round((time.perf_counter() - started) * 1000.0, 3),
                    "error": f"{type(exc).__name__}: {exc}",
                    "ready_before_first_request": False,
                    "state": "unavailable",
                })
            finally:
                self._warmup_ready.set()

        # Startup must return control to the WS/UI immediately.  The model
        # load is still paid once, but it runs behind the readiness handshake
        # instead of blocking the socket from binding for ten seconds.
        self._warmup_thread = threading.Thread(
            target=_warm,
            name="jarvis-local-warmup",
            daemon=True,
        )
        self._warmup_thread.start()

    def consume_streamed_mission(self):
        """Sprint 1: task_id миссии, стримленной в текущем потоке (или None)."""
        consume = getattr(self._agent, "consume_streamed_mission", None)
        if callable(consume):
            return consume()
        return None

    def start(self) -> None:
        """Запускает все фоновые сервисы."""
        with self._lock:
            if self._running:
                return
            self._running = True

            # Шаг 6: semantic-роутер прогревается до объявления готовности —
            # route() обязан отвечать за <= 40 мс с первого запроса.
            self._warmup_router()
            # C5: одноразовый бутстрап ключа из secrets.local.json в DPAPI
            # (делается до первых обращений к провайдеру).
            try:
                from core.brain.secrets import bootstrap_from_local_secrets
                bootstrap_from_local_secrets(self._settings)
            except Exception as exc:
                log.debug("secrets bootstrap пропущен: %s", exc)
            self._start_local_warmup()
            # C3: фоновый health-probe провайдера раз в минуту.
            try:
                self._brain.start_probe(interval_sec=60.0)
            except Exception as exc:
                log.debug("provider probe не запущен: %s", exc)
            # Never hold the WS/UI socket on model loading.  The readiness
            # event is reported through the runtime_status handshake; reflex
            # actions and TTS can start while the deliberate model warms.

            # Short-term memory manager
            max_size = getattr(getattr(self._settings, "limits", None), "short_memory_size", 20)
            from core.memory import SessionManager
            self._session = SessionManager(max_size=max_size)

            # TTS queue
            if self._settings.voice.tts_enabled and self._tts.is_available():
                self._tts_queue.start()
                log.info("TTS queue запущен")
            else:
                log.info("TTS отключен или недоступен")

            # Proactive
            self._proactor.start()
            self._runtime.start_scheduler()
            self._scheduler.start()
            shadow_cfg = getattr(self._settings, "shadow", None)
            self._shadow.start(interval_sec=int(getattr(shadow_cfg, "interval_sec", 300)))
            self._living.start()

            # Wake word (Фаза 3): если включён и движок доступен — слушает.
            ww = getattr(self, "_wake_word", None)
            if ww is not None:
                try:
                    ww.start()
                except Exception as exc:  # noqa: BLE001
                    log.warning("Wake word не запустился: %s", exc)

            log.info("Оркестратор запущен")

    def shutdown(self) -> None:
        """Корректно останавливает всё."""
        with self._lock:
            if not self._running:
                return
            self._running = False

        self._proactor.stop()
        self._runtime.stop_scheduler()
        self._scheduler.stop()
        self._living.stop()
        self._shadow.stop()
        ww = getattr(self, "_wake_word", None)
        if ww is not None:
            try:
                ww.stop()
            except Exception as exc:  # noqa: BLE001
                log.debug("Wake word stop: %s", exc)
        self._tts_queue.stop(wait=True)
        try:
            self._brain.close()
        except Exception as exc:
            log.debug("Brain Fabric shutdown cleanup skipped: %s", exc)
        try:
            self._kernel.close()
        except Exception as exc:
            log.debug("Cognitive kernel shutdown cleanup skipped: %s", exc)

        log.info("Оркестратор остановлен")

    def handle_input(self, text: str, *, channel: str = "text",
                     implicit_address: Optional[bool] = None) -> JarvisState:
        """C4-граница: виток обработки никогда не бросает исключение вызывающему.

        Raw-текст сбоя модели (ProviderUnavailable, NoRouteAvailable, цепочки
        BackendUnavailable) остаётся в логе; пользователь получает
        человеческую фразу из ``core.brain.error_messages``. Вся логика —
        в :meth:`_handle_input_impl`.
        """
        started = time.perf_counter()
        try:
            return self._handle_input_impl(text, channel=channel,
                                           implicit_address=implicit_address)
        except Exception as exc:
            log.exception("Виток обработки ввода упал")
            from core.brain.error_messages import user_message_for
            from core.voice.output import AssistantOutput
            message = user_message_for(exc)
            state = self._new_state(text)
            self._session.push("user", text)
            self._session.to_state(state)
            state["response"] = message
            state["error"] = f"{type(exc).__name__} (детали в логе)"
            state["assistant_output"] = AssistantOutput.failure(
                display_text=message,
                error=ErrorInfo(
                    ErrorCategory.UNKNOWN_FAILURE,
                    technical_message=f"{type(exc).__name__}: redacted, see logs",
                ),
            ).to_dict()
            return self._stamp_latency(state, started, "error")

    def _handle_input_impl(self, text: str, *, channel: str = "text",
                           implicit_address: Optional[bool] = None) -> JarvisState:
        """Полный цикл обработки пользовательского ввода.

        Единый вход (§3, §5): любой ввод идёт через один реальный путь —
        агентную миссию (intent -> risk -> MODEL SELECTION -> tool ->
        verify -> repair -> memory). Чтобы лёгкие запросы («привет»,
        простые команды) не платили цену за тяжёлый цикл планирования и
        фоновый поток, они завершаются **синхронно** внутри того же
        агентного пути (fast path, §3) и возвращают JarvisState сразу.
        Всё, что требует реальной работы (анализ, задача, инструмент),
        уходит в фоновую миссию с мгновенным ACK, как и задумано в ТЗ
        (§4, §5, §23): пользователь видит ACK, а работа продолжается
        асинхронно, присылать progress/result.

        Args:
            text: текст от пользователя.

        Returns:
            JarvisState с заполненными полями: response, tts_text, error, etc.
        """
        request_started = time.perf_counter()
        if not self._running:
            self.start()

        # Вкусы (память/вкусы): учимся из реплик о музыке/медиа незаметно и
        # не на горячем пути — observe() только regex, файл пишет если реальный
        # сигнал (жанр/настроение/артист) найден.
        if text and hasattr(self, "_taste"):
            try:
                self._taste.observe(text)
            except Exception as exc:  # noqa: BLE001
                log.debug("Taste observe: %s", exc)

        # Отмечаем активность для proactor
        self._proactor.mark_user_activity()
        # C3: probe-if-stale — после простоя > 5 мин первый запрос получает
        # свежую пробу провайдера до того, как пойдёт в мозг.
        try:
            self._brain.mark_activity()
            self._brain.probe_if_stale(max_idle_sec=300.0)
        except Exception as exc:
            log.debug("probe_if_stale пропущен: %s", exc)
        living_context = self._living.context.current
        living_state = (
            living_context.to_dict()
            if hasattr(living_context, "to_dict")
            else dict(vars(living_context)) if living_context is not None else {}
        )
        self._agent.set_user_context({
            **living_state,
            "music_preference": self._taste.context(),
        })

        # Шаг 4: clarify-цикл. Если сессия ждёт уточнения (TTL 60 c),
        # короткая реплика ("да", "первое", "закрой") резолвится по топ-2
        # кандидатам без полного перезапуска; исполняется исходный запрос
        # с синтезированным decision выбранного кандидата.
        pending = self._pending_clarification()
        if pending is not None:
            resolved = self._resolve_clarification(pending, text)
            if resolved is not None:
                cid, _reply = resolved
                self._clear_pending_clarification()
                try:
                    base = SemanticRoutingDecision.from_dict(pending.get("base"))
                except Exception as exc:
                    log.debug("Clarify base decision восстановить не удалось: %s", exc)
                    base = None
                if base is not None:
                    orig = str(pending.get("original") or "").strip()
                    if orig and _reply:
                        text = f"{_reply} {orig}"
                    else:
                        text = orig or _reply or text
                    self._route_memo = (text, self._decision_for_candidate(base, cid))

        # Single Source of Truth: единый семантический роутер
        decision = self._route_cached(text)
        pre_intent = semantic_intent_category(decision)
        self._emit_route_event(decision, self._llm_available_cached())

        # Understanding Layer: структура понимания читает решение единого роутера
        understanding = self._understanding.understand(text, channel=channel)
        if decision.kind == "mission":
            understanding.route = Route.MISSION
        elif decision.kind == "question":
            understanding.route = Route.QUICK_ANSWER
        elif decision.kind == "chat":
            understanding.route = Route.REFLEX
        elif decision.kind == "clarify":
            understanding.route = Route.CLARIFY
        elif decision.kind in ("action", "fresh_data"):
            understanding.route = Route.ACTION

        # P2C: one structured semantic pass. Only long-lived/control modes are
        # consumed here; ordinary conversation and immediate actions continue
        # through the established one-generation P0 path below.
        semantic = self._natural_missions.interpreter.interpret(text, source_role="user")
        if semantic.mode in {
            SemanticMode.SCHEDULED_MISSION, SemanticMode.CONDITIONAL_MISSION,
            SemanticMode.DELEGATED_MISSION, SemanticMode.MISSION_CONTROL,
            SemanticMode.FOLLOW_UP,
        }:
            natural = self._natural_missions.handle(
                text, source_role="user", source_id=f"input-{time.time_ns()}",
                interpretation=semantic,
            )
            state = self._direct_cognitive_response(
                text, natural.response or natural.clarification,
                mode=semantic.mode.value, verified=False,
            )
            state["semantic_mode"] = semantic.mode.value
            state["semantic_llm_calls"] = semantic.llm_calls
            state["required_integration"] = semantic.required_integration
            state["integration_configured"] = semantic.integration_configured
            if natural.mission is not None:
                state["mission_id"] = natural.mission.task_id
                state["mission_status"] = natural.mission.status.value
            return self._stamp_latency(state, request_started, "fast")

        # Reflex actions must reach the deterministic tool path before the
        # continuity coordinator tries to interpret them as a follow-up to a
        # stale mission (for example, "Системный статус" after "Открой
        # блокнот"). Conversation and voice addressing still use the
        # cognitive layer; explicit actions never pay that ambiguity tax.

        # Шаг 4: clarify как продуктовое поведение. semantic-уточнение
        # уходит обычным assistant_output (НЕ confirmation_required),
        # сессия запоминается с TTL 60 c.
        if decision.kind == "clarify" and decision.clarify_question:
            self._remember_clarification(decision.clarify_question, decision, text)
            state = self._new_state(text)
            self._session.push("user", text)
            self._session.push("assistant", decision.clarify_question)
            self._session.to_state(state)
            output = AssistantOutput.natural(decision.clarify_question, speech_mode="focused")
            spoken = self._queue_assistant_output(output)
            self._output_callback(decision.clarify_question)
            state["response"] = decision.clarify_question
            state["tts_text"] = spoken
            state["assistant_output"] = output.to_dict()
            state["mode"] = "clarification"
            return self._stamp_latency(state, request_started, "fast")

        cognitive_turn = None
        if pre_intent not in {"app", "system", "media", "file", "browser"}:
            # Text arriving through the explicit chat/WS input is implicitly
            # addressed to JARVIS. Voice callers can use ``cognitive``
            # directly with ``implicit_address=False`` before forwarding.
            if implicit_address is None:
                implicit_address = channel != "voice"
            cognitive_turn = self._cognitive.begin_interaction(
                text, channel=channel, implicit_address=implicit_address,
            )
        if cognitive_turn is not None and cognitive_turn.action == "wait":
            state = self._new_state(text)
            state["response"] = ""
            state["tts_text"] = None
            state["addressed_to_atlas"] = False
            state["address_confidence"] = cognitive_turn.confidence
            return self._stamp_latency(state, request_started, "fast")
        if cognitive_turn is not None and cognitive_turn.action in {"clarify", "self_knowledge"}:
            return self._stamp_latency(self._direct_cognitive_response(text, cognitive_turn.response), request_started, "fast")
        if cognitive_turn is not None and cognitive_turn.action in {"continue", "retry"} and cognitive_turn.goal:
            text = cognitive_turn.goal
            decision = self._route_cached(text)
            pre_intent = semantic_intent_category(decision)
            self._emit_route_event(decision, self._llm_available_cached())
            understanding = self._understanding.understand(text, channel=channel)
            if decision.kind == "mission":
                understanding.route = Route.MISSION
            elif decision.kind == "question":
                understanding.route = Route.QUICK_ANSWER
            elif decision.kind == "chat":
                understanding.route = Route.REFLEX
            elif decision.kind == "clarify":
                understanding.route = Route.CLARIFY
            elif decision.kind in ("action", "fresh_data"):
                understanding.route = Route.ACTION

        # Sprint 11: natural queries are answered from evidence-backed local
        # structured context. Action intents skip retrieval entirely: loading
        # Chroma/RAG/graph on a media or app command is a latency regression,
        # not intelligence.
        context_reply = (
            self._living.answer_context(text)
            if (not bool(getattr(self._settings, "deepseek_brain_mode", False))
                and pre_intent not in {"app", "system", "media", "file", "browser"})
            else None
        )
        if context_reply is not None:
            response = str(context_reply["answer"])
            output = AssistantOutput.natural(response, speech_mode="focused")
            state = self._new_state(text)
            self._session.push("user", text)
            self._session.push("assistant", response)
            self._session.to_state(state)
            state["response"] = response
            state["context_evidence"] = list(context_reply.get("evidence") or [])
            spoken = self._queue_assistant_output(output)
            self._output_callback(response)
            state["tts_text"] = spoken
            state["assistant_output"] = output.to_dict()
            return self._stamp_latency(state, request_started, "fast")

        # Единый путь (§3): тяжёлая задача -> фон (мгновенный ACK),
        # лёгкая -> синхронно в том же агентном цикле (без фоновой миссии).
        # Understanding Layer имеет слово первым: mission — всегда фон,
        # quick_answer — всегда синхронно (запрет уходить в mission даже
        # для сложных вопросов, фикс «почему...» в фоне). Остальное —
        # эвристика _should_run_background.
        if understanding.route == Route.MISSION:
            run_background = True
        elif understanding.route == Route.QUICK_ANSWER:
            run_background = False
            # Фаза 2: выделенный быстрый путь — вопрос -> (память | поиск
            # DuckDuckGo) -> сжатие локальной Qwen -> ответ за 1-3 сек.
            # НЕ уходит в агентный цикл действий и НЕ в миссию. Фикс A3:
            # любая деградация озвучивается живо (честно), не canned-фразой.
            if not bool(getattr(self._settings, "deepseek_brain_mode", False)):
                answer = self._quick_answer.answer(text)
                qa_state = self._direct_cognitive_response(
                    text,
                    answer.text,
                    mode="quick_answer",
                    verified=answer.verified,
                )
                qa_state["route"] = understanding.route.value
                qa_state["quick_answer"] = answer.to_dict()
                qa_state["confidence"] = understanding.confidence
                return self._stamp_latency(qa_state, request_started, "fast")
        else:
            run_background = self._should_run_background(text)
        self._living.observe_user_input(text, active_mission=run_background)
        living_context = self._living.context.current
        living_state = (
            living_context.to_dict()
            if hasattr(living_context, "to_dict")
            else dict(vars(living_context)) if living_context is not None else {}
        )
        self._agent.set_user_context({
            **living_state,
            "music_preference": self._taste.context(),
        })
        if run_background:
            mission = self.submit_goal(text)
            self._cognitive.state.current_goal = mission.goal
            self._cognitive.state.active_task = mission.current_step or "mission queued"
            self._cognitive.state.active_mission_id = mission.task_id
            self._cognitive.state.mission_state = mission.status.value
            self._cognitive.store.save(self._cognitive.state)
            ack = mission.acknowledgement or "Понял. Уже разбираюсь."
            state = self._new_state(text)
            self._session.push("user", text)
            self._session.to_state(state)
            state["response"] = ack
            ack_output = AssistantOutput.natural(ack, speech_mode="focused")
            rendered = self._speech_renderer.render(ack_output)
            state["tts_text"] = rendered.text if rendered else None
            state["assistant_output"] = ack_output.to_dict()
            state["mission_id"] = mission.task_id
            state["route"] = understanding.route.value
            return self._stamp_latency(state, request_started, "background")

        # Синхронный путь через единый агентный цикл
        # (intent -> risk -> MODEL SELECTION -> tool -> verify -> repair).
        outcome = self._agent.execute(text, decision=decision)
        if outcome.tool_used or outcome.mode in {"capability", "unknown_task"}:
            self._living.observe_capability_outcome(
                text, verified=outcome.verified,
                capability_id=outcome.tool_used or outcome.mode,
            )
            def _record_cognitive() -> None:
                self._cognitive.record_external_outcome(
                    goal=text, result=outcome.text, verified=bool(outcome.verified),
                    pending=[] if outcome.verified else ["independent result verification"],
                )
            if outcome.mode == "fast_path":
                threading.Thread(target=_record_cognitive,
                                 name="jarvis-cognitive-write", daemon=True).start()
            else:
                _record_cognitive()

        output = assistant_output_from_outcome(outcome)
        if bool(getattr(self._settings, "deepseek_brain_mode", False)):
            display = re.sub(r",?\s*сэр\b", "", output.display_text, flags=re.IGNORECASE)
            display = re.sub(r"\s+([.!?])", r"\1", display).strip()
            speech = output.speech_text
            if speech:
                speech = re.sub(r",?\s*сэр\b", "", speech, flags=re.IGNORECASE)
                speech = re.sub(r"\s+([.!?])", r"\1", speech).strip()
            output = AssistantOutput(
                display_text=display, speech_text=speech, debug=output.debug,
                error=output.error, speak=output.speak, speech_mode=output.speech_mode,
            )
        response = (output.display_text or "").strip()
        if not response:
            # C4: пустой ответ модели — человеческая фраза без имён провайдеров.
            from core.brain.error_messages import user_message_for
            response = user_message_for("assistant output was empty")
            output = AssistantOutput.failure(
                display_text=response,
                error=ErrorInfo(
                    ErrorCategory.UNKNOWN_FAILURE,
                    technical_message="assistant output was empty",
                ),
            )

        state = self._new_state(
            text,
            include_executive=outcome.mode not in {"fast_path", "conversation_fast"},
        )
        self._session.push("user", text)
        self._session.to_state(state)

        # Memory: remember substantive exchanges only. Reflex tool calls are
        # already verified by their own evidence and must not start a cold
        # Chroma/RAG/graph initialisation thread on the latency-critical path.
        if (not outcome.needs_confirmation
                and outcome.mode not in {"fast_path", "conversation_fast"}):
            self._remember_exchange_background(text, response)

        # Short-term: push assistant
        self._session.push("assistant", response)
        self._session.to_state(state)

        spoken = self._queue_assistant_output(output)

        # Output callback (печать/тост)
        self._output_callback(response)

        state["response"] = response
        state["tts_text"] = spoken
        state["assistant_output"] = output.to_dict()
        state["tool"] = outcome.tool_used or ""
        state["tool_used"] = outcome.tool_used or ""
        state["verified"] = bool(outcome.verified)
        state["mode"] = outcome.mode
        state["route"] = understanding.route.value
        state.setdefault("latency", {})["stages"] = dict(outcome.latency_stages)
        if outcome.needs_confirmation:
            state["confirmation_id"] = getattr(outcome, "confirmation_id", None)
            state["needs_confirmation"] = True
        return self._stamp_latency(state, request_started, "background" if run_background else "fast")

    def _should_run_background(self, text: str) -> bool:
        """Решает, уходит ли задача в фоновую миссию (с ACK) или исполняется синхронно.

        В фон уходят исследовательские задачи и всё, что ModelRouter
        классифицирует как достаточно сложное (score >= LOCAL_THRESHOLD).
        Простые команды и приветствия исполняются синхронно — без
        тяжёлого цикла планирования и фонового потока (TEST 1).
        """
        goal = (text or "").strip()
        if not goal:
            return False
        # A conversational question must stay on the immediate path.  The
        # previous score-only check sent "почему..." to a background mission,
        # showing an ACK while the CPU model kept thinking for tens of seconds.
        decision = self._route_cached(goal)
        if decision.kind in ("chat", "question"):
            return False
        if decision.kind == "mission":
            return True
        if decision.kind == "action" and decision.tool is None:
            return False  # unsupported: честный отказ синхронно
        cx = estimate_complexity(goal)
        # LOCAL_THRESHOLD из ModelRouter (0.35): выше — в фон.
        return cx.score >= 0.35

    # --------------------------------------------------------------------- #
    #  Ответ на подтверждение HIGH-risk операции (§21)
    # --------------------------------------------------------------------- #

    def answer_confirmation(self, confirmation_id: str, approved: bool) -> Optional[JarvisState]:
        """Отвечает на ожидающее подтверждение HIGH-risk операции.

        Args:
            confirmation_id: id из состояния/события подтверждения.
            approved: True — выполнить, False — отклонить.

        Returns:
            JarvisState с результатом (или None, если подтверждение не найдено).
        """
        outcome = self._agent.answer_confirmation(confirmation_id, approved)
        if outcome is None:
            return None

        output = assistant_output_from_outcome(outcome)
        text = (output.display_text or "").strip()
        if not text:
            # C4: пустой ответ модели — человеческая фраза без имён провайдеров.
            from core.brain.error_messages import user_message_for
            text = user_message_for("assistant output was empty")
            output = AssistantOutput.failure(
                display_text=text,
                error=ErrorInfo(
                    ErrorCategory.UNKNOWN_FAILURE,
                    technical_message="confirmation output was empty",
                ),
            )
        self._output_callback(text)
        spoken = self._queue_assistant_output(output)

        state: JarvisState = self._new_state("")
        state["response"] = text
        state["tts_text"] = spoken
        state["assistant_output"] = output.to_dict()
        state["tool"] = outcome.tool_used or ""
        state["verified"] = bool(outcome.verified)
        state["mode"] = outcome.mode
        if outcome.needs_confirmation:
            state["confirmation_id"] = getattr(outcome, "confirmation_id", None)
            state["needs_confirmation"] = True
        return state

    # --------------------------------------------------------------------- #
    #  Асинхронные миссии J.A.R.V.I.S. 3.0 (§3, §5, §6, §23)
    # --------------------------------------------------------------------- #

    def submit_goal(self, text: str,
                    on_event: Optional[Callable[[TaskEvent], None]] = None) -> Mission:
        """Принимает цель пользователя и запускает миссию АСИНХРОННО (§6).

        Возвращает управление немедленно — до завершения работы. Пользователь
        получает быстрое подтверждение (§5), а сама задача продолжает жить
        в фоне столько, сколько нужно (§4: 5 секунд, 2 минуты, 10 минут — норма).

        Args:
            text: цель человеческим языком.
            on_event: опциональный подписчик на события ЭТОЙ миссии (§23).

        Returns:
            ``Mission`` со статусом queued/acknowledging и готовым ``task_id``.
        """
        if not self._running:
            self.start()

        goal = (text or "").strip()
        self._proactor.mark_user_activity()

        # §5 — ACK формируется мгновенно. Если доступна локальная модель —
        # обогащается контекстной фразой (П1 §1.2); при сбое — canned fallback.
        decision = self._route_cached(goal)
        intent = semantic_intent_category(decision)
        # ACK must never pay the model-load/inference cost.  The mission
        # worker owns all deliberate reasoning after this line.
        ack = pick_acknowledgement(
            intent, goal=goal, settings=self._settings, allow_model=False,
        )
        # Emergency ACK stays deterministic and immediate, but the user-facing
        # runtime no longer repeats the legacy honorific on every background job.
        ack = re.sub(r",?\s*сэр\b", "", ack, flags=re.IGNORECASE)
        ack = re.sub(r"\s+([.!?])", r"\1", ack).strip()

        # Подписка ставится ДО запуска, но task_id известен только после
        # submit(). Держим его в изменяемой ячейке и добираем уже
        # опубликованные события из mission.events, чтобы ничего не потерять.
        task_holder: Dict[str, Optional[str]] = {"id": None}
        seen: set[int] = set()
        unsubscribe: Optional[Callable[[], None]] = None

        if on_event is not None:
            _TERMINAL_STATUSES = {"completed", "failed", "cancelled", "expired"}

            def _filtered(event: TaskEvent) -> None:
                if task_holder["id"] is None or event.task_id != task_holder["id"]:
                    return
                marker = id(event)
                if marker in seen:
                    return
                seen.add(marker)
                try:
                    on_event(event)
                except Exception as exc:
                    log.debug("Подписчик события упал: %s", exc)
                # ДЫРА 1 FIX: отписка при ЛЮБОМ терминальном состоянии миссии
                # (completed/failed/cancelled/expired). Раньше unsubscribe
                # вызывался только в happy-path _mission_runner; при исключении
                # в рантайме, отмене или истечении миссии подписка оставалась
                # навсегда — утечка при долгой работе.
                etype = str(getattr(event, "event_type", "") or "")
                status = str((getattr(event, "payload", None) or {}).get("status") or "")
                terminal = (
                    etype in (EVENT_TASK_COMPLETED, EVENT_TASK_FAILED)
                    or (etype == EVENT_TASK_PROGRESS and status in _TERMINAL_STATUSES)
                )
                if terminal:
                    unsub = task_holder.get("unsub")
                    if callable(unsub):
                        task_holder["unsub"] = None
                        try:
                            unsub()
                        except Exception as exc:
                            log.debug("Unsubscribe миссии %s: %s", task_holder["id"], exc)

            unsubscribe = self._runtime.subscribe(_filtered)
            task_holder["unsub"] = unsubscribe

        kernel_handle = self._kernel.submit(goal, context={"channel": "text"})
        kernel_record = self._kernel.ledger.load(kernel_handle.id)
        contract_payload = dict(kernel_record.contract) if kernel_record is not None else self._intake.classify(goal).to_dict()
        mission = self._runtime.submit(
            goal=goal,
            runner=self._mission_runner,
            metadata={"intent": intent, "source": "user",
                      "task_contract": contract_payload,
                      "routing_decision": decision.to_dict(),
                      "kernel_mission_id": kernel_handle.id,
                      "kernel_task_id": kernel_handle.task_id},
        )
        mission.acknowledgement = ack

        if on_event is not None:
            task_holder["id"] = mission.task_id
            # Догоняем события, опубликованные во время submit().
            for event in list(mission.events):
                marker = id(event)
                if marker not in seen:
                    seen.add(marker)
                    try:
                        on_event(event)
                    except Exception as exc:
                        log.debug("Подписчик события упал: %s", exc)
            # ДЫРА 1 FIX (2/2): миссия может добежать до терминального статуса
            # раньше, чем submit_goal вернёт управление (быстрый runner, hard
            # cap очереди и т.п.). Тогда терминальное событие ушло в момент,
            # когда task_holder["id"] ещё был None — отписка через _filtered
            # не сработает. Проверяем статус здесь и снимаем подписку сами.
            if mission.status.is_terminal:
                unsub = task_holder.get("unsub")
                if callable(unsub):
                    task_holder["unsub"] = None
                    try:
                        unsub()
                    except Exception as exc:
                        log.debug("Unsubscribe миссии %s: %s", mission.task_id, exc)

        # Немедленное подтверждение пользователю (§5).
        self._output_callback(ack)
        self._queue_assistant_output(AssistantOutput.natural(ack, speech_mode="focused"))

        if unsubscribe is not None:
            mission.metadata["_unsubscribe"] = unsubscribe
        return mission

    def schedule_mission(
        self, goal: str, trigger: MissionTrigger | Mapping[str, Any], *,
        context: Optional[Dict[str, Any]] = None,
        completion_criteria: Optional[Dict[str, Any]] = None,
        expires_at: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Mission:
        """Register a durable mission without creating an execution path beside Agent."""
        if not self._running:
            self.start()
        return self._runtime.schedule(
            goal, trigger, context=context, completion_criteria=completion_criteria,
            expires_at=expires_at, metadata=metadata,
        )

    def resume_mission(self, task_id: str) -> bool:
        return self._runtime.resume(task_id)

    def issue_authority(
        self, proposal: AuthorityProposal, *, user_instruction: str,
        source_id: str, source_role: str = "user",
    ):
        """Install a validated proposal only at the real user-input boundary."""
        return self._authority.issue(
            proposal, source_kind=ProvenanceKind.USER_INSTRUCTION,
            source_role=source_role, source_text=user_instruction, source_id=source_id,
        )

    def revoke_authority(self, grant_id: str, reason: str = "user revoked") -> bool:
        return self._authority.revoke(grant_id, reason)

    def check_authority(self, request: AuthorityRequest):
        return self._authority.check(request)

    def execute_authorized(self, request: AuthorityRequest, callback):
        """Run ``callback`` only while the grant is provably still valid.

        S3: проверка и исполнение под одним замком хранилища. Раздельные
        ``check_authority`` + вызов оставляли окно, в котором отзыв гранта
        уже произошёл, а действие всё ещё выполнялось.
        """
        return self._authority.execute_authorized(request, callback)

    def reschedule_mission(
        self, task_id: str, trigger: MissionTrigger | Mapping[str, Any],
    ) -> bool:
        return self._runtime.reschedule(task_id, trigger)

    def _durable_mission_runner(self, mission: Mission, cancel: threading.Event) -> str:
        if mission.metadata.get("durable_kind") == "reminder":
            text = str(mission.context.get("notification_text") or mission.goal)
            message = f"Напоминание: {text}"
            self._output_callback(message)
            self._queue_assistant_output(AssistantOutput.natural(message, speech_mode="focused"))
            mission.verification = {
                "verified": True,
                "method": "local_notification_dispatch",
                "detail": "notification dispatched to the active local output channel",
                "strict": True,
            }
            return message
        return self._mission_runner(mission, cancel)

    def _mission_runner(self, mission: Mission, cancel: threading.Event) -> str:
        """Исполнитель миссии: агент + память + озвучка результата."""
        kernel_mission_id = str(mission.metadata.get("kernel_mission_id", ""))
        if kernel_mission_id:
            try:
                self._kernel.transition(kernel_mission_id, "running", next_action="execute")
            except Exception as exc:
                log.debug("Kernel mission transition skipped: %s", exc)
        # Контекст диалога — в краткую память до начала работы.
        if self._session is not None:
            self._session.push("user", mission.goal)

        result_text = self._agent.run_mission(mission, cancel, decision=self._decision_from_metadata(mission))

        verification = mission.verification or {}
        verified = bool(verification.get("verified") is True)
        if kernel_mission_id:
            try:
                evidence = EvidenceRecordV2(
                    claim="mission desired state verified" if verified else "mission requires verification",
                    source="task_runtime",
                    confidence=1.0 if verified else 0.25,
                    expected_state={"verified": True},
                    observed_state={"verified": verified, "status": mission.status.value},
                    path="deliberate",
                )
                self._kernel.record_evidence(kernel_mission_id, evidence)
                self._kernel.transition(
                    kernel_mission_id,
                    "verified" if verified else "verification_failed",
                    next_action="" if verified else "repair or research",
                    observed_state={"verified": verified, "status": mission.status.value},
                )
            except Exception as exc:
                log.debug("Kernel mission evidence skipped: %s", exc)
        if not cancel.is_set():
            self._cognitive.record_external_outcome(
                goal=mission.goal, result=result_text, verified=verified,
                pending=[] if verified else ["mission desired state"],
                mission_id=mission.task_id,
            )

        # Память и озвучка — только для завершённых, не отменённых задач.
        if not cancel.is_set():
            self._remember_exchange_background(mission.goal, result_text)
            if self._session is not None:
                self._session.push("assistant", result_text)

            # БАГ 2 FIX: помечаем что _output_callback будет вызван для этой
            # миссии. _on_task_event в ws_server проверяет этот флаг чтобы
            # не создавать дублирующий пузырь из EVENT_TASK_COMPLETED.
            mission.metadata["_output_sent"] = True
            self._output_callback(result_text)
            self._queue_assistant_output(AssistantOutput.natural(result_text))

        # ДЫРА 1 FIX: вызываем unsubscribe при завершении миссии.
        # submit_goal сохраняет callable в metadata["_unsubscribe"] чтобы
        # отписаться от EventBus. Без этого каждая миссия с on_event оставляет
        # висячую подписку → утечка памяти при долгой работе.
        unsub = mission.metadata.pop("_unsubscribe", None)
        if callable(unsub):
            try:
                unsub()
            except Exception as exc:
                log.debug("Unsubscribe миссии %s: %s", mission.task_id, exc)

        return result_text

    def _remember_exchange_background(self, user_text: str, assistant_text: str) -> None:
        """Persist long-term memory off the interactive/voice critical path."""
        if not user_text or not assistant_text:
            return

        def _write() -> None:
            try:
                self._memory.remember_exchange(user_text, assistant_text)
            except Exception as exc:
                log.warning("Не удалось сохранить обмен в память: %s", exc)

        worker = threading.Thread(target=_write, name="jarvis-memory-write", daemon=True)
        worker.start()

    def wait_for(self, task_id: str, timeout: Optional[float] = None) -> Optional[Mission]:
        """Ждёт завершения миссии.

        ВАЖНО (§4): ``timeout`` — это сколько ВЫЗЫВАЮЩИЙ готов ждать, а НЕ
        лимит на саму задачу. ``None`` = ждать сколько угодно.
        """
        return self._runtime.wait(task_id, timeout=timeout)

    def cancel_mission(self, task_id: str) -> bool:
        """Отменяет миссию по ID (§24)."""
        return self._runtime.cancel(task_id)

    def pause_mission(self, task_id: str) -> bool:
        paused = self._runtime.pause(task_id)
        if paused:
            mission = self._runtime.get(task_id)
            if mission is not None:
                self._cognitive.state.current_goal = mission.goal
                self._cognitive.state.active_task = mission.current_step or ""
                self._cognitive.state.active_mission_id = mission.task_id
                self._cognitive.state.mission_state = "paused"
                self._cognitive.suspend_current()
        return paused

    def skip_mission_step(self, task_id: str) -> bool:
        return self._runtime.skip_step(task_id)

    def explain_mission_step(self, task_id: str) -> str:
        return self._runtime.explain_current_step(task_id)

    def get_mission(self, task_id: str) -> Optional[Mission]:
        """Возвращает миссию по ID (для UI/статуса)."""
        return self._runtime.get(task_id)

    def list_missions(self, include_terminal: bool = True) -> List[Mission]:
        """Список миссий (§24 — поддержка нескольких задач одновременно)."""
        return self._runtime.list_missions(include_terminal=include_terminal)

    def subscribe_events(self, callback: Callable[[TaskEvent], None]) -> Callable[[], None]:
        """Подписка на ВСЕ события задач (§23). Возвращает unsubscribe()."""
        return self._runtime.subscribe(callback)

    @property
    def runtime(self) -> TaskRuntime:
        return self._runtime

    @property
    def living(self):
        """Sprint 11 integration facade for local context and proactive policy."""
        return self._living

    @property
    def cognitive(self) -> CognitiveOrchestrator:
        """Sprint 13 typed continuity and factual self-knowledge facade."""
        return self._cognitive

    @property
    def brain(self):
        """Sprint 15 model-orchestration facade owned by Cognitive Core."""
        return self._brain

    @property
    def agent(self) -> Agent:
        return self._agent

    # --------------------------------------------------------------------- #
    #  Внутренние методы
    # --------------------------------------------------------------------- #

    # БАГ 12 FIX: второй (не гейтнутый) путь исполнения инструментов,
    # который жил здесь ранее, удалён целиком — см. тесты/regressions.
    # Единственный путь исполнения инструментов — Agent
    # (assess_risk → route guard → confirmation → verifier), native tool
    # calls парсятся в core/llm/tool_calls.py.

    def _default_output(self, text: str) -> None:
        """Дефолтный вывод: print + toast."""
        print(f"🤖 {text}")
        if self._settings.voice.tts_enabled:
            show_toast("АТЛАС", text[:100])

    def _proactive_output(self, text: str) -> None:
        """Вывод проактивного сообщения."""
        print(f"💡 [Проактивно] {text}")
        if self._settings.voice.tts_enabled:
            self._queue_assistant_output(
                AssistantOutput.natural(text, speech_mode="background")
            )
            show_toast("АТЛАС (проактивно)", text[:100])

    def _queue_assistant_output(self, output: AssistantOutput) -> Optional[str]:
        """Single typed path into TTS; returns the final safe spoken text."""
        rendered = self._speech_renderer.render(output)
        if rendered is None:
            return None
        if self._settings.voice.tts_enabled and self._tts.is_available():
            self._tts_queue.add_output(output)
        return rendered.text

    def _on_wake_word_detected(self) -> None:
        """Сработало слово-пробуждение: регистрируем и будим слушателя.

        Вызывается из потока wake-word. Безопасно: только логирует и
        помечает состояние — само «что делать дальше» вешает слушатель
        (STT/голосовой цикл). Эмитим WS-событие, если WS-сервер находится
        в этом же процессе (делегируется через callback).
        """
        log.info("Wake word: слово-пробуждение распознано — слушатель активен")
        # Лёгкий способ уведомить фронт/консоль без жёсткой связи:
        # делегируем в существующий механизм событий, если он доступен.
        emit = getattr(self, "_emit_wake_word_event", None)
        if callable(emit):
            try:
                emit()
            except Exception as exc:  # noqa: BLE001
                log.debug("Wake word event emit: %s", exc)

    def _quick_memory_retrieve(self, question: str) -> Optional[str]:
        """Ретривер памяти для Quick-Answer: живой контекст без сети.

        Использует ``self._living.answer_context`` (быстрый структурированный
        ответ из локального контекста) как источник «уже знаю это». Если
        ``_living`` ещё не создан или не ответил — возвращает None (дыра 1).
        """
        living = getattr(self, "_living", None)
        if living is None:
            return None
        try:
            reply = living.answer_context(question)
        except Exception as exc:  # noqa: BLE001
            log.debug("Quick-answer: живой контекст не ответил: %s", exc)
            return None
        if reply and reply.get("answer"):
            return str(reply["answer"])
        return None

    def _direct_cognitive_response(self, user_text: str, response: str, *,
                                   mode: str = "conversation",
                                   verified: bool = False) -> JarvisState:
        """Render a deterministic cognitive answer through the existing voice path."""
        output = AssistantOutput.natural(
            response,
            speech_mode="focused",
            debug={"mode": mode, "verified": verified, "source": "orchestrator"},
        )
        state = self._new_state(user_text)
        if self._session is not None:
            self._session.push("user", user_text)
            self._session.push("assistant", response)
            self._session.to_state(state)
        spoken = self._queue_assistant_output(output)
        self._output_callback(response)
        state["response"] = response
        state["tts_text"] = spoken
        state["assistant_output"] = output.to_dict()
        state["cognitive_state"] = self._cognitive.state.to_safe_dict()
        state["mode"] = mode
        state["verified"] = verified
        return state

    def provider_effective(self) -> Dict[str, Any]:
        """C3: правда о провайдере — источник ключа, живость, состояние цепи.

        Проблема D1 жила месяц именно потому, что «работает ли облако» не
        было видно снаружи. Это поле видно всегда: в runtime_diagnostics,
        в WS runtime_status и в каждом отчёте прогонов.
        """
        provider = str(getattr(self._settings, "deepseek_provider", "deepinfra") or "deepinfra")
        model = str(getattr(self._settings, "deepseek_model", "") or "")
        source = "missing"
        try:
            source = self._settings.api_key_source(provider)
        except Exception as exc:
            log.debug("api_key_source не удался: %s", exc)
        reachable = None
        last_probe = None
        brain = getattr(self, "_brain", None)
        if brain is not None:
            last_probe = (getattr(brain, "last_probe_at", {}) or {}).get(provider)
            if model:
                snapshot = brain.health.snapshot(f"{provider}:{model}")
                reachable = str(getattr(snapshot.status, "value", snapshot.status)) == "available"
            circuit = brain.circuit_state(provider, model)
        else:
            circuit = "closed"
        return {
            "tier": "FAST",
            "source": source,
            "reachable": reachable,
            "last_probe": last_probe,
            "circuit": circuit,
        }

    def runtime_diagnostics(self) -> Dict[str, Any]:
        """Startup/model diagnostics for the Wave 0 verification report."""
        # S4: счётчик утёкших исполнений (watchdog отдал управление, побочные
        # эффекты не остановлены). Импорт локальный: core.orchestrator тянет
        # core.actions уже на модульном уровне, а тут нужен именно модуль
        # executor с его состоянием, а не реэкспорт.
        from core.actions.executor import leaked_execution_stats
        return {
            "warmup": dict(self._warmup_diagnostics),
            "warmup_ready": self._warmup_ready.is_set(),
            "router": dict(getattr(self, "_router_diagnostics", {"ready": False})),
            "provider": self.provider_status(),
            "provider_effective": self.provider_effective(),
            "kernel": {"ledger": str(self._kernel.root / "missions.db"),
                       "capabilities": len(self._kernel.capabilities.snapshot())},
            "tools": leaked_execution_stats(),
            "budgets": {
                "fast": {"p50_ms": 600.0, "p95_ms": 1000.0, "hard_max_ms": 1500.0},
                "deliberate": {"first_progress_p95_ms": 2500.0, "p50_ms": 8000.0, "p95_ms": 15000.0},
                "research": {"first_progress_p95_ms": 3000.0, "source_timeout_ms": 8000.0},
                "background": {"enqueue_p95_ms": 100.0},
            },
        }

    def wait_for_runtime_ready(self, timeout: float | None = None) -> str:
        """Wait for the one-time local model warmup without blocking the WS loop.

        The UI socket is intentionally opened before the model is ready.  A
        command arriving during that small window must wait for the same
        readiness event instead of falling through to the misleading
        ``сейчас не отвечает`` response.  Callers run this method from their
        worker thread, never from the asyncio event loop.
        """
        if (not bool(getattr(self._settings, "warmup_local_on_start", False))
                and not bool(getattr(self._settings, "deepseek_brain_mode", False))):
            return str(self._warmup_diagnostics.get("state", "starting"))
        if not self._warmup_ready.is_set():
            configured = timeout
            if configured is None:
                configured = float(
                    getattr(self._settings, "server_start_timeout_sec", 90.0) or 90.0
                )
            self._warmup_ready.wait(timeout=max(0.0, float(configured)))
        return str(self._warmup_diagnostics.get("state", "starting"))

    @property
    def kernel(self) -> CognitiveKernel:
        """Canonical mission/evidence authority for integrations and tests."""
        return self._kernel

    @property
    def intake(self) -> UniversalIntake:
        return self._intake

    @property
    def tutor(self) -> TutorEngine:
        return self._tutor

    def teach(self, topic: str, *, level: str = "adaptive", mode: str = "socratic",
              session: Any = None) -> Dict[str, Any]:
        """Expose Tutor Mode without forcing a model call on the fast path."""
        result = self._tutor.teach(topic, level=level, mode=mode, session=session)
        return result.to_dict()

    def _check_reminders(self) -> bool:
        """Проверка сработавших напоминаний для proactor.

        Returns:
            True если есть сработавшие (TaskManager callback уже вывел текст).
        """
        # TaskManager использует callbacks, поэтому просто проверяем список
        reminders = self._task_manager.list_reminders()
        now = time.time()
        for r in reminders:
            if r["remaining_sec"] <= 0:
                return True
        return False

    def _nightly_consolidation(self) -> None:
        """Bounded local sleep-time consolidation; no external actions."""
        log.info("Ночная консолидация: запуск...")
        try:
            report = self._agent.executive.sleep()
            log.info("Ночная консолидация завершена: %s", report)
        except Exception as exc:
            log.warning("Ночная консолидация пропущена: %s", exc)

    # --------------------------------------------------------------------- #
    #  Свойства для доступа к компонентам
    # --------------------------------------------------------------------- #

    @property
    def council(self) -> CouncilRouter:
        return self._council

    @property
    def memory(self) -> MemoryRetriever:
        return self._memory

    @property
    def session(self):
        return self._session

    @property
    def tts_queue(self) -> TTSQueue:
        return self._tts_queue

    @property
    def proactor(self) -> Proactor:
        return self._proactor
