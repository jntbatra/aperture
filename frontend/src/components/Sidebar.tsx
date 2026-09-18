import { useRef, useState } from 'react'
import type { ConnectionInfo, ConversationSummary, Me } from '../lib/api'

const KIND_ICON: Record<string, string> = {
  csv: '▤',
  excel: '▦',
  sqlite: '◼',
  dump: '⛁',
  database: '⛁',
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
  const [collapsed, setCollapsed] = useState(false)

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

  if (collapsed) {
    return (
      <aside className="flex w-14 shrink-0 flex-col items-center gap-3 border-r border-(--color-edge) py-4">
        <button
          onClick={() => setCollapsed(false)}
          title="Show sidebar"
          className="grid h-9 w-9 place-items-center rounded-lg text-(--color-muted) transition hover:bg-(--color-edge) hover:text-white"
        >
          ☰
        </button>
        <button
          onClick={onNewChat}
          title="New chat"
          className="grid h-9 w-9 place-items-center rounded-lg text-(--color-muted) transition hover:bg-(--color-edge) hover:text-white"
        >
          +
        </button>
      </aside>
    )
  }

  return (
    <aside className="flex w-64 shrink-0 flex-col gap-4 border-r border-(--color-edge) bg-(--color-raised)/40 p-3">
      <div className="flex items-center justify-between px-1">
        <span className="text-sm font-semibold tracking-tight">Aperture</span>
        <button
          onClick={() => setCollapsed(true)}
          title="Hide sidebar"
          className="text-(--color-muted) transition hover:text-white"
        >
          ☰
        </button>
      </div>

      <button
        onClick={onNewChat}
        disabled={busy}
        className="flex items-center gap-2 rounded-xl border border-(--color-edge) px-3 py-2 text-sm transition hover:bg-(--color-edge)/50 disabled:opacity-40"
      >
        <span className="text-(--color-muted)">+</span> New chat
      </button>

      <section className="space-y-1">
        <div className="flex items-center justify-between px-1">
          <h2 className="text-[11px] tracking-wider text-(--color-muted) uppercase">Data</h2>
          <div className="flex gap-2 text-[11px] text-(--color-muted)">
            <button onClick={() => fileInput.current?.click()} className="hover:text-white">
              upload
            </button>
            <button onClick={() => setShowAdd((open) => !open)} className="hover:text-white">
              connect
            </button>
          </div>
        </div>

        <input
          ref={fileInput}
          type="file"
          accept=".csv,.tsv,.xlsx,.xlsm,.db,.sqlite,.sqlite3,.sql,.dump"
          className="hidden"
          onChange={(event) => {
            const file = event.target.files?.[0]
            if (file) onUpload(file)
            event.target.value = ''
          }}
        />

        {showAdd && (
          <form onSubmit={submitConnection} className="space-y-1.5 rounded-xl border border-(--color-edge) p-2">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="name"
              className="w-full rounded-lg border border-(--color-edge) bg-(--color-ink) px-2 py-1 text-xs outline-none"
            />
            <input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="postgresql+psycopg://…"
              className="w-full rounded-lg border border-(--color-edge) bg-(--color-ink) px-2 py-1 text-xs outline-none"
            />
            {error && <p className="text-[11px] text-rose-400">{error}</p>}
            <button
              type="submit"
              disabled={!name.trim() || !url.trim()}
              className="w-full rounded-lg bg-(--color-accent) py-1 text-xs font-medium text-black disabled:opacity-40"
            >
              Connect
            </button>
          </form>
        )}

        <ul className="space-y-0.5">
          {connections.map((connection) => (
            <li key={connection.name} className="group flex items-center">
              <button
                onClick={() => onSelectConnection(connection.name)}
                title={connection.target}
                className={`flex flex-1 items-center gap-2 truncate rounded-lg px-2 py-1.5 text-left text-sm transition ${
                  connection.name === activeConnection
                    ? 'bg-(--color-accent)/10 text-(--color-accent)'
                    : 'text-slate-300 hover:bg-(--color-edge)/50'
                }`}
              >
                <span className="text-xs opacity-60">{KIND_ICON[connection.kind] ?? '⛁'}</span>
                <span className="truncate">{connection.name}</span>
              </button>
              <button
                onClick={() => onDeleteConnection(connection.name)}
                className="px-1 text-xs text-(--color-muted) opacity-0 transition group-hover:opacity-100 hover:text-rose-400"
                title="Remove"
              >
                ×
              </button>
            </li>
          ))}
          {connections.length === 0 && (
            <li className="px-2 py-1 text-xs text-(--color-muted)">nothing loaded yet</li>
          )}
        </ul>
      </section>

      <section className="flex min-h-0 flex-1 flex-col gap-1">
        <h2 className="px-1 text-[11px] tracking-wider text-(--color-muted) uppercase">Chats</h2>
        <ul className="min-h-0 flex-1 space-y-0.5 overflow-auto">
          {conversations.map((conversation) => (
            <li key={conversation.id} className="group flex items-center">
              <button
                onClick={() => onSelectChat(conversation.id)}
                className={`flex-1 truncate rounded-lg px-2 py-1.5 text-left text-sm transition ${
                  conversation.id === activeId
                    ? 'bg-(--color-edge)/70 text-white'
                    : 'text-slate-300 hover:bg-(--color-edge)/40'
                }`}
              >
                {conversation.title}
              </button>
              <button
                onClick={() => onDeleteChat(conversation.id)}
                className="px-1 text-xs text-(--color-muted) opacity-0 transition group-hover:opacity-100 hover:text-rose-400"
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
