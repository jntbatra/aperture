/**
 * The account panel: plan, allowance, and the way out.
 *
 * Why usage is a bar and a fraction
 * ---------------------------------
 * "80% used" cannot be acted on and "16 questions" cannot be interpreted. Both
 * together answer the only question anyone has when they open this: do I need
 * to do something before Friday.
 *
 * Why the detailed allowance is shown separately
 * ----------------------------------------------
 * Because it *is* separate. A single combined bar would imply the two draw on
 * one pool, and a Pro customer would be surprised twice — once when detailed
 * ran out with 300 standard questions left, and again when they could not work
 * out why.
 */

import { useEffect, useState } from 'react';

import { getUsage, signOut, type Account as AccountInfo, type UsageInfo } from '../api';

type Props = {
  account: AccountInfo;
  onSignedOut: () => void;
  onUpgrade: () => void;
  onClose: () => void;
};

function Meter({ label, used, limit }: { label: string; used: number; limit: number }) {
  // -1 is the unlimited sentinel. A bar for something with no limit is a bar
  // that is always empty and means nothing, so it is not drawn.
  const unlimited = limit < 0;
  const fraction = unlimited ? 0 : Math.min(1, limit === 0 ? 1 : used / limit);
  const spent = !unlimited && used >= limit;

  return (
    <div className="meter">
      <div className="meter__head">
        <span>{label}</span>
        <strong>{unlimited ? `${used} · unlimited` : `${used} / ${limit}`}</strong>
      </div>
      {!unlimited && (
        <div className="meter__track">
          <div
            className={`meter__fill ${spent ? 'meter__fill--spent' : ''}`}
            style={{ width: `${fraction * 100}%` }}
          />
        </div>
      )}
    </div>
  );
}

export function Account({ account, onSignedOut, onUpgrade, onClose }: Props) {
  const [usage, setUsage] = useState<UsageInfo | null>(null);

  useEffect(() => {
    getUsage()
      .then(setUsage)
      .catch(() => setUsage(null));
  }, []);

  return (
    <>
      <div className="toggles__scrim" onClick={onClose} />
      <div className="toggles__panel account" role="dialog" aria-label="Account">
        <div className="toggles__head">
          <strong>{account.workspace}</strong>
          <span>
            {account.plan_label} plan
            {usage ? ` · ${usage.period}` : ''}
          </span>
        </div>

        {usage ? (
          <>
            <Meter
              label="Questions"
              used={usage.questions_used}
              limit={usage.questions_limit}
            />
            <Meter
              label="Detailed answers"
              used={usage.detailed_used}
              limit={usage.detailed_limit}
            />
            <Meter
              label="Connected databases"
              used={usage.connected_databases}
              limit={usage.connected_limit}
            />
          </>
        ) : (
          <p className="drift__note">Usage is unavailable.</p>
        )}

        <p className="drift__caveat">
          Asking for a detailed answer with none left gives you the medium tier
          rather than an error.
        </p>

        <div className="toggles__foot account__foot">
          {account.plan !== 'ENTERPRISE' && (
            <button className="primary-button" onClick={onUpgrade}>
              {account.plan === 'FREE' ? 'Upgrade' : 'Change plan'}
            </button>
          )}
          <button
            className="ghost-button"
            onClick={async () => {
              // Server-side first. Clearing only the cookie leaves the token
              // valid for anyone who captured it.
              await signOut().catch(() => undefined);
              onSignedOut();
            }}
          >
            Sign out
          </button>
        </div>
      </div>
    </>
  );
}
