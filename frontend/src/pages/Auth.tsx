/**
 * Sign in and sign up.
 *
 * One component, two modes
 * ------------------------
 * The forms differ by one field and one button label. Two components would be
 * two places to fix a validation bug, and the mode is a prop because switching
 * between them must not lose what the user already typed — a sign-in that
 * clears the email when you realise you need an account is a small cruelty
 * that costs signups.
 *
 * What this does not do yet
 * -------------------------
 * There is no session endpoint behind it. `onSubmit` is wired by the caller
 * and currently reports that. This is deliberate and visible rather than a
 * form that appears to work and silently does nothing: a fake login is worse
 * than an honest "not connected yet", because the first one is discovered by a
 * user and the second by whoever is reading this.
 *
 * Passwords are not validated here beyond length
 * ----------------------------------------------
 * Strength rules belong on the server, where they cannot be edited in
 * DevTools, and duplicating them here means two rules that disagree. The one
 * check kept is length, because it is instant feedback on the commonest
 * mistake rather than a security control.
 */

import { useState, type FormEvent } from 'react';

export type AuthMode = 'sign-in' | 'sign-up';

type Props = {
  mode: AuthMode;
  onModeChange: (mode: AuthMode) => void;
  onSubmit: (mode: AuthMode, email: string, password: string) => Promise<string | null>;
  onBack: () => void;
};

const MIN_PASSWORD = 12;

export function Auth({ mode, onModeChange, onSubmit, onBack }: Props) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const signingUp = mode === 'sign-up';
  const tooShort = signingUp && password.length > 0 && password.length < MIN_PASSWORD;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setProblem(null);
    try {
      setProblem(await onSubmit(mode, email, password));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth">
      <button className="link-button auth__back" onClick={onBack}>
        ← Back
      </button>

      <div className="auth__card">
        <span className="header__mark" />
        <h1>{signingUp ? 'Create your workspace' : 'Welcome back'}</h1>
        <p className="auth__sub">
          {signingUp
            ? 'Twenty questions a month, free. No card.'
            : 'Sign in to your workspace.'}
        </p>

        <form onSubmit={submit}>
          <label htmlFor="auth-email">Email</label>
          <input
            id="auth-email"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            placeholder="you@company.com"
          />

          <label htmlFor="auth-password">Password</label>
          <input
            id="auth-password"
            type="password"
            /* `new-password` on sign-up tells a password manager to offer a
               generated one, which is the single highest-value thing this form
               can do for account security. */
            autoComplete={signingUp ? 'new-password' : 'current-password'}
            required
            minLength={signingUp ? MIN_PASSWORD : undefined}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
          {signingUp && (
            <small className={tooShort ? 'auth__hint auth__hint--warn' : 'auth__hint'}>
              At least {MIN_PASSWORD} characters. Length beats punctuation.
            </small>
          )}

          {problem && <p className="auth__problem">{problem}</p>}

          <button className="primary-button auth__submit" type="submit" disabled={busy}>
            {busy ? 'Working…' : signingUp ? 'Create workspace' : 'Sign in'}
          </button>
        </form>

        <p className="auth__switch">
          {signingUp ? 'Already have a workspace?' : 'No workspace yet?'}{' '}
          <button
            className="link-button"
            onClick={() => onModeChange(signingUp ? 'sign-in' : 'sign-up')}
          >
            {signingUp ? 'Sign in' : 'Create one'}
          </button>
        </p>
      </div>
    </div>
  );
}
