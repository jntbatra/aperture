import { useState } from 'react'
import type { Step } from './Timeline'

const LABELS: Record<string, string> = {
  route: 'Reading the question',
  link_schema: 'Finding the right tables',
  clarify: 'Checking the question',
  generate_sql: 'Writing SQL',
  validate: 'Validating',
  cost_guard: 'Estimating cost',
  execute: 'Running the query',
  diagnose: 'Diagnosing the failure',
  diagnose_empty: 'Explaining the empty result',
  verify: 'Verifying the result',
  narrate: 'Summarising',
  chart: 'Choosing a chart',
  exhausted: 'Giving up',
  small_talk: 'Answering',
}

const TONE: Record<Step['tone'], string> = {
  normal: 'text-(--color-accent)',
  warn: 'text-amber-400',
  bad: 'text-rose-400',
}

/**
 * The pipeline, shown live and collapsed afterwards.
 *
 * Watching it repair a query is the most interesting thing this system does,
 * so it is visible while running -- but once an answer exists the detail is
 * noise, and it folds into one line.
 */
export function Thinking({ steps, running }: { steps: Step[]; running: boolean }) {
  const [open, setOpen] = useState(false)
  const repairs = steps.filter((s) => s.node === 'diagnose').length
  const showExpanded = running || open

  if (!steps.length && !running) return null

  return (
    <div className="mb-3">
      {!running && (
        <button
          onClick={() => setOpen((value) => !value)}
          className="flex items-center gap-1.5 text-xs text-(--color-muted) transition hover:text-slate-300"
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
          {steps.length} steps{repairs > 0 ? ` · ${repairs} repair${repairs > 1 ? 's' : ''}` : ''}
        </button>
      )}

      {showExpanded && (
        <ol className="mt-2 space-y-1 border-l border-(--color-edge) pl-3 text-[13px]">
          {steps.map((step, index) => (
            <li key={`${step.node}-${index}`} className="flex gap-2">
              <span className={TONE[step.tone]}>·</span>
              <span className="text-slate-400">{LABELS[step.node] ?? step.node}</span>
              {step.detail && <span className="text-(--color-muted)">{step.detail}</span>}
            </li>
          ))}
          {running && (
            <li className="flex gap-2 shimmer">
              <span className="text-(--color-accent)">·</span>
              <span className="text-(--color-muted)">working…</span>
            </li>
          )}
        </ol>
      )}
    </div>
  )
}
