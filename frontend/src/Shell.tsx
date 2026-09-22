/**
 * Which page is showing.
 *
 * Why there is no router library
 * ------------------------------
 * There are five screens and no nested routes, no route params, no loaders.
 * `react-router` would add a dependency, a provider and a mental model in
 * exchange for URL syncing this app does not currently need — the console is
 * one page with its own state, and the marketing pages are read in order.
 *
 * This is a decision that expires. The moment a screen needs to be linkable —
 * a shared conversation, a plan deep-link from an email — a real router earns
 * its place, and this file is the seam where it goes in.
 *
 * Why the console is not lazy-loaded
 * ----------------------------------
 * It is the reason people are here. Splitting it out makes the landing page
 * marginally faster and the first real interaction slower, which is the wrong
 * trade for a tool people sign into and then use all day.
 */

import { useState } from 'react';

import App from './App';
import { Auth, type AuthMode } from './pages/Auth';
import { Checkout } from './pages/Checkout';
import { Landing } from './pages/Landing';
import { Pricing } from './pages/Pricing';
import type { PlanInfo } from './api';

type Screen = 'landing' | 'pricing' | 'auth' | 'checkout' | 'console';

/** Where an unauthenticated visitor starts.
 *
 *  `console` while the API has no authentication, because gating the one
 *  working thing behind a login that does not exist yet would make the product
 *  unusable to demonstrate its own login page. This constant is the single
 *  line to change when `saas/auth.py` is wired into the API — and naming it
 *  is how that stays a deliberate switch rather than something to hunt for. */
const START: Screen = 'console';

export function Shell() {
  const [screen, setScreen] = useState<Screen>(START);
  const [mode, setMode] = useState<AuthMode>('sign-up');
  const [chosen, setChosen] = useState<PlanInfo | null>(null);

  const choose = (plan: PlanInfo) => {
    setChosen(plan);
    setScreen(plan.price_monthly_usd === 0 && !plan.custom_priced ? 'auth' : 'checkout');
    if (plan.price_monthly_usd === 0) setMode('sign-up');
  };

  if (screen === 'console') return <App onExit={() => setScreen('landing')} />;

  if (screen === 'landing') {
    return (
      <Landing
        onStart={() => {
          setMode('sign-up');
          setScreen('auth');
        }}
        onPricing={() => setScreen('pricing')}
        onSignIn={() => {
          setMode('sign-in');
          setScreen('auth');
        }}
      />
    );
  }

  if (screen === 'pricing') return <Pricing onChoose={choose} />;

  if (screen === 'checkout' && chosen) {
    return <Checkout plan={chosen} onBack={() => setScreen('pricing')} />;
  }

  return (
    <Auth
      mode={mode}
      onModeChange={setMode}
      onBack={() => setScreen('landing')}
      /* Returns the reason it did not work rather than throwing, because the
         form renders it inline and an unfinished backend is a normal answer
         here, not an exception. */
      onSubmit={async () => {
        return (
          'Accounts are not connected yet — the authentication boundary is ' +
          'built and tested but not yet wired into the API. Use the console ' +
          'directly for now.'
        );
      }}
    />
  );
}
