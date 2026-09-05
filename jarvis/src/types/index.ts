/**
 * J.A.R.V.I.S. v3.0 — Core Types
 * Single source of truth for all UI types.
 * Visual layer uses `ActivityEvent` (operational timeline semantics).
 * Legacy `Message`/`MessageRole` retained for the backend bridge adapter.
 *
 * EXTENDED for OLYMPUS / WHITE ROOM:
 *  - Theme system (frontend-only)
 *  - Session system (client-side, localStorage)
 *  - White Room first-run
 *  - Do NOT add any backend-dependent types here.
 */

/* ===== Entity States ===== */
export type EntityState =
  | 'idle'
  | 'listening'
  | 'thinking'
  | 'executing'
  | 'streaming'
  | 'error'
  | 'cloud';

/* ===== Themes (frontend-only) ===== */
export type ThemeName = 'ivory' | 'midnight' | 'glass' | 'olympus' | 'personal';

export interface ThemeSettings {
  theme: ThemeName;
  /** Panel transparency multiplier 0..1 (1 = full opacity, 0 = very glassy). */
  transparency: number;
  /** User override for the background asset (Personal theme only). */
  backgroundUrl?: string;
}

/* ===== Settings (frontend-only) ===== */
export interface AppSettings extends ThemeSettings {
  reduceMotion: boolean;
  primaryAccent: string;
  secondaryAccent: string;
  tertiaryAccent: string;
  energyAccent: string;
  successAccent: string;
  errorAccent: string;
  backgroundBase: string;
  panelTint: string;
  glowIntensity: number;
  saturation: number;
  contrast: number;
}

/* ===== White Room / first run ===== */
export interface UserProfile {
  name: string;
  /** How J.A.R.V.I.S. addresses the user, e.g. "sir". */
  honorific: string;
  createdAt: number;
}

/* ===== Activity Event Semantics (the operational timeline) =====
 * Each event type has its own visual language — not chat bubbles. */
export type ActivityKind =
  | 'command'      // USER objective — minimal, elegant
  | 'analysis'     // J.A.R.V.I.S. reasoning about the request
  | 'jarvis'       // J.A.R.V.I.S. response — prominent
  | 'action'       // system action performed (monospace, technical)
  | 'tool'         // tool invocation / result
  | 'result'       // clear outcome / deliverable
  | 'system'       // ambient system note — very subtle
  | 'progress';    // live task / operation progress (thinking/executing)

export interface BaseEvent {
  id: string;
  kind: ActivityKind;
  timestamp: number;
  /** primary text (rendered per-kind) */
  content: string;
  /** optional label shown in the kind tag, e.g. "FILE SYSTEM", "SEARCH" */
  label?: string;
  /** status for progress / action / tool events */
  status?: 'pending' | 'running' | 'completed' | 'failed';
  /** structured sub-steps for progress (TASK view) */
  steps?: TaskStep[];
  /** secondary detail line(s) */
  detail?: string;
  /** mono-technical payload (command, file list, JSON) */
  code?: string;
  /** tokens / duration metadata */
  tokens?: number;
  durationMs?: number;
  model?: string;
  error?: string;
}

export interface TaskStep {
  label: string;
  status: 'pending' | 'running' | 'completed' | 'failed';
}

export type ActivityEvent = BaseEvent;

/* ===== Legacy message model (kept for bridge compatibility) ===== */
export type MessageRole = 'user' | 'jarvis' | 'action' | 'system';
export interface MessageMetadata {
  tokens?: number;
  durationMs?: number;
  model?: string;
  toolsUsed?: string[];
  error?: string;
  thoughtProcess?: unknown;
}
export interface BaseMessage {
  id: string;
  role: MessageRole;
  timestamp: number;
  content: string;
  metadata?: MessageMetadata;
}
export type Message = BaseMessage;

/* ===== Sessions (client-side, frontend-only) ===== */
export interface Session {
  id: string;
  title: string;
  pinned: boolean;
  createdAt: number;
  updatedAt: number;
  /** number of events in the session (for the sidebar preview) */
  messageCount: number;
  /** short preview of the last user command */
  preview?: string;
}

export type WorkspaceTab = 'sources' | 'files' | 'execution' | 'details' | 'data';

/* ===== UI State ===== */
export interface UIStateData {
  entityState: EntityState;
  isDrawerOpen: boolean;
  isEventExpanded: Record<string, boolean>;
  streamingEventId: string | null;
  vitals: VitalsData;
  windowEffect: 'acrylic' | 'mica' | 'vibrancy' | 'linux-blur' | 'none';
  isSidebarOpen: boolean;
  isSettingsOpen: boolean;
}

/** Alias retained so the existing state machine keeps compiling. */
export type UIState = UIStateData;

export interface VitalsData {
  cpu: number;
  ram: number;
  modelStatus: 'local' | 'cloud' | 'standby' | 'loading';
  externalApi: 'connected' | 'disconnected' | 'standby';
  uptime: number;
}

/* ===== Command Input ===== */
export interface AttachedFile {
  id: string;
  name: string;
  size: number;
  type: string;
  preview?: string;
}

/* ===== Pending HIGH-risk confirmation (from backend) ===== */
export interface PendingConfirmation {
  id: string;
  prompt: string;
  tool: string;
  risk: { level?: string; [key: string]: unknown };
  /**
   * S3: объёмы полномочия, которые предложил сервер («один раз» / «на сеанс» /
   * «постоянно»). Пусто — обычное подтверждение без выдачи гранта.
   */
  scopes?: string[];
}

/* ===== Backend adapter contract (src/integrations/backend.ts) ===== */
export interface BackendAdapter {
  sendCommand(text: string, files: AttachedFile[]): Promise<void>;
  subscribeToEvents(cb: (e: BackendEvent) => void): () => void;
  getSystemVitals(): Promise<VitalsData>;
  interrupt(): Promise<void>;
  /** Ответить на ожидающее HIGH-risk подтверждение backend. */
  answerConfirmation(confirmationId: string, approved: boolean, scope?: string): Promise<void>;
}

export interface BackendEvent {
  type: BackendEventType;
  payload: unknown;
  timestamp: number;
}

export type BackendEventType =
  | 'event:command'
  | 'event:analysis'
  | 'event:jarvis:start'
  | 'event:jarvis:token'
  | 'event:jarvis:end'
  | 'event:action'
  | 'event:tool'
  | 'event:result'
  | 'event:system'
  | 'event:system_initiated'
  | 'event:seed'
  | 'event:progress'
  | 'state:thinking'
  | 'state:executing'
  | 'state:error'
  | 'state:idle'
  | 'state:streaming'
  | 'state:cloud'
  | 'state:listening'
  | 'vitals:update'
  | 'profile:status'
  | 'screen:capture'
  | 'event:wake_word_detected'
  | 'event:voice_input'
  | 'transport:connected'
  | 'transport:disconnected'
  | 'runtime:status'
  | 'model:status'
  | 'workspace:update'
  | 'confirmation:required';
