/**
 * J.A.R.V.I.S. v3.0 — White Room (first run)
 * Calm, light onboarding. The ONLY place J.A.R.V.I.S. appears large.
 * Captures how to address the user (real client-side profile).
 * On complete → fades out, revealing the main bridge; the large mark
 * becomes the small CoreSymbol in the titlebar/sidebar.
 */

import { useState, type KeyboardEvent, type FormEvent } from 'react';
import { motion } from 'framer-motion';
import { useTheme } from '@/stores/themeStore';
import { useFocusTrap } from '@/hooks/useFocusTrap';
import { CoreSymbol } from '@/components/CoreSymbol/CoreSymbol';
import styles from './WhiteRoom.module.css';

export function WhiteRoom() {
  const { completeOnboarding, reduceMotion } = useTheme();
  const [name, setName] = useState('');
  const [done, setDone] = useState(false);
  const roomRef = useFocusTrap<HTMLDivElement>(true, { trap: true, scrollLock: true });

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) return;
    completeOnboarding(trimmed);
    setDone(true);
  };

  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') { e.preventDefault(); submit(); }
  };

  const dur = reduceMotion ? 0.001 : (done ? 0.9 : 0.6);

  return (
    <motion.div
      ref={roomRef}
      className={styles.room}
      initial={{ opacity: 1 }}
      animate={{ opacity: done ? 0 : 1 }}
      transition={{ duration: dur, ease: [0.16, 1, 0.3, 1] }}
      style={{ pointerEvents: done ? 'none' : 'auto' }}
      role="dialog"
      aria-modal="true"
      aria-label="Первый запуск"
    >
      <div className={styles.center}>
        <motion.div
          className={styles.mark}
          initial={{ scale: 1, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          transition={{ duration: 0.6, ease: [0.16, 1, 0.3, 1] }}
        >
          <span className={styles.word}>J.A.R.V.I.S.</span>
          <span className={styles.coreLine}><CoreSymbol state="idle" size={26} /></span>
        </motion.div>

        {!done ? (
          <motion.div
            className={styles.form}
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.25, duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
          >
            <p className={styles.q}>Персонализация профиля</p>
            <form onSubmit={submit} className={styles.row}>
              <input
                className={styles.input}
                value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={onKey}
                placeholder="Ваше имя"
                autoFocus
                aria-label="Ваше имя"
              />
              <button type="submit" className={styles.btn} disabled={!name.trim()}>
                Продолжить →
              </button>
            </form>
            <p className={styles.note}>Enter также подтверждает.</p>
          </motion.div>
        ) : (
          <motion.p
            className={styles.confirm}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ duration: 0.5 }}
          >
            Профиль сохранён. Обращение можно изменить в настройках.
          </motion.p>
        )}
      </div>
    </motion.div>
  );
}
