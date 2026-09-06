# Testing — заметки (до запуска T1)

Этот файл — черновик заметок между merge security-hardening и запуском задачи T1
(тестовая инфраструктура). T1 ведёт свой журнал здесь же; нижеследующее — входные
данные для него и для Блока A, не законченная работа.

## Идея на Блок A: `provider_effective` в runtime_diagnostics / WS-статус

Проблема «работает ли облако» была невидима снаружи около месяца: пользователь
видел ответы локальной модели, считая, что говорит с облаком. Предложение —
явное поле диагностики:

```json
"provider_effective": {
  "tier": "FAST",
  "source": "dpapi" | "env" | "settings.json" | "missing",
  "reachable": true,
  "last_probe": "2026-09-06T18:00:00+00:00"
}
```

`reachable` — результат периодического лёгкого probe (дешёвый запрос статуса
провайдера, не генерация), `source` — откуда реально взят ключ. Поле должно
попасть в `runtime_diagnostics()` и в WS `runtime_status`, чтобы и UI, и
прогонные скрипты видели правду о провайдере.

## Найдено попутно: провод DPAPI ↔ tiers оборван (не чинить здесь — задача Блока A)

Ключ deepinfra лежит в DPAPI-хранилище и жив (проверено 2026-09-06: запрос
`GET /v1/openai/models` → HTTP 200), но цепочка доступности тира его не видит:

* ключ хранится: `data/brain/provider-secrets.dpapi`, конфиг —
  `config/settings.py:659` (`CredentialStoreConfig`, reference
  `DEEPINFRA_API_KEY`);
* чтение из DPAPI есть только в провайдерах мозга: `core/brain/providers.py:105-106`
  (и анклав Anthropic `:329-330`) — `self._secret_store.get(self.config.api_key_ref)`;
* а доступность тира решает `config/settings.py:731` `get_api_key()`: порядок —
  env `JARVIS_<P>_API_KEY` / `<P>_API_KEY` → `settings.json api_keys.<P>`;
  DPAPI-хранилище в цепочке отсутствует;
* следствие: `config/settings.py:806` (`is_tier_available` → `get_api_key`) даёт
  `False` → `ModelRouter.is_llm_available() = False` → роутер и чат живут без
  облака, `settings.json api_keys.deepinfra` при этом пуст, т.е. чинить нечего
  в конфиге — надо либо читить DPAPI в `get_api_key`, либо писать ключ в
  settings.json при установке.

Обход, использованный в прогоне фраз (2026-09-06): драйвер поднимает ключ из
DPAPI в `os.environ["DEEPINFRA_API_KEY"]` до создания Orchestrator'а. Артефакты:
`artifacts/phrase_run_nocloud.jsonl` (runtime default — без облака),
`artifacts/phrase_run_online.jsonl` (cloud forced), офлайн-прогон R5 — следующий.
