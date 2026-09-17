import { VegaEmbed } from 'react-vega'
import type { VisualizationSpec } from 'vega-embed'

interface Props {
  spec: Record<string, any> | null | undefined
}

export function ChartPanel({ spec }: Props) {
  if (!spec) return null

  // A single-number result renders as a stat tile rather than a chart: a bar
  // chart of one bar communicates nothing.
  const metric = spec.aperture?.kind === 'metric' ? spec.aperture : null
  if (metric) {
    const value =
      typeof metric.value === 'number' ? metric.value.toLocaleString() : String(metric.value)
    return (
      <div className="rounded-lg border border-(--color-edge) bg-(--color-panel) px-5 py-4">
        <div className="text-xs tracking-wide text-(--color-muted) uppercase">{metric.label}</div>
        <div className="mt-1 text-4xl font-semibold text-slate-100">{value}</div>
      </div>
    )
  }

  const embedded = {
    ...spec,
    width: 'container',
    height: 260,
    background: 'transparent',
  } as VisualizationSpec

  return (
    <div className="overflow-hidden rounded-lg border border-(--color-edge) bg-(--color-panel) p-3">
      <VegaEmbed
        className="w-full"
        spec={embedded}
        options={{ actions: false, theme: 'dark', renderer: 'canvas' }}
        onError={(error) => console.error('vega embed failed', error)}
      />
    </div>
  )
}
