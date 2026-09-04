from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import threading
import pytest

from config.settings import Settings, load_config
from core.agent import Agent, AgentConfig
from core.actions.base import ActionResult
from core.actions.media import play_music
from core.orchestrator import Orchestrator
from core.ws_server import JarvisWSServer
from core.agent import pick_acknowledgement
from persona.system_prompt import build_agent_system_prompt, build_system_prompt
from core.voice import AssistantOutput, TTSQueue
from core.verifier import verify_action_result
from core.safety import assess_risk


def test_fresh_install_is_silent_by_default() -> None:
    assert Settings().launcher.greeting_enabled is False
    production = load_config(Path(__file__).resolve().parents[2] / "config" / "settings.json")
    assert production.launcher.greeting_enabled is False


def test_cloud_conversation_prompt_does_not_ask_for_identity_or_force_honorific(
    settings, fake_backend,
) -> None:
    settings.deepseek_brain_mode = True
    settings.persona.address = ""
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))

    prompt = agent._build_conversation_prompt("", "привет, как дела?", fake_backend, compact=True)

    lowered = prompt.casefold()
    assert "спроси его" not in lowered
    assert "обращение: «сэр»" not in lowered
    assert "имя пользователя пока неизвестно" not in lowered
    assert "как его зовут" not in build_agent_system_prompt(settings).casefold()
    assert "обращайся «сёр»" not in build_system_prompt(settings).casefold()
    assert "сэр" not in pick_acknowledgement("media")


def test_currency_request_uses_fresh_public_data_not_model_memory(settings, fake_backend) -> None:
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    with patch("core.actions.public_data.cbr_currency_rates", return_value={
        "date": "2026-09-04T11:30:00+03:00",
        "rates": {
            "USD": {"rate": 86.89, "name": "Доллар США", "nominal": 1, "raw_value": 86.8872},
            "EUR": {"rate": 100.60, "name": "Евро", "nominal": 1, "raw_value": 100.598},
            "KZT": {"rate": 0.19, "name": "Казахстанских тенге", "nominal": 100, "raw_value": 19.0793},
            "CNY": {"rate": 12.92, "name": "Китайский юань", "nominal": 1, "raw_value": 12.9191},
        },
    }):
        outcome = agent.execute("курс доллара сегодня")

    assert outcome.tool_used == "public_data"
    assert outcome.verified is True
    assert outcome.action_result is not None
    assert outcome.action_result.args["kind"] == "currency"
    assert outcome.action_result.output["value"] == 86.89
    assert outcome.action_result.output["source"] == "cbr.ru"
    assert "2026-09-04" in outcome.action_result.output["fetched_at"]
    assert fake_backend.calls == []
    assert "USD" in outcome.text
    assert "86.89" in outcome.text
    assert "₽" in outcome.text


def test_currency_request_fallback_to_open_er_api(settings, fake_backend) -> None:
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    with patch("core.actions.public_data.cbr_currency_rates", return_value=None), \
         patch("core.actions.public_data.currency_rates", return_value={
             "USD": 0.0125, "EUR": 0.0115, "KZT": 5.8, "CNY": 0.09,
         }):
        outcome = agent.execute("курс доллара сегодня")

    assert outcome.tool_used == "public_data"
    assert outcome.verified is True
    assert outcome.action_result is not None
    assert outcome.action_result.args["kind"] == "currency"
    assert outcome.action_result.output["rates"]["USD"] == 0.0125
    assert outcome.action_result.output["source"] == "open.er-api.com"
    assert fake_backend.calls == []
    assert "USD" in outcome.text
    assert "₽" in outcome.text


def test_verified_fast_action_survives_cloud_finalizer_outage(settings, fake_backend) -> None:
    settings.deepseek_brain_mode = True
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    with patch.object(agent, "_finalize_tool_response", return_value="") as finalizer:
        outcome = agent.execute("который сейчас час")

    assert outcome.verified is True
    assert "Ошибка DeepInfra" not in outcome.text
    finalizer.assert_not_called()


@pytest.mark.parametrize("provider_failure", [
    Exception("DeepSeek provider error: 503 Service Unavailable"),
    TimeoutError("DeepSeek cloud gateway timed out after 30s"),
    "",
])
def test_fast_action_survives_all_provider_failure_modes(settings, fake_backend, provider_failure) -> None:
    settings.deepseek_brain_mode = True
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))

    patch_kwargs = (
        {"side_effect": provider_failure}
        if isinstance(provider_failure, Exception)
        else {"return_value": provider_failure}
    )

    # 1. Fast path execution: does not call finalizer and gives verified sensible response
    with patch.object(agent, "_finalize_tool_response", **patch_kwargs):
        outcome = agent.execute("который сейчас час")

    assert outcome.verified is True
    assert outcome.tool_used == "current_time"
    assert outcome.text and "Ошибка DeepInfra" not in outcome.text
    assert any(ch.isdigit() for ch in outcome.text)

    # 2. When finalizer is explicitly invoked on a verified tool result (e.g. non-fast path),
    # provider failure/timeout/empty does NOT turn verified=True into False or error
    with patch.object(agent, "_finalize_tool_response", **patch_kwargs):
        action_outcome = agent._execute_verified(
            goal="который сейчас час",
            tool="current_time",
            args={},
            mission=None,
            cancel=threading.Event(),
            trace=[],
            risk=assess_risk("который сейчас час"),
            caps=[],
            finalize_response=True,
        )

    assert action_outcome.verified is True
    assert action_outcome.tool_used == "current_time"
    assert action_outcome.text and "Ошибка DeepInfra" not in action_outcome.text
    assert "Сейчас" in action_outcome.text


def test_generic_music_defaults_to_online_youtube_and_opens_direct_content(
    settings, fake_backend,
) -> None:
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    with patch("core.actions.media.duckduckgo_search", return_value=[{
        "title": "Track",
        "url": "https://www.youtube.com/watch?v=fixture123",
        "snippet": "",
    }]), patch("core.actions.media._open_target", return_value=True), \
         patch("core.verifier._active_audio_sessions", return_value=["chrome.exe"]):
        outcome = agent.execute("поставь музыку")

    assert outcome.tool_used == "play_music"
    assert outcome.action_result is not None
    assert outcome.action_result.args["source"] == "youtube"
    assert outcome.action_result.args["allow_network"] is True
    assert outcome.action_result.output["stage"] == "video_opened"
    assert "youtube.com/watch" in outcome.action_result.output["url"]


def test_youtube_fallback_extracts_first_video_instead_of_stopping_at_search(
    settings, fake_backend,
) -> None:
    class Response:
        text = '{"videoId":"directFixture123"}'

        @staticmethod
        def raise_for_status() -> None:
            return None

    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    with patch("core.actions.media.duckduckgo_search", return_value=[]), \
         patch("core.actions.media.safe_http_get", return_value=Response()), \
         patch("core.actions.media._open_target", return_value=True), \
         patch("core.verifier._active_audio_sessions", return_value=["msedge.exe"]):
        outcome = agent.execute("поставь Imagine Dragons на ютубе")

    assert outcome.verified is True
    assert outcome.action_result is not None
    assert outcome.action_result.output["stage"] == "video_opened"
    assert outcome.action_result.output["url"].endswith("directFixture123")


def test_assistant_choice_words_never_become_the_search_query(settings, fake_backend) -> None:
    settings.deepseek_brain_mode = True
    fake_backend.set_answer("Мумий Тролль — Владивосток 2000")
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    with patch.object(agent, "_backend_for_routing", return_value=(fake_backend, None)), \
         patch("core.actions.media.duckduckgo_search", return_value=[{
        "title": "Track",
        "url": "https://www.youtube.com/watch?v=fixture456",
        "snippet": "",
    }]), patch("core.actions.media._open_target", return_value=True), \
         patch("core.verifier._active_audio_sessions", return_value=["chrome.exe"]):
        outcome = agent.execute("поставь музыку на ютубе. На свой вкус")

    query = str(outcome.action_result.args["query"]).casefold()
    assert "на свой вкус" not in query
    assert query == "мумий тролль — владивосток 2000"


def test_explicit_local_music_stays_local() -> None:
    with patch("core.actions.media._open_default_player", return_value=True), \
         patch("core.actions.media._request_playback", return_value=True), \
         patch("core.actions.media.duckduckgo_search") as search:
        result = play_music(source="local")

    assert result.ok is True
    assert result.args["source"] == "local"
    search.assert_not_called()


def test_deepseek_runtime_is_ready_without_local_model_warmup(settings) -> None:
    settings.deepseek_brain_mode = True
    orchestrator = Orchestrator(settings, output_callback=lambda _text: None)
    try:
        # Контракт готовности (2026-09-05): ready только после прогрева
        # semantic-роутера (шаг 6). start() делает это до объявления
        # готовности; здесь воспроизводим прогретый runtime без локальной
        # модели.
        orchestrator._warmup_router()
        server = JarvisWSServer(orchestrator)
        payload = server._runtime_status_payload()
    finally:
        orchestrator.shutdown()

    assert payload["state"] == "ready"
    assert payload["ready"] is True
    assert payload["router"]["ready"] is True


def test_conversation_latency_trace_records_first_token(settings, fake_backend) -> None:
    settings.offline_mode = True
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    agent.install_stream_sink(lambda _text: None)

    outcome = agent.execute("привет, как дела?")

    stages = outcome.latency_stages
    for name in (
        "input_received", "route_complete", "context_complete",
        "llm_request", "llm_first_token", "llm_complete", "response_ready",
    ):
        assert name in stages
    assert list(stages.values()) == sorted(stages.values())


def test_action_latency_trace_records_tool_and_verification(settings, fake_backend) -> None:
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    with patch("core.actions.media.duckduckgo_search", return_value=[{
        "title": "Track", "url": "https://www.youtube.com/watch?v=fixture", "snippet": "",
    }]), patch("core.actions.media._open_target", return_value=True), \
         patch("core.verifier._active_audio_sessions", return_value=["chrome.exe"]):
        outcome = agent.execute("поставь музыку")

    stages = outcome.latency_stages
    assert stages["tool_start"] <= stages["tool_end"] <= stages["verification_complete"]


def test_tts_queue_records_enqueue_and_first_audio_start() -> None:
    class Engine:
        def speak_rendered(self, _rendered) -> None:
            return None

        def stop_speaking(self) -> None:
            return None

    queue = TTSQueue(Engine())
    queue.start()
    try:
        assert queue.add_output(AssistantOutput.natural("Проверка звука")) is True
        assert queue.wait_until_done(timeout=1.0) is True
        telemetry = queue.last_telemetry
    finally:
        queue.stop()

    assert telemetry["enqueued_at_ms"] <= telemetry["first_audio_at_ms"]


def test_finalizer_dropping_currency_fact_is_repaired_deterministically(settings, fake_backend) -> None:
    settings.deepseek_brain_mode = True
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    # Finalizer distorts the verified fact by omitting number and currency symbol
    with patch("core.actions.public_data.cbr_currency_rates", return_value=None), \
         patch("core.actions.public_data.currency_rates", return_value={"USD": 0.0125}), \
         patch.object(agent, "_finalize_tool_response", return_value="Я проверил информацию для вас."):
        outcome = agent._execute_verified(
            goal="курс доллара сегодня",
            tool="public_data",
            args={"kind": "currency"},
            mission=None,
            cancel=threading.Event(),
            trace=[],
            risk=assess_risk("курс доллара сегодня", "public_data", {"kind": "currency"}),
            caps=[],
            finalize_response=True,
        )

    assert outcome.verified is True
    assert "finalizer_dropped_fact" in outcome.trace
    assert "80" in outcome.text
    assert "USD" in outcome.text
    assert "₽" in outcome.text


def test_finalizer_dropping_weather_fact_is_repaired_deterministically(settings, fake_backend) -> None:
    settings.deepseek_brain_mode = True
    agent = Agent(settings, config=AgentConfig(enable_skill_forge=False))
    fake_weather = {
        "location": {"name": "Москва", "lat": 55.75, "lon": 37.62},
        "current": {"temp": 18.5, "weather": "ясно", "wind": 5.0, "humidity": 60},
        "forecast": [],
    }
    # Finalizer hallucinates generic pleasant response omitting numeric temperature
    with patch("core.actions.weather.get_weather", return_value=fake_weather), \
         patch.object(agent, "_finalize_tool_response", return_value="Погода сегодня отличная и комфортная."):
        outcome = agent._execute_verified(
            goal="какая погода в москве",
            tool="weather",
            args={"forecast_days": 1},
            mission=None,
            cancel=threading.Event(),
            trace=[],
            risk=assess_risk("какая погода в москве", "weather", {"forecast_days": 1}),
            caps=[],
            finalize_response=True,
        )

    assert outcome.verified is True
    assert "finalizer_dropped_fact" in outcome.trace
    assert "18.5" in outcome.text
    assert "°C" in outcome.text
