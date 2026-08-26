import assert from 'node:assert/strict';
import type { BackendEvent } from '../src/types/index.ts';
import {
  confirmationFromEvent, createMission, fixtureMission, reduceMission, reducePresenceStream,
  visibleResponseTiming,
} from '../src/operator/model.ts';

const mission = createMission('  Установи   тестовую программу  ', 100);
assert.equal(mission.title, 'Установи тестовую программу');
assert.equal(mission.phase, 'research');
assert.deepEqual(mission.steps.map((step) => step.state), ['active', 'pending', 'pending', 'pending']);

const executing: BackendEvent = { type: 'state:executing', payload: null, timestamp: 101 };
const downloading = reduceMission(mission, executing);
assert.equal(downloading.phase, 'download');
assert.deepEqual(downloading.steps.map((step) => step.state), ['complete', 'active', 'pending', 'pending']);

const unverifiedResult = reduceMission(downloading, {
  type: 'event:result', payload: { success: true, result: 'installer exited with code 0' }, timestamp: 102,
});
assert.equal(unverifiedResult.phase, 'observe');
assert.equal(unverifiedResult.verified, false, 'success/exit code must not become verified UI success');

const verifiedResult = reduceMission(downloading, {
  type: 'event:result', payload: { success: true, verification: { status: 'verified' }, result: 'desired state observed' }, timestamp: 103,
});
assert.equal(verifiedResult.phase, 'verified');
assert.equal(verifiedResult.verified, true);
assert.deepEqual(verifiedResult.steps.map((step) => step.state), ['complete', 'complete', 'complete', 'complete']);

const confirmation = confirmationFromEvent({
  type: 'confirmation:required',
  payload: { confirmationId: 'grant-1', prompt: 'Разрешить установку?', tool: 'software.install', risk: { level: 'high' } },
  timestamp: 104,
});
assert.equal(confirmation?.id, 'grant-1');
assert.equal(confirmation?.risk.level, 'high');

assert.equal(fixtureMission('verified').evidence[0].value, 'VERIFIED');

const emptyStart = reducePresenceStream([], {
  type: 'event:jarvis:start', payload: { id: 'stream-1' }, timestamp: 200,
});
assert.deepEqual(emptyStart, [], 'stream start is typing state, not a persistent blank message');

const firstToken = reducePresenceStream(emptyStart, {
  type: 'event:jarvis:token', payload: { id: 'stream-1', token: 'Привет' }, timestamp: 201,
});
assert.deepEqual(firstToken.map((message) => message.text), ['Привет']);

const cancelledBeforeText = reducePresenceStream([], {
  type: 'event:jarvis:end', payload: { id: 'stream-2', content: '' }, timestamp: 202,
});
assert.deepEqual(cancelledBeforeText, [], 'empty end leaves no assistant bubble');

assert.deepEqual(visibleResponseTiming(120, 187), {
  input_received_ms: 120,
  first_visible_token_ms: 187,
  elapsed_ms: 67,
}, 'first-visible-token timing must be measured from input receipt');

console.log('operatorModel: 17 assertions passed');
