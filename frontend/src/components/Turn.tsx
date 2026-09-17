import type { FinalPayload } from '../lib/api'
import { ChartPanel } from './ChartPanel'
import { ResultTable } from './ResultTable'
import { Timeline, type Step } from './Timeline'

const STATUS_STYLE: Record<string, string> = {
  answered: 'border-(--color-accent)/40 bg-(--color-accent)/5',
  empty: 'border-amber-500/40 bg-amber-500/5',
  refused: 'border-rose-500/40 bg-rose-500/5',
  exhausted: 'border-rose-500/40 bg-rose-500/5',
}

export interface TurnData {
  question: string
  steps: Step[]
  final?: FinalPayload
  error?: string
  running: boolean
}

export function Turn({
  turn,
  busy,
  onAsk,
}: {
  turn: TurnData
  busy: boolean
  onAsk: (question: string) => void
}) {
  const final = turn.final
  return (
    <article className="space-y-3">
      <h2 className="text-base font-medium text-slate-100">{turn.question}</h2>

      {(turn.steps.length > 0 || turn.running) && (
        <Timeline steps={turn.steps} running={turn.running} />
      )}

      {turn.error && (
        <div className="rounded-lg border border-rose-500/40 bg-rose-500/5 px-4 py-3 text-sm text-rose-200">
          {turn.error}
        </div>
      )}

      {final?.sql && (
        <pre className="overflow-auto rounded-lg border border-(--color-edge) bg-(--color-panel) p-4 text-xs leading-relaxed text-sky-200">
          {final.sql}
        </pre>
      )}

      {final?.identifier_fixes && final.identifier_fixes.length > 0 && (
        <p className="text-xs text-amber-400">
          repaired identifiers: {final.identifier_fixes.join(', ')}
        </p>
      )}

      {final && (final.rows?.length ?? 0) > 0 && (
        <div className="grid gap-4 md:grid-cols-2">
          <ResultTable columns={final.columns ?? []} rows={final.rows ?? []} />
          <ChartPanel spec={final.chart_spec} />
        </div>
      )}

      {final?.verification?.map((finding) => (
        <div
          key={finding.kind}
          className="rounded-lg border border-amber-500/40 bg-amber-500/5 px-4 py-3 text-sm text-amber-100"
        >
          <span className="font-medium">Caveat</span> · {finding.message}
        </div>
      ))}

      {final?.answer && (
        <div
          className={`rounded-lg border px-4 py-3 text-sm whitespace-pre-line ${
            STATUS_STYLE[final.status ?? ''] ?? 'border-(--color-edge) bg-(--color-panel)'
          }`}
        >
          {final.answer}
        </div>
      )}

      {final?.assumptions && (
        <p className="text-xs text-(--color-muted)">assumptions: {final.assumptions}</p>
      )}

      {final?.suggestions && final.suggestions.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 pt-1">
          <span className="text-xs text-(--color-muted)">next:</span>
          {final.suggestions.map((suggestion) => (
            <button
              key={suggestion.text}
              title={suggestion.reason}
              onClick={() => onAsk(suggestion.text)}
              disabled={busy}
              className="rounded-full border border-(--color-edge) bg-(--color-panel) px-3 py-1 text-xs text-slate-300 transition hover:border-(--color-accent)/50 hover:text-white disabled:opacity-40"
            >
              {suggestion.text}
            </button>
          ))}
        </div>
      )}

      {final?.usage && <p className="text-xs text-(--color-muted)">{final.usage}</p>}
    </article>
  )
}
