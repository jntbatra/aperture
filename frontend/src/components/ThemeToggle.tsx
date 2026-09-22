/**
 * The theme control: light, dark, or follow the system.
 *
 * A three-way segmented control rather than a switch, because the third state
 * is not decoration. "Follow the system" is the default and cannot be
 * expressed by a toggle — a switch forces the first click to pin the user to a
 * theme forever, and "go back to following my desktop" then has no gesture.
 *
 * Labelled with words, not just icons. A sun and a moon are guessable; a
 * monitor glyph meaning "match my OS" is not, and this control is used once
 * and then never again, which is exactly when an unlabelled icon fails.
 */

import { useEffect, useState } from 'react';

import {
  applyTheme,
  loadTheme,
  saveTheme,
  watchSystemTheme,
  type ThemeChoice,
} from '../theme';

const CHOICES: { value: ThemeChoice; label: string; hint: string }[] = [
  { value: 'light', label: 'Light', hint: 'Always the light theme' },
  { value: 'dark', label: 'Dark', hint: 'Always the dark theme' },
  { value: 'system', label: 'Auto', hint: 'Follow your operating system' },
];

export function ThemeToggle() {
  const [choice, setChoice] = useState<ThemeChoice>(loadTheme);

  // The OS can change under us while the app is open — at sunset, or when
  // someone flips their desktop theme. Only acted on while the user is on
  // `system`; an explicit choice outranks the operating system.
  useEffect(() => watchSystemTheme(() => choice), [choice]);

  const pick = (next: ThemeChoice) => {
    setChoice(next);
    saveTheme(next);
    applyTheme(next);
  };

  return (
    <div className="theme-toggle" role="group" aria-label="Colour theme">
      {CHOICES.map((option) => (
        <button
          key={option.value}
          type="button"
          className={`theme-toggle__option ${
            choice === option.value ? 'theme-toggle__option--on' : ''
          }`}
          aria-pressed={choice === option.value}
          title={option.hint}
          onClick={() => pick(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
