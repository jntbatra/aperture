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
import { Composer } from './components/Composer'
import { Message, type TurnData } from './components/Message'
import { Sidebar } from './components/Sidebar'
import { describeStep } from './components/Timeline'

const STARTERS = [
  'What is in this data?',
  'Show the totals by category',
  'How has this changed over time?',
  'What stands out?',
]

export default function App() {
  const [me, setMe] = useState<Me | null>(null)
  const [conversations, setConversations] = useState<ConversationSummary[]>([])
  const [activeId, setActiveId] = useState('')
  const [connections, setConnections] = useState<ConnectionInfo[]>([])
  const [activeConnection, setActiveConnection] = useState('')
  const [turns, setTurns] = useState<TurnData[]>([])
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [dragging, setDragging] = useState(false)
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
    setActiveConnection(detail.connection || '')

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

  async function newChat(): Promise<string> {
    const conversation = await createConversation(activeConnection).catch(() => null)
    if (!conversation) return ''
    setActiveId(conversation.id)
    setTurns([])
    await refreshConversations()
    return conversation.id
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

  /** A file dropped or attached belongs to the chat it arrived in. */
  async function handleUpload(file: File) {
    setBusy(true)
    setNotice(`reading ${file.name}…`)
    const conversationId = activeId || (await newChat())
    try {
      const result = await uploadDataset(file, conversationId)
      setActiveConnection(result.name)
      setTurns((previous) => [
        ...previous,
        { question: `Uploaded ${file.name}`, steps: [], running: false, upload: result },
      ])
      await Promise.all([refreshConnections(), refreshConversations()])
      setNotice('')
    } catch (err) {
      setNotice(String(err instanceof Error ? err.message : err))
    } finally {
      setBusy(false)
    }
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

  const hasData = Boolean(activeConnection) || connections.length > 0

  return (
    <div
      className="flex h-full"
      onDragOver={(event) => {
        event.preventDefault()
        setDragging(true)
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(event) => {
        event.preventDefault()
        setDragging(false)
        const file = event.dataTransfer.files?.[0]
        if (file) handleUpload(file)
      }}
    >
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
        onAddConnection={async (name, url) => {
          await addConnection(name, url)
          setActiveConnection(name)
          await refreshConnections()
          await newChat()
        }}
        onSignOut={signOut}
      />

      <div className="relative flex min-w-0 flex-1 flex-col">
        {dragging && (
          <div className="pointer-events-none absolute inset-3 z-20 grid place-items-center rounded-2xl border-2 border-dashed border-(--color-accent)/60 bg-(--color-ink)/80">
            <p className="text-sm text-(--color-accent)">Drop a CSV, Excel, SQLite or SQL dump</p>
          </div>
        )}

        <header className="flex items-center justify-between px-6 py-3 text-sm">
          <span className="text-(--color-muted)">
            {activeConnection ? (
              <>
                asking <span className="text-slate-200">{activeConnection}</span>
              </>
            ) : (
              'no data yet'
            )}
          </span>
          {notice && <span className="text-xs text-(--color-muted) shimmer">{notice}</span>}
        </header>

        <main className="flex-1 overflow-auto">
          <div className="mx-auto max-w-3xl space-y-8 px-4 py-8">
            {turns.length === 0 && (
              <div className="pt-16 text-center">
                <h1 className="text-2xl font-semibold tracking-tight">
                  {hasData ? 'Ask anything about your data' : 'Start with a file'}
                </h1>
                <p className="mt-2 text-(--color-muted)">
                  {hasData
                    ? 'Every answer shows the SQL that produced it.'
                    : 'Drop a CSV, Excel workbook, SQLite file or SQL dump anywhere on this page.'}
                </p>
                {hasData && (
                  <div className="mt-8 grid gap-2 sm:grid-cols-2">
                    {STARTERS.map((starter) => (
                      <button
                        key={starter}
                        onClick={() => submit(starter)}
                        className="rounded-xl border border-(--color-edge) bg-(--color-panel) px-4 py-3 text-left text-sm text-slate-300 transition hover:border-(--color-muted) hover:text-white"
                      >
                        {starter}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}

            {turns.map((turn, index) => (
              <Message key={index} turn={turn} busy={busy} onAsk={submit} />
            ))}
            <div ref={endRef} />
          </div>
        </main>

        <Composer
          busy={busy}
          placeholder={hasData ? 'Ask about the data…' : 'Attach a file to begin…'}
          onSubmit={submit}
          onAttach={handleUpload}
        />
      </div>
    </div>
  )
}
