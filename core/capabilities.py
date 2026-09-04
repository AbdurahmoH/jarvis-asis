"""Capability Registry — единый паспорт возможностей J.A.R.V.I.S. (§12).

Существующий ``ToolRegistry`` (core/actions/registry.py) отвечает за то,
КАК вызвать инструмент (name / schema / run). Этот модуль отвечает за то,
КОГДА и СТОИТ ЛИ его вызывать:

    name, description, input_schema, examples, risk_level, permissions,
    speed, cost, internet_required, file_access, success_check,
    fallbacks, tags

Ключевая идея ТЗ (§12): модели НЕЛЬЗЯ отдавать все инструменты сразу.

    USER GOAL -> TOOL RETRIEVAL -> RELEVANT TOOLS ONLY -> MODEL -> STRUCTURED CALL

Модуль НЕ дублирует схемы инструментов: схема аргументов ВСЕГДА берётся из
``ToolRegistry`` (см. ``describe_tools_for_model``). Паспорт возможности
автоматически порождается при регистрации ``Tool`` (единый источник truth,
Q02): ``CapabilityRegistry`` покрывает ВСЕ инструменты реестра, а ручной
``_CAP_ANNOTATIONS`` несёт только качественные аннотации (теги/примеры/
риск/фолбэки), не дублируя описание и схему.

Retrieval (§12) — гибридный (Q01): офлайновый keyword-скоринг ПЛЮС
embedding-скоринг поверх ЭМБЕДДЕРА проекта (all-MiniLM-L6-v2, ChromaDB).
Embedding ловит синонимы, которые keyword не видит («поставь будильник» ->
``add_reminder``). При недоступности эмбеддера — тихий фолбэк на keyword.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

from core.actions.registry import DEFAULT_REGISTRY, ToolRegistry
from core.utils.logger import get_logger
from core.verifier import has_strict_verifier

__all__ = [
    "RiskLevel",
    "Speed",
    "Capability",
    "CapabilityRegistry",
    "CAPABILITIES",
    "retrieve_tools",
    "describe_tools_for_model",
]

log = get_logger(__name__)


# Веса гибридного скоринга (Q01). Embedding лучше ловит синонимы/длинные цели.
_KEYWORD_WEIGHT = 0.4
_EMBED_WEIGHT = 0.6
# Минимальная косинусная близость embedding, при которой инструмент
# квалифицируется как кандидат, если нет прямого keyword-попадания.
_EMBED_MIN_SIM = 0.20
# Нормировка keyword-скора в 0..1 (примерно: 2 совпавших тега ~ max).
_KEYWORD_NORM = 6.0


class RiskLevel(str, Enum):
    """Уровни риска (§21). HIGH требует подтверждения пользователя."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def requires_confirmation(self) -> bool:
        return self in (RiskLevel.HIGH, RiskLevel.CRITICAL)


class Speed(str, Enum):
    """Ожидаемая скорость инструмента (ориентир, НЕ лимит §4)."""

    INSTANT = "instant"   # < 1s, локально
    FAST = "fast"         # секунды
    SLOW = "slow"         # сеть / тяжёлые операции


@dataclass
class Capability:
    """Паспорт одной возможности (§12)."""

    name: str
    description: str = ""
    examples: List[str] = field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.MEDIUM
    permissions: List[str] = field(default_factory=list)
    requirements: List[str] = field(default_factory=list)
    required_any: List[str] = field(default_factory=list)
    speed: Speed = Speed.FAST
    cost: str = "free"                 # free | cheap | paid
    internet_required: bool = False
    file_access: str = "none"          # none | read | write
    success_check: str = ""            # как проверяется фактически (§14)
    fallbacks: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    model_visible: bool = True          # доступно ли generic user-goal discovery
    evidence_scope: str = "system"      # system | internal | user_visible
    examples_ru: List[str] = field(default_factory=list)
    counter_examples_ru: List[str] = field(default_factory=list)

    @classmethod
    def from_tool(cls, tool: Any, **overrides: Any) -> "Capability":
        """Авто-паспорт из ``Tool`` (единый источник truth, Q02).

        Базовые поля (name/description) берутся из самого инструмента, чтобы
        capabilities.py НЕ дублировал их вручную. Ручные аннотации
        (теги/примеры/риск) передаются через ``overrides`` и применяются
        поверх базы.
        """
        base = cls(
            name=getattr(tool, "name", "") or "",
            description=getattr(tool, "description", "") or "",
            risk_level=RiskLevel.MEDIUM,
            success_check="специализированной проверки нет — доверяем ok",
        )
        for key, value in overrides.items():
            if hasattr(base, key):
                setattr(base, key, value)
        return base

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "examples": list(self.examples),
            "risk_level": self.risk_level.value,
            "permissions": list(self.permissions),
            "requirements": list(self.requirements),
            "required_any": list(self.required_any),
            "speed": self.speed.value,
            "cost": self.cost,
            "internet_required": self.internet_required,
            "file_access": self.file_access,
            "success_check": self.success_check,
            "fallbacks": list(self.fallbacks),
            "tags": list(self.tags),
            "model_visible": self.model_visible,
            "evidence_scope": self.evidence_scope,
            "strict_verifier": has_strict_verifier(self.name),
        }


# --------------------------------------------------------------------------- #
#  Ручные качественные аннотации (Q02). НЕ дублируют схему/описание инструмента
#  — только то, что улучшает retrieval/риск-гейт и не выводится из Tool:
#  теги, примеры, уровень риска, фолбэки, проверка успеха, скорость.
#  Инструменты, не перечисленные здесь, получают авто-паспорт из Tool.
# --------------------------------------------------------------------------- #

_CAP_ANNOTATIONS: Dict[str, Dict[str, Any]] = {
    "open_app": dict(
        description="Открывает установленное Windows-приложение по разговорному имени.",
        examples=["открой телеграм", "запусти блокнот", "открой браузер"],
        risk_level=RiskLevel.LOW,
        speed=Speed.FAST,
        success_check="процесс приложения реально присутствует в системе (psutil)",
        fallbacks=[],
        tags=["app", "launch", "открой", "запусти", "telegram", "браузер", "программа"],
    ),
    "close_app": dict(
        description="Закрывает запущенное Windows-приложение по имени.",
        examples=["закрой блокнот", "закрой хром"],
        risk_level=RiskLevel.MEDIUM,
        permissions=["process_terminate"],
        speed=Speed.FAST,
        success_check="процесс реально исчез из списка запущенных",
        tags=["app", "close", "закрой", "заверши", "программа"],
    ),
    "volume": dict(
        description="Управляет громкостью системы: громче, тише, mute.",
        examples=["сделай тише", "громче", "выключи звук"],
        risk_level=RiskLevel.LOW,
        speed=Speed.INSTANT,
        success_check="ok от системного API громкости",
        tags=["system", "звук", "громкость", "volume", "mute"],
    ),
    "system_status": dict(
        description="Состояние системы: CPU, RAM, диск, батарея.",
        examples=["статус системы", "сколько занято памяти", "загрузка процессора"],
        risk_level=RiskLevel.LOW,
        speed=Speed.FAST,
        success_check="в ответе реально присутствуют метрики CPU/RAM/диск",
        tags=["system", "статус", "cpu", "память", "ram", "диск", "батарея"],
    ),
    "current_time": dict(
        description="Показывает локальные часы, дату и часовой пояс без сети.",
        examples=["который час", "сколько времени", "какая дата"],
        risk_level=RiskLevel.LOW,
        speed=Speed.INSTANT,
        success_check="ответ содержит локальное время и дату",
        tags=["system", "время", "час", "дата", "clock", "time"],
    ),
    "play_music": dict(
        description=(
            "Запускает указанную музыку через локальный путь/URI или выбранный "
            "источник. Поиск исполнителя/трека требует query, source=spotify "
            "либо youtube и allow_network=true."
        ),
        examples=["поставь музыку", "включи трек", "открой песню"],
        risk_level=RiskLevel.LOW,
        speed=Speed.FAST,
        success_check="медиаточка открыта launcher-ом",
        requirements=["конкретный трек, исполнитель, настроение или URI/путь"],
        required_any=["query", "mood", "uri", "path"],
        tags=["media", "музыка", "трек", "песня", "spotify", "youtube", "плеер"],
    ),
    "web_search": dict(
        description="Ищет информацию в интернете через DuckDuckGo.",
        examples=["найди информацию о X", "поищи документацию по Y"],
        risk_level=RiskLevel.LOW,
        speed=Speed.SLOW,
        internet_required=True,
        success_check="получен непустой список результатов",
        fallbacks=["web_fetch"],
        tags=["web", "поиск", "найди", "search", "интернет", "информация", "документация"],
    ),
    "public_data": dict(
        description="Получает свежие курсы валют и новости из публичных источников.",
        examples=["курс доллара сегодня", "последние новости технологий"],
        risk_level=RiskLevel.LOW,
        speed=Speed.SLOW,
        internet_required=True,
        success_check="ответ содержит свежие структурированные данные источника",
        tags=["web", "fresh", "currency", "news", "курс", "валюта", "новости"],
    ),
    "web_fetch": dict(
        description="Скачивает веб-страницу по URL и извлекает основной текст.",
        examples=["прочитай страницу https://...", "что написано на сайте X"],
        risk_level=RiskLevel.LOW,
        speed=Speed.SLOW,
        internet_required=True,
        success_check="страница загружена и содержит осмысленный текст (>50 символов)",
        fallbacks=["web_search"],
        tags=["web", "url", "страница", "сайт", "fetch", "прочитай", "документация"],
    ),
    "list_files": dict(
        description="Список файлов в каталоге внутри documents_dir.",
        examples=["покажи файлы", "что лежит в папке отчёты"],
        risk_level=RiskLevel.LOW,
        speed=Speed.INSTANT,
        file_access="read",
        success_check="возвращён непустой листинг",
        tags=["file", "файлы", "папка", "каталог", "список"],
    ),
    "read_file": dict(
        description="Читает текстовые, PDF и Office-документы из documents_dir или Downloads.",
        examples=["прочитай файл notes.txt", "расскажи о последнем PDF в загрузках"],
        risk_level=RiskLevel.LOW,
        speed=Speed.INSTANT,
        file_access="read",
        success_check="контент реально прочитан (непустой)",
        fallbacks=["search_files"],
        tags=["file", "прочитай", "файл", "документ", "текст"],
    ),
    "write_file": dict(
        description="Создаёт или перезаписывает текстовый файл в documents_dir.",
        examples=["создай файл заметки.txt", "запиши это в файл"],
        risk_level=RiskLevel.MEDIUM,
        permissions=["file_write"],
        speed=Speed.INSTANT,
        file_access="write",
        success_check="файл реально существует на диске и читается",
        tags=["file", "создай", "запиши", "сохрани", "файл", "документ"],
    ),
    "search_files": dict(
        description="Ищет файлы по имени/содержимому в documents_dir или Downloads и умеет выбрать последний.",
        examples=["найди файл отчёт", "найди последний PDF в загрузках"],
        risk_level=RiskLevel.LOW,
        speed=Speed.FAST,
        file_access="read",
        success_check="реально найдены совпадения (не 'ничего не найдено')",
        fallbacks=[],
        tags=["file", "найди", "поиск", "файл", "где", "документ"],
    ),
    "add_reminder": dict(
        description="Создаёт напоминание через N минут.",
        examples=["напомни через 10 минут про звонок", "поставь будильник на 7 утра"],
        risk_level=RiskLevel.LOW,
        speed=Speed.INSTANT,
        success_check="напоминание реально присутствует в списке активных",
        tags=["reminder", "напомни", "напоминание", "таймер", "будильник", "alarm"],
    ),
    "list_reminders": dict(
        description="Список активных напоминаний.",
        examples=["какие у меня напоминания"],
        risk_level=RiskLevel.LOW,
        speed=Speed.INSTANT,
        success_check="возвращён список",
        tags=["reminder", "напоминания", "список"],
    ),
    "cancel_reminder": dict(
        description="Отменяет напоминание по ID.",
        examples=["отмени напоминание 2"],
        risk_level=RiskLevel.MEDIUM,
        speed=Speed.INSTANT,
        success_check="напоминание исчезло из списка",
        tags=["reminder", "отмени", "напоминание"],
    ),
    "weather": dict(
        description="Текущая погода и прогноз (open-meteo).",
        examples=["какая погода", "погода на завтра"],
        risk_level=RiskLevel.LOW,
        speed=Speed.SLOW,
        internet_required=True,
        success_check="получен непустой прогноз",
        tags=["weather", "погода", "температура", "прогноз"],
    ),
    # ------------------------------------------------------------------ #
    #  Инструменты полного контроля над ПК (§21): мышь/клавиатура и
    #  браузерная автоматизация могут выполнить ЛЮБОЕ действие от имени
    #  пользователя (включая деструктивное через UI), поэтому всегда HIGH
    #  и проходят единый Risk Gate. Без паспорта они падали бы в дефолтный
    #  MEDIUM и работали без подтверждения — это была дыра B2.
    # ------------------------------------------------------------------ #
    "computer_mouse": dict(
        description="Управляет мышью: клики, перемещение, скролл по экрану.",
        examples=["кликни по кнопке", "нажми на ссылку"],
        risk_level=RiskLevel.HIGH,
        permissions=["ui_control"],
        speed=Speed.FAST,
        success_check="состояние экрана после клика соответствует цели",
        tags=["computer", "mouse", "клик", "курсор", "ui", "экран"],
    ),
    "computer_keyboard": dict(
        description="Вводит текст и нажимает клавиши в активном окне.",
        examples=["напечатай привет", "нажми enter"],
        risk_level=RiskLevel.HIGH,
        permissions=["ui_control"],
        speed=Speed.FAST,
        success_check="текст реально появился в целевом окне",
        tags=["computer", "keyboard", "набери", "введи", "клавиши", "ui"],
    ),
    "computer_screenshot": dict(
        description="Делает скриншот экрана для анализа состояния.",
        examples=["покажи что на экране", "сделай скриншот"],
        risk_level=RiskLevel.MEDIUM,
        permissions=["screen_read"],
        speed=Speed.FAST,
        success_check="скриншот получен и непустой",
        tags=["computer", "screenshot", "экран", "скриншот", "vision"],
    ),
    "browser_automation": dict(
        description="Автоматизирует браузер: навигация, клики, заполнение форм (Playwright).",
        examples=["открой сайт и заполни форму", "залогинься на сайте"],
        risk_level=RiskLevel.HIGH,
        permissions=["browser_control"],
        speed=Speed.SLOW,
        internet_required=True,
        success_check="конечная страница соответствует цели (URL/контент)",
        fallbacks=["web_fetch"],
        tags=["browser", "automation", "сайт", "форма", "playwright", "клик"],
        model_visible=False,
        evidence_scope="internal",
    ),
    "browser_bridge": dict(
        description=(
            "Управляет видимым production-браузером через DOM: открывает страницы, "
            "находит элементы, кликает, вводит текст, нажимает клавиши, читает и проверяет состояние после действия."
        ),
        examples=["открой сайт", "найди поле и введи текст", "нажми ссылку на странице"],
        risk_level=RiskLevel.HIGH,
        permissions=["browser_control"],
        speed=Speed.SLOW,
        internet_required=True,
        success_check="конечный URL/DOM или post-action state фактически наблюдён",
        fallbacks=["computer_mouse", "computer_keyboard"],
        tags=["browser", "automation", "dom", "visible", "web", "click", "type"],
        model_visible=True,
        evidence_scope="user_visible",
    ),
}


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Косинусное сходство двух векторов."""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class CapabilityRegistry:
    """Реестр паспортов возможностей + retrieval релевантных инструментов (§12)."""

    def __init__(self, tool_registry: Optional[ToolRegistry] = None) -> None:
        self._tools = tool_registry or DEFAULT_REGISTRY
        self._caps: Dict[str, Capability] = {}
        self._embedder = None
        self._embed_cache: Dict[str, List[float]] = {}
        self._embed_ready = False

        # 1) Ручные качественные аннотации (теги/примеры/риск) — базируются на
        #    реальных Tool, но обогащают retrieval/risk-гейт.
        for name, ann in _CAP_ANNOTATIONS.items():
            tool = self._tools.get(name)
            if tool is not None:
                self._caps[name] = Capability.from_tool(tool, **ann)
            else:
                # Аннотация без инструмента — консервативный паспорт.
                self._caps[name] = Capability(name=name, **ann)

        # 2) Единый источник truth (Q02): любой Tool без ручной аннотации
        #    автоматически получает паспорт из самого инструмента.
        for tool in self._tools.list_tools():
            if tool.name not in self._caps:
                self._caps[tool.name] = Capability.from_tool(tool)

        # 3) Обогащение разговорными примерами и контр-примерами (Шаг 1)
        from core.routing.capability_examples_ru import (
            CAPABILITY_COUNTER_EXAMPLES_RU,
            CAPABILITY_EXAMPLES_RU,
        )
        for name, cap in self._caps.items():
            if name in CAPABILITY_EXAMPLES_RU and not cap.examples_ru:
                cap.examples_ru = list(CAPABILITY_EXAMPLES_RU[name])
            if name in CAPABILITY_COUNTER_EXAMPLES_RU and not cap.counter_examples_ru:
                cap.counter_examples_ru = list(CAPABILITY_COUNTER_EXAMPLES_RU[name])

    # ------------------------------------------------------------------ #
    #  Доступ
    # ------------------------------------------------------------------ #
    def get(self, name: str) -> Optional[Capability]:
        """Паспорт инструмента или None, если инструмент вообще неизвестен."""
        return self._caps.get(name)

    def register(self, cap: Capability) -> None:
        """Регистрирует/обновляет паспорт (используется Skill Forge для новых умений)."""
        self._caps[cap.name] = cap
        log.debug("Capability зарегистрирована: %s", cap.name)

    def all(self, only_available: bool = True) -> List[Capability]:
        """Все паспорта. ``only_available`` — только те, чей инструмент реально в реестре."""
        out: List[Capability] = []
        for name, cap in self._caps.items():
            if only_available and name not in self._tools:
                continue
            out.append(cap)
        return out

    def risk_of(self, name: str) -> RiskLevel:
        cap = self.get(name)
        return cap.risk_level if cap else RiskLevel.MEDIUM

    def fallbacks_map(self) -> Dict[str, List[str]]:
        """Карта tool -> fallbacks для ``RepairLoop`` (§11)."""
        return {c.name: list(c.fallbacks) for c in self.all() if c.fallbacks}

    def resolve(self, names: Sequence[str], *, include_internal: bool = False) -> List[Capability]:
        """Resolve model-selected capability ids against the live registry.

        IDs are accepted only when their underlying Tool is currently
        registered.  This keeps discovery declarative: the model can inspect
        the whole surface, while only real, live capabilities can become
        executable schemas.
        """
        selected: List[Capability] = []
        seen: set[str] = set()
        for raw_name in names or ():
            name = str(raw_name or "").strip()
            if not name or name in seen or name not in self._tools:
                continue
            cap = self._caps.get(name)
            if cap is not None and (include_internal or cap.model_visible):
                selected.append(cap)
                seen.add(name)
        return selected

    def discover(self, goal: str, selected_ids: Sequence[str] = (), *,
                 top_k: int = 8,
                 exclude_ids: Sequence[str] = ()) -> List[Capability]:
        """Return a bounded, live schema set after a discovery decision.

        ``selected_ids`` comes from the reasoning model after it receives the
        compact surface catalogue.  Retrieval remains a recall backstop, not
        a whitelist: it may add relevant real tools but never removes a
        model-selected capability.
        """
        excluded = {str(name).strip() for name in exclude_ids if str(name).strip()}
        selected = [cap for cap in self.resolve(selected_ids) if cap.name not in excluded]
        seen = {cap.name for cap in selected}
        for cap in self.retrieve(goal, top_k=max(1, int(top_k)),
                                 use_embedding=True):
            if cap.name not in seen and cap.name not in excluded:
                selected.append(cap)
                seen.add(cap.name)
            if len(selected) >= top_k:
                break
        return selected[:top_k]

    def surface_summary(self) -> str:
        """Compact complete capability catalogue for model-led discovery.

        Schemas deliberately stay out of this turn.  They are loaded only
        after the model selects a few IDs via :meth:`discover`.
        """
        category_tags = (
            ("computer", {"computer", "mouse", "keyboard", "screenshot", "vision", "screen"}),
            ("applications", {"app", "launch", "close", "program"}),
            ("browser", {"browser", "automation", "playwright", "dom"}),
            ("web", {"web", "search", "url", "weather", "research"}),
            ("filesystem", {"file", "files", "document", "folder"}),
            ("system", {"system", "time", "volume", "status"}),
            ("media", {"media", "music", "track", "player"}),
            ("reminders", {"reminder", "alarm", "timer"}),
        )
        groups: Dict[str, List[Capability]] = {name: [] for name, _ in category_tags}
        groups["other"] = []
        for cap in self.all():
            if not cap.model_visible:
                continue
            tags = {str(tag).casefold() for tag in cap.tags}
            category = next(
                (name for name, markers in category_tags if tags & markers),
                "other",
            )
            groups[category].append(cap)
        lines: List[str] = []
        for category, caps in groups.items():
            if not caps:
                continue
            items = "; ".join(
                f"{cap.name}: {cap.description[:140]} [risk={cap.risk_level.value}]"
                for cap in sorted(caps, key=lambda item: item.name)
            )
            lines.append(f"{category}: {items}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    #  Embedding-слой (Q01) — ленивый, с тихим фолбэком
    # ------------------------------------------------------------------ #
    def _ensure_embedder(self) -> None:
        """Лениво поднимает эмбеддер проекта и прогревает кэш паспортов."""
        if self._embed_ready:
            return
        try:
            from core.memory.embedder import Embedder
            self._embedder = Embedder()
            for cap in self.all(only_available=False):
                self._embed_cache[cap.name] = self._embedder.embed_one(
                    self._cap_text(cap)
                )
            log.debug("CapabilityRegistry: embedding-слой готов (%d паспортов)",
                      len(self._embed_cache))
        except Exception as exc:  # chromadb/модель недоступны — фолбэк на keyword
            log.debug("Embedder недоступен, retrieval фолбэк на keyword: %s", exc)
            self._embedder = None
        finally:
            self._embed_ready = True

    @staticmethod
    def _cap_text(cap: Capability) -> str:
        """Текст паспорта для эмбеддинга: описание + примеры + теги."""
        return " ".join(
            [cap.description or "", " ".join(cap.examples), " ".join(cap.tags)]
        ).strip()

    def _cap_embedding(self, cap: Capability) -> Optional[List[float]]:
        if self._embedder is None:
            return None
        if cap.name in self._embed_cache:
            return self._embed_cache[cap.name]
        try:
            vec = self._embedder.embed_one(self._cap_text(cap))
        except Exception as exc:
            log.debug("Эмбеддинг паспорта %s не удался: %s", cap.name, exc)
            return None
        self._embed_cache[cap.name] = vec
        return vec

    def _embedding_scores(self, goal: str) -> Dict[str, float]:
        """Косинусная близость цели к каждому паспорту (0, если эмбеддер недоступен)."""
        self._ensure_embedder()
        if self._embedder is None:
            return {}
        try:
            goal_vec = self._embedder.embed_one(goal)
        except Exception as exc:
            log.debug("Эмбеддинг цели не удался: %s", exc)
            return {}
        out: Dict[str, float] = {}
        for cap in self.all(only_available=False):
            vec = self._cap_embedding(cap)
            if vec is None:
                continue
            out[cap.name] = _cosine(goal_vec, vec)
        return out

    # ------------------------------------------------------------------ #
    #  TOOL RETRIEVAL (§12) — гибридный: keyword + embedding (Q01)
    # ------------------------------------------------------------------ #
    def _keyword_score(self, cap: Capability, words: set) -> float:
        """Офлайновый детерминированный скоринг по тегам/имени/описанию/примерам."""
        score = 0.0
        for tag in cap.tags:
            tag_l = tag.lower()
            for w in words:
                if w == tag_l:
                    score += 3.0
                elif len(w) > 3 and (w.startswith(tag_l[:4]) or tag_l.startswith(w[:4])):
                    score += 1.5
        name_parts = set(cap.name.lower().split("_"))
        score += 2.0 * len(name_parts & words)
        desc_l = cap.description.lower()
        score += 0.5 * sum(1 for w in words if len(w) > 3 and w in desc_l)
        for ex in cap.examples:
            ex_l = ex.lower()
            score += 1.0 * sum(1 for w in words if len(w) > 3 and w in ex_l)
        return score

    def retrieve(self, goal: str, top_k: int = 5,
                 allow_internet: bool = True,
                 use_embedding: bool = True,
                 confidence_threshold: float = 0.0) -> List[Capability]:
        """Возвращает наиболее релевантные цели инструменты (гибридный скоринг).

        Гибридный скоринг (Q01): ``final = 0.4*keyword_norm + 0.6*embedding``.
        Инструмент квалифицируется, если есть прямое keyword-попадание ИЛИ
        embedding-близость выше ``_EMBED_MIN_SIM`` (ловит синонимы). При
        недоступности эмбеддера — тихий фолбэк на чистый keyword (поведение
        как раньше). ``confidence_threshold``>0 отсекает слабые совпадения.

        Args:
            goal: цель пользователя человеческим языком.
            top_k: сколько инструментов вернуть.
            allow_internet: если False — сетевые инструменты исключаются.
            use_embedding: если False — только keyword-скоринг.
            confidence_threshold: минимальный ``final``-скор для включения.

        Returns:
            Список ``Capability``, отсортированный по релевантности.
        """
        words = _tokenize(goal)
        if not words:
            return []

        caps = [
            c for c in self.all()
            if c.model_visible and not (c.internet_required and not allow_internet)
        ]
        if not caps:
            return []

        emb_scores = self._embedding_scores(goal) if use_embedding else {}

        scored: List[tuple[float, Capability]] = []
        for cap in caps:
            kw = self._keyword_score(cap, words)
            emb = emb_scores.get(cap.name, 0.0)
            kw_norm = min(1.0, kw / _KEYWORD_NORM)
            final = _KEYWORD_WEIGHT * kw_norm + _EMBED_WEIGHT * emb
            if final <= 0:
                continue
            # Квалификация: keyword-попадание ИЛИ заметная embedding-близость.
            if kw <= 0 and emb < _EMBED_MIN_SIM:
                continue
            if confidence_threshold and final < confidence_threshold:
                continue
            scored.append((final, cap))

        scored.sort(key=lambda x: (-x[0], x[1].name))
        return [c for _, c in scored[:top_k]]


def _tokenize(text: str) -> set[str]:
    """Слова запроса в нижнем регистре (с урезанием русских окончаний)."""
    raw = re.findall(r"[\w]+", (text or "").lower(), flags=re.UNICODE)
    words: set[str] = set()
    for w in raw:
        if len(w) < 2:
            continue
        words.add(w)
        # грубая нормализация русских словоформ: "телеграм"/"телеграмм"
        if len(w) > 5:
            words.add(w[:-1])
            words.add(w[:-2])
    return words


#: Глобальный реестр возможностей.
CAPABILITIES = CapabilityRegistry()


def retrieve_tools(goal: str, top_k: int = 5,
                   allow_internet: bool = True) -> List[Capability]:
    """Удобная обёртка над ``CAPABILITIES.retrieve`` (§12)."""
    return CAPABILITIES.retrieve(goal, top_k=top_k, allow_internet=allow_internet)


def describe_tools_for_model(caps: Sequence[Capability],
                             registry: Optional[ToolRegistry] = None) -> str:
    """Компактное описание инструментов для промпта модели.

    Отдаём ТОЛЬКО отобранные инструменты (§12) — с именем, назначением и
    схемой аргументов (схема ВСЕГДА из ToolRegistry, НЕ дублируется).
    """
    reg = registry or DEFAULT_REGISTRY
    lines: List[str] = []
    for cap in caps:
        tool = reg.get(cap.name)
        schema = getattr(tool, "input_schema", {}) if tool else {}
        props = (schema or {}).get("properties", {}) or {}
        required = (schema or {}).get("required", []) or []
        arg_desc = ", ".join(
            f"{k}{'*' if k in required else ''}:{(v or {}).get('type', 'any')}"
            for k, v in props.items()
        ) or "без аргументов"
        lines.append(f"- {cap.name}({arg_desc}) — {cap.description} [risk={cap.risk_level.value}]")
    return "\n".join(lines)
