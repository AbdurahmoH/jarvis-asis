import type { BackendEvent, PendingConfirmation } from '@/types';
import type { PresenceMessage } from '@/window/MessageStream';

export type UiMode = 'presence' | 'command_center';

export function reducePresenceStream(
  messages: PresenceMessage[], event: BackendEvent,
): PresenceMessage[] {
  if (event.type === 'event:jarvis:start') return messages;
  if (event.type !== 'event:jarvis:token' && event.type !== 'event:jarvis:end') return messages;
  const payload = (event.payload ?? {}) as { id?: string; token?: string; content?: string };
  const id = String(payload.id ?? '');
  const text = String(payload.content ?? payload.token ?? '');
  if (!id) return messages;
  const index = messages.findIndex((message) => message.id === id);
  if (!text.trim()) {
    return index < 0 ? messages : messages.filter((message) => message.id !== id);
  }
  if (index < 0) {
    return [...messages, { id, role: 'jarvis' as const, text, timestamp: event.timestamp }].slice(-30);
  }
  return messages.map((message) => message.id === id ? { ...message, text } : message);
}

export interface VisibleResponseTiming {
  input_received_ms: number;
  first_visible_token_ms: number;
  elapsed_ms: number;
}

export function visibleResponseTiming(inputReceivedMs: number, firstVisibleTokenMs: number): VisibleResponseTiming {
  return {
    input_received_ms: inputReceivedMs,
    first_visible_token_ms: firstVisibleTokenMs,
    elapsed_ms: Math.max(0, firstVisibleTokenMs - inputReceivedMs),
  };
}
export type MissionPhase = 'research' | 'download' | 'verify' | 'install' | 'observe' | 'verified' | 'error';

export interface MissionStep {
  id: 'research' | 'download' | 'verify' | 'install';
  label: string;
  state: 'pending' | 'active' | 'complete' | 'failed';
}

export interface OperatorEvidence {
  label: string;
  value: string;
  tone: 'neutral' | 'cyan' | 'amber' | 'lime';
}

export interface OperatorActivity {
  id: string;
  label: string;
  detail?: string;
  status: 'running' | 'complete' | 'failed';
  timestamp: number;
}

export interface OperatorMission {
  id: string;
  title: string;
  phase: MissionPhase;
  steps: MissionStep[];
  activities: OperatorActivity[];
  evidence: OperatorEvidence[];
  verified: boolean;
}

const STEP_LABELS: Record<MissionStep['id'], string> = {
  research: 'RESEARCH',
  download: 'DOWNLOAD',
  verify: 'VERIFY',
  install: 'INSTALL',
};

const ORDER: MissionStep['id'][] = ['research', 'download', 'verify', 'install'];

function compactTitle(command: string): string {
  const normalized = command.trim().replace(/\s+/g, ' ');
  if (normalized.length <= 54) return normalized;
  return `${normalized.slice(0, 51).trimEnd()}…`;
}

function stepState(step: MissionStep['id'], phase: MissionPhase): MissionStep['state'] {
  if (phase === 'error') return 'failed';
  if (phase === 'verified' || phase === 'observe') return 'complete';
  const active = phase === 'research' ? 0 : phase === 'download' ? 1 : phase === 'verify' ? 2 : 3;
  const index = ORDER.indexOf(step);
  return index < active ? 'complete' : index === active ? 'active' : 'pending';
}

function stepsFor(phase: MissionPhase): MissionStep[] {
  return ORDER.map((id) => ({ id, label: STEP_LABELS[id], state: stepState(id, phase) }));
}

export function createMission(command: string, now = Date.now()): OperatorMission {
  return {
    id: `mission-${now}`,
    title: compactTitle(command),
    phase: 'research',
    steps: stepsFor('research'),
    activities: [{ id: `activity-${now}`, label: 'Анализирую задачу и доступные инструменты', status: 'running', timestamp: now }],
    evidence: [],
    verified: false,
  };
}

function recordOf(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' ? value as Record<string, unknown> : null;
}

function textFromPayload(value: unknown): string {
  const record = recordOf(value);
  if (!record) return '';
  for (const key of ['content', 'result', 'message', 'detail', 'tool']) {
    if (typeof record[key] === 'string' && record[key]) return record[key] as string;
  }
  return '';
}

function explicitlyVerified(value: unknown): boolean {
  const record = recordOf(value);
  if (!record) return false;
  if (record.verified === true || record.status === 'verified') return true;
  const verification = recordOf(record.verification);
  return verification?.verified === true || verification?.status === 'verified';
}

function nextPhase(current: MissionPhase, event: BackendEvent): MissionPhase {
  if (event.type === 'state:error') return 'error';
  if (event.type === 'confirmation:required') return 'verify';
  if (event.type === 'state:executing') {
    if (current === 'research') return 'download';
    if (current === 'download') return 'verify';
    if (current === 'verify') return 'install';
  }
  if (event.type === 'event:result') return explicitlyVerified(event.payload) ? 'verified' : 'observe';
  return current;
}

export function reduceMission(current: OperatorMission, event: BackendEvent): OperatorMission {
  const phase = nextPhase(current.phase, event);
  const text = textFromPayload(event.payload);
  const relevant = event.type === 'event:action' || event.type === 'event:tool' || event.type === 'event:progress' || event.type === 'event:result' || event.type === 'event:system';
  const activities = relevant && text
    ? [...current.activities.map((item) => item.status === 'running' ? { ...item, status: 'complete' as const } : item), {
        id: `activity-${event.timestamp}-${current.activities.length}`,
        label: text,
        status: event.type === 'event:result' ? 'complete' as const : event.type === 'event:system' && phase === 'error' ? 'failed' as const : 'running' as const,
        timestamp: event.timestamp,
      }].slice(-5)
    : current.activities;

  const evidence = explicitlyVerified(event.payload)
    ? [
        { label: 'Статус', value: 'VERIFIED', tone: 'lime' as const },
        { label: 'Проверка', value: 'Результат подтверждён backend', tone: 'lime' as const },
      ]
    : current.evidence;

  return { ...current, phase, steps: stepsFor(phase), activities, evidence, verified: phase === 'verified' };
}

export function confirmationFromEvent(event: BackendEvent): PendingConfirmation | null {
  if (event.type !== 'confirmation:required') return null;
  const payload = recordOf(event.payload);
  if (!payload) return null;
  const id = typeof payload.confirmationId === 'string' ? payload.confirmationId : '';
  if (!id) return null;
  return {
    id,
    prompt: typeof payload.prompt === 'string' ? payload.prompt : 'Подтвердите действие',
    tool: typeof payload.tool === 'string' ? payload.tool : '',
    risk: recordOf(payload.risk) ?? {},
  };
}

export function fixtureMission(phase: MissionPhase): OperatorMission {
  const base = createMission('Установка тестовой программы', 1_725_000_000_000);
  const evidence: OperatorEvidence[] = phase === 'verified'
    ? [
        { label: 'Статус', value: 'VERIFIED', tone: 'lime' },
        { label: 'Проверок', value: '4 из 4', tone: 'lime' },
        { label: 'Версия', value: '3.0.21', tone: 'neutral' },
      ]
    : [
        { label: 'Источник', value: 'Официальный сайт издателя', tone: 'cyan' },
        { label: 'Риск', value: 'LOW', tone: 'cyan' },
        { label: 'Подпись', value: phase === 'verify' ? 'Действительна' : 'Ожидает проверки', tone: phase === 'verify' ? 'cyan' : 'neutral' },
      ];
  return {
    ...base,
    phase,
    steps: stepsFor(phase),
    verified: phase === 'verified',
    evidence,
    activities: phase === 'verified'
      ? [
          { id: 'a1', label: 'Приложение установлено', detail: 'C:\\Program Files\\VideoLAN\\VLC\\vlc.exe', status: 'complete', timestamp: Date.now() },
          { id: 'a2', label: 'Процесс успешно запущен', detail: 'Версия 3.0.21', status: 'complete', timestamp: Date.now() },
          { id: 'a3', label: 'Настройки применены и проверены', status: 'complete', timestamp: Date.now() },
        ]
      : [
          { id: 'a1', label: 'Найден официальный источник', detail: 'Официальный сайт издателя подтверждён', status: 'complete', timestamp: Date.now() },
          { id: 'a2', label: phase === 'verify' ? 'Цифровая подпись действительна' : 'Начинаю загрузку установочного файла', detail: phase === 'verify' ? 'Publisher: VideoLAN · SHA-256 6f78a5e4…91c0e823' : 'Загрузка через защищённое соединение', status: phase === 'verify' ? 'complete' : 'running', timestamp: Date.now() },
        ],
  };
}
