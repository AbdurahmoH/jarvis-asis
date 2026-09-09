# Cloud-only сборка: что отключено и как вернуть

Первая приватная релизная версия (2–3 доверенных пользователя) идёт без
локальной LLM: мозг — DeepSeek-V4-Flash на DeepInfra, ключ предустановлен.
Локальными остаются голос (Piper TTS, faster-whisper STT, wake word),
ChromaDB и sentence-transformer роутера — они не LLM.

## Что отключено флагом

`config/settings.py`: `local_llm_enabled: bool = False`.

| Механизм | Поведение при False |
|---|---|
| `core/llm/factory._build_backend` (провайдер `local`) | `BackendConfigError` с подсказкой про флаг |
| `Settings.is_tier_available` для локальных тиров | всегда False, даже если GGUF лежит на диске |
| `core/brain/bootstrap` (регистрация `LocalGGUFProvider`) | локальный провайдер в реестр мозга не попадает |
| Warmup оркестратора | локальная модель не греется, GGUF не скачивается (`auto_download_models` не используется) |

Файлы `core/llm/local_qwen.py` и `core/llm/llama_server.py` **остаются в
репозитории** и возвращаются в pro-версии.

## Как включить локальный мозг обратно (pro-версия)

Одна строка в `config/settings.json`:

```json
{ "local_llm_enabled": true }
```

(плюс, если нужен офлайн-режим целиком: `offline_mode: true`).

Пропущенные тесты локального пути (запускаются при True):

* `tests/test_p1_sprint.py::test_p1_no_local_heavy_escalation`
* `tests/test_sprint9.py::test_offline_coder_keeps_task_role_with_best_local_model`

## Ключ поставки (C5)

1. Сборщик кладёт ключ в `config/secrets.local.json` в корне проекта:
   `{"deepinfra_api_key": "<key>"}`. Без этого файла (или без env
   `JARVIS_BUILD_DEEPINFRA_API_KEY` / `DEEPINFRA_API_KEY`) сборка инсталлятора
   падает: `secrets.local.json missing — create it before packaging`.
2. Упаковщик (`scripts/package_local_runtime.py`) превращает ключ в
   DPAPI-блоб `data/brain/provider-secrets.dpapi` внутри пакета и кладёт копию
   `config/secrets.local.json` как запасной бутстрап.
3. При первом запуске приложения `bootstrap_from_local_secrets` переносит
   ключ из запасного файла в локальный DPAPI и **удаляет файл**.

`config/secrets.local.json`, `*.env` и `data/brain/provider-secrets.dpapi`
закрыты в `.gitignore`.

## Диагностика провайдера (C3)

Правда о провайдере видна снаружи — в `runtime_diagnostics()` и в WS
`runtime_status`:

```json
"provider_effective": {
  "tier": "FAST",
  "source": "env | settings.json | dpapi | missing",
  "reachable": true,
  "last_probe": "2026-09-06T20:00:00+00:00",
  "circuit": "closed | open | half_open"
}
```

Probe — раз в 60 с (daemon-поток `ProviderProbe`) и перед первым запросом
после простоя > 5 мин. Circuit breaker: 3 отказа/таймаута за 60 с → open на
90 с → одна half-open проба → закрытие при успехе.
