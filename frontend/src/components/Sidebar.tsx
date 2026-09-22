/**
 * The slide-over panel: chats, schema, graph and datasets.
 *
 * Four things that all answer "what can I ask, and what have I asked?" — so
 * they share one surface with tabs rather than four separate drawers competing
 * for the same screen edge.
 *
 * There used to be a fifth, History: a flat list of every question ever asked.
 * It was removed. Once questions became conversational, half its entries were
 * fragments — "and for April?", "they should be veg" — which have no subject
 * outside the thread that gave them one, so the list was mostly unreadable
 * rows. The one thing it did well, finding the query you wrote last week,
 * survives as the search box in Chats, which shows each hit alongside the
 * conversation it came from and opens it there.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  deleteConversation,
  deleteDataset,
  getGraph,
  getStats,
  listConversations,
  listDatasets,
  searchTurns,
  uploadDataset,
  type ConversationSummary,
  type Dataset,
  type SchemaGraph as SchemaGraphData,
  type SchemaResponse,
  type SearchHit,
  type Stats,
} from '../api';
import { SchemaGraph } from './SchemaGraph';

type Tab = 'chats' | 'graph' | 'tables' | 'data';

const TABS: { id: Tab; label: string }[] = [
  { id: 'chats', label: 'Chats' },
  { id: 'graph', label: 'Graph' },
  { id: 'tables', label: 'Tables' },
  { id: 'data', label: 'Data' },
];

interface Props {
  schema: SchemaResponse | null;
  datasetId: string | null;
  activeConversationId: string | null;
  highlight: string[];
  onClose: () => void;
  onOpenConversation: (id: string) => void;
  onConversationDeleted: (id: string) => void;
  onDatasetChange: (id: string | null) => void;
}

export function Sidebar({
  schema,
  datasetId,
  activeConversationId,
  highlight,
  onClose,
  onOpenConversation,
  onConversationDeleted,
  onDatasetChange,
}: Props) {
  // Chats first: resuming a conversation is the most common reason to open
  // this panel.
  const [tab, setTab] = useState<Tab>('chats');

  const [graph, setGraph] = useState<SchemaGraphData | null>(null);
  const [conversations, setConversations] = useState<ConversationSummary[] | null>(null);
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [search, setSearch] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => event.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  // The graph is refetched when the dataset changes, because it is a different
  // database with a different shape.
  useEffect(() => {
    getGraph(datasetId).then(setGraph).catch(() => setGraph(null));
  }, [datasetId]);

  const refreshChats = useCallback(() => {
    listConversations().then(setConversations).catch(() => setConversations([]));
    getStats().then(setStats).catch(() => setStats(null));
  }, []);

  useEffect(() => {
    if (tab === 'chats') refreshChats();
    if (tab === 'data') listDatasets().then(setDatasets).catch(() => setDatasets([]));
  }, [tab, refreshChats]);

  // Search runs against the server rather than filtering the loaded list: the
  // list holds conversation titles only, and the question you are looking for
  // is usually not the one a thread was named after.
  useEffect(() => {
    const term = search.trim();
    if (!term) {
      setHits(null);
      return;
    }
    // Debounced, so typing does not fire a request per keystroke.
    const timer = setTimeout(() => {
      searchTurns(term).then(setHits).catch(() => setHits([]));
    }, 200);
    return () => clearTimeout(timer);
  }, [search]);

  const upload = async (file: File) => {
    setBusy(true);
    setError(null);
    try {
      const dataset = await uploadDataset(file);
      setDatasets((current) => [dataset, ...current]);
      onDatasetChange(dataset.id);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Upload failed.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="drawer__scrim" onClick={onClose} />
      <aside className="drawer drawer--wide" role="dialog" aria-label="Database explorer">
        <header className="drawer__head">
          <div>
            <div className="drawer__title">Explore</div>
            {schema && (
              <div className="drawer__sub">
                {schema.table_count} tables · version {schema.version}
              </div>
            )}
          </div>
          <button className="ghost-button" onClick={onClose}>
            Close
          </button>
        </header>

        <nav className="tabs">
          {TABS.map((item) => (
            <button
              key={item.id}
              className={`tab ${tab === item.id ? 'tab--on' : ''}`}
              onClick={() => setTab(item.id)}
            >
              {item.label}
            </button>
          ))}
        </nav>

        <div className={`drawer__body ${tab === 'graph' ? 'drawer__body--flush' : ''}`}>
          {error && <div className="notice notice--error">{error}</div>}

          {tab === 'chats' && (
            <ChatList
              conversations={conversations}
              hits={hits}
              stats={stats}
              search={search}
              activeId={activeConversationId}
              onSearch={setSearch}
              onOpen={onOpenConversation}
              onDelete={async (id) => {
                await deleteConversation(id);
                setConversations((current) =>
                  (current ?? []).filter((item) => item.id !== id),
                );
                onConversationDeleted(id);
              }}
            />
          )}

          {tab === 'graph' &&
            (graph ? (
              <SchemaGraph graph={graph} highlight={highlight} />
            ) : (
              <div className="notice">Loading the graph…</div>
            ))}

          {tab === 'tables' && <TableList schema={schema} />}

          {tab === 'data' && (
            <DataList
              datasets={datasets}
              activeId={datasetId}
              busy={busy}
              fileInput={fileInput}
              onUpload={upload}
              onSelect={onDatasetChange}
              onDelete={async (id) => {
                await deleteDataset(id);
                setDatasets((current) => current.filter((d) => d.id !== id));
                if (datasetId === id) onDatasetChange(null);
              }}
            />
          )}
        </div>
      </aside>
    </>
  );
}

// --------------------------------------------------------------------------

function ChatList({
  conversations,
  hits,
  stats,
  search,
  activeId,
  onSearch,
  onOpen,
  onDelete,
}: {
  conversations: ConversationSummary[] | null;
  hits: SearchHit[] | null;
  stats: Stats | null;
  search: string;
  activeId: string | null;
  onSearch: (value: string) => void;
  onOpen: (id: string) => void;
  onDelete: (id: string) => void;
}) {
  if (!conversations) return <div className="notice">Loading conversations…</div>;

  // Threads with no questions in them are noise. They exist because the server
  // creates a thread before the first answer lands, so a question that failed
  // to send leaves one behind.
  const visible = conversations.filter((item) => item.turns > 0);

  return (
    <>
      <input
        className="search"
        placeholder="Search every question asked…"
        value={search}
        onChange={(event) => onSearch(event.target.value)}
      />

      {stats && stats.total > 0 && !hits && (
        <div className="metrics" style={{ marginBottom: 12 }}>
          <span className="metric">
            <strong>{stats.total}</strong> asked
          </span>
          {/* Over the turns where an answer was attempted. A clarification is
              not a failed answer — it is the agent declining to guess — and
              counting it as one read as a collapse. */}
          <span className="metric">
            <strong>{Math.round(stats.success_rate * 100)}%</strong> answered
          </span>
          {stats.clarified > 0 && (
            <span className="metric">
              <strong>{stats.clarified}</strong> asked back
            </span>
          )}
          <span className="metric">
            <strong>{stats.mean_seconds}s</strong> average
          </span>
          <span className="metric">
            <strong>{stats.total_tokens.toLocaleString()}</strong> tokens
          </span>
        </div>
      )}

      {/* Search results replace the thread list rather than sitting beside it:
          a hit is only meaningful with the conversation it belongs to, and
          showing both lists at once makes it unclear which one is being
          searched. */}
      {hits !== null ? (
        hits.length === 0 ? (
          <div className="notice">Nothing matches that search.</div>
        ) : (
          hits.map((hit) => (
            <button
              key={hit.id}
              className={`hit ${hit.ok ? '' : 'hit--failed'}`}
              onClick={() => hit.conversation_id && onOpen(hit.conversation_id)}
              disabled={!hit.conversation_id}
            >
              <span className="hit__question">{hit.question}</span>
              <span className="hit__meta">
                {hit.conversation_title ? `in "${hit.conversation_title}"` : 'no conversation'}
                {' · '}
                {new Date(hit.asked_at).toLocaleDateString()}
                {hit.row_count != null && ` · ${hit.row_count} rows`}
                {!hit.ok && ' · failed'}
              </span>
            </button>
          ))
        )
      ) : visible.length === 0 ? (
        <div className="notice">
          No conversations yet. Ask something, then ask a follow-up — it remembers
          the previous turns.
        </div>
      ) : (
        visible.map((item) => (
          <div
            key={item.id}
            className={`dataset ${item.id === activeId ? 'dataset--on' : ''}`}
          >
            <button className="dataset__pick" onClick={() => onOpen(item.id)}>
              <span className="dataset__name">{item.title}</span>
              <span className="dataset__meta">
                {item.turns} question{item.turns === 1 ? '' : 's'} ·{' '}
                {new Date(item.updated_at).toLocaleString()}
              </span>
              {/* The id, shown plainly. It is what the API takes, what the logs
                  record, and what you quote when reporting that a particular
                  conversation went wrong. */}
              <span className="dataset__id">{item.id}</span>
            </button>
            <button
              className="histrow__delete"
              onClick={() => onDelete(item.id)}
              aria-label={`Delete ${item.title}`}
            >
              ×
            </button>
          </div>
        ))
      )}
    </>
  );
}

// --------------------------------------------------------------------------

function TableList({ schema }: { schema: SchemaResponse | null }) {
  const [open, setOpen] = useState<string | null>(null);

  if (!schema) return <div className="notice">Loading the schema…</div>;

  return (
    <>
      {schema.tables.map((table) => (
        <div className="tablecard" key={table.name}>
          <button
            className="tablecard__head"
            onClick={() => setOpen(open === table.name ? null : table.name)}
            aria-expanded={open === table.name}
          >
            <span>{table.name}</span>
            <span className="tablecard__count">{table.columns.length} columns</span>
          </button>

          {open === table.name && (
            <div className="tablecard__body">
              {table.columns.map((column) => {
                // The API sends "name TYPE"; split on the first space so types
                // containing spaces survive intact.
                const at = column.indexOf(' ');
                const name = at === -1 ? column : column.slice(0, at);
                const type = at === -1 ? '' : column.slice(at + 1);
                return (
                  <div
                    className={`column ${table.primary_key.includes(name) ? 'column--pk' : ''}`}
                    key={column}
                  >
                    <span className="column__name">{name}</span>
                    <span className="column__type">{type}</span>
                  </div>
                );
              })}
              {table.references.length > 0 && (
                <div className="refs">references {table.references.join(', ')}</div>
              )}
            </div>
          )}
        </div>
      ))}
    </>
  );
}

// --------------------------------------------------------------------------

function DataList({
  datasets,
  activeId,
  busy,
  fileInput,
  onUpload,
  onSelect,
  onDelete,
}: {
  datasets: Dataset[];
  activeId: string | null;
  busy: boolean;
  fileInput: React.RefObject<HTMLInputElement | null>;
  onUpload: (file: File) => void;
  onSelect: (id: string | null) => void;
  onDelete: (id: string) => void;
}) {
  const [dragging, setDragging] = useState(false);

  return (
    <>
      <div
        className={`drop ${dragging ? 'drop--over' : ''}`}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          const file = event.dataTransfer.files[0];
          if (file) onUpload(file);
        }}
        onClick={() => fileInput.current?.click()}
      >
        <input
          ref={fileInput}
          type="file"
          hidden
          accept=".csv,.tsv,.xlsx,.xlsm,.xls,.sql,.dump,.backup"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) onUpload(file);
            event.target.value = '';
          }}
        />
        {busy ? (
          <span>Loading your file…</span>
        ) : (
          <>
            <strong>Drop a file, or click to choose</strong>
            <span className="drop__hint">CSV · Excel · PostgreSQL dump</span>
          </>
        )}
      </div>

      <button
        className={`dataset ${activeId === null ? 'dataset--on' : ''}`}
        onClick={() => onSelect(null)}
      >
        <span className="dataset__name">Configured database</span>
        <span className="dataset__meta">the one this server was started with</span>
      </button>

      {datasets.map((dataset) => (
        <div
          key={dataset.id}
          className={`dataset ${activeId === dataset.id ? 'dataset--on' : ''}`}
        >
          <button className="dataset__pick" onClick={() => onSelect(dataset.id)}>
            <span className="dataset__name">{dataset.name}</span>
            <span className="dataset__meta">
              {dataset.kind} · {dataset.tables.length} table
              {dataset.tables.length === 1 ? '' : 's'} ·{' '}
              {Object.values(dataset.row_counts)
                .reduce((sum, n) => sum + n, 0)
                .toLocaleString()}{' '}
              rows
            </span>
            {dataset.note && <span className="dataset__note">{dataset.note}</span>}
          </button>
          <button
            className="histrow__delete"
            onClick={() => onDelete(dataset.id)}
            aria-label={`Delete ${dataset.name}`}
          >
            ×
          </button>
        </div>
      ))}
    </>
  );
}
