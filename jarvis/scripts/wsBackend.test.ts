import assert from 'node:assert/strict';

import type { AttachedFile, BackendEvent } from '../src/types/index.ts';

type Handler<T> = ((event: T) => void) | null;

class FakeSocket {
  static instances: FakeSocket[] = [];

  readyState = 0;
  sent: string[] = [];
  onopen: Handler<Event> = null;
  onclose: Handler<CloseEvent> = null;
  onerror: Handler<Event> = null;
  onmessage: Handler<MessageEvent<string>> = null;
  readonly url: string;

  constructor(url: string) {
    this.url = url;
    FakeSocket.instances.push(this);
  }

  open() {
    this.readyState = 1;
    this.onopen?.({} as Event);
  }

  receive(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent<string>);
  }

  send(payload: string) {
    this.sent.push(payload);
  }

  close() {
    this.readyState = 3;
    this.onclose?.({} as CloseEvent);
  }
}

const { WebSocketBackend } = await import('../src/integrations/wsBackend.ts');

// S2: токен выдаёт лаунчер (в приложении — через invoke("ws_auth_token")).
// Провайдер асинхронный намеренно: тест проверяет, что кадр auth уходит первым
// даже когда токен приходит позже открытия сокета.
const transport = new WebSocketBackend(
  'ws://127.0.0.1:8771',
  (url) => new FakeSocket(url) as unknown as WebSocket,
  async () => {
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    return 'test-ws-token';
  },
);
const events: BackendEvent[] = [];
const unsubscribe = transport.subscribeToEvents((event) => events.push(event));
const socket = FakeSocket.instances[0];
assert.equal(socket.url, 'ws://127.0.0.1:8771');
// Токен не может оказаться в URL: он уходит только кадром auth.
assert.equal(socket.url.includes('token'), false);
socket.open();

// Команда отправлена сразу после open, не дожидаясь токена: она обязана
// оказаться ПОСЛЕ кадра аутентификации.
await transport.sendCommand('проверочная команда', [] as AttachedFile[]);
assert.deepEqual(JSON.parse(socket.sent[0]), { type: 'auth', token: 'test-ws-token' });
assert.deepEqual(JSON.parse(socket.sent[1]), { type: 'command', text: 'проверочная команда' });
assert.equal(events[0].type, 'event:command');

socket.receive({ type: 'state', state: 'thinking' });
socket.receive({
  type: 'event',
  event: {
    type: 'event:jarvis:end',
    payload: { id: 'answer-1', kind: 'jarvis', content: 'Реальный ответ' },
    timestamp: 42,
  },
});
socket.receive({
  type: 'confirmation_required',
  confirmation_id: 'confirm-1',
  prompt: 'Подтвердите действие',
  tool: 'filesystem.delete',
  risk: { level: 'high' },
});
assert.deepEqual(events.slice(1).map((event) => event.type), [
  'state:thinking',
  'event:jarvis:end',
  'confirmation:required',
]);

const save = transport.updateCloudSettings({
  provider: 'openrouter',
  base_url: 'https://openrouter.ai/api/v1',
  model: 'openai/gpt-4.1-mini',
  api_key: 'test-key-only',
});
await new Promise<void>((resolve) => setTimeout(resolve, 0));
assert.deepEqual(JSON.parse(socket.sent[2]), {
  type: 'settings:update',
  settings: {
    provider: 'openrouter',
    base_url: 'https://openrouter.ai/api/v1',
    model: 'openai/gpt-4.1-mini',
    api_key: 'test-key-only',
  },
});
socket.receive({
  type: 'settings:saved',
  ok: true,
  settings: {
    provider: 'openrouter',
    base_url: 'https://openrouter.ai/api/v1',
    model: 'openai/gpt-4.1-mini',
    has_api_key: true,
    api_key_masked: '••••only',
  },
});
assert.deepEqual(await save, {
  provider: 'openrouter',
  base_url: 'https://openrouter.ai/api/v1',
  model: 'openai/gpt-4.1-mini',
  has_api_key: true,
  api_key_masked: '••••only',
});
assert.equal(JSON.stringify(events).includes('test-key-only'), false);
// Токен не утекает ни в ленту событий, ни в payload настроек.
assert.equal(JSON.stringify(events).includes('test-ws-token'), false);

await transport.answerConfirmation('confirm-1', false);
assert.deepEqual(JSON.parse(socket.sent[3]), {
  type: 'confirm',
  confirmation_id: 'confirm-1',
  approve: false,
});

// S3: отказ не несёт объём — разрешать нечего.
await transport.answerConfirmation('screen-1', false, 'session');
assert.deepEqual(JSON.parse(socket.sent[4]), {
  type: 'confirm',
  confirmation_id: 'screen-1',
  approve: false,
});

// S3: выбранный человеком объём уходит на сервер вместе с согласием.
await transport.answerConfirmation('screen-2', true, 'session');
assert.deepEqual(JSON.parse(socket.sent[5]), {
  type: 'confirm',
  confirmation_id: 'screen-2',
  approve: true,
  scope: 'session',
});

// Без выбора объёма поле scope не подставляется клиентом.
await transport.answerConfirmation('confirm-3', true);
assert.deepEqual(JSON.parse(socket.sent[6]), {
  type: 'confirm',
  confirmation_id: 'confirm-3',
  approve: true,
});

await transport.hotkeyPressed();
assert.deepEqual(JSON.parse(socket.sent[7]), { type: 'hotkey_pressed' });

// Кадр auth отправлен ровно один раз за соединение.
assert.equal(socket.sent.filter((raw) => JSON.parse(raw).type === 'auth').length, 1);

unsubscribe();

// ---------------------------------------------------------------------------
// S2 (dev): токена нет (браузер без Tauri) — клиент НЕ притворяется
// авторизованным и не отправляет пустой auth. Решение остаётся за сервером:
// он либо запущен с JARVIS_WS_DEV_NOAUTH=1, либо закроет соединение.
// ---------------------------------------------------------------------------
const devTransport = new WebSocketBackend(
  'ws://127.0.0.1:8771',
  (url) => new FakeSocket(url) as unknown as WebSocket,
  async () => null,
);
const devUnsubscribe = devTransport.subscribeToEvents(() => {});
const devSocket = FakeSocket.instances[1];
devSocket.open();
await devTransport.hotkeyPressed();
assert.deepEqual(JSON.parse(devSocket.sent[0]), { type: 'hotkey_pressed' });
assert.equal(devSocket.sent.some((raw) => JSON.parse(raw).type === 'auth'), false);
devUnsubscribe();

console.log('wsBackend: auth-first frame, command, response, confirmation and masked settings passed');
