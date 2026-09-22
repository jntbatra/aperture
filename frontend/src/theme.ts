/**
 * Light, dark, or follow the system.
 *
 * Three states, not two
 * --------------------
 * `light` and `dark` are explicit choices. `system` is the default and means
 * "keep following the OS", which is a different thing from "currently light" —
 * a user on `system` who changes their desktop at sunset expects the app to
 * move with it, and one who picked `light` expects it not to.
 *
 * Collapsing this to a boolean is the usual mistake. It makes the first toggle
 * pin the user to whichever theme they happened to be seeing, permanently.
 *
 * Why the class goes on <html>
 * ----------------------------
 * `color-scheme` on the root is what makes the browser's own surfaces follow —
 * scrollbars, form controls, the flash between navigations. Setting it on
 * <body> leaves a white scrollbar beside a dark page.
 *
 * Why it is applied before React mounts
 * -------------------------------------
 * See `applyStoredTheme`, called from a blocking script in index.html. React
 * mounting is asynchronous, so a theme applied in an effect arrives one paint
 * too late and the user sees a white flash on every load. That flash is the
 * single most common dark-mode bug and it is only fixable before hydration.
 */

export type ThemeChoice = 'light' | 'dark' | 'system';

const STORE_KEY = 'aperture.theme';

function prefersDark(): boolean {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false;
}

export function loadTheme(): ThemeChoice {
  try {
    const stored = localStorage.getItem(STORE_KEY);
    return stored === 'light' || stored === 'dark' ? stored : 'system';
  } catch {
    // Private browsing. A missing preference should cost the preference, not
    // the page.
    return 'system';
  }
}

/** Resolve a choice to what is actually painted. */
export function resolveTheme(choice: ThemeChoice): 'light' | 'dark' {
  return choice === 'system' ? (prefersDark() ? 'dark' : 'light') : choice;
}

export function applyTheme(choice: ThemeChoice): void {
  document.documentElement.classList.toggle('dark', resolveTheme(choice) === 'dark');
}

export function saveTheme(choice: ThemeChoice): void {
  try {
    if (choice === 'system') localStorage.removeItem(STORE_KEY);
    else localStorage.setItem(STORE_KEY, choice);
  } catch {
    /* the choice still applies for this session */
  }
}

/**
 * Apply the stored theme immediately, before React exists.
 *
 * Called from a small blocking script in `index.html`. Blocking is correct
 * here and almost nowhere else: it runs in under a millisecond, and the thing
 * it prevents — a full-page white flash before the dark theme lands — is worse
 * than the delay.
 */
export function applyStoredTheme(): void {
  applyTheme(loadTheme());
}

/**
 * Follow the OS while the user is on `system`, and stop when they are not.
 *
 * Returns an unsubscribe function. Without this, a user on `system` keeps
 * whatever theme was resolved at load until they reload — which looks like the
 * toggle is broken rather than like nothing is listening.
 */
export function watchSystemTheme(getChoice: () => ThemeChoice): () => void {
  const query = window.matchMedia?.('(prefers-color-scheme: dark)');
  if (!query) return () => {};

  const onChange = () => {
    if (getChoice() === 'system') applyTheme('system');
  };

  query.addEventListener('change', onChange);
  return () => query.removeEventListener('change', onChange);
}
