import { useRef, useState } from 'react'
import type { ConnectionInfo, ConversationSummary, Me } from '../lib/api'

const KIND_LABEL: Record<string, string> = {
  csv: 'CSV',
  excel: 'Excel',
  sqlite: 'SQLite',
  database: 'database',
}

interface Props {
  me: Me | null
  conversations: ConversationSummary[]
  activeId: string
  connections: ConnectionInfo[]
  activeConnection: string
  busy: boolean
  onNewChat: () => void
  onSelectChat: (id: string) => void
  onDeleteChat: (id: string) => void
  onSelectConnection: (name: string) => void
  onDeleteConnection: (name: string) => void
  onUpload: (file: File) => void
  onAddConnection: (name: string, url: string) => Promise<void>
  onSignOut: () => void
}

export function Sidebar({
  me,
  conversations,
  activeId,
  connections,
  activeConnection,
  busy,
  onNewChat,
  onSelectChat,
  onDeleteChat,
  onSelectConnection,
  onDeleteConnection,
  onUpload,
  onAddConnection,
  onSignOut,
}: Props) {
  const fileInput = useRef<HTMLInputElement>(null)
  const [showAdd, setShowAdd] = useState(false)
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [error, setError] = useState('')

  async function submitConnection(event: React.FormEvent) {
    event.preventDefault()
    setError('')
    try {
      await onAddConnection(name.trim(), url.trim())
      setShowAdd(false)
      setName('')
      setUrl('')
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err))
    }
  }

  return (
    <aside className="flex w-72 shrink-0 flex-col gap-4 border-r border-(--color-edge) bg-(--color-panel)/40 p-4">
      <div>
        <h1 className="text-lg font-semibold tracking-tight">Aperture</h1>
        <p className="text-xs text-(--color-muted)">Chat with your data</p>
      </div>

      <button
        onClick={onNewChat}
        disabled={busy}
        className="rounded-lg border border-(--color-edge) px-3 py-2 text-sm transition hover:border-(--color-accent)/50 disabled:opacity-40"
      >
        + New chat
      </button>

      <section className="space-y-2">
        <div className="flex items-center justify-between">
          <h2 className="text-xs tracking-wide text-(--color-muted) uppercase">Data</h2>
          <div className="flex gap-2">
            <button
              onClick={() => fileInput.current?.click()}
              className="text-xs text-(--color-muted) hover:text-white"
              title="Upload CSV, Excel or SQLite"
            >
              upload
            </button>
            <button
              onClick={() => setShowAdd((open) => !open)}
              className="text-xs text-(--color-muted) hover:text-white"
              title="Connect a SQL database"
            >
              connect
            </button>
          </div>
        </div>

        <input
          ref={fileInput}
          type="file"
          accept=".csv,.tsv,.xlsx,.xlsm,.db,.sqlite,.sqlite3"
          className="hidden"
          onChange={(event) => {
            const file = event.target.files?.[0]
            if (file) onUpload(file)
            event.target.value = ''
          }}
        />

        {showAdd && (
          <form onSubmit={submitConnection} className="space-y-2 rounded-lg border border-(--color-edge) p-2">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="name (e.g. prod)"
              className="w-full rounded border border-(--color-edge) bg-(--color-ink) px-2 py-1 text-xs outline-none"
            />
            <input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="postgresql+psycopg://user:pw@host/db"
              className="w-full rounded border border-(--color-edge) bg-(--color-ink) px-2 py-1 text-xs outline-none"
            />
            {error && <p className="text-xs text-rose-400">{error}</p>}
            <button
              type="submit"
              disabled={!name.trim() || !url.trim()}
              className="w-full rounded bg-(--color-accent) px-2 py-1 text-xs font-medium text-black disabled:opacity-40"
            >
              Connect
            </button>
          </form>
        )}

        <ul className="space-y-1">
          {connections.map((connection) => (
            <li key={connection.name} className="group flex items-center gap-1">
              <button
                onClick={() => onSelectConnection(connection.name)}
                className={`flex-1 truncate rounded px-2 py-1.5 text-left text-sm transition ${
                  connection.name === activeConnection
                    ? 'bg-(--color-accent)/10 text-(--color-accent)'
                    : 'text-slate-300 hover:bg-(--color-edge)/40'
                }`}
                title={connection.target}
              >
                {connection.name}
                <span className="ml-1 text-xs text-(--color-muted)">
                  {KIND_LABEL[connection.kind] ?? connection.kind}
                </span>
              </button>
              <button
                onClick={() => onDeleteConnection(connection.name)}
                className="text-xs text-(--color-muted) opacity-0 transition group-hover:opacity-100 hover:text-rose-400"
                title="Remove this connection"
              >
                ×
              </button>
            </li>
          ))}
          {connections.length === 0 && (
            <li className="px-2 py-1 text-xs text-(--color-muted)">
              upload a file or connect a database
            </li>
          )}
        </ul>
      </section>

      <section className="flex min-h-0 flex-1 flex-col gap-2">
        <h2 className="text-xs tracking-wide text-(--color-muted) uppercase">Chats</h2>
        <ul className="min-h-0 flex-1 space-y-1 overflow-auto">
          {conversations.map((conversation) => (
            <li key={conversation.id} className="group flex items-center gap-1">
              <button
                onClick={() => onSelectChat(conversation.id)}
                className={`flex-1 truncate rounded px-2 py-1.5 text-left text-sm transition ${
                  conversation.id === activeId
                    ? 'bg-(--color-edge)/60 text-white'
                    : 'text-slate-300 hover:bg-(--color-edge)/40'
                }`}
              >
                {conversation.title}
              </button>
              <button
                onClick={() => onDeleteChat(conversation.id)}
                className="opacity-0 transition group-hover:opacity-100 text-xs text-(--color-muted) hover:text-rose-400"
                title="Delete chat"
              >
                ×
              </button>
            </li>
          ))}
          {conversations.length === 0 && (
            <li className="px-2 py-1 text-xs text-(--color-muted)">no chats yet</li>
          )}
        </ul>
      </section>

      <section className="border-t border-(--color-edge) pt-3">
        {me && !me.anonymous ? (
          <div className="flex items-center gap-2">
            {me.picture && <img src={me.picture} alt="" className="h-7 w-7 rounded-full" />}
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm">{me.name || me.email}</p>
              <button onClick={onSignOut} className="text-xs text-(--color-muted) hover:text-white">
                sign out
              </button>
            </div>
          </div>
        ) : (
          <div id="google-signin" className="flex justify-center" />
        )}
      </section>
    </aside>
  )
}
