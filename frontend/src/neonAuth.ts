/** The Neon Auth client. Handles sign-up, sign-in and tokens; the server decides
 *  whether a signed-in person is let in (see `startSession`). */

import { createAuthClient } from '@neondatabase/auth';

export const authClient = createAuthClient(import.meta.env.VITE_NEON_AUTH_URL);
