import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { LogicalSize } from '@tauri-apps/api/dpi';
import { getCurrentWindow } from '@tauri-apps/api/window';
import {
  Activity, AudioLines, Check, CircleStop, CloudSun, Cpu, FileSearch, Files,
  Gauge, Globe2, HardDrive, Headphones, Maximize2, Mic, Minimize2,
  MonitorUp, Music2, Palette, Radio, Search, Send, Settings2,
  Sparkles, Square, X, Zap,
} from 'lucide-react';
import type { PresenceState } from '@/bridge/StateMachine';
import { useTheme } from '@/stores/themeStore';
import type { AppSettings, PendingConfirmation, ThemeName } from '@/types';
import type { PresenceMessage } from '@/window/MessageStream';
import type { OperatorMission, UiMode } from './model';

export interface LiveSignal {
  id: string; kind: string; content: string; status: string; timestamp: number;
  tool?: string; payload?: Record<string, unknown>;
}
interface Props {
  messages: PresenceMessage[]; state: PresenceState; mode: UiMode; mission: OperatorMission | null;
  confirmation: PendingConfirmation | null; firstLaunch: boolean; onModeChange: (mode: UiMode) => void;
  onSend: (text: string) => void; onInterrupt: () => void; onVoiceListen: () => void;
  onConfirm: (approved: boolean) => void; onNewSession: () => void; connected: boolean;
  runtimeState: string; runtimeDiagnostics: Record<string, unknown>; signals: LiveSignal[];
}

type CardKind = 'system' | 'weather' | 'music' | 'research' | 'files' | 'computer' | 'task';
const CLOCK = new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit' });

function useClock() {
  const [clock, setClock] = useState(() => CLOCK.format(new Date()));
  useEffect(() => { const timer = window.setInterval(() => setClock(CLOCK.format(new Date())), 1000); return () => window.clearInterval(timer); }, []);
  return clock;
}
function windowAction(action: 'minimize' | 'close' | 'maximize') {
  try { const appWindow = getCurrentWindow(); if (action === 'minimize') void appWindow.minimize(); if (action === 'close') void appWindow.close(); if (action === 'maximize') void appWindow.toggleMaximize(); } catch { /* browser fixture */ }
}
export function syncWindowMode(mode: UiMode) {
  try {
    const appWindow = getCurrentWindow(); const workspace = mode === 'command_center';
    void appWindow.setResizable(workspace); void appWindow.setMinSize(new LogicalSize(workspace ? 1040 : 390, workspace ? 680 : 560));
    void appWindow.setSize(new LogicalSize(workspace ? 1380 : 390, workspace ? 860 : 560)); if (workspace) void appWindow.center();
  } catch { /* browser fixture */ }
}
function isVerified(signal?: LiveSignal) { return Boolean(signal && (/verified|completed|success/i.test(signal.status) || signal.payload?.verified === true)); }
function visualMeta(props: Pick<Props, 'state' | 'signals' | 'confirmation' | 'connected' | 'runtimeState'>) {
  const latest = props.signals.at(-1);
  if (props.runtimeState === 'starting' || props.runtimeState === 'loading_model') return { state: 'starting' as const, label: 'Запуск', detail: 'Модель проходит проверку' };
  if (!props.connected || props.runtimeState === 'unavailable' || props.state === 'error') return { state: 'error' as const, label: 'Ошибка', detail: 'Открой диагностику' };
  if (props.confirmation) return { state: 'needs_user' as const, label: 'Нужно решение', detail: 'Жду твоего подтверждения' };
  if (latest && /repair|retry|self.correct/i.test(`${latest.status} ${latest.kind}`)) return { state: 'repairing' as const, label: 'Исправляю', detail: latest.content };
  if (latest && /waiting|paused|scheduled|pending/i.test(latest.status)) return { state: 'waiting' as const, label: 'Жду', detail: latest.content };
  if (latest && /failed|error/i.test(latest.status)) return { state: 'error' as const, label: 'Не выполнено', detail: latest.content };
  if (isVerified(latest)) return { state: 'success' as const, label: 'Готово', detail: 'Результат подтверждён' };
  if (props.state === 'listening') return { state: 'listening' as const, label: 'Слушаю', detail: 'Говори' };
  if (props.state === 'thinking') return { state: 'thinking' as const, label: 'Думаю', detail: 'Собираю ответ' };
  if (props.state === 'executing') return { state: 'executing' as const, label: 'Выполняю', detail: 'Работает инструмент' };
  if (props.state === 'speaking') return { state: 'speaking' as const, label: 'Отвечаю', detail: 'Формирую реплику' };
  return { state: 'idle' as const, label: 'Готов', detail: 'Можно говорить или писать' };
}

function CommandInput({ props, compact = false }: { props: Props; compact?: boolean }) {
  const [value, setValue] = useState(''); const busy = props.state === 'thinking' || props.state === 'executing';
  const submit = () => { const text = value.trim(); if (!text) return; props.onSend(text); setValue(''); };
  return <form className={`commandInput ${compact ? 'compact' : ''}`} onSubmit={(event) => { event.preventDefault(); submit(); }}><AudioLines size={17} className="inputMark" /><input value={value} onChange={(event) => setValue(event.target.value)} placeholder={props.firstLaunch ? 'Как к тебе обращаться?' : 'Скажи, что нужно сделать'} aria-label="Команда для JARVIS" /><button type="button" className="voiceKey" onClick={props.onVoiceListen} aria-label="Голосовой ввод"><Mic size={18} /></button>{busy ? <button type="button" className="sendKey stop" onClick={props.onInterrupt} aria-label="Остановить"><CircleStop size={18} /></button> : <button type="submit" className="sendKey" disabled={!value.trim()} aria-label="Отправить"><Send size={17} /></button>}</form>;
}

function MessageList({ messages, compact = false }: { messages: PresenceMessage[]; compact?: boolean }) {
  const visible = messages.slice(compact ? -3 : -9);
  if (!visible.length) return <div className="conversationEmpty"><Sparkles size={18} /><strong>Начни разговор</strong><span>JARVIS сохранит контекст внутри текущей сессии.</span></div>;
  return <div className={`messageList ${compact ? 'compact' : ''}`} aria-live="polite">{visible.map((message) => <article key={message.id} data-role={message.role}><header><span>{message.role === 'jarvis' ? 'JARVIS' : 'ВЫ'}</span><time>{CLOCK.format(new Date(message.timestamp))}</time></header><p>{message.text || '•••'}</p></article>)}</div>;
}
function ConversationPane({ props }: { props: Props }) {
  const lastUser = [...props.messages].reverse().find((message) => message.role === 'user');
  return <section className="conversationPane glassPanel"><header className="panelHeading"><div><span>{lastUser ? 'Текущая задача' : 'Диалог'}</span><strong>{lastUser ? lastUser.text : 'Чем заняться?'}</strong></div><button onClick={props.onNewSession}>Новая</button></header><MessageList messages={props.messages} /></section>;
}

function classifySignal(signal: LiveSignal): CardKind {
  const source = `${signal.tool ?? ''} ${signal.kind} ${signal.content}`.toLowerCase();
  if (/system_status|cpu|gpu|ram|memory|систем/.test(source)) return 'system';
  if (/weather|погод|температур|forecast/.test(source)) return 'weather';
  if (/play_music|music|музык|трек|playlist/.test(source)) return 'music';
  if (/web_search|web_fetch|research|источник|поиск|исслед/.test(source)) return 'research';
  if (/list_files|read_file|write_file|search_files|file_|документ|файл|папк/.test(source)) return 'files';
  if (/open_app|close_app|browser_|screen_|key_press|type_text|computer|блокнот|браузер|приложен/.test(source)) return 'computer';
  return 'task';
}
function toolLabel(tool?: string, fallback = 'действие') {
  const labels: Record<string, string> = {
    play_music: 'воспроизведение', open_app: 'приложение', close_app: 'приложение',
    web_search: 'поиск в интернете', web_fetch: 'источник', system_status: 'состояние системы',
    weather: 'прогноз', list_files: 'просмотр файлов', read_file: 'документ', write_file: 'документ',
  };
  return tool ? (labels[tool] ?? fallback) : fallback;
}
const CARD_META: Record<CardKind, { title: string; icon: typeof Gauge; color: string }> = {
  system: { title: 'Система', icon: Cpu, color: 'blue' }, weather: { title: 'Погода', icon: CloudSun, color: 'cyan' }, music: { title: 'Сейчас играет', icon: Music2, color: 'magenta' }, research: { title: 'Исследование', icon: Globe2, color: 'purple' }, files: { title: 'Файлы', icon: Files, color: 'green' }, computer: { title: 'Действие на компьютере', icon: MonitorUp, color: 'orange' }, task: { title: 'Текущая задача', icon: Zap, color: 'purple' },
};
function urlsIn(text: string) { return text.match(/https?:\/\/[^\s)\]}>,]+/g)?.slice(0, 3) ?? []; }
function metricTokens(text: string) { return text.match(/(?:CPU|GPU|RAM|Memory|Память)[^,;\n]{0,28}/gi)?.slice(0, 4) ?? []; }
function fileTokens(text: string) { return text.match(/(?:[A-Za-z]:\\[^\n,;]+|\/[^\n,;]+)/g)?.slice(0, 4) ?? []; }

function SignalCard({ kind, signal }: { kind: CardKind; signal: LiveSignal }) {
  const meta = CARD_META[kind]; const Icon = meta.icon; const verified = isVerified(signal); const urls = urlsIn(signal.content); const metrics = metricTokens(signal.content); const files = fileTokens(signal.content);
  const hasSpecial = kind === 'music' || (kind === 'system' && metrics.length > 0) || kind === 'weather' || (kind === 'research' && urls.length > 0) || (kind === 'files' && files.length > 0);
  return <article className="contextCard glassPanel" data-kind={kind} data-color={meta.color}><header><span className="cardIcon"><Icon size={17} /></span><div><strong>{meta.title}</strong><small>{toolLabel(signal.tool, signal.kind)}</small></div><em data-ok={verified}>{verified ? 'Проверено' : signal.status}</em></header>{kind === 'music' && <div className="musicVisual"><span className="playState"><AudioLines size={17} /></span><div><span>{signal.content}</span></div></div>}{kind === 'system' && metrics.length > 0 && <div className="metricGrid">{metrics.map((metric) => <span key={metric}>{metric}</span>)}</div>}{kind === 'weather' && <div className="weatherReadout"><CloudSun size={34} /><strong>{signal.content.match(/-?\d+(?:[.,]\d+)?\s*°C?/i)?.[0] ?? 'Данные получены'}</strong></div>}{kind === 'research' && urls.length > 0 && <div className="sourceList">{urls.map((url) => <span key={url}><Globe2 size={12} />{url.replace(/^https?:\/\//, '')}</span>)}</div>}{kind === 'files' && files.length > 0 && <div className="fileList">{files.map((file) => <span key={file}><HardDrive size={12} />{file}</span>)}</div>}{!hasSpecial && <p>{signal.content}</p>}<footer><span>{CLOCK.format(new Date(signal.timestamp))}</span>{verified && <span><Check size={12} /> результат подтверждён</span>}</footer></article>;
}
function ContextCards({ signals }: { signals: LiveSignal[] }) {
  const cards = useMemo(() => { const latest = new Map<CardKind, LiveSignal>(); for (const signal of signals) latest.set(classifySignal(signal), signal); return [...latest.entries()].slice(-3).reverse(); }, [signals]);
  if (!cards.length) return null;
  return <aside className="contextColumn"><header className="columnTitle"><span>Контекст задачи</span><i>{cards.length}</i></header>{cards.map(([kind, signal]) => <SignalCard key={`${kind}-${signal.id}`} kind={kind} signal={signal} />)}</aside>;
}

function WorkRail({ props }: { props: Props }) {
  const meta = visualMeta(props); const latest = props.signals.at(-1);
  return <aside className="workRail"><section className="statusBanner glassPanel" data-state={meta.state}><i className="statusPulse" /><div><span>JARVIS</span><strong>{meta.label}</strong><p>{latest?.content || meta.detail}</p></div>{latest && <em data-ok={isVerified(latest)}>{isVerified(latest) ? 'проверено' : toolLabel(latest.tool, latest.kind)}</em>}</section><ContextCards signals={props.signals} /></aside>;
}

const QUICK_ACTIONS = [
  { label: 'Система', icon: Gauge, command: 'покажи состояние системы' }, { label: 'Погода', icon: CloudSun, command: 'какая погода сейчас' },
  { label: 'Музыка', icon: Headphones, command: 'поставь музыку' }, { label: 'Исследовать', icon: Search, command: 'помоги провести исследование' },
  { label: 'Файлы', icon: FileSearch, command: 'покажи мои файлы' },
];
function CommandDock({ props, compact = false }: { props: Props; compact?: boolean }) {
  return <footer className={`commandDock ${compact ? 'compact' : ''}`}>{!compact && <nav>{QUICK_ACTIONS.map(({ label, icon: Icon, command }) => <button key={label} onClick={() => props.onSend(command)}><Icon size={16} /><span>{label}</span></button>)}</nav>}<CommandInput props={props} compact={compact} />{!compact && <button className="voiceMode" onClick={props.onVoiceListen}><Radio size={17} /><span>Голос</span></button>}</footer>;
}

const PRESETS: Array<{ id: string; name: string; palette: Partial<AppSettings>; preview: string }> = [
  { id: 'spectra', name: 'Spectra', preview: 'linear-gradient(90deg,#43d9ff,#9b6cff,#ff4fa3,#ff9f43)', palette: { primaryAccent: '#43d9ff', secondaryAccent: '#9b6cff', tertiaryAccent: '#ff4fa3', energyAccent: '#ff9f43', successAccent: '#62e6a7', errorAccent: '#ff526d', backgroundBase: '#05060b', panelTint: '#111522' } },
  { id: 'reactor', name: 'Reactor', preview: 'linear-gradient(90deg,#68ffd5,#3388ff,#ffb340,#ff5c7a)', palette: { primaryAccent: '#68ffd5', secondaryAccent: '#3388ff', tertiaryAccent: '#ff5c7a', energyAccent: '#ffb340', successAccent: '#70f0a8', errorAccent: '#ff4b5f', backgroundBase: '#03080a', panelTint: '#0c1b20' } },
  { id: 'royal', name: 'Royal', preview: 'linear-gradient(90deg,#7cb7ff,#7557ff,#e251ff,#ff7a45)', palette: { primaryAccent: '#7cb7ff', secondaryAccent: '#7557ff', tertiaryAccent: '#e251ff', energyAccent: '#ff7a45', successAccent: '#66e3b4', errorAccent: '#ff5078', backgroundBase: '#06050d', panelTint: '#171127' } },
  { id: 'ember', name: 'Ember', preview: 'linear-gradient(90deg,#ffca58,#ff7a45,#ff436c,#9b65ff)', palette: { primaryAccent: '#ffca58', secondaryAccent: '#ff7a45', tertiaryAccent: '#ff436c', energyAccent: '#9b65ff', successAccent: '#7ee6a7', errorAccent: '#ff405b', backgroundBase: '#090604', panelTint: '#21150f' } },
];
const COLOR_FIELDS: Array<[keyof AppSettings, string]> = [['primaryAccent', 'Основной'], ['secondaryAccent', 'Вторичный'], ['tertiaryAccent', 'Маджента'], ['energyAccent', 'Энергия'], ['successAccent', 'Успех'], ['errorAccent', 'Ошибка'], ['backgroundBase', 'Фон'], ['panelTint', 'Стекло']];
function ThemeEngine({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { settings, setTheme, setPalette, setGlowIntensity, setSaturation, setContrast } = useTheme(); if (!open) return null;
  return <section className="themeEngine glassPanel"><header><div><span>Внешний вид</span><strong>Цветовая система</strong></div><button onClick={onClose}><X size={16} /></button></header><div className="presetGrid">{PRESETS.map((preset) => <button key={preset.id} onClick={() => setPalette(preset.palette)} data-active={settings.primaryAccent === preset.palette.primaryAccent}><i style={{ background: preset.preview }} /><span>{preset.name}</span></button>)}</div><div className="colorGrid">{COLOR_FIELDS.map(([key, label]) => <label key={key}><input type="color" value={String(settings[key])} onChange={(event) => setPalette({ [key]: event.target.value })} /><span>{label}</span></label>)}</div>{[['Свечение', settings.glowIntensity, 0, 1, setGlowIntensity], ['Насыщенность', settings.saturation, .7, 1.5, setSaturation], ['Контраст', settings.contrast, .85, 1.25, setContrast]].map(([label, value, min, max, setter]) => <label className="rangeField" key={String(label)}><span>{String(label)} <em>{Math.round(Number(value) * 100)}%</em></span><input type="range" min={Number(min)} max={Number(max)} step=".01" value={Number(value)} onChange={(event) => (setter as (value: number) => void)(Number(event.target.value))} /></label>)}<select value={settings.theme} onChange={(event) => setTheme(event.target.value as ThemeName)}><option value="olympus">Graphite space</option><option value="midnight">Black glass</option><option value="glass">Holographic glass</option><option value="personal">Personal background</option></select></section>;
}
function Diagnostics({ open, data, onClose }: { open: boolean; data: Record<string, unknown>; onClose: () => void }) {
  if (!open) return null; return <section className="diagnosticPanel glassPanel"><header><strong>Диагностика</strong><button onClick={onClose}><X size={15} /></button></header>{Object.entries(data).map(([key, value]) => <p key={key}><span>{key.replaceAll('_', ' ')}</span><b>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</b></p>)}</section>;
}

function StatusItem({ icon, label, value, tone }: { icon: ReactNode; label: string; value: string; tone?: string }) { return <span className="statusItem" data-tone={tone}><i>{icon}</i><span>{label}</span><strong>{value}</strong></span>; }
function TopBar({ props, onTheme, onDiagnostics }: { props: Props; onTheme: () => void; onDiagnostics: () => void }) {
  const clock = useClock(); const meta = visualMeta(props);
  return <header className="topBar" data-tauri-drag-region><div className="brandMark"><span>J</span><div><strong>JARVIS</strong><small>личный помощник</small></div></div><div className="realStatus"><StatusItem icon={<Activity size={13} />} label="Статус" value={meta.label} tone={meta.state} /><StatusItem icon={<Globe2 size={13} />} label="Связь" value={props.connected ? 'на связи' : 'нет связи'} tone={props.connected ? 'success' : 'error'} /></div><div className="windowTools"><time>{clock}</time><button onClick={onDiagnostics} aria-label="Диагностика"><Settings2 size={15} /></button><button onClick={onTheme} aria-label="Тема"><Palette size={15} /></button><button onClick={() => props.onModeChange('presence')} aria-label="Компактный режим"><Minimize2 size={15} /></button><button onClick={() => windowAction('minimize')} aria-label="Свернуть"><span>—</span></button><button onClick={() => windowAction('maximize')} aria-label="Развернуть"><Square size={12} /></button><button className="close" onClick={() => windowAction('close')} aria-label="Закрыть"><X size={15} /></button></div></header>;
}
function Confirmation({ props }: { props: Props }) { if (!props.confirmation) return null; return <div className="confirmationBar"><div><strong>Подтвердить действие</strong><span>{props.confirmation.prompt}</span></div><button onClick={() => props.onConfirm(true)}><Check size={14} /> Разрешить</button><button onClick={() => props.onConfirm(false)}><X size={14} /> Отмена</button></div>; }

function Workspace(props: Props) {
  const [theme, setTheme] = useState(false); const [diagnostics, setDiagnostics] = useState(false); const meta = visualMeta(props); const hasContext = props.signals.length > 0;
  return <main className="aiosWorkspace" data-state={meta.state}><div className="ambientLayers"><i /><i /><i /></div><TopBar props={props} onTheme={() => setTheme((value) => !value)} onDiagnostics={() => setDiagnostics((value) => !value)} /><section className="workspaceGrid" data-context={hasContext}><ConversationPane props={props} /><WorkRail props={props} /></section><CommandDock props={props} /><Confirmation props={props} /><ThemeEngine open={theme} onClose={() => setTheme(false)} /><Diagnostics open={diagnostics} data={props.runtimeDiagnostics} onClose={() => setDiagnostics(false)} /></main>;
}
function Presence(props: Props) {
  const [theme, setTheme] = useState(false); const meta = visualMeta(props);
  const latest = props.signals.at(-1);
  return <main className="aiosPresence" data-state={meta.state}><div className="presenceGlow" /><header data-tauri-drag-region><div className="presenceBrand"><i /><strong>JARVIS</strong><span>{meta.label}</span></div><nav><button onClick={() => setTheme((value) => !value)} aria-label="Тема"><Palette size={15} /></button><button onClick={() => props.onModeChange('command_center')} aria-label="Расширенный режим"><Maximize2 size={15} /></button><button onClick={() => windowAction('close')} aria-label="Закрыть"><X size={15} /></button></nav></header><section className="presenceStatus"><i className="statusPulse" /><div><strong>{latest ? toolLabel(latest.tool, latest.kind) : meta.label}</strong><span>{latest?.content || meta.detail}</span></div></section><MessageList messages={props.messages} compact /><div className="presenceQuick"><button onClick={props.onVoiceListen}><Mic size={15} /> Слушать</button><button onClick={() => props.onSend('покажи состояние системы')}><Gauge size={15} /> Система</button><button onClick={() => props.onSend('поставь музыку')}><Music2 size={15} /> Музыка</button></div><CommandDock props={props} compact /><ThemeEngine open={theme} onClose={() => setTheme(false)} /></main>;
}

export function OperatorShell(props: Props) {
  useEffect(() => syncWindowMode(props.mode), [props.mode]);
  useEffect(() => { const handler = (event: KeyboardEvent) => { if (event.ctrlKey && event.shiftKey && event.code === 'Space') { event.preventDefault(); props.onModeChange(props.mode === 'presence' ? 'command_center' : 'presence'); } }; window.addEventListener('keydown', handler); return () => window.removeEventListener('keydown', handler); }, [props]);
  return props.mode === 'presence' ? <Presence {...props} /> : <Workspace {...props} />;
}
