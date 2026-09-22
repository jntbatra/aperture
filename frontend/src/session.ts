/**
 * Who is signed in, if anyone.
 *
 * The server decides, not this file
 * ---------------------------------
 * The session lives in an httpOnly cookie, which JavaScript deliberately
 * cannot read — that is what stops an XSS bug from becoming a stolen session.
 * So "am I signed in?" is not a local question: it is `GET /api/auth/me`, and
 * a 401 is the answer "no".
 *
 * Any client-side flag claiming otherwise is a cache of a server decision, and
 * the failure mode of that cache is showing a signed-in interface to someone
 * whose session was revoked. Better to ask once at boot and treat every later
 * 401 as the truth.
 *
 * Three states, not two
 * ---------------------
 * `checking` is a real state and collapsing it into "signed out" produces the
 * flash of a login screen on every reload for users who are perfectly signed
 * in. It is the same class of bug as the dark-mode flash, and it is fixed the
 * same way: do not render a decision you have not made yet.
 *
 * Single-tenant deployments
 * -------------------------
 * When the server runs with `require_auth=false`, `/api/auth/me` succeeds
 * without any credential and reports the local workspace. The frontend needs
 * no mode switch of its own: it asks the same question and gets an answer
 * either way, which is exactly why the server returns a real principal in that
 * mode rather than None.
 */

import { ApiError, getAccount, type Account } from './api';

export type SessionState =
  | { status: 'checking' }
  | { status: 'signed-in'; account: Account }
  | { status: 'signed-out' };

export async function loadSession(): Promise<SessionState> {
  try {
    return { status: 'signed-in', account: await getAccount() };
  } catch (error) {
    if (error instanceof ApiError && error.needsAuth) {
      return { status: 'signed-out' };
    }
    // The API is unreachable, or broken. Not the same as signed out, but the
    // only safe rendering is the signed-out one: showing a console that cannot
    // talk to its backend is worse than showing a sign-in page.
    return { status: 'signed-out' };
  }
}
