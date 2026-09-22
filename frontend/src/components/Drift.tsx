/**
 * Has the agent started behaving differently?
 *
 * What this panel is allowed to claim
 * -----------------------------------
 * Nothing here knows whether an answer was *right*. There is no label. What
 * the history holds, for every question ever asked, is how it went: succeeded
 * or not, needed a repair or not, how long, how many tokens, whether the query
 * came back empty.
 *
 * So the claim is narrow, and the wording matters: **this set of questions was
 * answered measurably differently from that set**. A human decides what it
 * means. A rise in the failure rate is not proof the agent got worse — the
 * questions may have got harder.
 *
 * Why a p-value is on the screen
 * ------------------------------
 * The alternative is a row of percentages that move between any two samples,
 * which produces a false alarm on a slow Tuesday and trains everyone to ignore
 * the panel. A shift is shown only when it is both statistically significant
 * and large enough to act on; the metrics that held are shown underneath,
 * because "slower but no more failures" is a different situation from "both"
 * and only one of them is a correctness concern.
 *
 * Why "not enough yet" is a state with its own message
 * ----------------------------------------------------
 * An empty list of shifts and an unmeasurable history look identical and mean
 * opposite things. A fresh install rendering a calm green panel is a claim
 * nobody made.
 *
 * Why an improvement is not styled as an alarm
 * --------------------------------------------
 * Every metric is a rate of something undesirable, so a fall is good news and
 * still drift. Colouring by direction rather than by movement is what keeps
 * the panel readable at a glance.
 */

import { useEffect, useState } from 'react';

import { DRIFT_LABELS, getDrift, type DriftReport } from '../api';

function percent(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

/** A p-value small enough that the decimal expansion stops being informative.
 *  "p < 0.0001" says what "p = 0.0000" fails to. */
function significance(p: number): string {
  return p < 0.0001 ? 'p < 0.0001' : `p = ${p.toFixed(4)}`;
}

export function Drift({ onClose }: { onClose: () => void }) {
  const [report, setReport] = useState<DriftReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getDrift()
      .then(setReport)
      .catch((problem: Error) => setError(problem.message));
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => event.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <>
      <div className="toggles__scrim" onClick={onClose} />
      <div className="toggles__panel drift" role="dialog" aria-label="Drift">
        <div className="toggles__head">
          <strong>Has anything changed?</strong>
          <span>
            The most recent questions against the ones before them. This measures
            how the agent behaved, never whether it was right — nothing here knows
            that.
          </span>
        </div>

        {error && <p className="drift__note">Could not load: {error}</p>}
        {!report && !error && <p className="drift__note">Reading the history…</p>}

        {report && !report.enough_data && (
          <p className="drift__note">
            Not enough history to compare yet — {report.baseline_n} earlier and{' '}
            {report.recent_n} recent questions. This is not "no drift": it is
            nobody having looked.
          </p>
        )}

        {report && report.enough_data && (
          <>
            <p className="drift__note">
              Last <strong>{report.recent_n}</strong> questions against the{' '}
              <strong>{report.baseline_n}</strong> before them.
            </p>

            {report.shifts.length === 0 ? (
              <p className="drift__note">
                Nothing moved by enough to be worth reporting.
              </p>
            ) : (
              <ul className="drift__shifts">
                {report.shifts.map((shift) => (
                  <li
                    key={shift.metric}
                    className={`drift__shift ${
                      shift.worse ? 'drift__shift--worse' : 'drift__shift--better'
                    }`}
                  >
                    <div className="drift__metric">
                      {DRIFT_LABELS[shift.metric] ?? shift.metric}
                    </div>
                    <div className="drift__numbers">
                      {percent(shift.baseline)} → <strong>{percent(shift.recent)}</strong>
                      <span className="drift__p">{significance(shift.p_value)}</span>
                    </div>
                  </li>
                ))}
              </ul>
            )}

            {/* The metrics that held. Evidence too: a shift in latency with a
                flat failure rate is a performance story, not a correctness one. */}
            <table className="drift__rates">
              <tbody>
                {Object.entries(report.rates).map(([metric, [before, after]]) => (
                  <tr key={metric}>
                    <td>{DRIFT_LABELS[metric] ?? metric}</td>
                    <td>{percent(before)}</td>
                    <td>{percent(after)}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            <p className="drift__caveat">
              A shift is a prompt to go and look, not a verdict. Questions get
              harder, defaults get changed, models get swapped — all three show
              up here exactly like a regression does.
            </p>
          </>
        )}

        <div className="toggles__foot">
          <button className="ghost-button" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </>
  );
}
