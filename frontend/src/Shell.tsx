/**
 * The top-level component. There is one screen — the console — and no
 * sign-in, plans or accounts: whoever can reach the server can use it. Keep
 * the service on a private address (see deploy/EC2.md) rather than adding a
 * gate here.
 */

import App from './App';

export function Shell() {
  return <App />;
}
