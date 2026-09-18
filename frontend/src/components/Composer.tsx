import { useRef, useState } from 'react'

interface Props {
  busy: boolean
  placeholder: string
  onSubmit: (question: string) => void
  onAttach: (file: File) => void
}

const ACCEPT = '.csv,.tsv,.xlsx,.xlsm,.db,.sqlite,.sqlite3,.sql,.dump'

export function Composer({ busy, placeholder, onSubmit, onAttach }: Props) {
  const [value, setValue] = useState('')
  const fileInput = useRef<HTMLInputElement>(null)
  const textarea = useRef<HTMLTextAreaElement>(null)

  function submit() {
    const trimmed = value.trim()
    if (!trimmed || busy) return
    onSubmit(trimmed)
    setValue('')
    if (textarea.current) textarea.current.style.height = 'auto'
  }

  return (
    <div className="px-4 pb-4">
      <div className="mx-auto max-w-3xl">
        <div className="flex items-end gap-2 rounded-3xl border border-(--color-edge) bg-(--color-raised) px-3 py-2 shadow-lg transition focus-within:border-(--color-accent)/50">
          <input
            ref={fileInput}
            type="file"
            accept={ACCEPT}
            className="hidden"
            onChange={(event) => {
              const file = event.target.files?.[0]
              if (file) onAttach(file)
              event.target.value = ''
            }}
          />
          <button
            onClick={() => fileInput.current?.click()}
            disabled={busy}
            title="Attach CSV, Excel, SQLite or SQL dump"
            className="grid h-9 w-9 shrink-0 place-items-center rounded-full text-(--color-muted) transition hover:bg-(--color-edge) hover:text-white disabled:opacity-40"
          >
            <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.8">
              <path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>

          <textarea
            ref={textarea}
            rows={1}
            value={value}
            placeholder={placeholder}
            disabled={busy}
            onChange={(event) => {
              setValue(event.target.value)
              const element = event.target
              element.style.height = 'auto'
              element.style.height = `${Math.min(element.scrollHeight, 200)}px`
            }}
            onKeyDown={(event) => {
              // Enter sends, Shift+Enter makes a new line, as people expect.
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault()
                submit()
              }
            }}
            className="max-h-[200px] flex-1 resize-none bg-transparent py-2 text-[15px] outline-none placeholder:text-(--color-muted) disabled:opacity-60"
          />

          <button
            onClick={submit}
            disabled={busy || !value.trim()}
            title="Send"
            className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-(--color-accent) text-black transition hover:brightness-110 disabled:bg-(--color-edge) disabled:text-(--color-muted)"
          >
            {busy ? (
              <span className="h-2.5 w-2.5 rounded-sm bg-current" />
            ) : (
              <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M12 19V5M5 12l7-7 7 7" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            )}
          </button>
        </div>
        <p className="mt-2 text-center text-xs text-(--color-muted)">
          Aperture runs read-only SQL and shows every query it writes.
        </p>
      </div>
    </div>
  )
}
