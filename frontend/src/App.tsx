/**
 * Application shell: a conversation with a database.
 *
 * The shape of the state
 * ----------------------
 * One thread id, one list of exchanges, and the progress events for whichever
 * exchange is currently in flight. Everything else (schema, health, dataset) is
 * ambient.
 *
 * The thread id is the load-bearing piece. It is what the server uses to look
 * up the previous turns and attach them to the next question, which is what
 * makes "and for April?" a question at all. The client never invents one: the
 * server creates the thread on the first question and returns its id, so there
 * is no window in which the browser holds an id the server has not heard of.
 *
 * State is still plain `useState`. There are five things to track and none of
 * them are shared across distant parts of the tree; a state library here would
 * add indirection without removing any.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import './App.css';
import {
  askStreaming,
  getConversation,
  getHealth,
  getSchema,
  getSuggestions,
  type AskOptions,
  type AskResponse,
  type HealthResponse,
  type HistoryEntry,
  type ProgressEvent,
  type SchemaResponse,
} from './api';
import { Drift } from './components/Drift';
import { Sidebar } from './components/Sidebar';
import { ThemeToggle } from './components/ThemeToggle';
import { Toggles, loadOptions } from './components/Toggles';
import { Transcript, type Exchange } from './components/Transcript';

/** Where the current thread id is kept across reloads.
 *
 *  `localStorage` rather than the URL: the thread is not something the user
 *  shares or bookmarks, and putting an id in the address bar invites sharing a
 *  link that will not work for anyone else. */
const THREAD_KEY = 'aperture.thread';

/**
 * Suggest questions that fit the database actually connected.
 *
 * Hardcoded examples are worse than none: pointed at a real schema they name
 * tables that do not exist, and the first thing a new user clicks returns an
 * error. These are derived from the schema instead — the most-referenced
 * tables are the ones worth asking about, because being referenced is what
 * makes a table central.
 */
function suggestQuestions(schema: SchemaResponse | null): string[] {
  if (!schema || schema.tables.length === 0) return [];

  // Framework bookkeeping is not business data. Nobody wants to be invited to
  // ask about _prisma_migrations, and suggesting it wastes the one moment a new
  // user is deciding whether this tool understands their database.
  const isInternal = (name: string) =>
    name.startsWith('_') ||
    /^(schema_migrations|ar_internal_metadata|alembic_version|migrations|knex_migrations)/.test(
      name,
    ) ||
    /(^|_)(token|session|log|audit|cache)s?$/.test(name);

  const candidates = schema.tables.filter((table) => !isInternal(table.name));
  if (candidates.length === 0) return [];

  // How often each table is referenced by another. A table many others point
  // at is a hub — customers, orders, products — and is where useful questions
  // tend to live.
  const referencedBy = new Map<string, number>();
  for (const table of schema.tables) {
    for (const target of table.references) {
      referencedBy.set(target, (referencedBy.get(target) ?? 0) + 1);
    }
  }

  const ranked = [...candidates].sort(
    (a, b) =>
      (referencedBy.get(b.name) ?? 0) - (referencedBy.get(a.name) ?? 0) ||
      b.columns.length - a.columns.length,
  );

  const hub = ranked[0]?.name;
  const second = ranked[1]?.name;
  const withDate = ranked.find((table) =>
    table.columns.some((column) => /date|time|_at"?\s|At"\s/i.test(column)),
  );

  const suggestions = [
    hub && `How many rows are in ${hub}?`,
    withDate && `How many ${withDate.name} were created each month, most recent first?`,
    hub && second && `Show me ${hub} joined with ${second}`,
    ranked[2] && `What are the most common values in ${ranked[2].name}?`,
  ].filter((value): value is string => Boolean(value));

  return suggestions.slice(0, 4);
}

/**
 * Rebuild transcript turns from stored history.
 *
 * The store keeps the question, the answer, the SQL, and a bounded slice of the
 * result — the first fifty rows — so reopening a conversation still shows its
 * table rather than an apology. A query that returned more than that says so.
 *
 * The turn is still flagged `restored`, because the timings and token counts
 * describe the original run and there is no point re-displaying them as though
 * they were measured now.
 */
function toExchanges(entries: HistoryEntry[]): Exchange[] {
  return entries.map((entry) => ({
    key: `stored-${entry.id}`,
    question: entry.question,
    result: {
      question: entry.question,
      answer: entry.answer ?? '',
      conversation_id: entry.conversation_id,
      sql: entry.sql,
      columns: entry.result_preview?.columns ?? [],
      rows: entry.result_preview?.rows ?? [],
      row_count: entry.result_preview?.row_count ?? entry.row_count ?? 0,
      truncated: entry.result_preview?.truncated ?? false,
      ok: entry.ok,
      error: entry.error,
      repairs: entry.repairs ?? 0,
      model_calls: entry.model_calls ?? 0,
      input_tokens: 0,
      output_tokens: 0,
      seconds: entry.seconds ?? 0,
      tables_considered: [],
      clarification_asks: [],
      warnings: [],
    } satisfies AskResponse,
    error: null,
    pending: false,
    restored: true,
    restoredRowCount: entry.row_count,
  }));
}

/** Follow-ups to offer after an answer.
 *
 *  Deliberately generic and phrased as fragments. Their job is to demonstrate
 *  that fragments *work* — a user who has only ever used a search box does not
 *  know they can say "break that down by month", and one click teaches it
 *  better than any hint text. */
const FOLLOW_UPS = [
  'Break that down by month',
  'Show me the top 10',
  'Why might that be?',
];

type AppProps = {
  /** Leave the console for the marketing pages. Optional so the component can
   *  still be mounted on its own in a test or a storybook. */
  onExit?: () => void;
  account?: { workspace: string; plan_label: string };
  onAccount?: () => void;
};

export default function App({ onExit, account, onAccount }: AppProps = {}) {
  const [question, setQuestion] = useState('');
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [events, setEvents] = useState<ProgressEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [threadId, setThreadId] = useState<string | null>(null);

  // Quality toggles for the questions this browser asks. Restored from the last
  // session, because someone who wants every query reviewed should not have to
  // say so again each morning.
  const [options, setOptions] = useState<AskOptions>(() => loadOptions());

  // Opening questions. Proposed by the server from the table names and the
  // glossary, because questions derived from the schema shape alone came out
  // answerable and useless — "How many rows are in kitchen_profiles?" is a fact
  // about storage, not a question anybody has.
  const [suggested, setSuggested] = useState<string[] | null>(null);

  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [schema, setSchema] = useState<SchemaResponse | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  // Opened deliberately, never shown unprompted. A panel that pops up saying
  // something changed, on evidence that cannot distinguish a regression from a
  // settings change, is an alarm nobody would keep.
  const [driftOpen, setDriftOpen] = useState(false);

  // null means the database this server was started with; a string selects an
  // uploaded dataset.
  const [datasetId, setDatasetId] = useState<string | null>(null);

  // Holds the cancel function for an in-flight request, so a new question can
  // abandon the previous one rather than racing it.
  const cancelRef = useRef<(() => void) | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    getHealth().then(setHealth).catch(() => setHealth(null));
    getSchema().then(setSchema).catch(() => setSchema(null));
    // null keeps the schema-derived fallback in play until this resolves.
    getSuggestions()
      .then((payload) => setSuggested(payload.questions))
      .catch(() => setSuggested([]));
  }, []);

  // Restore the thread that was open when the page was last closed. A
  // conversation that evaporates on refresh is not a conversation.
  useEffect(() => {
    const saved = localStorage.getItem(THREAD_KEY);
    if (!saved) return;

    getConversation(saved)
      .then((detail) => {
        setThreadId(detail.conversation.id);
        setExchanges(toExchanges(detail.entries));
      })
      .catch(() => {
        // The thread is gone — a cleared store, or a different machine. Drop
        // the stale id rather than showing an error about something the user
        // never asked for.
        localStorage.removeItem(THREAD_KEY);
      });
  }, []);

  // Cancel any open stream when the component goes away, so a late answer
  // cannot arrive after unmount.
  useEffect(() => () => cancelRef.current?.(), []);

  const submit = useCallback(
    (text: string) => {
      const trimmed = text.trim();
      if (!trimmed) return;

      cancelRef.current?.();

      const key = `live-${Date.now()}`;
      setBusy(true);
      setEvents([]);
      setQuestion('');
      setExchanges((current) => [
        ...current,
        { key, question: trimmed, result: null, error: null, pending: false },
      ]);
      // Marked pending in its own update so the turn is on screen before the
      // progress panel appears under it.
      setExchanges((current) =>
        current.map((item) => (item.key === key ? { ...item, pending: true } : item)),
      );

      const settle = (patch: Partial<Exchange>) => {
        setExchanges((current) =>
          current.map((item) =>
            item.key === key ? { ...item, ...patch, pending: false } : item,
          ),
        );
        setBusy(false);
      };

      cancelRef.current = askStreaming(trimmed, {
        datasetId,
        conversationId: threadId,
        options,
        onProgress: (event) => setEvents((previous) => [...previous, event]),
        onResult: (payload) => {
          // The server creates the thread on the first question; this is where
          // the client learns its id.
          if (payload.conversation_id && payload.conversation_id !== threadId) {
            setThreadId(payload.conversation_id);
            localStorage.setItem(THREAD_KEY, payload.conversation_id);
          }
          settle({ result: payload });
        },
        onError: (message) => settle({ error: message }),
      });
    },
    [datasetId, threadId, options],
  );

  const startNewThread = useCallback(() => {
    cancelRef.current?.();
    setThreadId(null);
    setExchanges([]);
    setEvents([]);
    setBusy(false);
    setQuestion('');
    localStorage.removeItem(THREAD_KEY);
    inputRef.current?.focus();
  }, []);

  const openThread = useCallback(async (id: string) => {
    cancelRef.current?.();
    setDrawerOpen(false);
    setBusy(false);
    setEvents([]);

    const detail = await getConversation(id);
    setThreadId(detail.conversation.id);
    localStorage.setItem(THREAD_KEY, detail.conversation.id);
    setExchanges(toExchanges(detail.entries));
  }, []);

  const onKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends, Shift+Enter makes a newline — the convention every chat
    // interface uses, so it needs no explanation.
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      submit(question);
    }
  };

  const empty = exchanges.length === 0;
  const lastAnswered = !busy && exchanges.length > 0 && exchanges[exchanges.length - 1].result;

  return (
    <div className="shell">
      {driftOpen && <Drift onClose={() => setDriftOpen(false)} />}
      <header className="header">
        <div className="header__brand">
          <span className="header__mark" />
          <span>Aperture</span>
        </div>

        <div className="header__meta">
          <span>
            <span
              className={`header__dot ${health ? '' : 'header__dot--down'}`}
              aria-hidden="true"
            />
            {health ? `${health.tables} tables` : 'offline'}
          </span>
          {health && <span>{health.light_model}</span>}
          {datasetId && (
            <span className="pill">
              uploaded data
              <button onClick={() => setDatasetId(null)} aria-label="Use the configured database">
                ×
              </button>
            </span>
          )}
          {/* The chat id, shown plainly and copyable. It is what the API
              takes, what the server logs record, and what you quote when
              reporting that one particular conversation went wrong — so
              hiding it in a drawer would be hiding the one identifier that
              makes a report actionable. */}
          {threadId && (
            <button
              className="pill pill--id"
              title="Copy this chat's id"
              onClick={() => navigator.clipboard?.writeText(threadId)}
            >
              chat {threadId}
            </button>
          )}
          <ThemeToggle />
          <Toggles options={options} onChange={setOptions} disabled={busy} />
          <button
            className="ghost-button"
            onClick={() => setDriftOpen(true)}
            title="Compare recent questions against earlier ones"
          >
            Drift
          </button>
          <button className="ghost-button" onClick={startNewThread} disabled={empty && !threadId}>
            New chat
          </button>
          {onExit && (
            <button className="ghost-button" onClick={onExit}>
              Plans
            </button>
          )}
          {account && onAccount && (
            /* The workspace name is the thing that tells a user which account
               they are in before they act — the one piece of state that is
               dangerous to get wrong when someone has more than one. */
            <button className="ghost-button" onClick={onAccount}>
              {account.workspace}
            </button>
          )}
          <button
            className="ghost-button"
            aria-pressed={drawerOpen}
            onClick={() => setDrawerOpen((open) => !open)}
          >
            Explore
          </button>
        </div>
      </header>

      <main className={`main ${empty ? '' : 'main--thread'}`}>
        {empty && (
          <section className="hero">
            <h1 className="hero__title">
              Ask your database <em>anything</em>
            </h1>
            <p className="hero__subtitle">
              Questions in plain language, answered with real SQL you can read and check.
              Ask a follow-up and it remembers what you meant.
            </p>
          </section>
        )}

        {!empty && <Transcript exchanges={exchanges} events={events} onReply={submit} />}

        <div className="composer">
          <div className="ask">
            <textarea
              ref={inputRef}
              className="ask__input"
              placeholder={
                empty ? 'How many orders were placed last month?' : 'Ask a follow-up…'
              }
              value={question}
              rows={1}
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={onKeyDown}
              disabled={busy}
            />
            <button
              className="ask__submit"
              onClick={() => submit(question)}
              disabled={busy || !question.trim()}
            >
              {busy ? 'Working…' : 'Ask'}
            </button>
          </div>

          <div className="ask__hint">
            <kbd>Enter</kbd> to send · <kbd>Shift</kbd>+<kbd>Enter</kbd> for a new line
          </div>

          {/* Suggestions before the first question; follow-ups after an answer.
              Both exist for the same reason — to show what this accepts — but
              the second set is the one that teaches the conversational part. */}
          {empty && (
            <div className="examples">
              {(suggested?.length ? suggested : suggestQuestions(schema)).map((example) => (
                <button key={example} className="chip" onClick={() => submit(example)}>
                  {example}
                </button>
              ))}
            </div>
          )}

          {lastAnswered && (
            <div className="examples">
              {FOLLOW_UPS.map((text) => (
                <button key={text} className="chip" onClick={() => submit(text)}>
                  {text}
                </button>
              ))}
            </div>
          )}
        </div>
      </main>

      {drawerOpen && (
        <Sidebar
          schema={schema}
          datasetId={datasetId}
          activeConversationId={threadId}
          // Highlighting the tables the last answer used makes the graph show
          // which corner of the schema was actually walked.
          highlight={exchanges[exchanges.length - 1]?.result?.tables_considered ?? []}
          onClose={() => setDrawerOpen(false)}
          onOpenConversation={openThread}
          onConversationDeleted={(id) => {
            if (id === threadId) startNewThread();
          }}
          onDatasetChange={(id) => {
            setDatasetId(id);
            // A different database. The thread so far was about the old one,
            // and carrying its turns forward would feed the model SQL against
            // tables that no longer exist.
            startNewThread();
            getSchema().then(setSchema).catch(() => setSchema(null));
          }}
        />
      )}
    </div>
  );
}
