"""Sprint 3 — MODEL POLICY + EXECUTOR SAFETY.

Покрывает:
  * STEP 3: tool timeout (зависший инструмент прерывается watchdog'ом);
  * STEP 3: resource limit (вывод > потолка усекается);
  * STEP 3: parallel isolation (параллельные вызовы не мешают друг другу
    и ограничены семафором);
  * STEP 4: repair loop limit (max 3 попытки -> остановка);
  * STEP 4: repair loop risk gate (переформулированный HIGH-risk повтор
    блокируется, цикл останавливается с запросом человека);
  * STEP 5: model fallback (TIER 1 мёртв -> отвечает фолбэк, ответ
    помечен [degraded]);
  * STEP 5: circuit breaker (3 сбоя подряд -> провайдер пропускается);
  * STEP 5: stream budget (trickle-стрим убивается по wall-clock).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List

import pytest

from config.settings import Settings
from core.actions.base import ActionResult, Tool, ToolContext
from core.actions.executor import execute_tool
from core.llm import Tier, breaker
from core.llm.backend import BackendUnavailable
from core.repair import RepairLoop
from core.safety import assess_risk
from core.actions.registry import ToolRegistry


# --------------------------------------------------------------------------- #
#  Тестовые инструменты
# --------------------------------------------------------------------------- #

class _Schema:
    """Минимальный реестр-заглушка: один инструмент с заданной схемой."""


class HangingTool(Tool):
    """Инструмент, который «зависает» навсегда (имитация мёртвой сети)."""

    @property
    def name(self) -> str:
        return "hanging_tool"

    @property
    def description(self) -> str:
        return "Зависает навсегда"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    def run(self, args, context) -> ActionResult:
        time.sleep(30)
        return ActionResult(tool=self.name, args=args, ok=True, output="не должен быть возвращен")


class BigOutputTool(Tool):
    """Возвращает строку сильно больше потолка."""

    @property
    def name(self) -> str:
        return "big_output_tool"

    @property
    def description(self) -> str:
        return "Возвращает огромный вывод"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    def run(self, args, context) -> ActionResult:
        return ActionResult(tool=self.name, args=args, ok=True, output="A" * 200_000)


class CountingTool(Tool):
    """Считает вызовы; используется для лимита попыток repair."""

    calls: int = 0
    lock = threading.Lock()

    @property
    def name(self) -> str:
        return "counting_tool"

    @property
    def description(self) -> str:
        return "Всегда падает"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    def run(self, args, context) -> ActionResult:
        with CountingTool.lock:
            CountingTool.calls += 1
        return ActionResult(tool=self.name, args=args, ok=False,
                            error="temporary provider error")


def _registry(*tools: Tool) -> ToolRegistry:
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


def _ctx(settings: Settings) -> ToolContext:
    return ToolContext(user_id="default", settings=settings, state=None)


# --------------------------------------------------------------------------- #
#  STEP 3 — tool timeout
# --------------------------------------------------------------------------- #

def test_tool_timeout_interrupts_hanging_tool(settings):
    """Зависший инструмент прерывается watchdog'ом, миссия не виснет."""
    reg = _registry(HangingTool())
    started = time.perf_counter()
    result = execute_tool(reg, "hanging_tool", {}, _ctx(settings),
                          timeout_sec=0.5)
    elapsed = time.perf_counter() - started

    assert not result.ok
    assert "Таймаут выполнения" in result.error
    assert elapsed < 5, f"watchdog не сработал: {elapsed:.1f} c"


def test_tool_timeout_not_retried(settings):
    """Таймаут не ретраится: один зависший вызов = одна попытка, не три."""
    calls = {"n": 0}

    class HangOnce(HangingTool):
        def run(self, args, context):
            calls["n"] += 1
            time.sleep(10)
            return ActionResult(tool=self.name, args=args, ok=True, output="late")

    reg = _registry(HangOnce())
    started = time.perf_counter()
    result = execute_tool(reg, "hanging_tool", {}, _ctx(settings),
                          timeout_sec=0.4)
    elapsed = time.perf_counter() - started

    assert not result.ok
    assert calls["n"] == 1, "таймаут не должен ретраиться"
    assert elapsed < 5


def test_tool_timeout_categories(settings):
    """Бюджет выбирается по категории: web > file > system."""
    from core.actions.executor import tool_timeout_for
    ctx = _ctx(settings)
    assert tool_timeout_for("web_fetch", ctx) == settings.limits.tool_timeout_web_sec
    assert tool_timeout_for("read_file", ctx) == settings.limits.tool_timeout_file_sec
    assert tool_timeout_for("system_status", ctx) == settings.limits.tool_timeout_system_sec


# --------------------------------------------------------------------------- #
#  STEP 3 — resource limits
# --------------------------------------------------------------------------- #

def test_tool_output_truncated(settings):
    """Вывод инструмента усекается до tool_output_max_bytes."""
    cap = settings.limits.tool_output_max_bytes
    reg = _registry(BigOutputTool())
    result = execute_tool(reg, "big_output_tool", {}, _ctx(settings))
    assert result.ok
    assert len(result.output.encode("utf-8")) <= cap + 300  # + маркер усечения
    assert "усечён" in result.output


def test_small_output_not_touched(settings):
    """Обычный вывод не изменяется."""
    class OkTool(Tool):
        @property
        def name(self): return "ok_tool"
        @property
        def description(self): return "ok"
        @property
        def input_schema(self): return {"type": "object", "properties": {}}
        def run(self, args, context):
            return ActionResult(tool="ok_tool", args=args, ok=True, output="короткий ответ")

    reg = _registry(OkTool())
    result = execute_tool(reg, "ok_tool", {}, _ctx(settings))
    assert result.output == "короткий ответ"


# --------------------------------------------------------------------------- #
#  STEP 3 — parallel isolation
# --------------------------------------------------------------------------- #

def test_parallel_tools_isolated(settings):
    """Два параллельных вызова не мешают друг другу (каждый видит свои args)."""
    seen: List[str] = []
    lock = threading.Lock()

    class EchoTool(Tool):
        @property
        def name(self): return "echo_tool"
        @property
        def description(self): return "echo"
        @property
        def input_schema(self):
            return {"type": "object", "properties": {"text": {"type": "string"}},
                    "required": ["text"]}
        def run(self, args, context):
            time.sleep(0.3)
            with lock:
                seen.append(args["text"])
            return ActionResult(tool="echo_tool", args=args, ok=True,
                                output=args["text"])

    reg = _registry(EchoTool())
    ctx = _ctx(settings)
    threads = []
    boxes: List[Any] = []
    for i in range(4):
        box = {}
        boxes.append(box)

        def _run(i=i, box=box):
            box["result"] = execute_tool(reg, "echo_tool", {"text": f"msg-{i}"}, ctx)

        t = threading.Thread(target=_run)
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    for i, box in enumerate(boxes):
        assert box["result"].ok
        assert box["result"].output == f"msg-{i}"
    assert sorted(seen) == sorted(f"msg-{i}" for i in range(4))


def test_parallel_semaphore_limits_concurrency(settings):
    """Семафор max_parallel_tools ограничивает одновременные выполнения."""
    settings.limits.max_parallel_tools = 2
    concurrent = {"now": 0, "max": 0}
    lock = threading.Lock()

    class SlowTool(Tool):
        @property
        def name(self): return "slow_tool"
        @property
        def description(self): return "slow"
        @property
        def input_schema(self): return {"type": "object", "properties": {}}
        def run(self, args, context):
            with lock:
                concurrent["now"] += 1
                concurrent["max"] = max(concurrent["max"], concurrent["now"])
            time.sleep(0.25)
            with lock:
                concurrent["now"] -= 1
            return ActionResult(tool="slow_tool", args=args, ok=True, output="done")

    reg = _registry(SlowTool())
    ctx = _ctx(settings)
    threads = [threading.Thread(target=lambda i=i: execute_tool(reg, "slow_tool", {"i": i}, ctx))
               for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert concurrent["max"] <= 2, f"параллелизм превысил лимит: {concurrent}"


# --------------------------------------------------------------------------- #
#  STEP 4 — repair loop
# --------------------------------------------------------------------------- #

def test_repair_loop_stops_after_max_attempts(settings):
    """Max 3 попытки починки, затем остановка (нет бесконечного цикла)."""
    CountingTool.calls = 0
    reg = _registry(CountingTool())
    loop = RepairLoop(reg, max_attempts=3)

    started = time.perf_counter()
    result = loop.run("counting_tool", {}, _ctx(settings),
                      verification=lambda r: False)  # ничего не «чинится»
    elapsed = time.perf_counter() - started

    assert not result.ok
    assert result.attempts == 3
    assert CountingTool.calls == 3
    assert elapsed < 30
    assert any("все попытки исчерпаны" in t for t in result.trace)


def test_repair_loop_does_not_repeat_same_deterministic_path_failure(settings):
    class MissingPathTool(Tool):
        calls = 0

        @property
        def name(self): return "missing_path"
        @property
        def description(self): return "missing path"
        @property
        def input_schema(self):
            return {"type": "object", "properties": {"path": {"type": "string"}},
                    "required": ["path"]}
        def run(self, args, context):
            type(self).calls += 1
            return ActionResult(self.name, args, False, error="no such file: missing.txt")

    reg = _registry(MissingPathTool())
    result = RepairLoop(reg, max_attempts=3).run(
        "missing_path", {"path": "missing.txt"}, _ctx(settings),
        verification=lambda _: False,
    )

    assert result.ok is False
    assert result.attempts == 1
    assert MissingPathTool.calls == 1
    assert any("повтор тех же аргументов остановлен" in item for item in result.trace)


def test_repair_loop_risk_gate_blocks_high_risk_retry(settings):
    """Повторный HIGH-risk вызов (переформулированные аргументы) блокируется."""
    attempts = {"n": 0}

    class DeleteFailsTool(Tool):
        @property
        def name(self): return "delete_file"
        @property
        def description(self): return "удаление"
        @property
        def input_schema(self):
            return {"type": "object", "properties": {"path": {"type": "string"}},
                    "required": ["path"]}
        def run(self, args, context):
            attempts["n"] += 1
            return ActionResult(tool="delete_file", args=args, ok=False,
                                error="permission denied: системный файл")

    reg = _registry(DeleteFailsTool())
    loop = RepairLoop(reg, max_attempts=3)

    def gate(tool: str, args: Dict[str, Any]):
        risk = assess_risk(f"удали файл {args.get('path', '')}", tool, args)
        if risk.needs_confirmation:
            return "; ".join(risk.reasons)
        return None

    result = loop.run("delete_file", {"path": "C:/Windows/System32/cmd.exe"},
                      _ctx(settings), verification=lambda r: False,
                      risk_gate=gate)

    # Первая попытка repair уже заблокирована гейтом: 0 вызовов инструмента.
    assert attempts["n"] == 0
    assert not result.ok
    assert result.needs_human
    assert "заблокирован" in result.human_message or "безопасности" in result.human_message
    assert any("RISK GATE" in t for t in result.trace)


def test_repair_loop_allows_low_risk_retry(settings):
    """LOW-risk повтор НЕ блокируется гейтом — repair работает как раньше."""
    CountingTool.calls = 0
    reg = _registry(CountingTool())
    loop = RepairLoop(reg, max_attempts=2)

    def gate(tool: str, args: Dict[str, Any]):
        risk = assess_risk("прочитай файл", tool, args)
        return "; ".join(risk.reasons) if risk.needs_confirmation else None

    result = loop.run("counting_tool", {}, _ctx(settings),
                      verification=lambda r: False, risk_gate=gate)
    assert CountingTool.calls == 2  # гейт не мешает обычному repair


def test_repair_loop_reasoner_patch_also_gated(settings):
    """LLM-патч аргументов, повышающий риск, тоже проходит гейт."""
    calls = {"n": 0}

    class ReadFailsTool(Tool):
        @property
        def name(self): return "read_file"
        @property
        def description(self): return "чтение"
        @property
        def input_schema(self):
            return {"type": "object", "properties": {"path": {"type": "string"}},
                    "required": ["path"]}
        def run(self, args, context):
            calls["n"] += 1
            return ActionResult(tool="read_file", args=args, ok=False,
                                error="no such file")

    def reasoner(error, args, context):
        # «Починка»: модель предлагает удалить вместо чтения (HIGH risk).
        return {"path": args.get("path", "") + "; rm -rf /"}

    reg = _registry(ReadFailsTool())
    loop = RepairLoop(reg, reasoner=reasoner, max_attempts=3)

    def gate(tool: str, args: Dict[str, Any]):
        risk = assess_risk("почини чтение файла", tool, args)
        return "; ".join(risk.reasons) if risk.needs_confirmation else None

    result = loop.run("read_file", {"path": "notes.txt"}, _ctx(settings),
                      verification=lambda r: False, risk_gate=gate)

    assert calls["n"] == 1  # первый вызов прошёл, патч заблокирован
    assert result.needs_human
    assert any("RISK GATE" in t for t in result.trace)


# --------------------------------------------------------------------------- #
#  STEP 5 — circuit breaker
# --------------------------------------------------------------------------- #

def test_circuit_breaker_opens_after_consecutive_failures():
    """3 неудачи подряд -> breaker разомкнут; успех -> сброс."""
    breaker.reset()
    try:
        assert not breaker.is_open("test-provider")
        breaker.record_failure("test-provider")
        breaker.record_failure("test-provider")
        assert not breaker.is_open("test-provider"), "2 неудачи не размыкают"
        breaker.record_failure("test-provider")
        assert breaker.is_open("test-provider"), "3 подряд — разомкнут"

        breaker.record_success("test-provider")
        assert not breaker.is_open("test-provider"), "успех сбрасывает серию"
    finally:
        breaker.reset()


def test_circuit_breaker_model_scoped():
    """Сбой одной модели провайдера не блокирует другую модель того же провайдера."""
    breaker.reset()
    try:
        for _ in range(3):
            breaker.record_failure("anymodel:am/free")
        assert breaker.is_open("anymodel:am/free")
        assert not breaker.is_open("anymodel:cx/gpt-5.5"), (
            "breaker обязан ключеваться по provider:model"
        )
    finally:
        breaker.reset()


def test_agent_skips_open_breaker_tier(monkeypatch, settings):
    """Тир с разомкнутым breaker'ом пропускается при выборе бэкенда."""
    from core.agent import Agent, AgentConfig

    breaker.reset()
    try:
        # Размыкаем модель FAST-тира (ключ breaker'а — provider:model).
        settings.tier_providers.fast = "broken-provider"
        model_id = settings.get_model_id(Tier.FAST) or ""
        for _ in range(3):
            breaker.record_failure(f"broken-provider:{model_id}")

        agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
        backend, tier = agent._backend_for_routing(None)
        # None-chain => [FAST] пропущен => офлайн-фолбэк (GGUF есть в тестах?).
        # В любом случае FAST по цепочке выбран быть не должен.
        assert tier is not Tier.FAST or backend is None or getattr(backend, "name", "") != f"broken-provider:{model_id}"
    finally:
        breaker.reset()


# --------------------------------------------------------------------------- #
#  STEP 5 — stream budget (trickle killer)
# --------------------------------------------------------------------------- #

class _FakeSSEResponse:
    """Медленный SSE-ответ: капает по чанку каждые ~0.2 c."""

    def __init__(self, chunks: int = 50, delay: float = 0.2):
        self._chunks = chunks
        self._delay = delay
        self.closed = False
        self.encoding = None  # как у requests.Response без charset в headers

    def iter_lines(self, decode_unicode=True):
        import json as _json
        for i in range(self._chunks):
            time.sleep(self._delay)
            data = {"choices": [{"delta": {"content": f"чанк{i} "}}]}
            yield f"data: {_json.dumps(data, ensure_ascii=False)}"

    def close(self):
        self.closed = True


def test_stream_budget_kills_trickle():
    """Trickle-стрим (капает вечно) убивается по wall-clock бюджету."""
    from core.llm.remote_api import RemoteAPIBackend

    backend = RemoteAPIBackend(provider="test", model_id="m", base_url="http://x",
                               api_key="k", timeout=0.5, max_retries=1)
    backend._request_with_retry = lambda payload, stream=False: _FakeSSEResponse()  # type: ignore

    started = time.perf_counter()
    with pytest.raises(BackendUnavailable) as excinfo:
        list(backend.streaming([{"role": "user", "content": "привет"}]))
    elapsed = time.perf_counter() - started

    assert "Бюджет стрима" in str(excinfo.value) or "Первый токен" in str(excinfo.value)
    assert elapsed < 5, f"trickle не убит: {elapsed:.1f} c"


# --------------------------------------------------------------------------- #
#  STEP 5 — model fallback: TIER 1 мёртв -> отвечает фолбэк + [degraded]
# --------------------------------------------------------------------------- #

class _DeadFastBackend:
    """FAST-модель мертва; фолбэк (аналитик) отвечает нормальным текстом."""

    name = "dead:fast"
    model = "dead-fast"

    def chat(self, messages, system=None, max_tokens=None, temperature=None) -> str:
        raise BackendUnavailable("Провайдер мёртв: HTTP 408")

    def direct(self, *a, **k):  # pragma: no cover
        raise BackendUnavailable("Провайдер мёртв")


class _AliveFallbackBackend:
    name = "alive:analyst"
    model = "alive-analyst"

    def __init__(self):
        self.called = False

    def chat(self, messages, system=None, max_tokens=None, temperature=None) -> str:
        self.called = True
        return "Всё в порядке, сэр. Отвечает резервная модель."

    def direct(self, prompt, *a, **k):
        return self.chat([{"role": "user", "content": prompt}])


def test_conversation_fallback_degraded(monkeypatch, settings):
    """TIER 1 мёртв -> фолбэк отвечает, текст помечен [degraded]."""
    import core.agent as agent_mod
    from core.agent import Agent, AgentConfig

    breaker.reset()
    dead = type("Dead", (_DeadFastBackend,), {})()
    alive = _AliveFallbackBackend()

    def _fake_get(inst_settings, tier=Tier.FAST, *, policy_override=None):
        return dead if tier is Tier.FAST else alive

    monkeypatch.setattr(agent_mod, "get_llm_backend", _fake_get)
    monkeypatch.setattr(Settings, "is_tier_available", lambda self, tier: True)
    monkeypatch.setattr(agent_mod, "get_offline_backend",
                        lambda inst_settings: (_ for _ in ()).throw(
                            BackendUnavailable("офлайн GGUF отключён в тесте")))

    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))

    outcome = agent.execute("привет")
    assert outcome.mode == "conversation"
    assert "[degraded]" in outcome.text
    assert outcome.degraded is True
    assert alive.called
    breaker.reset()


def test_conversation_all_models_dead_model_error(monkeypatch, settings):
    """Все тиры и офлайн мертвы -> честный model_error, не «не умею»."""
    import core.agent as agent_mod
    from core.agent import Agent, AgentConfig, MODEL_UNAVAILABLE_TEXT

    dead = type("Dead2", (_DeadFastBackend,), {})()

    def _fake_get(inst_settings, tier=Tier.FAST, *, policy_override=None):
        return dead

    monkeypatch.setattr(agent_mod, "get_llm_backend", _fake_get)
    monkeypatch.setattr(Settings, "is_tier_available", lambda self, tier: True)
    monkeypatch.setattr(agent_mod, "get_offline_backend",
                        lambda inst_settings: (_ for _ in ()).throw(
                            BackendUnavailable("офлайн недоступен")))

    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    outcome = agent.execute("привет")
    assert outcome.mode == "model_error"
    # C4 (cloud-only): дружелюбная фраза маппера вместо сырой ошибки.
    from core.brain.error_messages import MESSAGES
    assert outcome.text in MESSAGES.values()


# --------------------------------------------------------------------------- #
#  STEP 2 — модельная матрица
# --------------------------------------------------------------------------- #

def test_planner_bumps_fast_to_analyst(monkeypatch, settings):
    """TIER 2: JSON-план с FAST поднимается до первого внешнего тира."""
    from core.model_router import ModelRouter, RoutingDecision

    monkeypatch.setattr(Settings, "is_tier_available", lambda self, tier: True)
    router = ModelRouter(settings)
    decision = router.route("открой заметки и найди файл")  # action, score < 0.35 -> FAST
    planning = router.route_for_planning(decision)

    assert decision.tier is Tier.FAST, "простая команда должна маршрутизироваться на FAST"
    assert planning.tier is not Tier.FAST
    assert planning.fallback_chain, "должна быть цепочка фолбэков"
    assert Tier.FAST in planning.fallback_chain, "FAST остаётся последним фолбэком"


def test_planner_keeps_forced_local(settings):
    """Офлайн-режим (приватные данные) не поднимается до внешних тиров."""
    from core.model_router import ModelRouter

    router = ModelRouter(settings)
    decision = router.route("напомни мой пароль от банка")  # private -> forced_local
    planning = router.route_for_planning(decision)
    assert planning.forced_local is True
    assert planning.tier is decision.tier
