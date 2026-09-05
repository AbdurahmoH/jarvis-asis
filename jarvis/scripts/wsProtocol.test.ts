import assert from 'node:assert/strict';

import { mapSocketEnvelope } from '../src/integrations/wsProtocol.ts';

const now = 1_725_000_000_000;

assert.deepEqual(
  mapSocketEnvelope({ type: 'state', state: 'thinking' }, now),
  [{ type: 'state:thinking', payload: null, timestamp: now }],
);

assert.deepEqual(
  mapSocketEnvelope({
    type: 'event',
    event: {
      type: 'event:jarvis:end',
      payload: { id: 'answer-1', kind: 'jarvis', content: 'Реальный ответ' },
      timestamp: 42,
    },
  }, now),
  [{
    type: 'event:jarvis:end',
    payload: { id: 'answer-1', kind: 'jarvis', content: 'Реальный ответ' },
    timestamp: 42,
  }],
);

assert.deepEqual(
  mapSocketEnvelope({ type: 'runtime_status', state: 'loading_model', ready: false, diagnostics: { backend: 'cpu' } }, now),
  [{ type: 'runtime:status', payload: { state: 'loading_model', ready: false, diagnostics: { backend: 'cpu' } }, timestamp: now }],
);

assert.deepEqual(
  mapSocketEnvelope({
    type: 'confirmation_required',
    confirmation_id: 'confirm-1',
    prompt: 'Подтвердите действие',
    tool: 'filesystem.delete',
    risk: { level: 'high' },
  }, now),
  [{
    type: 'confirmation:required',
    payload: {
      confirmationId: 'confirm-1',
      prompt: 'Подтвердите действие',
      tool: 'filesystem.delete',
      risk: { level: 'high' },
      // S3: подтверждение без выдачи полномочия приходит без scopes —
      // клиент получает пустой список, а не выдуманный «once».
      scopes: [],
    },
    timestamp: now,
  }],
);

// S3: объёмы полномочия приходят от сервера и доходят до UI как есть.
assert.deepEqual(
  mapSocketEnvelope({
    type: 'confirmation_required',
    confirmation_id: 'screen-1',
    prompt: 'Разрешить снимок экрана?',
    tool: 'screen_capture',
    risk: { level: 'medium' },
    scopes: ['once', 'session', 'permanent', 42],
  }, now),
  [{
    type: 'confirmation:required',
    payload: {
      confirmationId: 'screen-1',
      prompt: 'Разрешить снимок экрана?',
      tool: 'screen_capture',
      risk: { level: 'medium' },
      scopes: ['once', 'session', 'permanent'],
    },
    timestamp: now,
  }],
);

assert.deepEqual(mapSocketEnvelope({ type: 'unknown' }, now), []);

console.log('wsProtocol: 6 assertions passed');
