/**
 * The conversation, drawn as a transcript.
 *
 * Why a transcript rather than one answer at a time
 * -------------------------------------------------
 * The previous UI replaced the answer on every question. That is a correct
 * shape for a search box and the wrong one for an analyst: the second question
 * is almost always about the first ("and for April?", "break that down by
 * city"), and you cannot read a comparison whose other half has been erased.
 * Keeping the turns on screen is also what makes the follow-up *legible* — the
 * user can see exactly what context the model is answering against.
 *
 * Restored turns are not identical to live ones
 * ---------------------------------------------
 * Reloading the page replays the thread from the server, which stores the
 * question, the answer and the SQL — but deliberately not the result rows,
 * because a query like "list every customer" would turn the history into a
 * shadow copy of the database. A restored turn therefore shows the answer and
 * the SQL, and says plainly that the table is not kept rather than rendering an
 * empty one and letting the user conclude the query returned nothing.
 */

import { useEffect, useRef, useState } from 'react';
import type { AskResponse } from '../api';
import { Progress } from './Progress';
import { ResultTable, SqlPanel } from './ResultPanels';
import type { ProgressEvent } from '../api';

/** Tables named in the metrics row before it collapses to "+N more". */
const TABLES_SHOWN = 6;

export interface Exchange {
  /** Stable key. Not the array index: a re-render while a request is in flight
   *  would otherwise remount the whole list and lose scroll position. */
  key: string;
  question: string;
  result: AskResponse | null;
  error: string | null;
  /** True while the answer is still being produced. */
  pending: boolean;
  /** True for a turn replayed from the store, which has no result rows. */
  restored?: boolean;
  /** Row count recorded at the time, for restored turns. */
  restoredRowCount?: number | null;
}

interface Props {
  exchanges: Exchange[];
  /** Progress events for the in-flight turn, if any. */
  events: ProgressEvent[];
  /** Answer a clarifying question by clicking one of its options. */
  onReply?: (text: string) => void;
}

export function Transcript({ exchanges, events, onReply }: Props) {
  const endRef = useRef<HTMLDivElement>(null);

  // Follow the conversation down as it grows, the way every chat interface
  // does. Keyed on the number of turns and on whether the last one is still
  // working, so the view also scrolls when an answer lands and the turn grows
  // taller.
  const last = exchanges[exchanges.length - 1];
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [exchanges.length, last?.pending, last?.result]);

  return (
    <div className="thread">
      {exchanges.map((exchange, index) => (
        <ExchangeView
          key={exchange.key}
          exchange={exchange}
          events={events}
          // Only the newest turn can still be answered. Options on an older
          // clarification are stale — that question was already resolved.
          onReply={index === exchanges.length - 1 ? onReply : undefined}
        />
      ))}
      <div ref={endRef} />
    </div>
  );
}

/**
 * One row of buttons per ambiguity, answered together.
 *
 * Why not one question at a time
 * ------------------------------
 * Because the reply to the first would resolve one dimension and the agent
 * would have to ask again for the second, which is two more round trips and a
 * loop risk. Collecting all the answers and sending them as one sentence keeps
 * it to a single exchange.
 *
 * Why buttons rather than a text box
 * ----------------------------------
 * Shown "(by revenue / by order frequency)" and given somewhere to type, a user
 * answers "Yes" — observed, and it is a perfectly reasonable thing to type at a
 * question phrased like that. The options are the answer; they should be
 * clickable.
 */
function AskForm({
  asks,
  onReply,
}: {
  asks: { question: string; options: string[] }[];
  onReply: (text: string) => void;
}) {
  const [chosen, setChosen] = useState<Record<number, string>>({});
  const answered = asks.filter((_, index) => chosen[index]).length;

  const send = (picks: Record<number, string>) => {
    // Sent as prose rather than a structured payload: the agent reads it as the
    // next turn in the conversation, and "by revenue, last 30 days" is exactly
    // what a person would have typed.
    onReply(asks.map((_, index) => picks[index]).filter(Boolean).join(', '));
  };

  return (
    <div className="askform">
      {asks.map((ask, index) => (
        <div className="askform__row" key={ask.question}>
          <div className="askform__label">{ask.question}</div>
          <div className="examples">
            {ask.options.map((option) => (
              <button
                key={option}
                className={`chip ${chosen[index] === option ? 'chip--on' : ''}`}
                onClick={() => {
                  const picks = { ...chosen, [index]: option };
                  setChosen(picks);
                  // The last outstanding answer sends immediately: making
                  // someone click a button and then a second button to confirm
                  // is a step that carries no decision.
                  if (Object.keys(picks).length === asks.length) send(picks);
                }}
              >
                {option}
              </button>
            ))}
          </div>
        </div>
      ))}

      {asks.length > 1 && answered < asks.length && (
        <div className="askform__hint">
          {answered} of {asks.length} answered
        </div>
      )}
    </div>
  );
}

function ExchangeView({
  exchange,
  events,
  onReply,
}: {
  exchange: Exchange;
  events: ProgressEvent[];
  onReply?: (text: string) => void;
}) {
  const { question, result, error, pending, restored } = exchange;
  const asking = result?.error === 'needs_clarification';
  const asks = asking ? result.clarification_asks ?? [] : [];

  return (
    <article className="turn">
      <div className="turn__ask">
        <div className="bubble">{question}</div>
      </div>

      <div className="turn__reply">
        {pending && <Progress events={events} />}

        {error && <div className="notice notice--error">{error}</div>}

        {result && (
          <>
            {/* A clarification is not an error, and must not be styled as one.
                It is the agent declining to guess, which is the behaviour we
                asked for. */}
            <div
              className={`answer__body ${
                asking ? 'answer__body--asking' : result.ok ? '' : 'answer__body--error'
              }`}
            >
              {result.answer}
            </div>

            {asks.length > 0 && onReply && <AskForm asks={asks} onReply={onReply} />}

            <div className="metrics">
              {!restored && (
                <span className="metric">
                  <strong>{result.seconds.toFixed(1)}s</strong>
                </span>
              )}
              {!restored && (
                <span className="metric">
                  <strong>{result.model_calls}</strong> model calls
                </span>
              )}
              {!restored && (
                <span className="metric">
                  <strong>
                    {(result.input_tokens + result.output_tokens).toLocaleString()}
                  </strong>{' '}
                  tokens
                </span>
              )}
              {result.repairs > 0 && (
                <span className="metric metric--warn">
                  <strong>{result.repairs}</strong> repair
                  {result.repairs === 1 ? '' : 's'}
                </span>
              )}
              {result.tables_considered.length > 0 && (
                // Truncated. A follow-up often widens the search and comes back
                // with twenty-odd tables, which wraps the metrics row to three
                // lines and buries the numbers next to it. The full list is in
                // the SQL, which is right below.
                <span className="metric" title={result.tables_considered.join(', ')}>
                  {result.tables_considered.slice(0, TABLES_SHOWN).join(', ')}
                  {result.tables_considered.length > TABLES_SHOWN &&
                    ` +${result.tables_considered.length - TABLES_SHOWN} more`}
                </span>
              )}
            </div>

            {/* Above the SQL, not below it. The whole point is to send the
                reader to the query before they act on the figure — a caveat
                printed under the result table has already been scrolled past. */}
            {result.warnings?.length > 0 && (
              <div className="caution" role="note">
                <strong>This number may not mean what it looks like.</strong>
                <ul>
                  {result.warnings.map((warning) => (
                    <li key={warning}>{warning}</li>
                  ))}
                </ul>
              </div>
            )}

            {result.sql && <SqlPanel sql={result.sql} />}

            {result.columns.length > 0 && (
              <ResultTable
                columns={result.columns}
                rows={result.rows}
                truncated={result.truncated}
                rowCount={result.row_count}
              />
            )}

            {/* Only when the table genuinely is not there. A restored turn that
                kept its rows needs no apology, and printing one next to a
                visible table would be nonsense. */}
            {restored && result.sql && result.columns.length === 0 && (
              <div className="turn__note">
                {exchange.restoredRowCount
                  ? `${exchange.restoredRowCount.toLocaleString()} rows were returned. `
                  : ''}
                The table was not kept — re-run the query to see it.
              </div>
            )}
          </>
        )}
      </div>
    </article>
  );
}
