export interface NodeUpdate {
  [key: string]: unknown
}

export interface FinalPayload {
  conversation_id?: string
  answer?: string
  sql?: string
  columns?: string[]
  rows?: unknown[][]
  row_count?: number
  chart_spec?: Record<string, unknown> | null
  assumptions?: string
  verification?: { kind: string; message: string }[]
  insights?: { kind: string; message: string }[]
  clarifying_question?: string
  clarify_options?: string[]
  suggestions?: { text: string; reason: string }[]
  status?: string
  attempts?: number
  identifier_fixes?: string[]
  usage?: string
}

export interface ConversationSummary {
  id: string
  title: string
  connection: string
  updated_at: number
  messages: number
}

export interface StoredMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  payload: FinalPayload
  created_at: number
}

export interface ConnectionInfo {
  name: string
  kind: string
  dialect?: string
  target?: string
}

export interface Me {
  id: string
  email: string
  name: string
  picture: string
  anonymous: boolean
  auth_enabled: boolean
}

/**
 * Where the API lives.
 *
 * In development Vite proxies /api to the backend. In production the backend
 * serves this bundle itself, so the routes sit at the root -- prefixing /api
 * there 404s every call and the app silently looks empty.
 */
const API_BASE = import.meta.env.DEV ? '/api' : ''

const TOKEN_KEY = 'aperture.token'

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? ''
}

export function setToken(token: string): void {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

function headers(extra: Record<string, string> = {}): Record<string, string> {
  const token = getToken()
  return token ? { ...extra, authorization: `Bearer ${token}` } : extra
}

async function json<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: headers(init.body ? { 'content-type': 'application/json' } : {}),
  })
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    throw new Error(detail.detail ?? `${response.status} ${response.statusText}`)
  }
  return response.json() as Promise<T>
}

export const fetchMe = () => json<Me>('/me')
export const fetchConversations = () => json<ConversationSummary[]>('/conversations')
export const fetchConversation = (id: string) =>
  json<{ id: string; title: string; connection: string; messages: StoredMessage[] }>(
    `/conversations/${id}`,
  )
export const createConversation = (connection: string) =>
  json<{ id: string; title: string; connection: string }>('/conversations', {
    method: 'POST',
    body: JSON.stringify({ connection }),
  })
export const renameConversation = (id: string, title: string) =>
  json<{ ok: boolean }>(`/conversations/${id}`, { method: 'PATCH', body: JSON.stringify({ title }) })
export const deleteConversation = (id: string) =>
  json<{ ok: boolean }>(`/conversations/${id}`, { method: 'DELETE' })

export const fetchConnections = () =>
  json<{ active: string; connections: ConnectionInfo[] }>('/connections')
export const addConnection = (name: string, url: string) =>
  json<{ ok: boolean }>('/connections', { method: 'POST', body: JSON.stringify({ name, url }) })
export const removeConnection = (name: string) =>
  json<{ ok: boolean }>(`/connections/${encodeURIComponent(name)}`, { method: 'DELETE' })

export const fetchAuthConfig = () =>
  json<{ enabled: boolean; client_id: string }>('/auth/config')
export const signInWithGoogle = (credential: string) =>
  json<{ token: string; user: Me }>('/auth/google', {
    method: 'POST',
    body: JSON.stringify({ credential }),
  })

export interface UploadResult {
  name: string
  kind: string
  rows: number
  table: string
  tables: { name: string; rows: number; columns: number }[]
  conversation_id: string
  columns: { name: string; type: string }[]
}

export async function uploadDataset(file: File, conversationId = ''): Promise<UploadResult> {
  const form = new FormData()
  form.append('file', file)
  if (conversationId) form.append('conversation_id', conversationId)
  const response = await fetch(`${API_BASE}/upload`, { method: 'POST', headers: headers(), body: form })
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    throw new Error(detail.detail ?? 'upload failed')
  }
  return response.json() as Promise<UploadResult>
}

export interface StreamHandlers {
  onStart?: (payload: { conversation_id: string; connection: string }) => void
  onNode: (node: string, update: NodeUpdate) => void
  onFinal: (payload: FinalPayload) => void
  onError: (message: string) => void
}

/**
 * Read the /ask server-sent event stream.
 *
 * EventSource cannot POST, so frames are parsed by hand. The server sends CRLF
 * line endings, so they are normalised before framing -- splitting on "\n\n"
 * alone silently drops every event.
 */
export async function askStream(
  question: string,
  conversationId: string,
  connection: string,
  handlers: StreamHandlers,
): Promise<void> {
  const response = await fetch(`${API_BASE}/ask`, {
    method: 'POST',
    headers: headers({ 'content-type': 'application/json' }),
    body: JSON.stringify({ question, conversation_id: conversationId, connection }),
  })

  if (!response.ok || !response.body) {
    const detail = await response.json().catch(() => ({}))
    handlers.onError(detail.detail ?? `request failed: ${response.status}`)
    return
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')

    let boundary = buffer.indexOf('\n\n')
    while (boundary !== -1) {
      const frame = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      boundary = buffer.indexOf('\n\n')

      let event = 'message'
      const dataLines: string[] = []
      for (const line of frame.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim()
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
      }
      if (!dataLines.length) continue

      try {
        const payload = JSON.parse(dataLines.join('\n'))
        if (event === 'node') handlers.onNode(payload.node, payload.update ?? {})
        else if (event === 'final') handlers.onFinal(payload)
        else if (event === 'start') handlers.onStart?.(payload)
        else if (event === 'error') handlers.onError(payload.message ?? 'unknown error')
      } catch {
        // A partial frame is not an error; the next chunk completes it.
      }
    }
  }
}
