/**
 * J.A.R.V.I.S. v3.0 — Settings Panel
 * Theme · Panel transparency · Reduce motion · Personal background · Honorific
 * · Cloud LLM provider (провайдер/модель/ключ) через существующий WS-транспорт.
 * Логика ключа не меняется — только интерфейс к готовым методам транспорта.
 */

import { useEffect, useState, type ChangeEvent } from 'react';
import { motion } from 'framer-motion';
import { X, Sun, Moon, Layers, Crown, User, Cloud, KeyRound, Loader2, CheckCircle2, AlertCircle } from 'lucide-react';
import { useUIState } from '@/hooks/useUIState';
import { useTheme } from '@/stores/themeStore';
import { useFocusTrap } from '@/hooks/useFocusTrap';
import { cloudSettingsApi } from '@/hooks/useBackendBridge';
import type { CloudSettings } from '@/integrations/wsBackend';
import type { ThemeName } from '@/types';
import styles from './SettingsPanel.module.css';

const THEMES: { id: ThemeName; label: string; icon: typeof Sun }[] = [
  { id: 'ivory', label: 'Ivory', icon: Sun },
  { id: 'midnight', label: 'Midnight', icon: Moon },
  { id: 'glass', label: 'Glass', icon: Layers },
  { id: 'olympus', label: 'Olympus', icon: Crown },
  { id: 'personal', label: 'Personal', icon: User },
];

function themeSwatch(t: ThemeName): string {
  switch (t) {
    case 'ivory': return 'linear-gradient(135deg,#f4f6fa,#e3e8f0)';
    case 'midnight': return 'linear-gradient(135deg,#0b0d18,#161a2a)';
    case 'glass': return 'linear-gradient(135deg,#0a0e1a,#1a2238)';
    case 'olympus': return 'linear-gradient(135deg,#0a0a1e,#1a1640)';
    case 'personal': return 'linear-gradient(135deg,#1a1030,#2a1a4a)';
  }
}

type CloudStatus = 'loading' | 'ready' | 'saving' | 'error';

/** Секция «Облако»: провайдер/модель/ключ через существующий WS-транспорт. */
function CloudSection() {
  const [status, setStatus] = useState<CloudStatus>('loading');
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [cloud, setCloud] = useState<CloudSettings | null>(null);
  const [provider, setProvider] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [model, setModel] = useState('');
  const [apiKey, setApiKey] = useState('');

  useEffect(() => {
    let alive = true;
    cloudSettingsApi.getCloudSettings()
      .then((s) => {
        if (!alive) return;
        setCloud(s);
        setProvider(s.provider);
        setBaseUrl(s.base_url);
        setModel(s.model);
        setStatus('ready');
      })
      .catch((e: unknown) => {
        if (!alive) return;
        setError(e instanceof Error ? e.message : 'Backend недоступен');
        setStatus('error');
      });
    return () => { alive = false; };
  }, []);

  const save = async (clearKey = false) => {
    if (status === 'saving') return;
    setStatus('saving');
    setError(null);
    setSaved(false);
    try {
      const s = await cloudSettingsApi.updateCloudSettings({
        provider: provider.trim() || undefined,
        base_url: baseUrl.trim() || undefined,
        model: model.trim() || undefined,
        api_key: clearKey ? undefined : (apiKey ? apiKey : undefined),
        clear_api_key: clearKey || undefined,
      });
      setCloud(s);
      setProvider(s.provider);
      setBaseUrl(s.base_url);
      setModel(s.model);
      setApiKey('');
      setStatus('ready');
      setSaved(true);
      window.setTimeout(() => setSaved(false), 2200);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Не удалось сохранить');
      setStatus('error');
    }
  };

  const dirty = status === 'ready' && cloud != null && (
    provider !== cloud.provider || baseUrl !== cloud.base_url
    || model !== cloud.model || apiKey.trim().length > 0
  );

  return (
    <section className={styles.group}>
      <label className={styles.label}>
        Облако · LLM-провайдер
        {cloud && (
          <span className={styles.value} data-has-key={cloud.has_api_key}>
            {cloud.has_api_key ? `ключ: ${cloud.api_key_masked}` : 'ключ не задан'}
          </span>
        )}
      </label>

      {status === 'loading' && (
        <div className={styles.cloudNote}><Loader2 size={14} className={styles.spinSoft} /> Читаю настройки backend…</div>
      )}
      {status === 'error' && (
        <div className={styles.cloudNoteError}><AlertCircle size={14} /> {error ?? 'Backend недоступен'}</div>
      )}

      {(status === 'ready' || status === 'saving' || status === 'error') && cloud && (
        <>
          <div className={styles.cloudGrid}>
            <label className={styles.field}>
              <span>Провайдер</span>
              <input className={styles.text} value={provider} onChange={(e) => setProvider(e.target.value)} aria-label="Провайдер" />
            </label>
            {provider !== 'local' ? (
              <label className={styles.field}>
                <span>Модель</span>
                <input className={styles.text} value={model} onChange={(e) => setModel(e.target.value)} aria-label="Модель" />
              </label>
            ) : (
              <div className={styles.field} aria-label="Локальный runtime">
                <span>Модель</span>
                <div className={styles.value}>Локальный runtime</div>
              </div>
            )}
          </div>
          <label className={styles.field}>
            <span>Base URL</span>
            <input className={styles.text} value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} aria-label="Base URL" spellCheck={false} />
          </label>
          <label className={styles.field}>
            <span>API-ключ {cloud.has_api_key && <em className={styles.keyMask}>текущий: {cloud.api_key_masked}</em>}</span>
            <input
              className={styles.text}
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="Новый ключ (не отображается и не возвращается обратно)"
              aria-label="API-ключ"
              autoComplete="off"
            />
          </label>
          <div className={styles.cloudActions}>
            <button className={styles.saveBtn} onClick={() => void save()} disabled={status === 'saving' || !dirty}>
              {status === 'saving'
                ? <><Loader2 size={14} className={styles.spinSoft} /> Сохранение…</>
                : saved
                  ? <><CheckCircle2 size={14} /> Сохранено</>
                  : <><Cloud size={14} /> Сохранить</>}
            </button>
            {cloud.has_api_key && (
              <button className={styles.resetBtn} onClick={() => void save(true)} disabled={status === 'saving'}>
                <KeyRound size={13} /> Стереть ключ
              </button>
            )}
          </div>
          <p className={styles.hint}>Ключ передаётся только в settings:update и хранится на стороне backend; интерфейс получает лишь маску.</p>
        </>
      )}
    </section>
  );
}

export function SettingsPanel() {
  const { dispatch } = useUIState();
  const { settings, setTheme, setTransparency, setReduceMotion, setBackgroundUrl, profile, updateProfile } = useTheme();
  const [honorific, setHonorific] = useState(profile?.honorific ?? '');

  const close = () => dispatch({ type: 'SET_SETTINGS_OPEN', payload: false });
  const panelRef = useFocusTrap<HTMLDivElement>(true, { onEscape: close, scrollLock: true, trap: true });

  const onBgPick = (e: ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    const url = URL.createObjectURL(f);
    setBackgroundUrl(url);
  };

  return (
    <motion.div
      ref={panelRef}
      key="settings"
      className={styles.overlay}
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        onClick={close}
        role="dialog"
        aria-modal="true"
        aria-label="Настройки"
      >
        <motion.div
          className={styles.panel}
          initial={{ y: 24, opacity: 0, scale: 0.98 }}
          animate={{ y: 0, opacity: 1, scale: 1 }}
          exit={{ y: 24, opacity: 0, scale: 0.98 }}
          transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
          onClick={(e) => e.stopPropagation()}
        >
          <div className={styles.header}>
            <h2 className={styles.title}>Настройки</h2>
            <button className={styles.close} onClick={close} aria-label="Закрыть">
              <X size={18} />
            </button>
          </div>

          <div className={styles.body}>
            <section className={styles.group}>
              <label className={styles.label}>Тема</label>
              <div className={styles.themes}>
                {THEMES.map((t) => (
                  <button
                    key={t.id}
                    className={`${styles.theme} ${settings.theme === t.id ? styles.themeActive : ''}`}
                    onClick={() => setTheme(t.id)}
                    aria-pressed={settings.theme === t.id}
                  >
                    <span className={styles.swatch} style={{ background: themeSwatch(t.id) }} />
                    <span className={styles.themeLabel}>{t.label}</span>
                  </button>
                ))}
              </div>
            </section>

            <section className={styles.group}>
              <label className={styles.label}>
                Прозрачность панелей
                <span className={styles.value}>{Math.round((1 - settings.transparency) * 100)}% плотность</span>
              </label>
              <input
                type="range"
                min={0}
                max={1}
                step={0.01}
                value={settings.transparency}
                onChange={(e) => setTransparency(Number(e.target.value))}
                className={styles.slider}
                aria-label="Прозрачность панелей"
              />
              <p className={styles.hint}>Содержимое сообщений остаётся плотным — текст не теряет читаемость.</p>
            </section>

            <section className={styles.group}>
              <label className={styles.toggle}>
                <input
                  type="checkbox"
                  checked={settings.reduceMotion}
                  onChange={(e) => setReduceMotion(e.target.checked)}
                />
                <span>Уменьшить анимацию (Reduce Motion)</span>
              </label>
              <p className={styles.hint}>Также наследуется из системной настройки «Уменьшить движение».</p>
            </section>

            {settings.theme === 'personal' && (
              <section className={styles.group}>
                <label className={styles.label}>Фон (Personal)</label>
                <label className={styles.fileBtn}>
                  Загрузить изображение
                  <input type="file" accept="image/*" hidden onChange={onBgPick} />
                </label>
                {settings.backgroundUrl && (
                  <button className={styles.resetBtn} onClick={() => setBackgroundUrl(undefined)}>
                    Сбросить фон
                  </button>
                )}
              </section>
            )}

            <section className={styles.group}>
              <label className={styles.label}>Обращение</label>
              <input
                className={styles.text}
                value={honorific}
                onChange={(e) => setHonorific(e.target.value)}
                onBlur={() => profile && updateProfile({ honorific: honorific.trim() })}
                placeholder="например, по имени"
                aria-label="Обращение"
              />
              <p className={styles.hint}>Необязательное обращение. Оставьте пустым для нейтральных ответов.</p>
            </section>

            <CloudSection />
          </div>
        </motion.div>
      </motion.div>
  );
}
