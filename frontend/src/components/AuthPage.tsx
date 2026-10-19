/**
 * Sign in or request access.
 *
 * Sign-up does not grant access: the administrator approves each request by
 * email, so the success state for it is "waiting", not "signed in".
 */

import { useState, type FormEvent } from 'react';
import { ApiError, startSession, type Me } from '../api';
import { authClient } from '../neonAuth';

export function AuthPage({ onSignedIn }: { onSignedIn: (me: Me) => void }) {
  const [mode, setMode] = useState<'login' | 'signup'>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      // Neon Auth proves who this is. Whether they may come in is the
      // administrator's decision, made on the server.
      const credentials = { email, password };
      const identity =
        mode === 'signup'
          ? await authClient.signUp.email({ ...credentials, name: email.split('@')[0] })
          : await authClient.signIn.email(credentials);
      if (identity.error) throw new Error(identity.error.message ?? 'Could not sign in.');

      const token = await authClient.token();
      if (!token.data?.token) throw new Error('Could not sign in.');
      onSignedIn(await startSession(token.data.token));
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        // Signed in to Neon but not yet let in. Leave no half-signed-in state.
        await authClient.signOut().catch(() => undefined);
        setNotice(err.message);
        setMode('login');
        setPassword('');
      } else {
        setError(err instanceof Error ? err.message : 'Something went wrong. Try again.');
      }
    } finally {
      setBusy(false);
    }
  }

  const field: React.CSSProperties = {
    width: '100%',
    padding: '0.6rem 0.75rem',
    border: '1px solid var(--border)',
    borderRadius: 'var(--radius-md)',
    background: 'var(--card)',
    color: 'var(--foreground)',
    font: 'inherit',
    boxSizing: 'border-box',
  };

  return (
    <main style={{ minHeight: '100vh', display: 'grid', placeItems: 'center', padding: '1rem' }}>
      <form
        onSubmit={submit}
        style={{
          width: 'min(24rem, 100%)',
          display: 'grid',
          gap: '0.9rem',
          padding: '1.75rem',
          background: 'var(--card)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius-xl)',
        }}
      >
        <h1 style={{ margin: 0, fontSize: '1.4rem' }}>
          {mode === 'login' ? 'Sign in to Aperture' : 'Request access'}
        </h1>
        {notice && <p style={{ margin: 0, color: 'var(--success)' }}>{notice}</p>}
        <input
          style={field}
          type="email"
          placeholder="Email"
          autoComplete="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
        />
        <input
          style={field}
          type="password"
          placeholder={mode === 'signup' ? 'Password (10+ characters)' : 'Password'}
          autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
          minLength={mode === 'signup' ? 10 : undefined}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />
        {error && (
          <p role="alert" style={{ margin: 0, color: 'var(--destructive)' }}>
            {error}
          </p>
        )}
        <button
          type="submit"
          disabled={busy}
          style={{
            ...field,
            background: 'var(--primary)',
            color: 'var(--primary-foreground)',
            border: 'none',
            cursor: busy ? 'wait' : 'pointer',
          }}
        >
          {mode === 'login' ? 'Sign in' : 'Request access'}
        </button>
        <button
          type="button"
          onClick={() => {
            setMode(mode === 'login' ? 'signup' : 'login');
            setError(null);
            setNotice(null);
          }}
          style={{
            background: 'none',
            border: 'none',
            color: 'var(--muted-foreground)',
            cursor: 'pointer',
            font: 'inherit',
          }}
        >
          {mode === 'login' ? 'No account? Request access' : 'Have an account? Sign in'}
        </button>
      </form>
    </main>
  );
}
