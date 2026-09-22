/**
 * The marketing page.
 *
 * What it claims, and why it is short
 * -----------------------------------
 * Every number on this page is one that was measured and is written down in
 * `docs/07-decisions.md`: 58.7% on BIRD mini-dev, 0.7% of queries needing a
 * repair, three independent layers stopping writes. Nothing here says "AI
 * powered" or "enterprise grade", because a claim nobody can check is worth
 * less than a number someone can argue with.
 *
 * The honest headline
 * -------------------
 * The pitch is not "it is always right". It is "you can see the query". That
 * is the actual differentiator against pasting a schema into a chat window,
 * and overselling accuracy to an analyst who will immediately test it is the
 * fastest way to lose them.
 */

type Props = {
  onStart: () => void;
  onPricing: () => void;
  onSignIn: () => void;
};

const PROOF = [
  {
    figure: '58.7%',
    label: 'on BIRD mini-dev',
    note: '150 human-written questions over 11 real databases, scored against gold queries.',
  },
  {
    figure: '3 layers',
    label: 'stop writes',
    note: 'A read-only role, a parser that rejects anything but SELECT, and a read-only transaction.',
  },
  {
    figure: '0.7%',
    label: 'needed a repair',
    note: 'Most queries run first time. The ones that fail are rewritten with the error attached.',
  },
];

const STEPS = [
  {
    title: 'Connect a read-only role',
    body:
      'We test it before saving — four write attempts, inside a transaction we roll back. ' +
      'A role that can write is refused, with the reason.',
  },
  {
    title: 'Ask in plain language',
    body:
      'It reads your schema, walks the foreign keys, and writes SQL. Follow-ups work: ' +
      '"and for April?" means what you think it means.',
  },
  {
    title: 'Read the query',
    body:
      'Every answer shows the SQL that produced it. If a figure looks wrong you can see ' +
      'why in ten seconds instead of trusting a sentence.',
  },
];

export function Landing({ onStart, onPricing, onSignIn }: Props) {
  return (
    <div className="landing">
      <nav className="landing__nav">
        <span className="landing__brand">
          <span className="header__mark" />
          Aperture
        </span>
        <div className="landing__nav-actions">
          <button className="ghost-button" onClick={onPricing}>
            Pricing
          </button>
          <button className="ghost-button" onClick={onSignIn}>
            Sign in
          </button>
          <button className="primary-button" onClick={onStart}>
            Start free
          </button>
        </div>
      </nav>

      <header className="landing__hero">
        <h1>
          Ask your database <em>anything</em>
        </h1>
        <p>
          A conversational SQL analyst that shows you the query it wrote. It cannot
          write to your data — not as a policy, as a database role you grant and we
          verify.
        </p>
        <div className="landing__cta">
          <button className="primary-button" onClick={onStart}>
            Start free — 20 questions
          </button>
          <button className="ghost-button" onClick={onPricing}>
            See pricing
          </button>
        </div>
        <p className="landing__fineprint">No card. Connect a read-only role in a minute.</p>
      </header>

      {/* Measured numbers, each traceable to the decision record. A landing
          page of adjectives is one an analyst discounts entirely. */}
      <section className="landing__proof">
        {PROOF.map((item) => (
          <div key={item.label} className="proof">
            <strong>{item.figure}</strong>
            <span>{item.label}</span>
            <p>{item.note}</p>
          </div>
        ))}
      </section>

      <section className="landing__steps">
        <h2>How it works</h2>
        <ol>
          {STEPS.map((step) => (
            <li key={step.title}>
              <h3>{step.title}</h3>
              <p>{step.body}</p>
            </li>
          ))}
        </ol>
      </section>

      <section className="landing__honest">
        <h2>What it does not do</h2>
        <p>
          It does not always get the answer right, and it says so rather than
          hiding it. When a question has more than one defensible reading it asks
          instead of guessing. When a figure may be inflated by a join, it warns
          you. When an answer quotes a number the rows do not support, it is caught
          and rewritten.
        </p>
        <p>
          There is no endpoint that accepts raw SQL, on any of the five ways in —
          web, API, SDK, CLI or MCP. That is the boundary the whole thing is built
          on, and an escape hatch would remove it.
        </p>
      </section>

      <footer className="landing__foot">
        <span>Aperture</span>
        <div>
          <button className="link-button" onClick={onPricing}>
            Pricing
          </button>
          <button className="link-button" onClick={onSignIn}>
            Sign in
          </button>
        </div>
      </footer>
    </div>
  );
}
