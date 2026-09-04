"""Model Router — выбор модели по СЛОЖНОСТИ задачи (§15, §17).

Критерии выбора (§15):
    complexity, reasoning requirement, context size, confidence,
    privacy, speed, cost, availability.

КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО (§4, §15):
    "если ответ дольше N секунд -> плохая модель"
Латентность НЕ является критерием качества и НЕ используется здесь ни как
вход, ни как причина отказа. Долгий ответ — нормальный ответ.

Политика (§17):
    простая задача            -> LOCAL FIRST (Qwen3-4B, FAST)
    сложная / глубокая        -> внешняя модель (analyst / coder / architect)
    внешняя недоступна        -> graceful fallback обратно на локальную
    приватные данные          -> принудительно локально

Модуль работает поверх существующих ``core.llm`` (Tier, factory,
tier_resolver) — не заменяет их, а добавляет осознанное решение «какой
тир нужен этой задаче».
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config.settings import Settings
from core.llm.tiers import ESCALATION_ORDER, Tier
from core.utils.logger import get_logger

__all__ = [
    "TaskComplexity",
    "RoutingDecision",
    "ModelRouter",
    "estimate_complexity",
    "classify_conversation",
]

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
#  Оценка сложности
# --------------------------------------------------------------------------- #

@dataclass
class TaskComplexity:
    """Профиль сложности задачи (§15). Латентность здесь отсутствует намеренно."""

    score: float                       # 0.0 (тривиально) .. 1.0 (максимум)
    reasoning_required: bool = False
    context_tokens: int = 0
    code_related: bool = False
    architectural: bool = False
    private: bool = False
    multi_step: bool = False
    signals: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": round(self.score, 3),
            "reasoning_required": self.reasoning_required,
            "context_tokens": self.context_tokens,
            "code_related": self.code_related,
            "architectural": self.architectural,
            "private": self.private,
            "multi_step": self.multi_step,
            "signals": list(self.signals),
        }


_TRIVIAL_RE = re.compile(
    r"^\s*(привет|здравствуй|хай|hi|hello|спасибо|пока|как дела|ок|окей|да|нет|"
    r"доброе утро|добрый день|добрый вечер|спокойной ночи)\b",
    re.IGNORECASE,
)

_AMBIGUOUS_FOLLOWUP_RE = re.compile(
    r"^\s*(что именно|какой именно|который именно|а какой|а что|и что|ну и|"
    r"а теперь|продолжай|дальше|интересно|почему|зачем|как так)\s*[?!.,…-]*\s*$",
    re.IGNORECASE,
)

_AMBIGUOUS_ACTION_RE = re.compile(
    r"^\s*(открой|закрой|включи|выключи|поставь|напомни|запиши|сохрани|"
    r"удали|отправь|пошли|напиши|сделай|создай|перемести|скопируй)"
    r"(?:\s+(?:это|его|её|ее|там|тут|мне|ему|ей|файл|файла|приложение|"
    r"сообщение|напоминание|что-нибудь|что нибудь|что-то|что то))?\s*[?!.,…-]*\s*$",
    re.IGNORECASE,
)

_SIMPLE_COMMAND_RE = re.compile(
    r"\b(открой|запусти|закрой|включи|выключи|сделай (тише|громче)|громкость|"
    r"напомни|погода|статус|open|launch|close|volume)\b",
    re.IGNORECASE,
)

_REASONING_RE = re.compile(
    r"\b(проанализируй|анализ|сравни|сравнение|объясни|почему|обоснуй|"
    r"исследуй|изучи|разбери|оцени|выбери лучш|плюсы и минусы|стратег|"
    r"analyze|compare|explain|research|evaluate|investigate)\b",
    re.IGNORECASE,
)

_CODE_RE = re.compile(
    r"\b(код|скрипт|функци|класс|рефактор|дебаг|отлад|баг|исключени|traceback|"
    r"python|javascript|typescript|react|sql|api|компилир|тест[ыу]?|"
    r"code|script|function|refactor|debug|bug|compile)\b",
    re.IGNORECASE,
)

_ARCH_RE = re.compile(
    r"\b(архитектур|спроектируй|проектирован|схем[аыу] системы|микросервис|"
    r"масштабир|ревью|review|architecture|design (a |the )?system|migrate)\b",
    re.IGNORECASE,
)

_PRIVATE_RE = re.compile(
    r"\b(пароль|паспорт|карт[аыу] (банк|кредит)|снилс|инн|секрет|приватн|"
    r"личн[ыа]|конфиденциальн|password|secret|private|confidential|ssn)\b",
    re.IGNORECASE,
)

_MULTISTEP_RE = re.compile(
    r"\b(затем|потом|после этого|сначала|шаг \d|этап|и потом|"
    r"then|after that|step \d)\b",
    re.IGNORECASE,
)


def estimate_complexity(goal: str, context_tokens: int = 0,
                        multi_step_hint: bool = False) -> TaskComplexity:
    """Оценивает сложность задачи детерминированно и офлайн (§15).

    Args:
        goal: цель пользователя.
        context_tokens: оценка размера контекста (из ingest, §7).
        multi_step_hint: планировщик уже знает, что шагов несколько.

    Returns:
        ``TaskComplexity``. Время выполнения НЕ учитывается (§4).
    """
    text = (goal or "").strip()
    signals: List[str] = []
    score = 0.15  # база: даже простая команда — не нулевая работа

    if _TRIVIAL_RE.match(text):
        signals.append("тривиальное обращение")
        score = 0.02
    elif _SIMPLE_COMMAND_RE.search(text):
        signals.append("простая детерминированная команда")
        score = 0.1

    reasoning = bool(_REASONING_RE.search(text))
    if reasoning:
        signals.append("требуется рассуждение/анализ")
        score += 0.35

    code = bool(_CODE_RE.search(text))
    if code:
        signals.append("связано с кодом")
        score += 0.25

    arch = bool(_ARCH_RE.search(text))
    if arch:
        signals.append("архитектурная задача")
        score += 0.4

    private = bool(_PRIVATE_RE.search(text))
    if private:
        signals.append("приватные данные — только локально")

    multi = multi_step_hint or bool(_MULTISTEP_RE.search(text))
    if multi:
        signals.append("многошаговая задача")
        score += 0.2

    # Длина запроса как признак объёма работы (НЕ времени).
    length = len(text)
    if length > 2000:
        signals.append(f"очень большой ввод ({length} символов)")
        score += 0.25
    elif length > 500:
        signals.append(f"объёмный ввод ({length} символов)")
        score += 0.1

    if context_tokens > 6000:
        signals.append(f"большой контекст (~{context_tokens} токенов)")
        score += 0.2
    elif context_tokens > 2000:
        score += 0.1

    return TaskComplexity(
        score=max(0.0, min(1.0, score)),
        reasoning_required=reasoning,
        context_tokens=context_tokens,
        code_related=code,
        architectural=arch,
        private=private,
        multi_step=multi,
        signals=signals,
    )


# --------------------------------------------------------------------------- #
#  Sprint 2 — CONVERSATION vs ACTION (офлайн, консервативно)
# --------------------------------------------------------------------------- #

#: Явные глаголы действия. Их наличие = запрос уходит в обычный planner-путь
#: (там свой risk gate и валидация) — гейт разговора НЕ срабатывает.
_ACTION_VERB_RE = re.compile(
    r"\b(открой|открыть|открой|закрой|закрыть|запусти|запустить|включи|выключи|"
    r"найди|найти|поищи|поиск|искать|прочитай|прочитать|запиши|сохрани|удали|"
    r"создай|перенеси|переименуй|напомни|поставь|включи|скачай|загрузи|"
    r"покажи|выведи|посмотри|проверь|запусти|останови|отмени|"
    r"open|close|launch|find|search|read|write|save|delete|create|show)\b",
    re.IGNORECASE,
)

#: Доменные существительные без глагола, которые всё равно означают действие
#: («погода сегодня» = вызвать инструмент погоды). Консервативно -> planner.
_DOMAIN_NOUN_RE = re.compile(
    r"(?:\bпогод|\bпрогноз\w*|\bтемператур[аы]\b(?:\s+(?:на улице|сейчас|за окном))?|"
    r"\bнапоминани\w*|\bзаметк\w*|\bстатус\w*\s+систем|\bсистемн\w*\s+статус|"
    r"\bгромкост\w*|\bяркост\w*|\bбатаре\w*|\bаккумулятор\w*|\bпроцессор\w*|\bcpu\b)",
    re.IGNORECASE,
)

#: Разговорные маркеры: творчество, объяснения, smalltalk, мнение.
_CONVERSATION_HINT_RE = re.compile(
    r"\b(расскажи|расскажи-ка|объясни|объяснение|почему|зачем|отчего|что такое|"
    r"кто так(ой|ая|ое)|кто был|придумай|сочини|напиши (сказку|историю|стих|"
    r"стихотворение|эссе|письмо|поздравление)|анекдот|шутку|сказку|историю|"
    r"как дела|как ты|как жизнь|поговорим|поболтаем|твоё мнение|что думаешь|"
    r"тебе нравится|тебе нравится|расскажи мне|мне скучно|мне грустно|"
    r"спасибо|благодарю|привет|здравствуй|доброе утро|добрый день|добрый вечер)\b",
    re.IGNORECASE,
)


def classify_conversation(goal: str, intent: str) -> tuple:
    """Консервативный офлайн-гейт «разговор vs действие» (Sprint 2).

    Возвращает ``(is_conversation, reason)``. True — ТОЛЬКО когда офлайн-сигналы
    уверенно говорят «это разговор, действий нет»: тогда агент отвечает напрямую,
    БЕЗ списка инструментов и без JSON-плана (слабая fast-модель больше не может
    галлюцинировать вызов инструмента).

    Приоритет безопасности (Sprint 2 STEP 4):
      1. не запускать ненужный tool;
      2. не потерять настоящий action;
      3. не ломать существующие команды.
    Поэтому любое сомнение (явный глагол действия, intent файла/приложения/
      системы/медиа) отправляет запрос в существующий planner-путь.

    Args:
        goal: текст запроса.
        intent: интент из ``resolve_keyword_tool`` (offline).
    """
    text = (goal or "").strip()
    if not text:
        return False, ""

    # A bare follow-up or an action verb without a concrete target is not an
    # authorization to touch the system.  Keep it on the conversational path
    # so the brain can use history and ask one natural clarification.
    if _AMBIGUOUS_FOLLOWUP_RE.match(text):
        return True, "неоднозначное продолжение без действия"
    if _AMBIGUOUS_ACTION_RE.match(text):
        return True, "действие без конкретной цели"

    # Явный глагол действия — точно не разговор (даже если есть «расскажи»:
    # «расскажи и найди» — действие).
    if _ACTION_VERB_RE.search(text):
        return False, ""

    # Доменное существительное без глагола («погода сегодня») — действие
    # над конкретной операционной зоной: консервативно в planner.
    if _DOMAIN_NOUN_RE.search(text):
        return False, ""

    # Smalltalk-обращение («привет», «как дела») — точно разговор.
    if _TRIVIAL_RE.match(text):
        return True, "smalltalk-обращение"

    # Интент, означающий конкретную операционную зону (файл/приложение/
    # система/медиа/браузер) — консервативно в planner.
    if intent not in ("none", "web"):
        return False, ""

    # Разговорный маркер без глаголов действия — разговор.
    if _CONVERSATION_HINT_RE.search(text):
        return True, "вопрос/творчество без действия"

    # Короткая реплика без всяких маркеров («мне скучно») — разговор;
    # длинное неявное — в planner (пусть решает модель с инструментами).
    if intent == "none" and len(text.split()) <= 8:
        return True, "короткая реплика без признаков действия"

    return False, ""


# --------------------------------------------------------------------------- #
#  Решение о маршрутизации
# --------------------------------------------------------------------------- #

@dataclass
class RoutingDecision:
    """Куда отправить задачу (§15, §17)."""

    tier: Tier
    complexity: TaskComplexity
    reason: str = ""
    fallback_chain: List[Tier] = field(default_factory=list)
    forced_local: bool = False
    provider: str = ""
    model: str = ""
    role: str = ""
    reason_code: str = ""
    brain_route: Any = field(default=None, repr=False)
    request_text: str = field(default="", repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier": self.tier.value,
            "reason": self.reason,
            "fallback_chain": [t.value for t in self.fallback_chain],
            "forced_local": self.forced_local,
            "provider": self.provider,
            "model": self.model,
            "role": self.role,
            "reason_code": self.reason_code,
            "complexity": self.complexity.to_dict(),
        }


class ModelRouter:
    """Выбирает тир модели по сложности задачи и доступности (§15, §17)."""

    #: Порог, ниже которого локальная модель обязана справиться сама.
    LOCAL_THRESHOLD = 0.35
    #: Порог, выше которого нужен самый сильный тир.
    ARCHITECT_THRESHOLD = 0.75

    def __init__(self, settings: Settings, brain_fabric: Any = None) -> None:
        self._settings = settings
        self._brain_fabric = brain_fabric

    # ------------------------------------------------------------------ #
    def route(self, goal: str, context_tokens: int = 0,
              multi_step_hint: bool = False,
              local_confidence: Optional[float] = None) -> RoutingDecision:
        """Возвращает тир для задачи.

        Args:
            goal: цель пользователя.
            context_tokens: размер контекста.
            multi_step_hint: известно, что шагов несколько.
            local_confidence: уверенность локальной модели (0..1), если её
                уже спрашивали. Низкая уверенность повышает шанс эскалации.

        Returns:
            ``RoutingDecision`` с основным тиром и цепочкой fallback.
        """
        cx = estimate_complexity(goal, context_tokens, multi_step_hint)

        # 1) Приватность (§15) — жёстко локально, без внешних API.
        if cx.private and not bool(getattr(self._settings, "deepseek_brain_mode", False)):
            return RoutingDecision(
                tier=Tier.FAST,
                complexity=cx,
                reason="приватные данные: обработка только локальной моделью",
                fallback_chain=[],
                forced_local=True,
            )

        # 2) Низкая уверенность локальной модели повышает сложность.
        score = cx.score
        if local_confidence is not None and local_confidence < 0.5:
            score = min(1.0, score + 0.25)
            cx.signals.append(f"низкая уверенность локальной модели ({local_confidence:.2f})")

        if self._brain_fabric is not None:
            semantic = self._semantic_role(cx, score, goal)
            return self._semantic_decision(goal, cx, semantic, context_tokens)

        # 3) Выбор целевого тира по сложности (НЕ по времени §4).
        if score < self.LOCAL_THRESHOLD:
            target = Tier.FAST
            reason = f"простая задача (score={score:.2f}) — local first"
        elif cx.architectural or score >= self.ARCHITECT_THRESHOLD:
            target = Tier.ARCHITECT
            reason = f"архитектурная/очень сложная задача (score={score:.2f})"
        elif cx.code_related:
            target = Tier.CODER
            reason = f"задача по коду (score={score:.2f})"
        else:
            target = Tier.ANALYST
            reason = f"нужен анализ/рассуждение (score={score:.2f})"

        # Sprint 9 P1: offline — это доступность модели, не роль задачи.
        # CODER/ANALYST/ARCHITECT сохраняются, а factory уже выбирает для
        # этой роли лучшую доступную локальную GGUF. Так кодовая задача не
        # попадает в conversational FAST pipeline.
        if bool(getattr(self._settings, "offline_mode", False)):
            return RoutingDecision(
                tier=target,
                complexity=cx,
                reason=f"{reason}; offline model availability",
                fallback_chain=[],
                forced_local=True,
            )

        chain = self._build_chain(target)
        if not chain:
            # Ни один внешний тир не настроен — честная деградация (§17).
            return RoutingDecision(
                tier=Tier.FAST,
                complexity=cx,
                reason=f"{reason}; внешние модели недоступны — graceful fallback на локальную",
                fallback_chain=[],
                forced_local=True,
            )

        return RoutingDecision(
            tier=chain[0],
            complexity=cx,
            reason=reason if chain[0] is target else f"{reason}; тир '{target.value}' недоступен",
            fallback_chain=chain[1:],
        )

    @staticmethod
    def _semantic_role(cx: TaskComplexity, score: float, goal: str):
        from core.brain import BrainRole
        if cx.code_related:
            return BrainRole.CODER
        if cx.architectural:
            return BrainRole.PLANNER
        if re.search(r"(?i)\b(исследуй|research|найди источ|изучи)\w*", goal or ""):
            return BrainRole.RESEARCH
        if cx.reasoning_required or score >= ModelRouter.LOCAL_THRESHOLD:
            return BrainRole.REASONING
        return BrainRole.FAST

    def _semantic_decision(self, goal: str, cx: TaskComplexity, role: Any,
                           context_tokens: int) -> RoutingDecision:
        from core.brain import BrainRequest, NoRouteAvailable, PrivacyClass
        privacy = (
            PrivacyClass.PERSONAL
            if bool(getattr(self._settings, "deepseek_brain_mode", False))
            else (PrivacyClass.LOCAL_ONLY if cx.private else PrivacyClass.PERSONAL)
        )
        try:
            route = self._brain_fabric.select_route(BrainRequest(
                user_request=goal, role=role, privacy=privacy,
                context_tokens=context_tokens,
            ))
        except NoRouteAvailable as exc:
            if bool(getattr(self._settings, "deepseek_brain_mode", False)):
                # Production migration has one brain by design. Preserve the
                # selected provider/model in telemetry, but let execution
                # surface the exact provider failure instead of routing local.
                return RoutingDecision(
                    tier=self._role_to_tier(role), complexity=cx,
                    reason=f"DeepInfra route unavailable: {exc}",
                    fallback_chain=[], forced_local=False,
                    provider=str(getattr(self._settings, "deepseek_provider", "deepinfra")),
                    model=str(getattr(self._settings, "deepseek_model", "")),
                    role=role.value, reason_code="DEEPINFRA_UNAVAILABLE",
                    request_text=goal,
                )
            log.debug("BrainFabric semantic route unavailable, legacy fallback: %s", exc)
            saved = self._brain_fabric
            self._brain_fabric = None
            try:
                return self.route(goal, context_tokens=context_tokens,
                                  multi_step_hint=cx.multi_step)
            finally:
                self._brain_fabric = saved
        tier = self._role_to_tier(role)
        return RoutingDecision(
            tier=tier, complexity=cx,
            reason=f"semantic role {role.value}: {route.reason_code}",
            fallback_chain=[], forced_local=privacy is PrivacyClass.LOCAL_ONLY,
            provider=route.primary.provider, model=route.primary.model,
            role=role.value, reason_code=route.reason_code,
            brain_route=route, request_text=goal,
        )

    @staticmethod
    def _role_to_tier(role: Any) -> Tier:
        from core.brain import BrainRole
        if role is BrainRole.CODER:
            return Tier.CODER
        if role in {BrainRole.PLANNER, BrainRole.CRITIC}:
            return Tier.ARCHITECT
        if role in {BrainRole.REASONING, BrainRole.RESEARCH, BrainRole.VISION}:
            return Tier.ANALYST
        return Tier.FAST

    # ------------------------------------------------------------------ #
    def route_for_planning(self, routing: RoutingDecision) -> RoutingDecision:
        """TIER 2 (Sprint 3): маршрут для генерации JSON-плана.

        Планировщику нужна НАДЁЖНОСТЬ tool calling'а, а не скорость: слабая
        FAST-модель (am/free) регулярно ломает JSON и галлюцинирует имена
        инструментов, сжигая попытки §13. Поэтому если роутер отправил
        задачу на FAST, планирование поднимается до первого доступного
        внешнего тира (обычно ANALYST). Разговор (TIER 1) остаётся на FAST —
        там JSON не нужен. Офлайн-режим (forced_local) не трогаем.
        """
        if self._brain_fabric is not None and routing.brain_route is not None:
            from core.brain import BrainRole
            return self._semantic_decision(
                routing.request_text, routing.complexity, BrainRole.PLANNER,
                routing.complexity.context_tokens,
            )
        if routing.forced_local or routing.tier is not Tier.FAST:
            return routing
        for tier in ESCALATION_ORDER[1:]:
            if not self._is_available(tier):
                continue
            rest = [t for t in ESCALATION_ORDER
                    if t is not tier and t is not Tier.FAST and self._is_available(t)]
            return RoutingDecision(
                tier=tier,
                complexity=routing.complexity,
                reason=(f"{routing.reason}; TIER 2: JSON-план — минимум "
                        f"'{tier.value}' (надёжный tool calling)"),
                fallback_chain=rest + [Tier.FAST],
            )
        # Внешних тиров нет вообще — остаёмся на FAST (честная деградация §17).
        return routing

    # ------------------------------------------------------------------ #
    def _build_chain(self, target: Tier) -> List[Tier]:
        """Цепочка доступных тиров: целевой, затем соседние, затем локальный."""
        candidates: List[Tier] = [target]
        # Сначала пробуем более сильные, затем более слабые внешние.
        idx = ESCALATION_ORDER.index(target)
        candidates += [t for t in ESCALATION_ORDER[idx + 1:]]
        candidates += [t for t in reversed(ESCALATION_ORDER[:idx]) if t is not Tier.FAST]
        candidates.append(Tier.FAST)

        chain: List[Tier] = []
        for tier in candidates:
            if tier in chain:
                continue
            if self._is_available(tier):
                chain.append(tier)
        return chain

    def is_llm_available(self) -> bool:
        """Доступность cloud-провайдера по конфигурации (health-эквивалент).

        Используется semantic-роутером как ctx.llm_available: реальный
        признак, а не флаг режима deepseek_brain_mode.
        """
        try:
            return bool(self._settings.is_tier_available(Tier.FAST))
        except Exception as exc:
            log.debug("Проверка доступности провайдера не удалась: %s", exc)
            return False

    def _is_available(self, tier: Tier) -> bool:
        """Реальная доступность тира по конфигурации (ключ / путь к модели)."""
        try:
            return bool(self._settings.is_tier_available(tier))
        except Exception as exc:
            log.debug("Проверка доступности тира %s не удалась: %s", tier, exc)
            return tier is Tier.FAST
