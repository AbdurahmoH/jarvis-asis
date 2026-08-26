from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from config.settings import Settings, load_config
from core.agent import Agent, AgentConfig
from core.actions.base import ActionResult
from core.actions.media import play_music
from core.orchestrator import Orchestrator
from core.ws_server import JarvisWSServer
from core.agent import pick_acknowledgement
from persona.system_prompt import build_agent_system_prompt, build_system_prompt
from core.voice import AssistantOutput, TTSQueue


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
    with patch("core.actions.public_data.currency_rates", return_value={
        "USD": 0.0125, "EUR": 0.0115, "KZT": 5.8, "CNY": 0.09,
    }):
        outcome = agent.execute("курс доллара сегодня")

    assert outcome.tool_used == "public_data"
    assert outcome.verified is True
    assert outcome.action_result is not None
    assert outcome.action_result.args["kind"] == "currency"
    assert outcome.action_result.output["rates"]["USD"] == 0.0125
    assert fake_backend.calls == []


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
        server = JarvisWSServer(orchestrator)
        payload = server._runtime_status_payload()
    finally:
        orchestrator.shutdown()

    assert payload["state"] == "ready"
    assert payload["ready"] is True


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
