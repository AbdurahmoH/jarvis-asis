"""Sprint 1 — корреляция, стриминг и надёжность WS-ответов.

Проверяет три вещи на РЕАЛЬНОМ транспорте (JarvisWSServer + websockets):
  * STEP 1: один correlation ID на весь логический ответ (start/end пары
    совпадают; параллельные запросы не перемешиваются);
  * STEP 2: sync-путь стримит реальный поток модели (start → token… → end
    с одним ID, токены кумулятивны, не фейк-чанкинг);
  * STEP 5: сбой провайдера -> дружелюбный end без зависания.

Без pytest-asyncio: гоняем через asyncio.run(), как test_ws_server.py.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
import websockets  # type: ignore

from config.settings import Settings
from core.agent import Agent, AgentConfig, MODEL_UNAVAILABLE_TEXT
from core.llm import Tier
from core.llm.backend import BackendUnavailable
from core.orchestrator import Orchestrator
from core.structured import AnswerStreamExtractor
from core.ws_server import JarvisWSServer
from tests.conftest import FakeBackend


# --------------------------------------------------------------------------- #
#  Unit: инкрементальный извлекатель «answer»
# --------------------------------------------------------------------------- #

def _feed_all(deltas):
    ex = AnswerStreamExtractor()
    out = ""
    for d in deltas:
        out = ex.feed(d)
    return out


def test_extractor_streams_answer_progressively():
    raw = '{"tool": null, "answer": "Привет! Чем могу помочь?", "risk": "low"}'
    deltas = [raw[i:i + 7] for i in range(0, len(raw), 7)]
    ex = AnswerStreamExtractor()
    seen = []
    for d in deltas:
        v = ex.feed(d)
        if v and (not seen or v != seen[-1]):
            seen.append(v)
    assert seen, "текст должен появляться прогрессивно"
    assert seen == sorted(seen, key=len), "кумулятивность: только рост"
    assert _feed_all(deltas) == "Привет! Чем могу помочь?"


def test_extractor_handles_escapes_and_cut_escape():
    raw = '{"answer": "строка с \\nпереносом и \\"кавычкой\\" и \\u0430"}'
    deltas = [raw[i:i + 5] for i in range(0, len(raw), 5)]
    assert _feed_all(deltas) == 'строка с \nпереносом и "кавычкой" и а'
    # разрез ровно посередине \u-эскейпа не должен показывать мусор
    ex = AnswerStreamExtractor()
    ex.feed('{"answer": "x')
    v = ex.feed('\\u0')  # незавершённый \u0430
    assert v == "x", f"незавершённый escape не должен ломать текст: {v!r}"
    assert ex.feed('430"} ') == "xа"


def test_extractor_plain_text_and_tool_plan():
    # модель ответила чистым текстом без JSON — показывать нечего
    assert _feed_all(["просто", " текст", " без JSON"]) == ""
    # план с инструментом: answer пуст -> пусто (нет фейкового текста)
    assert _feed_all(['{"tool": "open_app", "answer": "", "arguments": {"name": "x"}}']) == ""


def test_orchestrator_sink_passthrough():
    orch = Orchestrator(Settings())
    sink = lambda s: None  # noqa: E731
    orch.install_stream_sink(sink)
    assert getattr(orch._agent._stream_tls, "sink", None) is sink
    orch.clear_stream_sink()
    assert getattr(orch._agent._stream_tls, "sink", None) is None


# --------------------------------------------------------------------------- #
#  WS-харness: легкие оркестраторы
# --------------------------------------------------------------------------- #

class SyncPairOrchestrator:
    """Последовательный/параллельный sync-ответ через output_callback."""

    def __init__(self, delay: float = 0.0) -> None:
        self._output_callback = lambda text: None
        self._delay = delay

    def start(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def handle_input(self, text: str):
        if self._delay:
            time.sleep(self._delay)
        self._output_callback(f"Ответ на: {text}")
        return {"needs_confirmation": False, "response": f"Ответ на: {text}"}

    def subscribe_events(self, callback):
        return lambda: None

    def list_missions(self, include_terminal=True):
        return []

    def cancel_mission(self, task_id):
        return False

    def answer_confirmation(self, confirmation_id, approved):
        return None


class AgentSinkOrchestrator(SyncPairOrchestrator):
    """Прокси в реальный Agent с поддержкой stream sink (как Orchestrator)."""

    def __init__(self, agent: Agent) -> None:
        super().__init__()
        self._agent = agent

    def install_stream_sink(self, sink) -> None:
        self._agent.install_stream_sink(sink)

    def clear_stream_sink(self) -> None:
        self._agent.clear_stream_sink()

    def handle_input(self, text: str):
        outcome = self._agent.execute(text)
        self._output_callback(outcome.text)
        return {"needs_confirmation": False, "response": outcome.text}


class StreamingFakeBackend(FakeBackend):
    """FakeBackend, чей streaming() отдаёт план-JSON настоящими дельтами."""

    def streaming(self, messages, system=None, max_tokens=None, temperature=None):
        text = self.chat(messages, system=system)
        step = 6
        for i in range(0, len(text), step):
            yield text[i:i + step]


class DeadBackend(FakeBackend):
    """Все вызовы (chat и streaming) падают по недоступности провайдера."""

    def chat(self, messages, system=None, max_tokens=None, temperature=None) -> str:
        raise BackendUnavailable("Провайдер anymodel недоступен: HTTP 408: таймаут 7.0 с")

    def streaming(self, messages, system=None, max_tokens=None, temperature=None):
        raise BackendUnavailable("Провайдер anymodel недоступен: HTTP 408: таймаут 7.0 с")
        yield ""  # pragma: no cover (генератор)


@pytest.fixture
def stream_backend(monkeypatch):
    from core import llm as llm_mod
    import core.agent as agent_mod

    backend = StreamingFakeBackend()

    def _fake_get(settings, tier=Tier.FAST, *, policy_override=None):
        return backend

    monkeypatch.setattr(llm_mod, "get_llm_backend", _fake_get)
    monkeypatch.setattr(agent_mod, "get_llm_backend", _fake_get)
    monkeypatch.setattr(Settings, "is_tier_available", lambda self, tier: True)
    return backend


@pytest.fixture
def dead_backend(monkeypatch):
    from core import llm as llm_mod
    import core.agent as agent_mod

    backend = DeadBackend()

    def _fake_get(settings, tier=Tier.FAST, *, policy_override=None):
        return backend

    monkeypatch.setattr(llm_mod, "get_llm_backend", _fake_get)
    monkeypatch.setattr(agent_mod, "get_llm_backend", _fake_get)
    monkeypatch.setattr(Settings, "is_tier_available", lambda self, tier: True)
    return backend


def _serve(orch, port: int) -> JarvisWSServer:
    server = JarvisWSServer(orch, host="127.0.0.1", port=port)
    # Sprint 5: отключаем приветствие — тесты ожидают только ответы на свои
    # команды, а не стартовое greeting.
    server._settings.launcher.greeting_enabled = False
    server.start()
    return server


async def _collect(ws, seconds: float, until_end_count: int = 1):
    """Собирает сообщения до N end-событий или таймаута."""
    events = []
    ends = 0
    deadline = time.time() + seconds
    while time.time() < deadline and ends < until_end_count:
        try:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=0.5))
        except asyncio.TimeoutError:
            continue
        events.append(msg)
        if msg.get("type") == "event" and msg["event"]["type"] == "event:jarvis:end":
            ends += 1
    return events


def _jarvis_events(events):
    return [(m["event"]["type"], m["event"]["payload"]) for m in events
            if m.get("type") == "event" and m["event"]["type"].startswith("event:jarvis")]


# --------------------------------------------------------------------------- #
#  STEP 1 — стабильный correlation ID
# --------------------------------------------------------------------------- #

def test_sync_pair_single_stable_id():
    async def _run():
        server = _serve(SyncPairOrchestrator(), port=8805)
        try:
            async with websockets.connect("ws://127.0.0.1:8805") as ws:  # type: ignore
                await ws.recv()  # приветствие state:idle
                await ws.send(json.dumps({"type": "command", "text": "первый"}))
                ev1 = await _collect(ws, 5)
                await ws.send(json.dumps({"type": "command", "text": "второй"}))
                ev2 = await _collect(ws, 5)
            return ev1, ev2
        finally:
            server.shutdown()

    ev1, ev2 = asyncio.run(_run())
    j1, j2 = _jarvis_events(ev1), _jarvis_events(ev2)
    assert [t for t, _ in j1] == ["event:jarvis:start", "event:jarvis:end"], j1
    assert [t for t, _ in j2] == ["event:jarvis:start", "event:jarvis:end"], j2
    # ОДИН id на пару start/end (STEP 1: больше никакой timestamp-гонки)
    assert j1[0][1]["id"] == j1[1][1]["id"]
    assert j2[0][1]["id"] == j2[1][1]["id"]
    # разные логические ответы — разные id
    assert j1[0][1]["id"] != j2[0][1]["id"]
    assert j1[1][1]["content"] == "Ответ на: первый"
    assert j2[1][1]["content"] == "Ответ на: второй"


def test_parallel_commands_do_not_mix():
    async def _run():
        server = _serve(SyncPairOrchestrator(delay=0.4), port=8806)
        try:
            async with websockets.connect("ws://127.0.0.1:8806") as ws1, \
                       websockets.connect("ws://127.0.0.1:8806") as ws2:  # type: ignore
                await ws1.recv()
                await ws2.recv()
                await ws1.send(json.dumps({"type": "command", "text": "задача А"}))
                await ws2.send(json.dumps({"type": "command", "text": "задача Б"}))
                ev1 = await _collect(ws1, 6, until_end_count=2)  # broadcast: оба ответа
                ev2 = await _collect(ws2, 6, until_end_count=2)
            return ev1, ev2
        finally:
            server.shutdown()

    ev1, ev2 = asyncio.run(_run())
    for ev in (ev1, ev2):
        pairs = {}
        for etype, payload in _jarvis_events(ev):
            pairs.setdefault(payload["id"], []).append(etype)
        assert len(pairs) == 2, f"ожидали 2 независимых ответа, got {pairs}"
        for rid, kinds in pairs.items():
            assert kinds == ["event:jarvis:start", "event:jarvis:end"], (rid, kinds)
    ends = [p["content"] for t, p in _jarvis_events(ev1) if t == "event:jarvis:end"]
    assert sorted(ends) == ["Ответ на: задача А", "Ответ на: задача Б"]


# --------------------------------------------------------------------------- #
#  STEP 2 — реальный стриминг sync-пути
# --------------------------------------------------------------------------- #

def test_ws_streaming_sequence(stream_backend, settings):
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    server = _serve(AgentSinkOrchestrator(agent), port=8807)

    async def _run():
        try:
            async with websockets.connect("ws://127.0.0.1:8807") as ws:  # type: ignore
                await ws.recv()
                await ws.send(json.dumps({"type": "command", "text": "привет"}))
                return await _collect(ws, 8)
        finally:
            server.shutdown()

    events = asyncio.run(_run())
    seq = _jarvis_events(events)
    kinds = [t for t, _ in seq]
    assert kinds[0] == "event:jarvis:start", seq
    assert kinds[-1] == "event:jarvis:end", seq
    assert "event:jarvis:token" in kinds, f"стриминга нет: {seq}"
    # один логический ответ — один ID на всём протяжении
    ids = {p["id"] for _, p in seq}
    assert len(ids) == 1, ids
    rid = ids.pop()
    # токены кумулятивны и растут (не фейк-чанки, а нарастающий текст)
    tokens = [p["token"] for t, p in seq if t == "event:jarvis:token"]
    for prev, nxt in zip(tokens, tokens[1:]):
        assert nxt.startswith(prev), (prev, nxt)
    final = seq[-1][1]["content"]
    expected = "Сэр, я вас понял. Это тестовый ответ Джарвиса."
    assert final == expected, final
    assert tokens[-1] == expected, tokens[-1]
    assert rid  # uuid-подобный, не timestamp-зависимый


# --------------------------------------------------------------------------- #
#  STEP 2 (миссии) — длинные задачи стримятся с correlation = task_id
# --------------------------------------------------------------------------- #

def test_mission_streaming_via_task_events(stream_backend, settings):
    """Фоновая миссия (длинный вопрос) стримится: chunks -> end(task_id)."""
    orch = Orchestrator(settings)
    # The production WS gate now rejects commands while the local runtime is
    # genuinely starting.  This fixture supplies the explicit ready state
    # that the fake streaming backend represents.
    orch._warmup_diagnostics["state"] = "ready"
    orch._warmup_ready.set()
    server = JarvisWSServer(orch, host="127.0.0.1", port=8809)
    server._settings.launcher.greeting_enabled = False
    server.start()
    # Принуждаем фоновый путь: достаточно сложная, но НЕ research-цель
    # (research-конвейер — отдельный путь, вне Sprint 1).
    # Принуждаем фоновый путь: задача написания эссе (kind=mission).
    # Обоснование (R12): устраняем союз "и", который ложно триггерил split_compound_commands.
    goal = "Напиши развёрнутое эссе о влиянии паровых машин на промышленность девятнадцатого века"

    async def _run():
        try:
            async with websockets.connect("ws://127.0.0.1:8809") as ws:  # type: ignore
                await ws.recv()
                await ws.send(json.dumps({"type": "command", "text": goal}))
                # ACK миссии — тоже start/end пара; ждём конец, у которого
                # были токены с тем же id (т.е. реальный стрим миссии).
                events = []
                token_ids = set()
                deadline = time.time() + 30
                while time.time() < deadline:
                    try:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=0.5))
                    except asyncio.TimeoutError:
                        continue
                    events.append(msg)
                    if msg.get("type") != "event":
                        continue
                    ev = msg["event"]
                    if ev["type"] == "event:jarvis:token":
                        token_ids.add(ev["payload"]["id"])
                    elif ev["type"] == "event:jarvis:end" and ev["payload"]["id"] in token_ids:
                        break
                # Отсекаем предварительный ACK миссии, оставляя последовательность стрима задачи (R12)
                return [m for m in events if m.get("type") == "event" and m["event"]["payload"].get("id") in token_ids]
        finally:
            server.shutdown()

    events = asyncio.run(_run())
    seq = _jarvis_events(events)
    kinds = [t for t, _ in seq]
    assert "event:jarvis:token" in kinds, f"миссия не стримится: {seq}"
    assert kinds[-1] == "event:jarvis:end", seq
    # вся последовательность — один task_id
    ids = {p["id"] for _, p in seq}
    assert len(ids) == 1, ids
    # финальный текст закрыл пузырь тем же id (не дубль-карточкой)
    final = seq[-1][1]["content"]
    expected = "Сэр, я вас понял. Это тестовый ответ Джарвиса."
    assert final == expected, final


# --------------------------------------------------------------------------- #
#  STEP 5 — сбой провайдера: быстро, дружелюбно, пузырь закрыт
# --------------------------------------------------------------------------- #

def test_provider_failure_friendly_and_fast(dead_backend, settings):
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    server = _serve(AgentSinkOrchestrator(agent), port=8808)

    async def _run():
        try:
            async with websockets.connect("ws://127.0.0.1:8808") as ws:  # type: ignore
                await ws.recv()
                t0 = time.time()
                await ws.send(json.dumps({"type": "command", "text": "привет"}))
                events = await _collect(ws, 10)
                return events, time.time() - t0
        finally:
            server.shutdown()

    events, elapsed = asyncio.run(_run())
    seq = _jarvis_events(events)
    ends = [p["content"] for t, p in seq if t == "event:jarvis:end"]
    assert ends and ends[0].startswith(MODEL_UNAVAILABLE_TEXT), ends
    # сбой провайдера признётся быстро, а не 45-секундным ожиданием
    assert elapsed < 5.0, f"долгий отказ: {elapsed:.1f}с"
    # сырые HTTP-детали не утекают в чат
    assert any("HTTP 408" in c for c in ends)
