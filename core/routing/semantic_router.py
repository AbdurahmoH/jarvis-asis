"""Каскадный семантический роутер Jarvis (Tier-0 Shortcut + Tier-0B Semantic k-NN + Tier-1 Fast LLM).

Архитектура:
  1. Exact Shortcut (канонические полнофразовые совпадения без модели).
  2. Semantic k-NN (ONNX multilingual эмбеддинги + softmax-взвешенное голосование + штрафы за контр-примеры).
  3. Grey Zone Decision (при низкой уверенности/марже: FAST LLM или русскоязычный clarify).
  4. Независимый Risk Gate (гарантия нулевого False Negative по High-Risk операциям).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

from config.settings import DEFAULT_EMBEDDING_MODEL, Settings
from core.capabilities import CAPABILITIES
from core.routing.anchors_ru import ANCHORS_BY_KIND
from core.routing.capability_examples_ru import (
    CAPABILITY_COUNTER_EXAMPLES_RU,
    CAPABILITY_EXAMPLES_RU,
)
from core.utils.logger import get_logger
from core.utils.paths import PROJECT_ROOT

log = get_logger(__name__)

# Пути по умолчанию
DEFAULT_MODEL_DIR = PROJECT_ROOT / "data" / "models" / "embeddings" / DEFAULT_EMBEDDING_MODEL
DEFAULT_CACHE_DIR = PROJECT_ROOT / "data" / "cache"
DEFAULT_THRESHOLDS_PATH = PROJECT_ROOT / "core" / "routing" / "thresholds.json"


def normalize_text(text: str) -> str:
    """Нормализация текста запроса: нижний регистр, ё->е, удаление пунктуации."""
    cleaned = re.sub(r"[^\w\s]", " ", (text or "").casefold().replace("ё", "е"))
    return " ".join(cleaned.split())


@dataclass
class RoutingContext:
    """Контекст исполнения маршрутизации."""

    llm_available: bool = False
    fast_llm_fn: Optional[Callable[[str, List[Tuple[str, float]]], Dict[str, Any]]] = None
    settings: Optional[Settings] = None
    allow_clarify: bool = True
    #: Обращение из профиля пользователя ("Алекс", "Сэр"); пусто — без обращения.
    addressing: str = ""


@dataclass
class RoutingDecision:
    """Результат маршрутизации пользовательского ввода."""

    kind: str                             # action | question | chat | mission | fresh_data | clarify
    tool: Optional[str]                   # инструмент или None
    confidence: float                     # уверенность (0.0 .. 1.0)
    tier: str                             # shortcut | semantic | llm | clarify | fallback_chat
    risk: str                             # low | medium | high | critical
    needs_confirmation: bool              # флаг подтверждения опасного действия
    candidates: List[Tuple[str, float]] = field(default_factory=list)
    clarify_question: Optional[str] = None
    trace: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "tool": self.tool,
            "confidence": round(self.confidence, 4),
            "tier": self.tier,
            "risk": self.risk,
            "needs_confirmation": self.needs_confirmation,
            "candidates": [(name, round(score, 4)) for name, score in self.candidates],
            "clarify_question": self.clarify_question,
            "trace": self.trace,
        }

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "RoutingDecision":
        """Восстановление решения из словаря (например из metadata миссии)."""
        if not data:
            raise ValueError("routing decision payload is empty")
        return cls(
            kind=str(data.get("kind", "chat")),
            tool=data.get("tool"),
            confidence=float(data.get("confidence", 0.0)),
            tier=str(data.get("tier", "none")),
            risk=str(data.get("risk", "low")),
            needs_confirmation=bool(data.get("needs_confirmation", False)),
            candidates=[
                (str(name), float(score))
                for name, score in (data.get("candidates") or [])
            ],
            clarify_question=data.get("clarify_question"),
            trace=dict(data.get("trace") or {}),
        )


# --------------------------------------------------------------------------- #
#  Risk Gate: Независимая оценка рисков
# --------------------------------------------------------------------------- #

_SYSTEM_PATH_RE = re.compile(
    r"(?i)(\\windows\\|system32|hosts\b|drivers\\etc|program files|hkey_|реестр|registry|\b[a-z]:\\windows)",
)

_MASS_INDICATORS_RE = re.compile(
    r"(?i)\b(все|всё|всех|всем|каждый|каждого|подчистую|целиком|полностью|массово|\*\.[a-z0-9]+)\b",
)

_DESTRUCTIVE_ACTIONS_RE = re.compile(
    r"(?i)(удал\w*|сотр\w*|стереть|очист\w*|снес\w*|убей|убить|прибей|прибить|перезапиш\w*|форматир\w*|сбрось|сброс\w*|уничтож\w*|переустанов\w*|wipe|format|destroy|terminate|rm\s*-?rf|rmdir|remove|delete)",
)

_POWER_SESSION_RE = re.compile(
    r"(?i)(заверши.*сеанс|перезагруз\w*|выключ\w*.*компьютер|отключ\w*.*питани\w*|гибернац\w*|explorer[\.\s]+exe|shutdown|reboot|power off)",
)

_SECURITY_WEAKEN_RE = re.compile(
    r"(?i)(брандмауэр|firewall|защит\w*|defender|антивирус\w*|antivirus|uac|контрол[ья] учетн\w*|eventlog|уязвимост\w*|цифров\w* подпис\w*|драйвер\w*|nmap|открыт\w* порт\w*)",
)

_SENSITIVE_DATA_RE = re.compile(
    r"(?i)(парол\w*|password|токен\w*|secret\w*|api key|cvv|ключ\w*|сертификат\w*|credential\w*|id_rsa|ssh|баз\w* клиент\w*)",
)

_UNSUPERVISED_AUTONOMY_RE = re.compile(
    r"(?i)(пока меня нет|без меня|в мое отсутствие|автономно)",
)

_FORMAT_DISK_RE = re.compile(
    r"(?i)(форматир\w*|размет\w*|fat32|ntfs|exfat|ext4|жесткий диск|накопител\w*|раздел\w*)",
)

_HARDWARE_FIRMWARE_RE = re.compile(
    r"(?i)(биос\w*|bios|разгон\w*|разогн\w*|тактов\w* частот\w*|веб[\s-]камер\w*)",
)

_UI_PHYSICAL_AUTOMATION_RE = re.compile(
    r"(?i)(кликни|нажми|прокрути|напечатай|авторизуйся|заполни форму|вкладк\w* браузер\w*)",
)


#: Исполняемые файлы в аргументах — запуск чужого кода (эскалация из safety.py).
_EXECUTABLE_RE = re.compile(r"(?i)\.(exe|bat|cmd|ps1|vbs|js|jar|msi|scr)\b")

# --------------------------------------------------------------------------- #
#  Tier-0C: структурные паттерны с извлекаемыми аргументами (extraction)
# --------------------------------------------------------------------------- #

#: Напоминание с явным временем срабатывания (извлекаемый временной аргумент).
_T0C_REMINDER_RE = re.compile(r"(?i)\b(напомни|напомнить|напоминание|будильник|таймер)\b")
_T0C_TIME_EXPR_RE = re.compile(
    r"(?i)(через\s+(?:\d+|пару|несколько|пол|полтора|десять|двадцать|тридцать|сорок|пять|десять)?\w*\s*(?:минут\w*|секунд\w*|час\w*)"
    r"|\d{1,2}[:\s]\d{2}"
    r"|в\s+\d{1,2}\s*(?:час|:))")

#: Громкость с явным числовым аргументом / процентами (извлекаемый аргумент).
_T0C_VOLUME_VERB_NOUN_RE = re.compile(r"(?i)\b(?:громкость|звук)\b")
_T0C_VOLUME_ARG_RE = re.compile(
    r"(?i)(?:на\s+\d+\s*%|до\s+\d+\s*%|\d+\s*%|наполовину|до\s+половины|на\s+(?:десять|двадцать|тридцать|сорок|пятьдесят)\s+процент\w*)")

#: Отправка сообщений и финансовые операции (перенесено из safety.py, HIGH).
_SENDING_FINANCE_RE = re.compile(
    r"(?i)(отправ\w*|пошли\b|напиши\s+письмо|send\s+(mail|email|message)|"
    r"оплат\w*|плат\w*\s+(за|картой)|купи\b|покуп\w*|payment|purchase|checkout|"
    r"перевед[ия]\s+деньги)"
)

#: Запись на диск (MEDIUM — выполняем, но фиксируем).
_WRITE_OPS_RE = re.compile(
    r"(?i)(запиши|сохран\w*|созда[йь]\w*\s+(файл|документ)|перезапиш\w*|\bwrite\b|\bsave\b)"
)

#: Установка ПО (MEDIUM).
_INSTALL_RE = re.compile(r"(?i)(установ\w*|\binstall\b|pip\s+install|npm\s+i\b)")

#: Загрузка файла из сети (MEDIUM).
_DOWNLOAD_RE = re.compile(r"(?i)(скачай|скачать|загрузи\s+файл|\bdownload\b)")

#: Завершение процессов (MEDIUM; «без сохранения» эскалируется выше до HIGH).
_PROCESS_KILL_RE = re.compile(r"(?i)(закрой|закрыть|заверши\s+процесс|\bkill\b|terminate)")

#: Структурная инспекция аргументов инструмента на предмет инъекций и деструктивных операций (§21).
_SHELL_INJECTION_RE = re.compile(r"[;|`]|&&|\|\||\$\(|\$\{")
_DESTRUCTIVE_CMDS_RE = re.compile(
    r"(?i)\b(rm\s+-[rf]+|del\s+/[fqs]+|rmdir\s+/[sq]+|format\s+[a-z]:|mkfs|drop\s+(table|database)|truncate\s+table|chmod\s+777|chmod\s+-R)\b"
)
_DANGEROUS_MASKS_RE = re.compile(r"(?i)(^|[\\/])\*(\.\*)?$|/\*|\\[*]")
_DANGEROUS_TARGET_NAME_RE = re.compile(r"(?i)\b(danger|critical|exploit|payload|rootkit|malware)\b")


def _collect_arg_strings(args: Any) -> List[str]:
    """Рекурсивно извлекает все строковые значения из аргументов инструмента."""
    res: List[str] = []
    if isinstance(args, str):
        res.append(args)
    elif isinstance(args, Mapping):
        for v in args.values():
            res.extend(_collect_arg_strings(v))
    elif isinstance(args, (list, tuple, set)):
        for v in args:
            res.extend(_collect_arg_strings(v))
    return res


def _args_text(args_hint: Optional[Dict[str, Any]]) -> str:
    """Плоское текстовое представление значений аргументов для эскалации."""
    if not args_hint:
        return ""
    parts: List[str] = []
    for value in args_hint.values():
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (list, tuple)):
            parts.extend(str(v) for v in value)
        elif isinstance(value, dict):
            parts.extend(str(v) for v in value.values())
    return " ".join(parts)


def assess_risk(
    tool: Optional[str] = None,
    args_hint: Optional[Dict[str, Any]] = None,
    text: str = "",
    return_reasons: bool = False,
) -> Union[Tuple[str, bool], Tuple[str, bool, List[str]]]:
    """Независимая оценка риска действия.

    Гарантирует 0 False Negatives для деструктивных, системных и приватных операций
    даже до определения конкретного инструмента. Аргументы инструмента (пути,
    имена файлов, команды) эскалируют риск так же, как текст запроса.
    """
    reasons: List[str] = []
    level = "low"
    raw_text = text or ""
    norm_t = normalize_text(raw_text)
    # Аргументы участвуют в эскалации наравне с текстом запроса.
    args_t = _args_text(args_hint)
    norm_args = normalize_text(args_t)

    # 1. Анализ рискованности назначенного инструмента
    # Per-action override: browser_bridge и screen_capture имеют высокий
    # паспортный риск по умолчанию, но безопасные действия (навигация,
    # чтение DOM, снимок) не требуют подтверждения — риск low.
    _browser_safe_actions = {
        "open", "navigate", "inspect_dom", "find", "read",
        "wait", "extract", "observe", "close", "type",
    }
    if tool:
        action_val = str((args_hint or {}).get("action", "")).casefold()
        cap = CAPABILITIES.get(tool)
        if cap is not None:
            _use_passport_risk = True
            if tool == "browser_bridge" and action_val in _browser_safe_actions:
                _use_passport_risk = False
            elif tool in ("computer_screenshot", "screen_capture"):
                _use_passport_risk = False
            elif tool == "computer_mouse" and action_val == "move":
                _use_passport_risk = False
            elif tool == "computer_keyboard" and action_val == "focus_window":
                _use_passport_risk = False
            if _use_passport_risk and cap.risk_level.value in ("high", "critical"):
                level = cap.risk_level.value
                reasons.append(f"рискованный инструмент {tool}")

    # 2. Системные каталоги и реестр
    if _SYSTEM_PATH_RE.search(raw_text):
        level = "critical"
        reasons.append("доступ к системным путям или реестру")

    # 3. Управление питанием и сеансом
    if _POWER_SESSION_RE.search(norm_t):
        level = "high"
        reasons.append("управление питанием или завершение сеанса ОС")

    # 4. Ослабление безопасности и аудит
    if _SECURITY_WEAKEN_RE.search(norm_t):
        level = "critical"
        reasons.append("изменение параметров безопасности или аудит уязвимостей")

    # 5. Секретные и учетные данные
    if _SENSITIVE_DATA_RE.search(norm_t):
        level = "high"
        reasons.append("работа с конфиденциальными данными или секретами")

    # 6. Массовые и деструктивные операции
    is_destructive = bool(_DESTRUCTIVE_ACTIONS_RE.search(norm_t))
    is_mass = bool(_MASS_INDICATORS_RE.search(norm_t))

    if is_destructive:
        if is_mass or any(k in norm_t for k in ("temp", "корзин", "диск", "лог", "браузер", "флешк", "fat32", "истори")):
            level = "critical"
            reasons.append("массовое или необратимое удаление данных")
        else:
            level = "high"
            reasons.append("деструктивная операция с файлами/процессами")
    elif is_mass and any(w in norm_t for w in ("архив", "сгруппир", "перенес", "скопир", "удал", "очист", "сотр", "снес", "закрой", "убей", "процесс", "файл", "диск")):
        level = "high"
        reasons.append("массовая обработка файлов или процессов")
    elif _FORMAT_DISK_RE.search(norm_t) and any(k in norm_t for k in ("диск", "флешк", "раздел", "накопител")):
        level = "critical"
        reasons.append("деструктивная операция форматирования накопителя")
    elif _HARDWARE_FIRMWARE_RE.search(norm_t):
        level = "high"
        reasons.append("низкоуровневая настройка оборудования или прошивки")
    elif _UNSUPERVISED_AUTONOMY_RE.search(norm_t):
        level = "high"
        reasons.append("автономная модификация системы в отсутствие пользователя")
    elif "без сохранения" in norm_t:
        level = "high"
        reasons.append("закрытие приложений без сохранения данных")
    elif _UI_PHYSICAL_AUTOMATION_RE.search(norm_t) and any(act in norm_t for act in ("кликни", "нажми", "прокрути", "напечатай", "авторизуйся", "заполни")):
        level = "high"
        reasons.append("прямая эмуляция пользовательского ввода")

    # 6b. Отправка/финансы (HIGH), запись/установка/загрузка/процессы (MEDIUM)
    if _SENDING_FINANCE_RE.search(norm_t):
        level = max_level(level, "high")
        reasons.append("отправка сообщений или финансовая операция")
    elif _PROCESS_KILL_RE.search(norm_t):
        level = max_level(level, "medium")
        reasons.append("завершение процессов или закрытие приложений")
    if _WRITE_OPS_RE.search(norm_t):
        level = max_level(level, "medium")
        reasons.append("запись на диск")
    if _INSTALL_RE.search(norm_t):
        level = max_level(level, "medium")
        reasons.append("установка ПО")
    if _DOWNLOAD_RE.search(norm_t):
        level = max_level(level, "medium")
        reasons.append("загрузка файла из сети")

    # 7. Структурная инспекция аргументов инструмента (инъекции, команды, пути, маски)
    for arg_val in _collect_arg_strings(args_hint):
        if _SHELL_INJECTION_RE.search(arg_val):
            level = max_level(level, "critical")
            reasons.append("shell-метасимволы или инъекция в аргументах инструмента")
        if _DESTRUCTIVE_CMDS_RE.search(arg_val):
            level = max_level(level, "critical")
            reasons.append("деструктивная команда в аргументах инструмента")
        if _SYSTEM_PATH_RE.search(arg_val):
            level = max_level(level, "critical")
            reasons.append("аргумент указывает на системный путь или реестр")
        if _DANGEROUS_MASKS_RE.search(arg_val):
            level = max_level(level, "critical")
            reasons.append("опасная маска пути в аргументах инструмента")
        if _DANGEROUS_TARGET_NAME_RE.search(arg_val):
            level = max_level(level, "high")
            reasons.append("аргумент указывает на потенциально опасный целевой объект")
        if _DESTRUCTIVE_ACTIONS_RE.search(arg_val):
            level = max_level(level, "high")
            reasons.append("аргумент содержит деструктивную операцию")
        if _MASS_INDICATORS_RE.search(arg_val):
            level = max_level(level, "high")
            reasons.append("массовая операция в аргументах инструмента")
        if _EXECUTABLE_RE.search(arg_val) and level == "low":
            level = max_level(level, "medium")
            reasons.append("операция с исполняемым файлом")

    needs_confirmation = level in ("high", "critical")
    if return_reasons:
        return level, needs_confirmation, reasons
    return level, needs_confirmation


def max_level(a: str, b: str) -> str:
    """Максимальный из двух уровней риска."""
    order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    return a if order.get(a, 0) >= order.get(b, 0) else b


# --------------------------------------------------------------------------- #
#  Семантический индекс и роутер
# --------------------------------------------------------------------------- #

class SemanticRouter:
    """Каскадный семантический роутер Jarvis с k-NN взвешенным голосованием."""

    # Канонические полнофразовые шорткаты: ТОЛЬКО точные совпадения
    CANONICAL_SHORTCUTS: Dict[str, Tuple[str, str]] = {
        "громче": ("action", "volume"),
        "погромче": ("action", "volume"),
        "тише": ("action", "volume"),
        "потише": ("action", "volume"),
        "выключи звук": ("action", "volume"),
        "включи звук": ("action", "volume"),
        "без звука": ("action", "volume"),
        "который час": ("action", "current_time"),
        "который сейчас час": ("action", "current_time"),
        "сколько времени": ("action", "current_time"),
        "сколько сейчас времени": ("action", "current_time"),
        "точное время": ("action", "current_time"),
        "текущее время": ("action", "current_time"),
        "какая дата": ("action", "current_time"),
        "какое сегодня число": ("action", "current_time"),
        "статус системы": ("action", "system_status"),
        "состояние системы": ("action", "system_status"),
        "системный статус": ("action", "system_status"),
        "статус компьютера": ("action", "system_status"),
        "погода": ("fresh_data", "weather"),
        "погода сегодня": ("fresh_data", "weather"),
        "прогноз погоды": ("fresh_data", "weather"),
        "курс доллара": ("fresh_data", "public_data"),
        "курс евро": ("fresh_data", "public_data"),
        "курс валют": ("fresh_data", "public_data"),
        "сделай скриншот": ("action", "computer_screenshot"),
        "снимок экрана": ("action", "computer_screenshot"),
        "скриншот": ("action", "computer_screenshot"),
        "поставь музыку": ("action", "play_music"),
        "включи музыку": ("action", "play_music"),
        "стоп музыка": ("action", "play_music"),
    }

    def __init__(
        self,
        model_dir: Optional[Path] = None,
        cache_dir: Optional[Path] = None,
        thresholds_path: Optional[Path] = None,
        k: int = 7,
        alpha: float = 1.5,
        tau: float = 0.08,
    ) -> None:
        self.model_dir = Path(model_dir or DEFAULT_MODEL_DIR)
        self.cache_dir = Path(cache_dir or DEFAULT_CACHE_DIR)
        self.thresholds_path = Path(thresholds_path or DEFAULT_THRESHOLDS_PATH)

        self.k = k
        self.alpha = alpha
        self.tau = tau

        self._tokenizer: Optional[Tokenizer] = None
        self._session: Optional[ort.InferenceSession] = None
        self._index_ready = False

        # Данные kNN индекса
        self._index_texts: List[str] = []
        self._index_kinds: List[str] = []
        self._index_tools: List[Optional[str]] = []
        self._index_counter_for: List[Optional[str]] = []
        self._index_vectors: Optional[np.ndarray] = None

        # Пороги калибровки
        self.confidence_threshold = 0.44
        self.margin_threshold = 0.05
        self._load_thresholds()

    def _load_thresholds(self) -> None:
        if self.thresholds_path.exists():
            try:
                with open(self.thresholds_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.confidence_threshold = float(data.get("confidence_threshold", self.confidence_threshold))
                    self.margin_threshold = float(data.get("margin_threshold", self.margin_threshold))
                    self.k = int(data.get("k", self.k))
                    self.alpha = float(data.get("alpha", self.alpha))
                    self.tau = float(data.get("tau", self.tau))
            except Exception as exc:
                log.debug("Не удалось прочесть thresholds.json: %s", exc)

    def _ensure_model(self) -> None:
        if self._session is not None and self._tokenizer is not None:
            return

        model_path = self.model_dir / "onnx" / "model.onnx"
        tok_path = self.model_dir / "tokenizer.json"

        if not model_path.exists() or not tok_path.exists():
            raise RuntimeError(f"Модель эмбеддингов не найдена в {self.model_dir}")

        self._tokenizer = Tokenizer.from_file(str(tok_path))
        self._tokenizer.enable_padding(length=128)
        self._tokenizer.enable_truncation(max_length=128)

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 4
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(str(model_path), sess_options=opts, providers=["CPUExecutionProvider"])

    def embed_text(self, text: str) -> np.ndarray:
        self._ensure_model()
        assert self._tokenizer is not None and self._session is not None

        enc = self._tokenizer.encode(text)
        inputs = {
            "input_ids": np.array([enc.ids], dtype=np.int64),
            "attention_mask": np.array([enc.attention_mask], dtype=np.int64),
            "token_type_ids": np.array([enc.type_ids], dtype=np.int64),
        }
        out = self._session.run(None, inputs)[0]
        mask = inputs["attention_mask"][..., None]
        pooled = np.sum(out * mask, axis=1) / np.clip(mask.sum(axis=1), a_min=1e-9, a_max=None)
        vec = pooled[0]
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec

    def _build_corpus(self) -> Tuple[List[str], List[str], List[Optional[str]], List[Optional[str]], str]:
        texts: List[str] = []
        kinds: List[str] = []
        tools: List[Optional[str]] = []
        counter_for: List[Optional[str]] = []

        # 1. Примеры возможностей (action / fresh_data)
        for tool_name, examples in CAPABILITY_EXAMPLES_RU.items():
            kind = "fresh_data" if tool_name in ("weather", "public_data") else "action"
            for ex in examples:
                texts.append(ex)
                kinds.append(kind)
                tools.append(tool_name)
                counter_for.append(None)

        # 2. Контр-примеры (привязаны к инструменту для оттягивания ложных срабатываний метафор)
        for tool_name, counter_ex in CAPABILITY_COUNTER_EXAMPLES_RU.items():
            for cex in counter_ex:
                texts.append(cex)
                kinds.append("chat")
                tools.append(None)
                counter_for.append(tool_name)

        # 3. Семантические якоря (chat, question, mission, action-unsupported)
        for kind, anchors in ANCHORS_BY_KIND.items():
            for anchor in anchors:
                texts.append(anchor)
                kinds.append(kind)
                tools.append(None)
                counter_for.append(None)

        # Контентный хеш для кэша
        corpus_hash = hashlib.sha256("###".join(texts).encode("utf-8")).hexdigest()
        return texts, kinds, tools, counter_for, corpus_hash

    def ensure_index(self) -> None:
        """Построение или загрузка кэшированного векторного индекса."""
        if self._index_ready:
            return

        texts, kinds, tools, counter_for, corpus_hash = self._build_corpus()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = self.cache_dir / f"routing_index_{corpus_hash[:16]}.npz"

        if cache_file.exists():
            try:
                data = np.load(str(cache_file))
                self._index_vectors = data["vectors"]
                self._index_texts = texts
                self._index_kinds = kinds
                self._index_tools = tools
                self._index_counter_for = counter_for
                self._index_ready = True
                log.debug("Семантический индекс загружен из кэша (%d записей)", len(texts))
                return
            except Exception as exc:
                log.debug("Ошибка чтения кэша индекса: %s", exc)

        log.info("Построение семантического индекса роутера (%d записей)...", len(texts))
        self._ensure_model()
        vectors = np.array([self.embed_text(t) for t in texts], dtype=np.float32)

        np.savez_compressed(str(cache_file), vectors=vectors, hash=corpus_hash)
        self._index_vectors = vectors
        self._index_texts = texts
        self._index_kinds = kinds
        self._index_tools = tools
        self._index_counter_for = counter_for
        self._index_ready = True
        log.info("Семантический индекс сохранен в %s", cache_file)

    def warmup(self) -> float:
        """Прогрев ONNX сессии и векторного индекса.

        Возвращает время прогрева в миллисекундах.
        """
        t0 = time.perf_counter()
        self.ensure_index()
        self.embed_text("статус системы")
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        log.info("SemanticRouter прогрет за %.2f мс", elapsed_ms)
        return elapsed_ms

    @staticmethod
    def _tier0c_match(raw: str, norm: str) -> Optional[Tuple[str, Optional[str], str]]:
        """Tier-0C: узкие детерминированные правила со структурным извлечением аргументов (extraction)."""
        if not norm:
            return None
        # 1. Напоминание с явным временем срабатывания (извлекаемый аргумент времени)
        if _T0C_REMINDER_RE.search(norm) and _T0C_TIME_EXPR_RE.search(norm):
            return ("action", "add_reminder", "reminder_with_time")
        # 2. Громкость с явным числовым аргументом / процентами
        if _T0C_VOLUME_VERB_NOUN_RE.search(norm) and _T0C_VOLUME_ARG_RE.search(norm):
            return ("action", "volume", "volume_with_percent")
        return None

    def route(self, text: str, ctx: Optional[RoutingContext] = None) -> RoutingDecision:
        """Главный каскадный метод маршрутизации."""
        ctx = ctx or RoutingContext()
        raw = (text or "").strip()
        norm = normalize_text(raw)
        t0 = time.perf_counter()

        trace: Dict[str, Any] = {"raw_text": raw, "norm_text": norm}

        # 0. Независимая оценка риска
        risk_level, needs_confirmation = assess_risk(None, None, raw)
        trace["initial_risk"] = risk_level
        trace["initial_needs_confirmation"] = needs_confirmation

        # 1. Tier-0A: Shortcut (только точное полнофразовое совпадение)
        if norm in self.CANONICAL_SHORTCUTS:
            s_kind, s_tool = self.CANONICAL_SHORTCUTS[norm]
            tool_risk, tool_conf = assess_risk(s_tool, None, raw)
            trace["tier"] = "shortcut"
            trace["latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)
            return RoutingDecision(
                kind=s_kind,
                tool=s_tool,
                confidence=1.0,
                tier="shortcut",
                risk=tool_risk,
                needs_confirmation=tool_conf,
                candidates=[(s_tool, 1.0)],
                trace=trace,
            )

        # 1b. Tier-0C: детерминированные высокоточные шаблоны (узкие
        # правила "глагол + объект"). Риск оценивается как для инструмента,
        # так что destructive-правило подтверждение не теряет.
        t0c = self._tier0c_match(raw, norm)
        if t0c is not None:
            c_kind, c_tool, c_reason = t0c
            c_risk, c_needs_conf = assess_risk(c_tool, None, raw)
            trace["tier"] = "pattern"
            trace["pattern"] = c_reason
            trace["latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)
            return RoutingDecision(
                kind=c_kind,
                tool=c_tool,
                confidence=0.9,
                tier="pattern",
                risk=c_risk,
                needs_confirmation=c_needs_conf,
                candidates=[(c_tool or c_kind, 0.9)],
                trace=trace,
            )

        # 2. Tier-0B: Semantic k-NN с softmax-взвешенным голосованием
        self.ensure_index()
        assert self._index_vectors is not None

        q_vec = self.embed_text(raw)
        sims = np.dot(self._index_vectors, q_vec)

        # Топ-k соседей
        top_k_idx = np.argsort(sims)[::-1][: self.k]
        s_max = float(sims[top_k_idx[0]])

        # Softmax-веса
        top_sims = np.array([float(sims[i]) for i in top_k_idx])
        weights = np.exp((top_sims - s_max) / self.tau)
        weights /= weights.sum()

        tool_scores: Dict[str, float] = {}
        kind_scores: Dict[str, float] = {}

        for rank, idx in enumerate(top_k_idx):
            w = float(weights[rank])
            s = float(sims[idx])
            t = self._index_tools[idx]
            kd = self._index_kinds[idx]
            cf = self._index_counter_for[idx]

            if t is not None:
                tool_scores[t] = tool_scores.get(t, 0.0) + w * s
                act_k = "fresh_data" if t in ("weather", "public_data") else "action"
                kind_scores[act_k] = kind_scores.get(act_k, 0.0) + w * s
            else:
                kind_scores[kd] = kind_scores.get(kd, 0.0) + w * s

            # Штраф за попадание в контр-пример
            if cf is not None:
                tool_scores[cf] = max(0.0, tool_scores.get(cf, 0.0) - self.alpha * w * s)

        candidates: List[Tuple[str, Optional[str], float]] = []
        for t, sc in tool_scores.items():
            if sc > 0.001:
                kd = "fresh_data" if t in ("weather", "public_data") else "action"
                candidates.append((kd, t, sc))
        for kd, sc in kind_scores.items():
            if sc > 0.001:
                if kd not in ("action", "fresh_data"):
                    candidates.append((kd, None, sc))
                elif kd == "action":
                    unsup_sc = sum(
                        float(weights[r]) * float(sims[idx])
                        for r, idx in enumerate(top_k_idx)
                        if self._index_kinds[idx] == "action" and self._index_tools[idx] is None
                    )
                    if unsup_sc > 0.001:
                        candidates.append(("action", None, unsup_sc))

        candidates.sort(key=lambda x: -x[2])
        if not candidates:
            candidates = [("chat", None, s_max)]

        top1_kind, top1_tool, top1_score = candidates[0]
        top2_score = candidates[1][2] if len(candidates) > 1 else 0.0
        margin_agg = top1_score - top2_score

        top_candidates_ui = [
            (c[1] or c[0], round(c[2], 4)) for c in candidates[:4]
        ]

        trace["s_max"] = round(s_max, 4)
        trace["margin_agg"] = round(margin_agg, 4)
        trace["top_candidates"] = top_candidates_ui

        # Оценка риска с учетом предсказанного инструмента
        final_risk, final_needs_conf = assess_risk(top1_tool, None, raw)

        # Проверка порога уверенности и маржи
        if s_max >= self.confidence_threshold and margin_agg >= self.margin_threshold:
            trace["tier"] = "semantic"
            trace["latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)
            return RoutingDecision(
                kind=top1_kind,
                tool=top1_tool,
                confidence=s_max,
                tier="semantic",
                risk=final_risk,
                needs_confirmation=final_needs_conf,
                candidates=top_candidates_ui,
                trace=trace,
            )

        # 3. Серая зона (Grey Zone)
        trace["grey_zone"] = True

        # FAST LLM tier (если подключен)
        if ctx.llm_available and ctx.fast_llm_fn is not None:
            try:
                llm_res = ctx.fast_llm_fn(raw, top_candidates_ui)
                if isinstance(llm_res, dict) and llm_res.get("kind"):
                    llm_kind = str(llm_res.get("kind") or "chat")
                    llm_tool = llm_res.get("tool")
                    llm_conf = float(llm_res.get("confidence") or 0.8)
                    llm_risk, llm_needs_conf = assess_risk(llm_tool, None, raw)
                    trace["tier"] = "llm"
                    trace["latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)
                    return RoutingDecision(
                        kind=llm_kind,
                        tool=llm_tool,
                        confidence=llm_conf,
                        tier="llm",
                        risk=llm_risk,
                        needs_confirmation=llm_needs_conf,
                        candidates=top_candidates_ui,
                        trace=trace,
                    )
            except Exception as exc:
                log.warning("FAST LLM классификация не удалась: %s", exc)

        # Одно уточнение на русском языке (из слов пользователя, без "Сэр")
        if ctx.allow_clarify and len(top_candidates_ui) >= 2:
            single_dominant = margin_agg >= self.margin_threshold and s_max < self.confidence_threshold
            clarify_q = self._build_clarify_question(
                raw, top_candidates_ui[:2],
                addressing=ctx.addressing,
                single_dominant=single_dominant,
            )
            trace["tier"] = "clarify"
            trace["clarify_single_dominant"] = single_dominant
            trace["latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)
            return RoutingDecision(
                kind="clarify",
                tool=None,
                confidence=s_max,
                tier="clarify",
                risk=final_risk,
                needs_confirmation=final_needs_conf,
                candidates=top_candidates_ui,
                clarify_question=clarify_q,
                trace=trace,
            )

        # 4. Fallback: Chat
        trace["tier"] = "fallback_chat"
        trace["latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        return RoutingDecision(
            kind="chat",
            tool=None,
            confidence=max(0.1, s_max),
            tier="fallback_chat",
            risk=final_risk,
            needs_confirmation=final_needs_conf,
            candidates=top_candidates_ui,
            trace=trace,
        )

    @staticmethod
    def _extract_object(raw: str) -> str:
        """Объект запроса словами пользователя: 'выруби блокнот' -> 'блокнот'.

        Вырезаются глаголы-действия, вопросительные слова и мусор. Пусто,
        если после вырезания ничего осмысленного не осталось.
        """
        words = re.sub(r"[^\w\s-]", " ", (raw or ""), flags=re.UNICODE).split()
        stop = {
            # глаголы действия и их формы
            "открой", "открыть", "открывай", "включи", "включить", "включай",
            "выключи", "выключить", "выруби", "вырубить", "запусти", "запустить",
            "закрой", "закрыть", "прикрой", "поставь", "поставить", "ставь",
            "сделай", "сделать", "убери", "убрать", "погромче", "потише",
            "громче", "тише", "покажи", "показать", "найди", "найти", "поищи",
            "ищи", "искать", "поиск", "удали", "удалить", "сотри", "стереть",
            "напомни", "напоминание", "напомнить", "сними", "снять",
            "скриншот", "проверь", "проверить", "узнай", "узнать",
            "разбери", "разобрать", "создай", "создать", "скопируй", "скопировать",
            "перемести", "переместить", "запиши", "записать", "прочитай", "прочитать",
            "скачай", "скачать", "установи", "установить", "отмени", "отменить",
            # вопросительные слова
            "сколько", "который", "какая", "какое", "какой", "что", "кто", "где",
            "когда", "почему", "зачем", "как", "каким", "чем",
            # миссионные и вежливые fillers
            "пока", "меня", "нет", "без", "меня", "отсутствие", "автономно",
            "пожалуйста", "сэр", "мне", "бы", "давай", "давайте", "нужно",
            "надо", "хочу", "тут", "там", "его", "её", "ее", "их",
            "это", "этот", "эту", "мой", "моя", "мои", "твой", "твоя",
        }
        kept = [
            w for w in words
            if w.casefold().replace("ё", "е") not in {s.strip().casefold().replace("ё", "е") for s in stop}
            and len(w) > 1
        ]
        return " ".join(kept).strip(" ,.")

    @staticmethod
    def _build_clarify_question(
        raw: str,
        candidates: List[Tuple[str, float]],
        addressing: str = "",
        single_dominant: bool = False,
    ) -> str:
        """Человеческий уточняющий вопрос из слов пользователя.

        - обращение из профиля (addressing), пусто — без обращения;
        - объект — словами пользователя («Блокнот»), не техописания;
        - один доминирующий кандидат — да/нет-вопрос, два — «или».
        """
        short = {
            "open_app": "открыть",
            "close_app": "закрыть",
            "volume": "сделать громче или тише",
            "system_status": "проверить состояние системы",
            "current_time": "узнать время",
            "play_music": "включить музыку",
            "web_search": "поискать в интернете",
            "weather": "посмотреть погоду",
            "public_data": "посмотреть курс валют",
            "add_reminder": "поставить напоминание",
            "list_reminders": "показать напоминания",
            "cancel_reminder": "отменить напоминание",
            "search_files": "искать файлы",
            "list_files": "показать файлы",
            "list_files_recursive": "показать всю структуру папок",
            "read_file": "прочитать файл",
            "write_file": "записать файл",
            "file_copy": "скопировать файл",
            "file_move": "переместить файл",
            "computer_keyboard": "напечатать текст",
            "computer_mouse": "кликнуть мышью",
            "computer_screenshot": "сделать скриншот",
            "screen_capture": "сделать скриншот",
            "browser_automation": "поработать в браузере",
            "browser_bridge": "поработать в браузере",
            "chat": "просто поболтать",
            "question": "ответить на вопрос",
            "mission": "разобрать как поручение",
            "action": "выполнить команду",
        }
        c1 = candidates[0][0]
        p1 = short.get(c1, c1)
        obj = SemanticRouter._extract_object(raw)
        prefix = f"{addressing.strip()}, " if (addressing or "").strip() else ""
        if single_dominant:
            core_q = f"«{obj.capitalize()}» — {p1}?" if obj else f"{p1.capitalize()}?"
        else:
            p2 = short.get(candidates[1][0], candidates[1][0])
            core_q = f"«{obj.capitalize()}» — {p1} или {p2}?" if obj else f"{p1.capitalize()} или {p2}?"
        return f"{prefix}уточните, пожалуйста: {core_q}"


# Синглтон роутера для вызовов
_GLOBAL_ROUTER: Optional[SemanticRouter] = None


def get_router() -> SemanticRouter:
    global _GLOBAL_ROUTER
    if _GLOBAL_ROUTER is None:
        _GLOBAL_ROUTER = SemanticRouter()
    return _GLOBAL_ROUTER


def route(text: str, ctx: Optional[RoutingContext] = None) -> RoutingDecision:
    """Единая точка входа в семантическую маршрутизацию."""
    return get_router().route(text, ctx)


_FILE_TOOLS = {
    "list_files", "search_files", "list_files_recursive",
    "read_file", "write_file", "file_copy", "file_move",
}
_BROWSER_TOOLS = {
    "browser_automation", "browser_bridge", "computer_keyboard",
    "computer_mouse", "computer_screenshot", "screen_capture",
}


def intent_category(decision: Optional["RoutingDecision"]) -> str:
    """Категория намерения для ACK/executive — производная от decision."""
    if decision is None:
        return "none"
    tool = decision.tool
    if tool in ("open_app", "close_app"):
        return "app"
    if tool in ("volume", "current_time", "system_status"):
        return "system"
    if tool == "play_music":
        return "media"
    if tool in ("weather", "public_data", "web_search"):
        return "web"
    if tool in _FILE_TOOLS:
        return "file"
    if tool in _BROWSER_TOOLS:
        return "browser"
    if decision.kind == "mission":
        return "mission"
    return "none"
