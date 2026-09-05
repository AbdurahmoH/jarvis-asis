/**
 * J.A.R.V.I.S. — живой WebSocket-транспорт к локальному Python core.
 *
 * Никаких моков: если core не слушает localhost, команда отклоняется и UI
 * получает фактическую ошибку соединения. Секреты существуют только в payload
 * settings:update и никогда не эмитятся в ленту событий или console.
 */

import type {
  ActivityEvent,
  AttachedFile,
  BackendAdapter,
  BackendEvent,
  VitalsData,
} from '@/types';
import {
  mapSocketEnvelope,
  type TransportEvent,
} from './wsProtocol.ts';

export interface CloudSettings {
  provider: string;
  base_url: string;
  model: string;
  has_api_key: boolean;
  api_key_masked: string;
}

export interface CloudSettingsPatch {
  provider?: string;
  base_url?: string;
  model?: string;
  /** Пустая строка означает «не менять»; очистка — только clear_api_key. */
  api_key?: string;
  clear_api_key?: boolean;
}

type Listener = (event: BackendEvent) => void;
type SocketFactory = (url: string) => WebSocket;
/** S2: источник токена WS-аутентификации. null — токен недоступен. */
export type TokenProvider = () => Promise<string | null>;

/**
 * S2: токен приходит из Tauri-лаунчера по `invoke("ws_auth_token")`.
 *
 * Не из URL и не из query string: адрес соединения попадает в логи, в историю
 * и в телеметрию, а токен — это полный доступ к командам, экрану и настройкам.
 * В браузере (vite dev без Tauri) команды нет — возвращаем null, и тогда
 * сервер обязан быть запущен с JARVIS_WS_DEV_NOAUTH=1, иначе он закроет
 * соединение сам.
 */
export const tauriTokenProvider: TokenProvider = async () => {
  try {
    const core = await import('@tauri-apps/api/core');
    const token = await core.invoke<string>('ws_auth_token');
    return typeof token === 'string' && token.length > 0 ? token : null;
  } catch {
    return null;
  }
};

type SettingsRequest = {
  kind: 'get' | 'save';
  resolve: (settings: CloudSettings) => void;
  reject: (reason: Error) => void;
};

const STANDBY_VITALS: VitalsData = {
  cpu: 0,
  ram: 0,
  modelStatus: 'standby',
  externalApi: 'standby',
  uptime: 0,
};

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object'
    ? value as Record<string, unknown>
    : null;
}

function readCloudSettings(value: unknown): CloudSettings | null {
  const settings = asRecord(value);
  if (!settings
    || typeof settings.provider !== 'string'
    || typeof settings.base_url !== 'string'
    || typeof settings.model !== 'string'
    || typeof settings.has_api_key !== 'boolean'
    || typeof settings.api_key_masked !== 'string') {
    return null;
  }

  return {
    provider: settings.provider,
    base_url: settings.base_url,
    model: settings.model,
    has_api_key: settings.has_api_key,
    api_key_masked: settings.api_key_masked,
  };
}

function socketError(message: string): Error {
  return new Error(`WebSocket J.A.R.V.I.S.: ${message}`);
}

/** Реальный локальный транспорт для command/settings/confirmation protocol. */
export class WebSocketBackend implements BackendAdapter {
  private readonly listeners = new Set<Listener>();
  private readonly connectionListeners = new Set<(connected: boolean) => void>();
  private readonly settingsRequests: SettingsRequest[] = [];
  private socket: WebSocket | null = null;
  private connectPromise: Promise<WebSocket> | null = null;
  private resolveConnection: ((socket: WebSocket) => void) | null = null;
  private rejectConnection: ((error: Error) => void) | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectAttempt = 0;
  private connected = false;
  private vitals: VitalsData = STANDBY_VITALS;
  private sequence = 0;

  private readonly url: string;
  private readonly socketFactory: SocketFactory;
  private readonly tokenProvider: TokenProvider;
  /** Токен запрашивается у лаунчера один раз на процесс. */
  private tokenPromise: Promise<string | null> | null = null;
  /** Отправка кадра auth текущего соединения; send() ждёт именно её. */
  private authSent: Promise<void> | null = null;

  constructor(
    url: string,
    socketFactory: SocketFactory = (endpoint) => new WebSocket(endpoint),
    tokenProvider: TokenProvider = tauriTokenProvider,
  ) {
    this.url = url;
    this.socketFactory = socketFactory;
    this.tokenProvider = tokenProvider;
  }

  isConnected(): boolean {
    return this.connected && this.socket?.readyState === WebSocket.OPEN;
  }

  subscribeToConnection(listener: (connected: boolean) => void): () => void {
    this.connectionListeners.add(listener);
    listener(this.isConnected());
    return () => this.connectionListeners.delete(listener);
  }

  subscribeToEvents(listener: Listener): () => void {
    this.listeners.add(listener);
    void this.connect().catch(() => {
      // Ошибка уже отправлена в подписчиков; нельзя логировать payload settings.
    });
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0 && this.reconnectTimer !== null) {
        clearTimeout(this.reconnectTimer);
        this.reconnectTimer = null;
      }
    };
  }

  async sendCommand(text: string, files: AttachedFile[]): Promise<void> {
    const content = text.trim();
    if (!content) return;
    // UI listens to this as an input-overlay close signal. It is emitted
    // before network work so a slow backend can never block the screen.
    if (typeof window !== 'undefined') window.dispatchEvent(new CustomEvent('jarvis:command-sent'));

    const command: ActivityEvent = {
      id: `command-${Date.now()}-${this.sequence++}`,
      kind: 'command',
      content,
      timestamp: Date.now(),
    };
    this.emit({ type: 'event:command', payload: command, timestamp: command.timestamp });

    if (files.length > 0) {
      this.emit({
        type: 'event:system',
        payload: {
          id: `attachment-notice-${Date.now()}-${this.sequence++}`,
          kind: 'system',
          content: 'Вложения выбраны в интерфейсе, но текущий WebSocket-протокол передаёт только текст команды.',
          timestamp: Date.now(),
        } satisfies ActivityEvent,
        timestamp: Date.now(),
      });
    }

    await this.send({ type: 'command', text: content });
  }

  async interrupt(): Promise<void> {
    await this.send({ type: 'interrupt' });
  }

  /** Desktop global shortcut: preserve the established launcher WS event. */
  async hotkeyPressed(): Promise<void> {
    await this.send({ type: 'hotkey_pressed' });
  }

  async voiceListen(): Promise<void> { await this.send({ type: 'voice_listen' }); }

  /** Ambient sources are informational and do not enter the command router. */
  async ambientInitiated(text: string): Promise<void> {
    if (text.trim()) await this.send({ type: 'ambient_initiated', text: text.trim() });
  }

  async saveFirstLaunchName(name: string): Promise<void> {
    if (name.trim()) await this.send({ type: 'first_launch', name: name.trim() });
  }

  async answerConfirmation(confirmationId: string, approved: boolean): Promise<void> {
    await this.send({ type: 'confirm', confirmation_id: confirmationId, approve: approved });
  }

  getSystemVitals(): Promise<VitalsData> {
    return Promise.resolve(this.vitals);
  }

  getCloudSettings(): Promise<CloudSettings> {
    return this.requestSettings('get', { type: 'settings:get' });
  }

  updateCloudSettings(patch: CloudSettingsPatch): Promise<CloudSettings> {
    return this.requestSettings('save', { type: 'settings:update', settings: patch });
  }

  private requestSettings(kind: SettingsRequest['kind'], payload: object): Promise<CloudSettings> {
    return new Promise<CloudSettings>((resolve, reject) => {
      const request: SettingsRequest = { kind, resolve, reject };
      this.settingsRequests.push(request);
      void this.send(payload).catch((error: unknown) => {
        this.settleSettings(request, 'reject', error instanceof Error ? error : socketError('не удалось отправить настройки'));
      });
    });
  }

  private async send(payload: object): Promise<void> {
    const socket = await this.connect();
    // S2: connect() возвращается сразу, если сокет уже OPEN, поэтому порядок
    // кадров держится на отдельном ожидании аутентификации — иначе команда,
    // отправленная в том же тике, что и open, опередила бы кадр auth.
    if (this.authSent) await this.authSent;
    if (socket.readyState !== WebSocket.OPEN) {
      throw socketError('соединение ещё не готово');
    }
    socket.send(JSON.stringify(payload));
  }

  private connect(): Promise<WebSocket> {
    if (this.socket?.readyState === WebSocket.OPEN) return Promise.resolve(this.socket);
    if (this.socket?.readyState === WebSocket.CONNECTING && this.connectPromise) {
      return this.connectPromise;
    }
    if (this.connectPromise) return this.connectPromise;

    const socket = this.socketFactory(this.url);
    this.socket = socket;
    this.connectPromise = new Promise<WebSocket>((resolve, reject) => {
      this.resolveConnection = resolve;
      this.rejectConnection = reject;
    });

    socket.onopen = () => {
      // S2: кадр аутентификации уходит ПЕРВЫМ. Промис ставится синхронно в
      // onopen, поэтому любой send() в том же тике его дождётся.
      this.authSent = this.authenticate(socket);
      void this.authSent.then(() => {
        this.connected = true;
        this.reconnectAttempt = 0;
        this.resolveConnection?.(socket);
        this.clearConnectionPromise();
        for (const listener of this.connectionListeners) listener(true);
      });
    };
    socket.onmessage = (event) => this.handleMessage(event.data);
    socket.onerror = () => this.failConnection('не удалось подключиться к локальному backend');
    socket.onclose = () => this.failConnection('соединение с локальным backend закрыто');

    return this.connectPromise;
  }

  /**
   * S2: отправляет `{type:"auth", token}` до любого другого кадра.
   *
   * Если токен недоступен (браузер без Tauri), кадр не отправляется: решение
   * остаётся за сервером — он либо запущен с JARVIS_WS_DEV_NOAUTH=1, либо
   * закроет соединение с кодом 1008. Клиент не притворяется авторизованным.
   */
  private async authenticate(socket: WebSocket): Promise<void> {
    if (!this.tokenPromise) {
      this.tokenPromise = this.tokenProvider().catch(() => null);
    }
    const token = await this.tokenPromise;
    if (!token) return;
    if (socket.readyState !== WebSocket.OPEN) return;
    socket.send(JSON.stringify({ type: 'auth', token }));
  }

  private clearConnectionPromise(): void {
    this.connectPromise = null;
    this.resolveConnection = null;
    this.rejectConnection = null;
  }

  private failConnection(message: string): void {
    if (!this.socket && !this.connectPromise && !this.connected) {
      return;
    }
    const error = socketError(message);
    this.rejectConnection?.(error);
    this.clearConnectionPromise();
    this.socket = null;
    this.authSent = null;
    const wasConnected = this.connected;
    this.connected = false;
    this.emit({
      type: 'event:system',
      payload: {
        id: `transport-${Date.now()}-${this.sequence++}`,
        kind: 'system',
        content: message,
        error: message,
        timestamp: Date.now(),
      } satisfies ActivityEvent,
      timestamp: Date.now(),
    });
    this.emit({ type: 'state:error', payload: null, timestamp: Date.now() });
    if (wasConnected || this.listeners.size > 0) {
      for (const listener of this.connectionListeners) listener(false);
      this.scheduleReconnect();
    }
  }

  private scheduleReconnect(): void {
    if (this.listeners.size === 0 || this.reconnectTimer !== null) return;
    const attempt = this.reconnectAttempt++;
    const base = Math.min(5000, 250 * (2 ** Math.min(attempt, 5)));
    const jitter = Math.floor(Math.random() * 120);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      void this.connect().catch(() => this.scheduleReconnect());
    }, base + jitter);
  }

  private handleMessage(data: unknown): void {
    if (typeof data !== 'string') return;

    let envelope: unknown;
    try {
      envelope = JSON.parse(data);
    } catch {
      this.emit({
        type: 'event:system',
        payload: {
          id: `protocol-${Date.now()}-${this.sequence++}`,
          kind: 'system',
          content: 'Backend вернул некорректное WebSocket-сообщение.',
          error: 'bad JSON from backend',
          timestamp: Date.now(),
        } satisfies ActivityEvent,
        timestamp: Date.now(),
      });
      this.emit({ type: 'state:error', payload: null, timestamp: Date.now() });
      return;
    }

    const record = asRecord(envelope);
    if (!record) return;
    const type = record.type;
    if (type === 'settings') {
      const settings = readCloudSettings(record.settings);
      const request = this.settingsRequests.find((candidate) => candidate.kind === 'get');
      if (request && settings) this.settleSettings(request, 'resolve', settings);
      return;
    }
    if (type === 'settings:saved') {
      const settings = readCloudSettings(record.settings);
      const request = this.settingsRequests.find((candidate) => candidate.kind === 'save');
      if (request && settings) this.settleSettings(request, 'resolve', settings);
      return;
    }
    if (type === 'error' && this.settingsRequests.length > 0) {
      const message = typeof record.message === 'string' ? record.message : 'Backend отклонил настройки.';
      this.settleSettings(this.settingsRequests[0], 'reject', socketError(message));
    }

    for (const event of mapSocketEnvelope(envelope)) this.dispatchTransportEvent(event);
  }

  private dispatchTransportEvent(event: TransportEvent): void {
    if (event.type === 'vitals:update') {
      const vitals = asRecord(event.payload);
      if (vitals) this.vitals = { ...this.vitals, ...vitals } as VitalsData;
    }
    this.emit(event as BackendEvent);
  }

  private settleSettings(
    request: SettingsRequest,
    result: 'resolve' | 'reject',
    value: CloudSettings | Error,
  ): void {
    const index = this.settingsRequests.indexOf(request);
    if (index >= 0) this.settingsRequests.splice(index, 1);
    if (result === 'resolve') request.resolve(value as CloudSettings);
    else request.reject(value as Error);
  }

  private emit(event: BackendEvent): void {
    for (const listener of this.listeners) listener(event);
  }
}
