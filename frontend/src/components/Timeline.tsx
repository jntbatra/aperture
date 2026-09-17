const LABELS: Record<string, string> = {
  route: 'routing',
  link_schema: 'linking schema',
  generate_sql: 'writing SQL',
  validate: 'validating',
  cost_guard: 'estimating cost',
  execute: 'executing',
  diagnose: 'diagnosing failure',
  diagnose_empty: 'explaining empty result',
  narrate: 'summarising',
  chart: 'choosing chart',
  exhausted: 'giving up',
  small_talk: 'answering',
}

export interface Step {
  node: string
  detail: string
  tone: 'normal' | 'warn' | 'bad'
}

export function describeStep(node: string, update: Record<string, any>): Step {
  let detail = ''
  let tone: Step['tone'] = 'normal'

  if (node === 'link_schema') {
    const tables = (update.linked_tables ?? []).length
    const metrics = update.trace?.[0]?.metrics ?? []
    detail = `${tables} tables${metrics.length ? ` · ${metrics.join(', ')}` : ''}`
  } else if (node === 'validate') {
    const fixes = update.identifier_fixes ?? []
    if (update.status === 'refused') {
      detail = 'refused'
      tone = 'bad'
    } else if (fixes.length) {
      detail = `repaired ${fixes.length} identifier${fixes.length > 1 ? 's' : ''}`
      tone = 'warn'
    } else if (update.last_error) {
      detail = String(update.last_error).slice(0, 60)
      tone = 'warn'
    }
  } else if (node === 'cost_guard') {
    if (update.estimated_cost != null) detail = `cost ${Math.round(update.estimated_cost).toLocaleString()}`
    if (update.last_error) tone = 'warn'
  } else if (node === 'execute') {
    if (update.last_error) {
      detail = String(update.last_error).split('\n')[0].slice(0, 70)
      tone = 'warn'
    } else {
      detail = `${update.row_count ?? 0} rows · ${Math.round(update.elapsed_ms ?? 0)}ms`
    }
  } else if (node === 'diagnose') {
    detail = `attempt ${update.attempts ?? 0}`
    tone = 'warn'
  } else if (node === 'diagnose_empty') {
    detail = update.diagnosis ? 'cause found' : 'no obvious cause'
    tone = 'warn'
  } else if (node === 'exhausted') {
    tone = 'bad'
  }

  return { node, detail, tone }
}

const TONE_CLASS: Record<Step['tone'], string> = {
  normal: 'text-(--color-accent)',
  warn: 'text-amber-400',
  bad: 'text-rose-400',
}

export function Timeline({ steps, running }: { steps: Step[]; running: boolean }) {
  return (
    <ol className="space-y-1 text-sm">
      {steps.map((step, index) => (
        <li key={`${step.node}-${index}`} className="flex gap-2">
          <span className={TONE_CLASS[step.tone]}>›</span>
          <span className="text-slate-300">{LABELS[step.node] ?? step.node}</span>
          {step.detail && <span className="text-(--color-muted)">{step.detail}</span>}
        </li>
      ))}
      {running && (
        <li className="flex gap-2 text-(--color-muted)">
          <span className="animate-pulse">›</span>
          <span className="animate-pulse">working…</span>
        </li>
      )}
    </ol>
  )
}
