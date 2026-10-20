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
import { Landing } from './components/Landing';

export function Shell() {
  // `undefined` while the first check is in flight, `null` when signed out.
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  // Signed-out visitors see the home page first; the form is one click away.
  const [signingIn, setSigningIn] = useState(false);

  useEffect(() => {
    getMe().then(setMe, () => setMe(null));
  }, []);

  if (me === undefined) return null;
  if (me === null) {
    return signingIn ? (
      <AuthPage onSignedIn={setMe} onBack={() => setSigningIn(false)} />
    ) : (
      <Landing onEnter={() => setSigningIn(true)} />
    );
  }

  return (
    <>
      <App
        onSignOut={() =>
          Promise.allSettled([logout(), authClient.signOut()]).then(() => {
            setSigningIn(false);
            setMe(null);
          })
        }
        email={me.email}
      />
    </>
  );
}
