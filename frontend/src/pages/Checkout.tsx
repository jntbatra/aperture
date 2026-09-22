/**
 * Choosing a plan. Payment is not wired yet, and says so.
 *
 * Why this page exists before payment does
 * ----------------------------------------
 * The plan a customer wants is worth capturing whether or not money can move
 * today: it is the thing that tells you which tier to build for, and a page
 * that is already correct about what each tier costs and includes is the part
 * that takes judgement. Wiring Razorpay to a page that already knows what it
 * is selling is a contained job; deciding what you are selling is not.
 *
 * Why the unfinished state is stated on the page
 * ----------------------------------------------
 * A checkout button that appears to work and silently does nothing is
 * discovered by a customer. One that says "payment is not connected yet" is
 * discovered by whoever is reading the screen. The second is cheaper by a
 * wide margin, and the first is how a product acquires a reputation before it
 * has acquired users.
 *
 * What Razorpay will need, when it is wired
 * -----------------------------------------
 *  1. An order created **server-side**. The amount must never come from the
 *     browser — a client that can name its own price is a client that will.
 *  2. The checkout script opened with that order id.
 *  3. The signature verified **server-side** with the key secret, which never
 *     reaches this file. The callback from the browser is a claim, not proof.
 *  4. The webhook treated as the real event, and made idempotent: it arrives
 *     more than once, and it arrives when the browser has already closed.
 *
 * The entitlement is granted by the webhook, never by this page. A page that
 * upgrades a plan on a successful-looking callback can be upgraded by anyone
 * who can send that callback.
 */

import { useState } from 'react';

import type { PlanInfo } from '../api';

type Props = {
  plan: PlanInfo;
  onBack: () => void;
};

type Currency = 'usd' | 'inr';

export function Checkout({ plan, onBack }: Props) {
  // INR first: the price is a round number in that currency and most early
  // customers are here. The toggle is a display choice, not a conversion.
  const [currency, setCurrency] = useState<Currency>('inr');
  const [noted, setNoted] = useState(false);

  const amount =
    currency === 'inr'
      ? `₹${plan.price_monthly_inr.toLocaleString('en-IN')}`
      : `$${plan.price_monthly_usd}`;

  return (
    <div className="checkout">
      <button className="link-button" onClick={onBack}>
        ← Back to pricing
      </button>

      <div className="checkout__card">
        <h1>{plan.label}</h1>

        {plan.custom_priced ? (
          <p className="checkout__amount">Priced per agreement</p>
        ) : (
          <>
            <p className="checkout__amount">
              {amount} <span>/month</span>
            </p>
            <div className="pricing__currency" role="group" aria-label="Currency">
              {(['inr', 'usd'] as Currency[]).map((option) => (
                <button
                  key={option}
                  type="button"
                  className={currency === option ? 'is-on' : ''}
                  aria-pressed={currency === option}
                  onClick={() => setCurrency(option)}
                >
                  {option.toUpperCase()}
                </button>
              ))}
            </div>
          </>
        )}

        <ul className="checkout__lines">
          <li>
            {plan.questions_per_month < 0
              ? 'Unlimited questions'
              : `${plan.questions_per_month.toLocaleString()} questions a month`}
          </li>
          <li>
            {plan.detailed_per_month < 0
              ? 'Unlimited detailed answers'
              : plan.detailed_per_month === 0
                ? 'Fast answers'
                : `${plan.detailed_per_month} detailed answers included`}
          </li>
          <li>
            {plan.max_connected_databases < 0
              ? 'Unlimited connected databases'
              : `${plan.max_connected_databases} connected database${
                  plan.max_connected_databases === 1 ? '' : 's'
                }`}
          </li>
        </ul>

        {/* Stated plainly on the surface a customer is looking at, not buried
            in a comment. An unfinished checkout that looks finished is found
            by the wrong person. */}
        <div className="checkout__pending">
          <strong>Payment is not connected yet.</strong>
          <p>
            Razorpay is a later task. Tell us which plan you want and we will set
            it up manually — you will not be charged from this page.
          </p>
        </div>

        <button
          className="primary-button"
          disabled={noted}
          onClick={() => setNoted(true)}
        >
          {noted ? 'Noted — we will be in touch' : `I want ${plan.label}`}
        </button>
      </div>
    </div>
  );
}
