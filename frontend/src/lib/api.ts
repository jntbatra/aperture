export type NodeUpdate = Record<string, unknown>

export interface FinalPayload {
  answer?: string
  sql?: string
  columns?: string[]
  rows?: unknown[][]
  row_count?: number
  chart_spec?: Record<string, unknown> | null
  assumptions?: string
  status?: string
  attempts?: number
  identifier_fixes?: string[]
  usage?: string
}

export interface StreamHandlers {
  onNode: (node: string, update: NodeUpdate) => void
  onFinal: (payload: FinalPayload) => void
  onError: (message: string) => void
}

/**
 * Read the /ask server-sent event stream.
 *
 * EventSource cannot POST, so the stream is parsed by hand: split on blank
 * lines, then read the `event:` and `data:` fields of each frame.
 */
export async function askStream(
  question: string,
  threadId: string,
  handlers: StreamHandlers,
): Promise<void> {
  const response = await fetch('/api/ask', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ question, thread_id: threadId }),
  })

  if (!response.ok || !response.body) {
    handlers.onError(`request failed: ${response.status}`)
    return
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

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
        else if (event === 'error') handlers.onError(payload.message ?? 'unknown error')
      } catch {
        // A partial frame is not an error; the next chunk completes it.
      }
    }
  }
}

export interface SchemaSummary {
  dialect: string
  tables: { name: string; rows: number; columns: string[] }[]
  empty_tables: string[]
  foreign_keys: number
}

export async function fetchSchema(): Promise<SchemaSummary> {
  const response = await fetch('/api/schema')
  return response.json()
}
