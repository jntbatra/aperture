import { useState } from 'react'
import type { FinalPayload, UploadResult } from '../lib/api'
import { ChartPanel } from './ChartPanel'
import { ResultTable } from './ResultTable'
import { Thinking } from './Thinking'
import type { Step } from './Timeline'

export interface TurnData {
  question: string
  steps: Step[]
  final?: FinalPayload
  upload?: UploadResult
  error?: string
  running: boolean
}

const KIND_LABEL: Record<string, string> = {
  csv: 'CSV',
  excel: 'Excel workbook',
  sqlite: 'SQLite database',
  dump: 'SQL dump',
}

function Bubble({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[80%] rounded-3xl rounded-br-lg bg-(--color-raised) px-4 py-2.5 text-[15px] whitespace-pre-wrap">
        {children}
      </div>
    </div>
  )
}

function Collapsible({ title, children }: { title: string; children: React.ReactNode }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="overflow-hidden rounded-xl border border-(--color-edge)">
      <button
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center gap-2 bg-(--color-panel) px-3 py-2 text-xs text-(--color-muted) transition hover:text-slate-300"
      >
        <svg
          viewBox="0 0 24 24"
          className={`h-3 w-3 transition-transform ${open ? 'rotate-90' : ''}`}
          fill="none"
          stroke="currentColor"
          strokeWidth="2.5"
        >
          <path d="M9 18l6-6-6-6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        {title}
      </button>
      {open && <div className="border-t border-(--color-edge)">{children}</div>}
    </div>
  )
}

export function Message({
  turn,
  busy,
  onAsk,
}: {
  turn: TurnData
  busy: boolean
  onAsk: (question: string) => void
}) {
  const final = turn.final

  // A file that arrived in this chat, rendered as its own card.
  if (turn.upload) {
    const upload = turn.upload
    return (
      <div className="rise space-y-3">
        <Bubble>{turn.question}</Bubble>
        <div className="rounded-2xl border border-(--color-edge) bg-(--color-panel) p-4">
          <div className="flex items-baseline justify-between">
            <span className="font-medium">{upload.name}</span>
            <span className="text-xs text-(--color-muted)">
              {KIND_LABEL[upload.kind] ?? upload.kind}
            </span>
          </div>
          <ul className="mt-2 space-y-0.5 text-sm text-slate-400">
            {upload.tables.map((table) => (
              <li key={table.name}>
                {table.name} — {table.rows.toLocaleString()} rows, {table.columns} columns
              </li>
            ))}
          </ul>
          <p className="mt-3 text-xs text-(--color-muted)">
            This chat now asks questions of {upload.name}.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="rise space-y-4">
      <Bubble>{turn.question}</Bubble>

      <div className="space-y-3">
        <Thinking steps={turn.steps} running={turn.running} />

        {turn.error && (
          <div className="rounded-xl border border-rose-500/40 bg-rose-500/5 px-4 py-3 text-sm text-rose-200">
            {turn.error}
          </div>
        )}

        {final?.answer && (
          <div className="text-[15px] leading-relaxed whitespace-pre-line text-slate-100">
            {final.answer}
          </div>
        )}

        {final?.clarify_options && final.clarify_options.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {final.clarify_options.map((option) => (
              <button
                key={option}
                onClick={() => onAsk(option)}
                disabled={busy}
                className="rounded-full border border-(--color-accent)/40 bg-(--color-accent)/10 px-3 py-1.5 text-sm text-(--color-accent) transition hover:bg-(--color-accent)/20 disabled:opacity-40"
              >
                {option}
              </button>
            ))}
          </div>
        )}

        {final && (final.rows?.length ?? 0) > 0 && (
          <div className="grid gap-3 lg:grid-cols-2">
            <ResultTable columns={final.columns ?? []} rows={final.rows ?? []} />
            <ChartPanel spec={final.chart_spec} />
          </div>
        )}

        {final?.verification?.map((finding) => (
          <div
            key={finding.kind}
            className="rounded-xl border border-amber-500/30 bg-amber-500/5 px-4 py-3 text-sm text-amber-100/90"
          >
            <span className="font-medium">Caveat</span> · {finding.message}
          </div>
        ))}

        {final?.insights?.map((insight) => (
          <p key={insight.kind} className="text-sm text-(--color-muted)">
            {insight.message}
          </p>
        ))}

        {final?.sql && (
          <Collapsible title="See the SQL">
            <pre className="overflow-auto bg-(--color-ink) p-4 text-xs leading-relaxed text-sky-200">
              {final.sql}
            </pre>
            {final.assumptions && (
              <p className="border-t border-(--color-edge) px-4 py-2 text-xs text-(--color-muted)">
                assumptions: {final.assumptions}
              </p>
            )}
            {final.identifier_fixes && final.identifier_fixes.length > 0 && (
              <p className="border-t border-(--color-edge) px-4 py-2 text-xs text-amber-400/80">
                repaired identifiers: {final.identifier_fixes.join(', ')}
              </p>
            )}
          </Collapsible>
        )}

        {final?.suggestions && final.suggestions.length > 0 && (
          <div className="flex flex-wrap gap-2 pt-1">
            {final.suggestions.map((suggestion) => (
              <button
                key={suggestion.text}
                title={suggestion.reason}
                onClick={() => onAsk(suggestion.text)}
                disabled={busy}
                className="rounded-full border border-(--color-edge) px-3 py-1.5 text-sm text-slate-400 transition hover:border-(--color-muted) hover:text-slate-200 disabled:opacity-40"
              >
                {suggestion.text}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
