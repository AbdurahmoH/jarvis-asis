/**
 * J.A.R.V.I.S. v3.0 — Backend Bridge Hook
 *
 * Точка интеграции frontend ↔ backend.
 * Подключает реальный локальный WebSocket-транспорт. UI больше не объявляет
 * backend готовым заранее: readiness и reconnect приходят из транспорта.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useUIState } from '@/hooks/useUIState';
import { useSessions } from '@/stores/sessionStore';
import {
  createBackend,
  TRANSPORT_STATE_MAP,
} from '@/integrations/backend';
import type {
  ActivityEvent, AttachedFile, EntityState, VitalsData, PendingConfirmation,
} from '@/types';

// Production always uses the local WebSocket. The mock exists only when
// VITE_VISUAL_PREVIEW=1 is explicitly supplied for static visual review.
const backend = createBackend();

/** Настройки облака (провайдер/модель/ключ) — существующий WS-транспорт, протокол не меняется. */
export const cloudSettingsApi = backend;

// Single source of truth for transport -> entity state (kept in backend.ts).
const STATE_MAP: Record<string, EntityState> = TRANSPORT_STATE_MAP;

export function useBackendBridge() {
  const { dispatch } = useUIState();
  const { appendEvent, updateEvent } = useSessions();
  // Refs so the subscription effect never re-subscribes on session changes.
  const appendRef = useRef(appendEvent);
  const updateRef = useRef(updateEvent);
  appendRef.current = appendEvent;
  updateRef.current = updateEvent;

  const streamingId = useRef<string | null>(null);
  const [pendingConfirmation, setPendingConfirmation] = useState<PendingConfirmation | null>(null);
  const [isConnected, setIsConnected] = useState(() => backend.isConnected());

  useEffect(() => {
    const unsub = backend.subscribeToEvents((e) => {
      // Смена состояния сущности
      if (e.type.startsWith('state:')) {
        const next = STATE_MAP[e.type];
        if (next) dispatch({ type: 'SET_ENTITY_STATE', payload: next });
        return;
      }

      if (e.type === 'runtime:status') {
        const runtime = e.payload as { ready?: boolean; state?: string };
        setIsConnected(backend.isConnected());
        if (runtime.ready === true) {
          dispatch({ type: 'UPDATE_VITALS', payload: { modelStatus: 'local' } });
        } else if (runtime.state === 'loading_model' || runtime.state === 'starting') {
          dispatch({ type: 'UPDATE_VITALS', payload: { modelStatus: 'loading' } });
        }
        return;
      }

      // Vitals — kept in UI state only (no permanent CPU/RAM dashboard rendered)
      if (e.type === 'vitals:update') {
        dispatch({ type: 'UPDATE_VITALS', payload: e.payload as Partial<VitalsData> });
        return;
      }

      // Timeline events live per-session in the session store.
      if (e.type === 'event:seed' || e.type === 'event:command') {
        appendRef.current(e.payload as ActivityEvent);
        return;
      }

      // J.A.R.V.I.S. streaming
      if (e.type === 'event:jarvis:start') {
        const ev = e.payload as ActivityEvent;
        streamingId.current = ev.id;
        dispatch({ type: 'SET_STREAMING_MESSAGE', payload: ev.id });
        appendRef.current({ ...ev, kind: 'jarvis', content: '' });
        return;
      }
      if (e.type === 'event:jarvis:token') {
        const { id, token } = e.payload as { id: string; token: string };
        // Если id токена не совпал с открытым пузырём (race id на стороне
        // backend), обновляем открытый пузырь — ответ не должен пропадать.
        updateRef.current(streamingId.current ?? id, { content: token });
        return;
      }
      if (e.type === 'event:jarvis:end') {
        const ev = e.payload as ActivityEvent;
        // Backend fast-path может сформировать start/end id в разные
        // миллисекунды; закрываем реально открытый пузырь, а не мёртвый id.
        const openId = streamingId.current;
        streamingId.current = null;
        dispatch({ type: 'SET_STREAMING_MESSAGE', payload: null });
        updateRef.current(!openId || ev.id === openId ? ev.id : openId, ev);
        return;
      }

      // Pending HIGH-risk confirmation from backend
      if (e.type === 'confirmation:required') {
        setPendingConfirmation(e.payload as PendingConfirmation);
        return;
      }

      // Любые другие события активности
      if (e.type.startsWith('event:')) {
        appendRef.current(e.payload as ActivityEvent);
      }
    });
    const unsubConnection = backend.subscribeToConnection((connected) => {
      setIsConnected(connected);
    });

    // Стартовые vitals
    backend.getSystemVitals().then((v) => dispatch({ type: 'UPDATE_VITALS', payload: v }));

    return () => {
      unsub();
      unsubConnection();
    };
  }, [dispatch]);

  const sendCommand = useCallback(async (text: string, files: AttachedFile[]) => {
    dispatch({ type: 'SET_ENTITY_STATE', payload: 'thinking' });
    await backend.sendCommand(text, files);
  }, [dispatch]);

  const interrupt = useCallback(async () => {
    await backend.interrupt();
  }, []);

  const answerConfirmation = useCallback(async (approved: boolean, scope?: string) => {
    const current = pendingConfirmation;
    if (!current) return;
    await backend.answerConfirmation(current.id, approved, scope);
    setPendingConfirmation(null);
  }, [pendingConfirmation]);

  return { sendCommand, interrupt, answerConfirmation, pendingConfirmation, isConnected };
}
