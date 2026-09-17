import { useEffect, useRef, useState } from 'react'
import { askStream, fetchSchema, type FinalPayload, type SchemaSummary } from './lib/api'
import { ChartPanel } from './components/ChartPanel'
import { ResultTable } from './components/ResultTable'
import { Timeline, describeStep, type Step } from './components/Timeline'

interface Turn {
  question: string
  steps: Step[]
  final?: FinalPayload
  error?: string
  running: boolean
}

const SUGGESTIONS = [
  'How many orders were delivered each month?',
  'What was our revenue last month?',
  'Which items are ordered most often?',
  'What is the cancellation rate by month?',
  'Total refunds issued last month',
]

const STATUS_STYLE: Record<string, string> = {
  answered: 'border-(--color-accent)/40 bg-(--color-accent)/5',
  empty: 'border-amber-500/40 bg-amber-500/5',
  refused: 'border-rose-500/40 bg-rose-500/5',
  exhausted: 'border-rose-500/40 bg-rose-500/5',
}

export default function App() {
  const [turns, setTurns] = useState<Turn[]>([])
  const [question, setQuestion] = useState('')
  const [schema, setSchema] = useState<SchemaSummary | null>(null)
  const [busy, setBusy] = useState(false)
  const threadId = useRef(`web-${Math.random().toString(36).slice(2, 8)}`)
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    fetchSchema().then(setSchema).catch(() => setSchema(null))
  }, [])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [turns])

  async function submit(text: string) {
    const trimmed = text.trim()
    if (!trimmed || busy) return

    setQuestion('')
    setBusy(true)
    const index = turns.length
    setTurns((previous) => [...previous, { question: trimmed, steps: [], running: true }])

    const patch = (update: Partial<Turn>) =>
      setTurns((previous) =>
        previous.map((turn, position) => (position === index ? { ...turn, ...update } : turn)),
      )

    await askStream(trimmed, threadId.current, {
      onNode: (node, update) => {
        setTurns((previous) =>
          previous.map((turn, position) =>
            position === index
              ? { ...turn, steps: [...turn.steps, describeStep(node, update as any)] }
              : turn,
          ),
        )
      },
      onFinal: (payload) => patch({ final: payload, running: false }),
      onError: (message) => patch({ error: message, running: false }),
    }).catch((err) => patch({ error: String(err), running: false }))

    setBusy(false)
  }

  return (
    <div className="mx-auto flex h-full max-w-5xl flex-col px-6 py-6">
      <header className="mb-6 flex items-baseline justify-between border-b border-(--color-edge) pb-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Aperture</h1>
          <p className="text-sm text-(--color-muted)">
            Ask a SQL database questions in plain English. Read-only, always shows its SQL.
          </p>
        </div>
        {schema && (
          <div className="text-right text-xs text-(--color-muted)">
            <div>
              {schema.dialect} · {schema.tables.length} tables · {schema.foreign_keys} foreign keys
            </div>
            {schema.empty_tables.length > 0 && <div>{schema.empty_tables.length} empty tables</div>}
          </div>
        )}
      </header>

      <main className="flex-1 space-y-8 overflow-auto pr-1">
        {turns.length === 0 && (
          <div className="space-y-3">
            <p className="text-sm text-(--color-muted)">Try one of these:</p>
            <div className="flex flex-wrap gap-2">
              {SUGGESTIONS.map((suggestion) => (
                <button
                  key={suggestion}
                  onClick={() => submit(suggestion)}
                  className="rounded-full border border-(--color-edge) bg-(--color-panel) px-3 py-1.5 text-sm text-slate-300 transition hover:border-(--color-accent)/50 hover:text-white"
                >
                  {suggestion}
                </button>
              ))}
            </div>
          </div>
        )}

        {turns.map((turn, index) => (
          <article key={index} className="space-y-3">
            <h2 className="text-base font-medium text-slate-100">{turn.question}</h2>

            <Timeline steps={turn.steps} running={turn.running} />

            {turn.error && (
              <div className="rounded-lg border border-rose-500/40 bg-rose-500/5 px-4 py-3 text-sm text-rose-200">
                {turn.error}
              </div>
            )}

            {turn.final?.sql && (
              <pre className="overflow-auto rounded-lg border border-(--color-edge) bg-(--color-panel) p-4 text-xs leading-relaxed text-sky-200">
                {turn.final.sql}
              </pre>
            )}

            {turn.final?.identifier_fixes && turn.final.identifier_fixes.length > 0 && (
              <p className="text-xs text-amber-400">
                repaired identifiers: {turn.final.identifier_fixes.join(', ')}
              </p>
            )}

            {turn.final && (
              <div className="grid gap-4 md:grid-cols-2">
                <ResultTable columns={turn.final.columns ?? []} rows={turn.final.rows ?? []} />
                <ChartPanel spec={turn.final.chart_spec} />
              </div>
            )}

            {turn.final?.answer && (
              <div
                className={`rounded-lg border px-4 py-3 text-sm whitespace-pre-line ${
                  STATUS_STYLE[turn.final.status ?? ''] ?? 'border-(--color-edge) bg-(--color-panel)'
                }`}
              >
                {turn.final.answer}
              </div>
            )}

            {turn.final?.verification?.map((finding) => (
              <div
                key={finding.kind}
                className="rounded-lg border border-amber-500/40 bg-amber-500/5 px-4 py-3 text-sm text-amber-100"
              >
                <span className="font-medium">Caveat</span> · {finding.message}
              </div>
            ))}

            {turn.final?.assumptions && (
              <p className="text-xs text-(--color-muted)">assumptions: {turn.final.assumptions}</p>
            )}

            {turn.final?.suggestions && turn.final.suggestions.length > 0 && (
              <div className="flex flex-wrap items-center gap-2 pt-1">
                <span className="text-xs text-(--color-muted)">next:</span>
                {turn.final.suggestions.map((suggestion) => (
                  <button
                    key={suggestion.text}
                    title={suggestion.reason}
                    onClick={() => submit(suggestion.text)}
                    disabled={busy}
                    className="rounded-full border border-(--color-edge) bg-(--color-panel) px-3 py-1 text-xs text-slate-300 transition hover:border-(--color-accent)/50 hover:text-white disabled:opacity-40"
                  >
                    {suggestion.text}
                  </button>
                ))}
              </div>
            )}

            {turn.final?.usage && <p className="text-xs text-(--color-muted)">{turn.final.usage}</p>}
          </article>
        ))}
        <div ref={endRef} />
      </main>

      <form
        onSubmit={(event) => {
          event.preventDefault()
          submit(question)
        }}
        className="mt-6 flex gap-2 border-t border-(--color-edge) pt-4"
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
  )
}
