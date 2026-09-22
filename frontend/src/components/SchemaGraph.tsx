/**
 * The foreign-key graph, drawn.
 *
 * Why show it at all
 * ------------------
 * This graph *is* the retrieval mechanism. When the agent answers a question it
 * picks a starting table and walks these edges outward, so seeing the graph is
 * seeing how the agent navigates — and, more practically, it tells a user which
 * tables can be joined before they phrase a question that needs an impossible
 * join.
 *
 * Layout comes from the server
 * ----------------------------
 * Positions are computed with NetworkX and arrive with the data. The client is
 * a pure renderer: no layout engine, no physics simulation, and the picture is
 * identical on every load rather than settling differently each time.
 *
 * Fitting a 75-table schema into a sidebar
 * ----------------------------------------
 * Node size is scaled down as the graph grows, edge labels are hidden past a
 * threshold, and `fitView` zooms to the extent. Beyond roughly 40 tables the
 * labels stop being readable at fit-zoom, which is expected — the shape is the
 * information at that size, and the user can zoom for detail.
 */

import { useMemo } from 'react';
import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import type { SchemaGraph as SchemaGraphData } from '../api';

/** Past this many tables, edge labels become unreadable noise at fit-zoom. */
const LABEL_LIMIT = 18;

/** Past this, shrink nodes so a large schema still fits the viewport. */
const COMPACT_LIMIT = 30;

type TableNodeData = {
  label: string;
  columns: number;
  degree: number;
  compact: boolean;
  highlighted: boolean;
};

function TableNode({ data }: NodeProps) {
  const { label, columns, degree, compact, highlighted } = data as TableNodeData;

  return (
    <div className={`gnode ${compact ? 'gnode--compact' : ''} ${highlighted ? 'gnode--on' : ''}`}>
      {/* React Flow needs handles to anchor edges. They are visually hidden:
          this is a diagram to read, not a flowchart to rewire. */}
      <Handle type="target" position={Position.Top} className="gnode__handle" />
      <span className="gnode__name">{label}</span>
      {!compact && (
        <span className="gnode__meta">
          {columns} cols{degree > 0 ? ` · ${degree} link${degree === 1 ? '' : 's'}` : ''}
        </span>
      )}
      <Handle type="source" position={Position.Bottom} className="gnode__handle" />
    </div>
  );
}

const nodeTypes = { table: TableNode };

interface Props {
  graph: SchemaGraphData;
  /** Tables the last answer actually used, highlighted so the walk is visible. */
  highlight?: string[];
}

export function SchemaGraph({ graph, highlight = [] }: Props) {
  const compact = graph.nodes.length > COMPACT_LIMIT;
  const showLabels = graph.nodes.length <= LABEL_LIMIT;
  const active = useMemo(() => new Set(highlight), [highlight]);

  const nodes: Node[] = useMemo(
    () =>
      graph.nodes.map((node) => ({
        id: node.id,
        type: 'table',
        position: { x: node.x, y: node.y },
        data: {
          label: node.label,
          columns: node.columns,
          degree: node.degree,
          compact,
          highlighted: active.has(node.id),
        },
        // Nothing here should be editable; this is a picture of a real schema.
        draggable: false,
        connectable: false,
      })),
    [graph.nodes, compact, active],
  );

  const edges: Edge[] = useMemo(
    () =>
      graph.edges.map((edge) => {
        const lit = active.has(edge.source) && active.has(edge.target);
        return {
          id: edge.id,
          source: edge.source,
          target: edge.target,
          label: showLabels ? edge.label : undefined,
          animated: lit,
          style: {
            stroke: lit ? 'var(--clay)' : 'var(--line-strong)',
            strokeWidth: lit ? 2 : 1.2,
          },
          labelStyle: { fontSize: 9, fill: 'var(--ink-faint)' },
          labelBgStyle: { fill: 'var(--paper)', fillOpacity: 0.85 },
        };
      }),
    [graph.edges, showLabels, active],
  );

  if (graph.nodes.length === 0) {
    return <div className="notice">No tables to draw.</div>;
  }

  return (
    <div className="graph">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        // Generous padding: edge labels ("orders.customer_id = customers.id")
        // extend well beyond the node boxes that fitView measures, so a tight
        // fit clips them at the panel edge.
        fitViewOptions={{ padding: compact ? 0.12 : 0.3, maxZoom: 1 }}
        minZoom={0.15}
        maxZoom={2.5}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="var(--line)" />
        <Controls showInteractive={false} position="bottom-right" />
        {/* No minimap. React Flow sizes minimap nodes from explicit node
            width/height, which custom nodes do not carry, so it rendered as an
            empty grey block — broken chrome in a panel where space is the
            scarce resource. Pan and zoom cover navigation. */}
      </ReactFlow>

      <div className="graph__legend">
        {graph.nodes.length} tables · {graph.edges.length} relationships
        {!showLabels && ' · zoom in for join conditions'}
        {highlight.length > 0 && ' · highlighted tables were used in the last answer'}
      </div>
    </div>
  );
}
