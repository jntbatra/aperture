/**
 * The pricing page.
 *
 * The numbers come from the server
 * --------------------------------
 * `GET /api/plans` returns the tiers. They are pricing policy, they live in
 * `saas/plans.py`, and they are what billing enforces. A second copy in this
 * component is a copy that will disagree with billing on the day one of them
 * changes — and the direction it disagrees in is "we advertised more than we
 * deliver", which is the expensive direction.
 *
 * Two currencies, chosen not converted
 * ------------------------------------
 * $15 and ₹1,000 are both set. A live conversion would move the advertised
 * price daily, and ₹1,000 is a round number in a way that $15 converted is
 * not. The toggle switches which is shown; it does not compute anything.
 *
 * Why "unlimited" is rendered from a sentinel
 * -------------------------------------------
 * The API sends -1. Printing that would be absurd, and the guard has to live
 * here because the alternative — the server sending the string "unlimited" —
 * makes every numeric comparison a string comparison for every other consumer.
 *
 * Why Enterprise never shows a price
 * ----------------------------------
 * Its price is 0, and so is Free's. Zero means two opposite things in that
 * table, which is exactly why `custom_priced` is a separate flag rather than
 * something to infer. Inferring it renders "$0" beside "unlimited everything".
 */

import { useEffect, useState } from 'react';

import { getPlans, type PlanInfo } from '../api';

type Currency = 'usd' | 'inr';

function limit(value: number): string {
  return value < 0 ? 'Unlimited' : value.toLocaleString();
}

function price(plan: PlanInfo, currency: Currency): string {
  if (plan.custom_priced) return "Let's talk";
  if (plan.price_monthly_usd === 0 && plan.price_monthly_inr === 0) return 'Free';
  return currency === 'usd'
    ? `$${plan.price_monthly_usd}`
    : `₹${plan.price_monthly_inr.toLocaleString('en-IN')}`;
}

/** What each tier actually gives you, in the order someone decides in. */
function lines(plan: PlanInfo): string[] {
  const rows = [
    `${limit(plan.questions_per_month)} questions a month`,
    plan.detailed_per_month === 0
      ? 'Fast answers'
      : `${limit(plan.detailed_per_month)} detailed answers included`,
    plan.strong_model ? 'Answered by the strong model' : 'Answered by the fast model',
    `${limit(plan.max_connected_databases)} connected database${
      plan.max_connected_databases === 1 ? '' : 's'
    }`,
    `${limit(plan.max_uploaded_datasets)} upload${
      plan.max_uploaded_datasets === 1 ? '' : 's'
    }`,
    `${limit(plan.max_seats)} seat${plan.max_seats === 1 ? '' : 's'}`,
    `Up to ${limit(plan.row_limit)} rows per answer`,
    `${limit(plan.history_retention_days)} days of history`,
  ];
  if (plan.features.includes('sdk')) rows.push('API and SDK access');
  if (plan.features.includes('sso')) rows.push('SSO and audit log');
  return rows;
}

export function Pricing({ onChoose }: { onChoose: (plan: PlanInfo) => void }) {
  const [plans, setPlans] = useState<PlanInfo[] | null>(null);
  const [currency, setCurrency] = useState<Currency>('usd');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getPlans()
      .then(setPlans)
      .catch((problem: Error) => setError(problem.message));
  }, []);

  return (
    <section className="pricing">
      <header className="pricing__head">
        <h1>Pay for the questions, not the seats</h1>
        <p>
          A question is a few model calls. A detailed one is several times that,
          so it has its own allowance rather than quietly eating your month.
        </p>

        <div className="pricing__currency" role="group" aria-label="Currency">
          {(['usd', 'inr'] as Currency[]).map((option) => (
            <button
              key={option}
              type="button"
              className={currency === option ? 'is-on' : ''}
              aria-pressed={currency === option}
              onClick={() => setCurrency(option)}
            >
              {option === 'usd' ? 'USD' : 'INR'}
            </button>
          ))}
        </div>
      </header>

      {error && <p className="pricing__error">Could not load plans: {error}</p>}
      {!plans && !error && <p className="pricing__error">Loading…</p>}

      <div className="pricing__grid">
        {plans?.map((plan) => (
          <article
            key={plan.name}
            /* Pro is marked, and it is marked because it is the one most
               people should buy — not as a dark pattern. The free tier is a
               real product and the enterprise tier is a conversation. */
            className={`plan ${plan.name === 'PRO' ? 'plan--featured' : ''}`}
          >
            {plan.name === 'PRO' && <span className="plan__badge">Most chosen</span>}
            <h2>{plan.label}</h2>
            <div className="plan__price">
              {price(plan, currency)}
              {!plan.custom_priced && plan.price_monthly_usd > 0 && (
                <span className="plan__period">/month</span>
              )}
            </div>

            <ul className="plan__lines">
              {lines(plan).map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>

            <button
              type="button"
              className={plan.name === 'PRO' ? 'primary-button' : 'ghost-button'}
              onClick={() => onChoose(plan)}
            >
              {plan.custom_priced
                ? 'Contact us'
                : plan.price_monthly_usd === 0
                  ? 'Start free'
                  : `Choose ${plan.label}`}
            </button>
          </article>
        ))}
      </div>

      <p className="pricing__note">
        Asking for a detailed answer when your allowance is spent gives you the
        medium tier, not an error. Nothing stops mid-question.
      </p>
    </section>
  );
}
