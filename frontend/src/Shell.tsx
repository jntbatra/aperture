/**
 * Which screen is showing, and whether the visitor may see it.
 *
 * Gating happens here and is enforced elsewhere
 * ---------------------------------------------
 * This file decides what to *render*. It is not the security boundary — the
 * boundary is `PrincipalDep` on every data route, and a determined visitor
 * can render whatever they like from DevTools. What this file must not do is
 * show a working console to someone the server will refuse, because that
 * produces an interface full of red errors instead of a sign-in page.
 *
 * Single-tenant deployments need no special case
 * ----------------------------------------------
 * When the server runs with `require_auth=false`, `/api/auth/me` answers
 * without a credential and reports the local workspace, so the session check
 * below resolves to `signed-in` and the console opens directly. That is why
 * the server returns a real principal in that mode rather than None: it means
 * this file has one code path, and the hosted path is the one that gets
 * exercised locally every day.
 *
 * Why there is still no router library
 * ------------------------------------
 * Six screens, no nested routes, no route params. The moment a screen needs to
 * be linkable — a shared conversation, a plan deep-link from an email — a real
 * router earns its place and this file is the seam where it goes in.
 */

import { useCallback, useEffect, useState } from 'react';

import App from './App';
import { Account } from './pages/Account';
import { Auth, type AuthMode } from './pages/Auth';
import { Checkout } from './pages/Checkout';
import { Landing } from './pages/Landing';
import { Pricing } from './pages/Pricing';
import { ApiError, signIn, signUp, type PlanInfo } from './api';
import { loadSession, type SessionState } from './session';

type Screen = 'landing' | 'pricing' | 'auth' | 'checkout' | 'console';

export function Shell() {
  const [session, setSession] = useState<SessionState>({ status: 'checking' });
  const [screen, setScreen] = useState<Screen>('landing');
  const [mode, setMode] = useState<AuthMode>('sign-up');
  const [chosen, setChosen] = useState<PlanInfo | null>(null);
  const [accountOpen, setAccountOpen] = useState(false);

  const refresh = useCallback(async () => {
    const next = await loadSession();
    setSession(next);
    if (next.status === 'signed-in') setScreen('console');
    return next;
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  /* `checking` is a real state. Collapsing it into "signed out" flashes a
     sign-in screen on every reload for users who are perfectly signed in —
     the same class of bug as the dark-mode flash. */
  if (session.status === 'checking') {
    return <div className="boot">Loading…</div>;
  }

  const choose = (plan: PlanInfo) => {
    setChosen(plan);
    if (session.status !== 'signed-in' && !plan.custom_priced && plan.price_monthly_usd === 0) {
      setMode('sign-up');
      setScreen('auth');
      return;
    }
    setScreen('checkout');
  };

  const submit = async (nextMode: AuthMode, email: string, password: string) => {
    try {
      if (nextMode === 'sign-up') await signUp(email, password);
      else await signIn(email, password);
    } catch (error) {
      // Returned rather than thrown: the form renders it inline, and a wrong
      // password is an ordinary outcome of a sign-in, not an exception.
      return error instanceof ApiError ? error.message : 'Something went wrong.';
    }
    await refresh();
    return null;
  };

  if (screen === 'console' && session.status === 'signed-in') {
    return (
      <>
        <App
          onExit={() => setScreen('pricing')}
          account={session.account}
          onAccount={() => setAccountOpen(true)}
        />
        {accountOpen && (
          <Account
            account={session.account}
            onClose={() => setAccountOpen(false)}
            onUpgrade={() => {
              setAccountOpen(false);
              setScreen('pricing');
            }}
            onSignedOut={() => {
              setAccountOpen(false);
              setSession({ status: 'signed-out' });
              setScreen('landing');
            }}
          />
        )}
      </>
    );
  }

  if (screen === 'pricing') {
    return <Pricing onChoose={choose} />;
  }

  if (screen === 'checkout' && chosen) {
    return <Checkout plan={chosen} onBack={() => setScreen('pricing')} />;
  }

  if (screen === 'auth') {
    return (
      <Auth
        mode={mode}
        onModeChange={setMode}
        onBack={() => setScreen('landing')}
        onSubmit={submit}
      />
    );
  }

  return (
    <Landing
      onStart={() => {
        if (session.status === 'signed-in') {
          setScreen('console');
          return;
        }
        setMode('sign-up');
        setScreen('auth');
      }}
      onPricing={() => setScreen('pricing')}
      onSignIn={() => {
        if (session.status === 'signed-in') {
          setScreen('console');
          return;
        }
        setMode('sign-in');
        setScreen('auth');
      }}
    />
  );
}
