/**
 * The SQL disclosure and the results table.
 *
 * Both are collapsible panels. The answer comes first because that is what was
 * asked for; the query and the raw rows are evidence, available on demand.
 * Showing the SQL matters — an analyst who cannot check the query cannot trust
 * the answer.
 */

import { useState, type ReactNode } from 'react';

const SQL_KEYWORDS =
  /\b(SELECT|FROM|WHERE|JOIN|LEFT|RIGHT|INNER|OUTER|ON|GROUP BY|ORDER BY|HAVING|LIMIT|OFFSET|AS|AND|OR|NOT|IN|IS|NULL|COUNT|SUM|AVG|MIN|MAX|DISTINCT|CASE|WHEN|THEN|ELSE|END|WITH|UNION|ALL|DESC|ASC|BETWEEN|LIKE|CAST)\b/gi;

/** Minimal keyword highlighting.
 *
 *  A full SQL tokeniser would be more correct, but this is a read-only display
 *  of a query we generated ourselves — the cost/benefit of pulling in a syntax
 *  highlighting dependency for it is poor. The text is escaped by React before
 *  it reaches the DOM, so this cannot inject markup.
 */
function highlight(sql: string): ReactNode[] {
  const parts: ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  SQL_KEYWORDS.lastIndex = 0;
  while ((match = SQL_KEYWORDS.exec(sql)) !== null) {
    if (match.index > lastIndex) parts.push(sql.slice(lastIndex, match.index));
    parts.push(
      <span className="sql__keyword" key={`${match.index}-${match[0]}`}>
        {match[0]}
      </span>,
    );
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < sql.length) parts.push(sql.slice(lastIndex));
  return parts;
}

function Caret({ open }: { open: boolean }) {
  return (
    <svg
      className={`panel__caret ${open ? 'panel__caret--open' : ''}`}
      width="12"
      height="12"
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden="true"
    >
      <path
        d="M6 3.5l5 4.5-5 4.5"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

interface PanelProps {
  title: string;
  defaultOpen?: boolean;
  action?: ReactNode;
  children: ReactNode;
}

export function Panel({ title, defaultOpen = false, action, children }: PanelProps) {
  const [open, setOpen] = useState(defaultOpen);

  return (
    <section className="panel">
      {/* The header is a container, not a button. The toggle and the action are
          siblings: nesting a <button> inside a <button> is invalid HTML, and
          browsers resolve it unpredictably — the copy control either swallows
          the toggle click or fires both. */}
      <div className="panel__head">
        <button
          className="panel__toggle"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
        >
          <Caret open={open} />
          {title}
        </button>
        {open && action ? action : null}
      </div>
      {open && <div className="panel__body">{children}</div>}
    </section>
  );
}

export function SqlPanel({ sql }: { sql: string }) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(sql);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard blocked (insecure origin or denied permission) — ignore */
    }
  };

  return (
    <Panel
      title="SQL"
      action={
        <button className="copy" onClick={copy}>
          {copied ? 'Copied' : 'Copy'}
        </button>
      }
    >
      <pre className="sql">{highlight(sql)}</pre>
    </Panel>
  );
}

/** Matches a string that is wholly a number, including decimals and negatives. */
const NUMERIC_TEXT = /^-?\d+(\.\d+)?$/;

function renderCell(value: unknown) {
  if (value === null || value === undefined) {
    return { className: 'is-null', text: 'null' };
  }
  if (typeof value === 'boolean') {
    return { className: '', text: value ? 'true' : 'false' };
  }
  if (typeof value === 'number') {
    return { className: 'is-number', text: String(value) };
  }

  // Exact numeric types arrive as strings, not numbers: PostgreSQL NUMERIC and
  // SQLite DECIMAL are serialised as text to avoid the precision loss of a
  // JavaScript float. Typing alone would leave money columns left-aligned in a
  // ragged column, so the value is inspected rather than just its type.
  const text = String(value);
  return { className: NUMERIC_TEXT.test(text) ? 'is-number' : '', text };
}

interface TableProps {
  columns: string[];
  rows: unknown[][];
  truncated: boolean;
  rowCount: number;
}

export function ResultTable({ columns, rows, truncated, rowCount }: TableProps) {
  if (columns.length === 0) return null;

  return (
    <Panel title={`Results · ${rowCount.toLocaleString()} row${rowCount === 1 ? '' : 's'}`}>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              {columns.map((column) => (
                <th key={column}>{column}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={rowIndex}>
                {row.map((value, cellIndex) => {
                  const cell = renderCell(value);
                  return (
                    <td key={cellIndex} className={cell.className} title={cell.text}>
                      {cell.text}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {truncated && (
        <div className="table-note">
          Showing the first {rows.length.toLocaleString()} rows — the full result set is larger.
        </div>
      )}
    </Panel>
  );
}
