/* Theme state. The tokens themselves live in theme.css; this only decides
 * which set is active and remembers the choice.
 *
 * The initial value is already on <html> by the time React mounts — index.html
 * resolves it in a blocking <head> script so the first paint is never the
 * wrong theme. This hook reads that attribute rather than recomputing it, so
 * the two can't disagree.
 */
import { useCallback, useEffect, useState } from 'react';

const KEY = 'vq-theme';

const current = () =>
  typeof document !== 'undefined' &&
  document.documentElement.getAttribute('data-theme') === 'light'
    ? 'light'
    : 'dark';

function apply(theme) {
  const root = document.documentElement;
  /* Transitions are enabled only for the duration of the switch — leaving them
     on would make every ordinary hover and live-value update fade. */
  root.classList.add('theming');
  root.setAttribute('data-theme', theme);
  root.style.colorScheme = theme;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', theme === 'light' ? '#F3F5F8' : '#0A0A0C');
  window.setTimeout(() => root.classList.remove('theming'), 320);
}

export function useTheme() {
  const [theme, setTheme] = useState(current);

  /* Follow the OS only while the user hasn't expressed a preference. */
  useEffect(() => {
    if (localStorage.getItem(KEY)) return undefined;
    const mq = window.matchMedia('(prefers-color-scheme: light)');
    const onChange = (e) => {
      const next = e.matches ? 'light' : 'dark';
      apply(next);
      setTheme(next);
    };
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);

  const toggle = useCallback(() => {
    setTheme((prev) => {
      const next = prev === 'light' ? 'dark' : 'light';
      try {
        localStorage.setItem(KEY, next);
      } catch {
        /* Private mode — the theme still applies, it just won't be remembered. */
      }
      apply(next);
      return next;
    });
  }, []);

  return { theme, toggle };
}
