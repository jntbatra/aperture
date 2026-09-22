/**
 * Shows what the agent is doing while it works.
 *
 * A question takes several seconds because it involves two or three model
 * calls. A bare spinner for that long reads as "broken". Naming each step —
 * finding tables, writing SQL, running it — makes the wait legible, and when
 * something fails the user can see how far it got.
 */

import { STAGE_LABELS, type ProgressEvent, type Stage } from '../api';

/** The order stages normally occur in. `repairing` is omitted because it is
 *  not part of the happy path; it is rendered inline when it happens. */
const SEQUENCE: Stage[] = [
  'seeds',
  'schema',
  'generating',
  'validating',
  'executing',
  'answering',
];

function Tick() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M3.5 8.5l3 3 6-7"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

interface Props {
  events: ProgressEvent[];
}

export function Progress({ events }: Props) {
  if (events.length === 0) return null;

  const latest = events[events.length - 1];
  const seen = new Set(events.map((event) => event.stage));
  const repairs = events.filter((event) => event.stage === 'repairing');

  // Only show stages that have started. Listing the whole sequence up front
  // would promise steps that may never run — a repair can end the request
  // early, and an empty result skips the answering call entirely.
  const visible = SEQUENCE.filter((stage) => seen.has(stage));

  return (
    <div className="progress" role="status" aria-live="polite">
      {visible.map((stage) => {
        const isActive = stage === latest.stage;
        return (
          <div
            key={stage}
            className={`progress__row ${isActive ? 'progress__row--active' : 'progress__row--done'}`}
          >
            <span className="progress__tick">
              {isActive ? <span className="spinner" /> : <Tick />}
            </span>
            {STAGE_LABELS[stage]}
          </div>
        );
      })}

      {repairs.length > 0 && (
        <div className="progress__row progress__row--active">
          <span className="progress__tick">
            <span className="spinner" />
          </span>
          {STAGE_LABELS.repairing}
          {repairs.length > 1 && ` (attempt ${repairs.length + 1})`}
        </div>
      )}

      {typeof latest.detail.sql === 'string' && (
        <div className="progress__detail">{latest.detail.sql}</div>
      )}
      {Array.isArray(latest.detail.tables) && (
        <div className="progress__detail">
          tables: {(latest.detail.tables as string[]).join(', ')}
        </div>
      )}
      {typeof latest.detail.error === 'string' && (
        <div className="progress__detail">{latest.detail.error}</div>
      )}
    </div>
  );
}
