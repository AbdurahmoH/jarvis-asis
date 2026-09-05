"""WS-события маршрутизации: route + runtime_status (R9, R10).

R10: каждое обращение сопровождается WS-событием route с полной телеметрией
решения роутера: {tier, kind, tool, confidence, margin, top3, risk,
needs_confirmation, latency_ms, llm_available}.

R9: readiness объявляется только с прогретым semantic-роутером; провайдер —
отдельное поле runtime_status, не выводится из режима.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
import websockets  # type: ignore

from core.memory.short_term import SessionManager
from core.orchestrator import Orchestrator
from core.ws_server import JarvisWSServer

DECISION_FIELDS = (
    "tier", "kind", "tool", "confidence", "margin", "top3",
    "risk", "needs_confirmation", "latency_ms", "llm_available",
)


def _make_ready_orchestrator(settings, port: int) -> JarvisWSServer:
    """Реальный Orchestrator с прогретым роутером и лёгкой сессией."""
    orch = Orchestrator(settings)
    orch._warmup_diagnostics["state"] = "ready"
    orch._warmup_ready.set()
    orch._router_diagnostics = {"ready": True, "warmup_ms": 1.0, "probe_ms": 1.0}
    orch._running = True
    orch._session = SessionManager(max_size=20)
    server = JarvisWSServer(orch, host="127.0.0.1", port=port)
    server._settings.launcher.greeting_enabled = False
    return server


def test_ws_route_event_contains_full_decision(settings):
    """«статус системы» -> WS-событие route с полным решением роутера."""
    async def _run():
        server = _make_ready_orchestrator(settings, 8812)
        server.start()
        try:
            async with websockets.connect("ws://127.0.0.1:8812") as ws:  # type: ignore
                idle = json.loads(await ws.recv())
                assert idle["type"] == "state", idle
                status = json.loads(await ws.recv())
                assert status["type"] == "runtime_status", status
                # R9: ready объявлен только вместе с готовым роутером,
                # провайдер — отдельное поле.
                assert status["ready"] is True
                assert status["router"]["ready"] is True
                assert "provider" in status

                await ws.send(json.dumps({"type": "command", "text": "статус системы"}))
                deadline = time.time() + 20
                route_event = None
                while time.time() < deadline:
                    try:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
                    except asyncio.TimeoutError:
                        continue
                    if msg.get("type") == "route":
                        route_event = msg
                        break
                return route_event
        finally:
            server.shutdown()

    route_event = asyncio.run(_run())
    assert route_event is not None, "WS-событие route не получено"
    assert route_event.get("route"), route_event
    decision = route_event.get("decision") or {}
    for field in DECISION_FIELDS:
        assert field in decision, f"в route.decision нет поля {field}: {decision}"
    assert decision["kind"] == "action"
    assert decision["tool"] == "system_status"
    assert decision["tier"] in ("shortcut", "semantic", "pattern")
    assert decision["llm_available"] is False
    assert isinstance(decision["latency_ms"], (int, float))


def test_ws_route_event_carries_top3_and_margin(settings):
    """В route.decision входят top3-кандидаты и margin из trace роутера."""
    async def _run():
        server = _make_ready_orchestrator(settings, 8813)
        server.start()
        try:
            async with websockets.connect("ws://127.0.0.1:8813") as ws:  # type: ignore
                await ws.recv()  # state idle
                await ws.recv()  # runtime_status
                await ws.send(json.dumps({"type": "command", "text": "открой блокнот"}))
                deadline = time.time() + 20
                while time.time() < deadline:
                    try:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
                    except asyncio.TimeoutError:
                        continue
                    if msg.get("type") == "route":
                        return msg
                return None
        finally:
            server.shutdown()

    route_event = asyncio.run(_run())
    assert route_event is not None, "WS-событие route не получено"
    decision = route_event["decision"]
    assert decision["tool"] == "open_app"
    assert isinstance(decision["top3"], list) and decision["top3"], decision
    assert isinstance(decision["margin"], (int, float))
