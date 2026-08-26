/**
 * J.A.R.V.I.S. v3.0 — Theme + Settings + White Room store (frontend-only).
 *
 * Owns: active theme, panel transparency, reduce-motion preference,
 * background override (Personal theme), and the user profile captured in
 * the White Room. Persists to localStorage. Applies everything to
 * <html> as data-attributes / CSS custom properties so the CSS theme
 * system can react without any backend involvement.
 *
 * This store contains NO backend-dependent logic.
 */

import {
  createContext, useContext, useEffect, useMemo, useState, type ReactNode,
} from 'react';
import type { AppSettings, ThemeName, UserProfile } from '@/types';

const SETTINGS_KEY = 'jarvis.settings.v3';
const PROFILE_KEY = 'jarvis.profile.v3';

function hexToRgb(hex: string): string {
  const value = hex.replace('#', '');
  const normalized = value.length === 3 ? value.split('').map((part) => part + part).join('') : value;
  const parsed = Number.parseInt(normalized, 16);
  if (!Number.isFinite(parsed)) return '17, 21, 34';
  return `${(parsed >> 16) & 255}, ${(parsed >> 8) & 255}, ${parsed & 255}`;
}

const DEFAULT_SETTINGS: AppSettings = {
  theme: 'olympus',
  transparency: 0.35,
  reduceMotion: false,
  primaryAccent: '#38d9e6',
  secondaryAccent: '#9b6cff',
  tertiaryAccent: '#ff4fa3',
  energyAccent: '#ff9f43',
  successAccent: '#62e6a7',
  errorAccent: '#ff526d',
  backgroundBase: '#05060b',
  panelTint: '#111522',
  glowIntensity: 0.72,
  saturation: 1.12,
  contrast: 1.04,
};

function loadSettings(): AppSettings {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    if (raw) return { ...DEFAULT_SETTINGS, ...(JSON.parse(raw) as Partial<AppSettings>) };
  } catch {
    /* corrupt entry — fall back to defaults */
  }
  return DEFAULT_SETTINGS;
}

function loadProfile(): UserProfile | null {
  try {
    const raw = localStorage.getItem(PROFILE_KEY);
    if (raw) return JSON.parse(raw) as UserProfile;
  } catch {
    /* ignore */
  }
  return null;
}

interface ThemeContextValue {
  settings: AppSettings;
  profile: UserProfile | null;
  hasOnboarded: boolean;
  reduceMotion: boolean;
  setTheme: (t: ThemeName) => void;
  setTransparency: (v: number) => void;
  setReduceMotion: (v: boolean) => void;
  setBackgroundUrl: (url: string | undefined) => void;
  setPalette: (palette: Partial<Pick<AppSettings, 'primaryAccent' | 'secondaryAccent' | 'tertiaryAccent' | 'energyAccent' | 'successAccent' | 'errorAccent' | 'backgroundBase' | 'panelTint'>>) => void;
  setGlowIntensity: (v: number) => void;
  setSaturation: (v: number) => void;
  setContrast: (v: number) => void;
  completeOnboarding: (name: string, honorific?: string) => void;
  updateProfile: (p: Partial<UserProfile>) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [settings, setSettings] = useState<AppSettings>(loadSettings);
  const [profile, setProfile] = useState<UserProfile | null>(loadProfile);
  const [systemReduce, setSystemReduce] = useState(false);

  // Track OS-level reduced-motion preference.
  useEffect(() => {
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
    const update = () => setSystemReduce(mq.matches);
    update();
    mq.addEventListener('change', update);
    return () => mq.removeEventListener('change', update);
  }, []);

  const reduceMotion = settings.reduceMotion || systemReduce;

  // Reflect state onto <html> so the CSS theme layer can react.
  useEffect(() => {
    const el = document.documentElement;
    el.setAttribute('data-theme', settings.theme);
    el.setAttribute('data-reduce-motion', reduceMotion ? 'true' : 'false');
    el.style.setProperty('--transparency', String(settings.transparency));
    el.style.setProperty('--accent-primary', settings.primaryAccent);
    el.style.setProperty('--accent-secondary', settings.secondaryAccent);
    el.style.setProperty('--accent-tertiary', settings.tertiaryAccent);
    el.style.setProperty('--accent-energy', settings.energyAccent);
    el.style.setProperty('--accent-success', settings.successAccent);
    el.style.setProperty('--accent-error', settings.errorAccent);
    el.style.setProperty('--surface-base', settings.backgroundBase);
    el.style.setProperty('--surface-panel-rgb', hexToRgb(settings.panelTint));
    el.style.setProperty('--glow-intensity', String(settings.glowIntensity));
    el.style.setProperty('--ui-saturation', String(settings.saturation));
    el.style.setProperty('--ui-contrast', String(settings.contrast));
    if (settings.backgroundUrl) {
      el.style.setProperty('--personal-bg', `url("${settings.backgroundUrl}")`);
    } else {
      el.style.removeProperty('--personal-bg');
    }
  }, [settings, reduceMotion]);

  // Persist.
  useEffect(() => {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
  }, [settings]);
  useEffect(() => {
    if (profile) localStorage.setItem(PROFILE_KEY, JSON.stringify(profile));
  }, [profile]);

  const value = useMemo<ThemeContextValue>(
    () => ({
      settings,
      profile,
      hasOnboarded: !!profile,
      reduceMotion,
      setTheme: (t) => setSettings((s) => ({ ...s, theme: t })),
      setTransparency: (v) =>
        setSettings((s) => ({ ...s, transparency: Math.max(0, Math.min(1, v)) })),
      setReduceMotion: (v) => setSettings((s) => ({ ...s, reduceMotion: v })),
      setBackgroundUrl: (url) => setSettings((s) => ({ ...s, backgroundUrl: url })),
      setPalette: (palette) => setSettings((s) => ({ ...s, ...palette })),
      setGlowIntensity: (v) => setSettings((s) => ({ ...s, glowIntensity: Math.max(0, Math.min(1, v)) })),
      setSaturation: (v) => setSettings((s) => ({ ...s, saturation: Math.max(0.7, Math.min(1.5, v)) })),
      setContrast: (v) => setSettings((s) => ({ ...s, contrast: Math.max(0.85, Math.min(1.25, v)) })),
      completeOnboarding: (name, honorific = '') => {
        setProfile({ name: name.trim() || 'Guest', honorific, createdAt: Date.now() });
      },
      updateProfile: (p) => setProfile((prev) => (prev ? { ...prev, ...p } : prev)),
    }),
    [settings, profile, reduceMotion]
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme() {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error('useTheme must be used within ThemeProvider');
  return ctx;
}
