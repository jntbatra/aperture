/**
 * Google Identity Services, loaded only when sign-in is configured.
 *
 * The browser receives an ID token which the backend verifies against Google's
 * public keys, so there is no client secret anywhere in this app.
 */
declare global {
  interface Window {
    google?: any
  }
}

let scriptPromise: Promise<void> | null = null

function loadScript(): Promise<void> {
  if (scriptPromise) return scriptPromise
  scriptPromise = new Promise((resolve, reject) => {
    const existing = document.querySelector('script[data-gsi]')
    if (existing) return resolve()
    const script = document.createElement('script')
    script.src = 'https://accounts.google.com/gsi/client'
    script.async = true
    script.defer = true
    script.dataset.gsi = 'true'
    script.onload = () => resolve()
    script.onerror = () => reject(new Error('could not load Google sign-in'))
    document.head.appendChild(script)
  })
  return scriptPromise
}

export async function renderGoogleButton(
  clientId: string,
  element: HTMLElement,
  onCredential: (credential: string) => void,
): Promise<void> {
  await loadScript()
  if (!window.google?.accounts?.id) throw new Error('Google sign-in unavailable')

  window.google.accounts.id.initialize({
    client_id: clientId,
    callback: (response: { credential: string }) => onCredential(response.credential),
  })
  window.google.accounts.id.renderButton(element, {
    theme: 'filled_black',
    size: 'medium',
    text: 'signin_with',
    shape: 'pill',
  })
}

export function signOutGoogle(): void {
  window.google?.accounts?.id?.disableAutoSelect?.()
}

export {}
