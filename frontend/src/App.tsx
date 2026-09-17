import { useCallback, useEffect, useRef, useState } from 'react'
import {
  addConnection,
  askStream,
  createConversation,
  deleteConversation,
  fetchAuthConfig,
  fetchConnections,
  fetchConversation,
  fetchConversations,
  fetchMe,
  removeConnection,
  setToken,
  signInWithGoogle,
  uploadDataset,
  type ConnectionInfo,
  type ConversationSummary,
  type Me,
} from './lib/api'
import { renderGoogleButton, signOutGoogle } from './lib/google'
import { Sidebar } from './components/Sidebar'
import { describeStep } from './components/Timeline'
import { Turn, type TurnData } from './components/Turn'

const STARTERS = [
  'What was total revenue by region?',
  'How did this change month by month?',
  'Which product sells the most?',
  'What share of orders were refunded?',
]

export default function App() {
  const [me, setMe] = useState<Me | null>(null)
  const [conversations, setConversations] = useState<ConversationSummary[]>([])
  const [activeId, setActiveId] = useState('')
  const [connections, setConnections] = useState<ConnectionInfo[]>([])
  const [activeConnection, setActiveConnection] = useState('')
  const [turns, setTurns] = useState<TurnData[]>([])
  const [question, setQuestion] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const endRef = useRef<HTMLDivElement>(null)

  const refreshConnections = useCallback(async () => {
    const data = await fetchConnections().catch(() => null)
    if (!data) return
    setConnections(data.connections)
    setActiveConnection((current) => current || data.active)
  }, [])

  const refreshConversations = useCallback(async () => {
    setConversations(await fetchConversations().catch(() => []))
  }, [])

  useEffect(() => {
    fetchMe().then(setMe).catch(() => setMe(null))
    refreshConnections()
    refreshConversations()
  }, [refreshConnections, refreshConversations])

  // Sign-in button renders only when the server has a client id configured.
  useEffect(() => {
    if (!me || !me.anonymous || !me.auth_enabled) return
    let cancelled = false
    fetchAuthConfig()
      .then(async ({ enabled, client_id }) => {
        const host = document.getElementById('google-signin')
        if (!enabled || !client_id || !host || cancelled) return
        await renderGoogleButton(client_id, host, async (credential) => {
          const result = await signInWithGoogle(credential).catch((err) => {
            setNotice(String(err.message ?? err))
            return null
          })
          if (!result) return
          setToken(result.token)
          setMe(await fetchMe())
          await Promise.all([refreshConnections(), refreshConversations()])
        })
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [me, refreshConnections, refreshConversations])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [turns])

  async function openChat(id: string) {
    setActiveId(id)
    const detail = await fetchConversation(id).catch(() => null)
    if (!detail) return
    setActiveConnection(detail.connection || activeConnection)

    // Rebuild the transcript: each question and the answer it produced.
    const restored: TurnData[] = []
    for (const message of detail.messages) {
      if (message.role === 'user') {
        restored.push({ question: message.content, steps: [], running: false })
      } else if (restored.length) {
        restored[restored.length - 1].final = message.payload
      }
    }
    setTurns(restored)
  }

  async function newChat() {
    const conversation = await createConversation(activeConnection).catch(() => null)
    if (!conversation) return
    setActiveId(conversation.id)
    setTurns([])
    await refreshConversations()
  }

  async function removeChat(id: string) {
    await deleteConversation(id).catch(() => undefined)
    if (id === activeId) {
      setActiveId('')
      setTurns([])
    }
    await refreshConversations()
  }

  async function submit(text: string) {
    const trimmed = text.trim()
    if (!trimmed || busy) return
    setQuestion('')
    setBusy(true)
    setNotice('')

    const index = turns.length
    setTurns((previous) => [...previous, { question: trimmed, steps: [], running: true }])
    const patch = (update: Partial<TurnData>) =>
      setTurns((previous) =>
        previous.map((turn, position) => (position === index ? { ...turn, ...update } : turn)),
      )

    await askStream(trimmed, activeId, activeConnection, {
      onStart: ({ conversation_id }) => setActiveId(conversation_id),
      onNode: (node, update) =>
        setTurns((previous) =>
          previous.map((turn, position) =>
            position === index
              ? { ...turn, steps: [...turn.steps, describeStep(node, update as any)] }
              : turn,
          ),
        ),
      onFinal: (payload) => patch({ final: payload, running: false }),
      onError: (message) => patch({ error: message, running: false }),
    }).catch((err) => patch({ error: String(err), running: false }))

    setBusy(false)
    await refreshConversations()
  }

  async function handleUpload(file: File) {
    setBusy(true)
    setNotice(`reading ${file.name}…`)
    try {
      const result = await uploadDataset(file)
      setNotice(`${result.name}: ${result.rows.toLocaleString()} rows loaded`)
      setActiveConnection(result.name)
      await refreshConnections()
      await newChat()
    } catch (err) {
      setNotice(String(err instanceof Error ? err.message : err))
    } finally {
      setBusy(false)
    }
  }

  async function handleAddConnection(name: string, url: string) {
    await addConnection(name, url)
    setActiveConnection(name)
    await refreshConnections()
    await newChat()
  }

  function signOut() {
    signOutGoogle()
    setToken('')
    setActiveId('')
    setTurns([])
    fetchMe().then(setMe).catch(() => setMe(null))
    refreshConnections()
    refreshConversations()
  }

  return (
    <div className="flex h-full">
      <Sidebar
        me={me}
        conversations={conversations}
        activeId={activeId}
        connections={connections}
        activeConnection={activeConnection}
        busy={busy}
        onNewChat={newChat}
        onSelectChat={openChat}
        onDeleteChat={removeChat}
        onSelectConnection={async (name) => {
          setActiveConnection(name)
          await newChat()
        }}
        onDeleteConnection={async (name) => {
          await removeConnection(name).catch(() => undefined)
          if (name === activeConnection) setActiveConnection('')
          await refreshConnections()
        }}
        onUpload={handleUpload}
        onAddConnection={handleAddConnection}
        onSignOut={signOut}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-(--color-edge) px-6 py-3">
          <div className="text-sm text-(--color-muted)">
            {activeConnection ? (
              <>
                asking <span className="text-slate-200">{activeConnection}</span>
              </>
            ) : (
              'no data connected'
            )}
          </div>
          {notice && <div className="text-xs text-(--color-muted)">{notice}</div>}
        </header>

        <main className="flex-1 space-y-8 overflow-auto px-6 py-6">
          {turns.length === 0 && (
            <div className="space-y-3">
              <p className="text-sm text-(--color-muted)">
                {connections.length
                  ? 'Ask anything about the connected data.'
                  : 'Upload a CSV, Excel workbook or SQLite file to begin.'}
              </p>
              {connections.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {STARTERS.map((starter) => (
                    <button
                      key={starter}
                      onClick={() => submit(starter)}
                      className="rounded-full border border-(--color-edge) bg-(--color-panel) px-3 py-1.5 text-sm text-slate-300 transition hover:border-(--color-accent)/50 hover:text-white"
                    >
                      {starter}
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}

          {turns.map((turn, index) => (
            <Turn key={index} turn={turn} busy={busy} onAsk={submit} />
          ))}
          <div ref={endRef} />
        </main>

        <form
          onSubmit={(event) => {
            event.preventDefault()
            submit(question)
          }}
          className="flex gap-2 border-t border-(--color-edge) px-6 py-4"
        >
          <input
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="Ask about the data…"
            className="flex-1 rounded-lg border border-(--color-edge) bg-(--color-panel) px-4 py-2.5 text-sm outline-none placeholder:text-(--color-muted) focus:border-(--color-accent)/60"
          />
          <button
            type="submit"
            disabled={busy || !question.trim()}
            className="rounded-lg bg-(--color-accent) px-4 py-2.5 text-sm font-medium text-black transition disabled:opacity-40"
          >
            {busy ? 'Asking…' : 'Ask'}
          </button>
        </form>
      </div>
    </div>
  )
}
