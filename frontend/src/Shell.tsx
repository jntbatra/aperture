/**
 * The top-level component. Nothing is shown until there is a signed-in,
 * approved user; the server enforces the same rule on every /api route, so
 * this gate is only about showing the right screen.
 */

import { useEffect, useState } from 'react';
import App from './App';
import { getMe, logout, type Me } from './api';
import { authClient } from './neonAuth';
import { AuthPage } from './components/AuthPage';

export function Shell() {
  // `undefined` while the first check is in flight, `null` when signed out.
  const [me, setMe] = useState<Me | null | undefined>(undefined);

  useEffect(() => {
    getMe().then(setMe, () => setMe(null));
  }, []);

  if (me === undefined) return null;
  if (me === null) return <AuthPage onSignedIn={setMe} />;

  return (
    <>
      <App />
      <button
        onClick={() =>
          Promise.allSettled([logout(), authClient.signOut()]).then(() => setMe(null))
        }
        title={`Signed in as ${me.email}`}
        style={{
          position: 'fixed',
          top: '0.5rem',
          right: '0.75rem',
          zIndex: 50,
          background: 'var(--card)',
          color: 'var(--muted-foreground)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius-md)',
          padding: '0.25rem 0.6rem',
          font: 'inherit',
          fontSize: '0.8rem',
          cursor: 'pointer',
        }}
      >
        Sign out
      </button>
    </>
  );
}
