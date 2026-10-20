/**
 * The page people see before they sign in.
 *
 * Says what Aperture is, shows it working, and shows the five ways to use it.
 * No data is fetched here: the server is closed to anyone not signed in, and a
 * marketing page must not depend on it being reachable.
 */

import { useEffect, useState } from 'react';
import { ThemeToggle } from './ThemeToggle';
import './Landing.css';

const REPO = 'https://github.com/jntbatra/aperture';

const DEMO = {
  question: 'Which five customers spent the most last month?',
  sql: [
    ['SELECT', ' c.name, ', 'SUM', '(o.total) AS spent'],
    ['FROM', ' orders o'],
    ['JOIN', ' customers c ON c.id = o.customer_id'],
    ['WHERE', " o.created_at >= date('now','start of month','-1 month')"],
    ['GROUP BY', ' c.name ORDER BY', ' spent DESC LIMIT', ' 5'],
  ],
  rows: [
    ['Meera Kapoor', '₹48,210'],
    ['Arjun Rao', '₹41,930'],
    ['Sana Sheikh', '₹37,455'],
    ['Dev Malhotra', '₹33,008'],
    ['Isha Nair', '₹29,870'],
  ],
};

const FEATURES = [
  {
    title: 'Plain-English questions',
    body: 'Type what you want to know. Follow-ups remember the thread, and when a question could mean two things it asks instead of guessing.',
    icon: 'M4 5h16v10H9l-5 4V5z',
  },
  {
    title: 'The SQL, always shown',
    body: 'Every answer comes with the query behind it. Copy it, read it, or press Run to re-run it. No model call, no cost.',
    icon: 'M8 7l-5 5 5 5M16 7l5 5-5 5M14 4l-4 16',
  },
  {
    title: 'Bring any data',
    body: 'Drop in a CSV, an Excel file or a Postgres dump. Or point it at a database you already have. It reads the schema itself.',
    icon: 'M12 3c4.4 0 8 1.3 8 3s-3.6 3-8 3-8-1.3-8-3 3.6-3 8-3zm8 3v6c0 1.7-3.6 3-8 3s-8-1.3-8-3V6m16 6v6c0 1.7-3.6 3-8 3s-8-1.3-8-3v-6',
  },
  {
    title: 'Read-only by design',
    body: 'One SELECT at a time, only your tables, a row cap and a timeout. It cannot change or delete anything, whatever it is asked.',
    icon: 'M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3zm-3 9l2 2 4-4',
  },
  {
    title: 'Your chats, kept',
    body: 'Every conversation is saved to your account, searchable, and yours alone. Come back tomorrow and carry on.',
    icon: 'M12 8v4l3 2M21 12a9 9 0 11-3-6.7L21 8m0-4v4h-4',
  },
  {
    title: 'Invite only',
    body: 'Every new account waits for the owner to approve it by email. Nobody wanders in.',
    icon: 'M3 7l9 6 9-6M4 5h16a1 1 0 011 1v12a1 1 0 01-1 1H4a1 1 0 01-1-1V6a1 1 0 011-1z',
  },
];

const STEPS = [
  ['Ask', 'You write a question the way you would say it out loud.'],
  ['Understand', 'Aperture reads your tables and picks the ones that matter.'],
  ['Write', 'A model drafts one safe SELECT. A checker proves it is read-only.'],
  ['Answer', 'You get the result, a sentence, and the SQL to check it.'],
];

const WAYS = [
  {
    id: 'python',
    label: 'Python',
    file: 'app.py',
    code: `from aperture_sql import Aperture

client = Aperture("https://your-server", api_key="…")
answer = client.ask("How many orders last month?")

print(answer.answer)   # There were 1,204 orders.
print(answer.sql)      # SELECT count(*) FROM orders …`,
  },
  {
    id: 'ts',
    label: 'TypeScript',
    file: 'app.ts',
    code: `import { ApertureClient } from "aperture-sql";

const client = new ApertureClient({
  baseUrl: "https://your-server",
  apiKey: process.env.APERTURE_API_KEY,
});

const { answer, sql, rows } = await client.ask(
  "How many orders last month?",
);`,
  },
  {
    id: 'rest',
    label: 'REST',
    file: 'terminal',
    code: `curl -X POST https://your-server/api/ask \\
  -H "Authorization: Bearer $APERTURE_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"question": "How many customers are there?"}'`,
  },
  {
    id: 'mcp',
    label: 'MCP',
    file: 'claude_desktop_config.json',
    code: `{
  "mcpServers": {
    "aperture": {
      "command": "python",
      "args": ["-m", "sqlagent.mcp_server"]
    }
  }
}`,
  },
  {
    id: 'cli',
    label: 'CLI',
    file: 'terminal',
    code: `$ python -m sqlagent "top 5 customers by spend"

  Meera Kapoor    ₹48,210
  Arjun Rao       ₹41,930
  …`,
  },
];

/** Types the question out, then reveals the SQL and the table, then loops. */
function useDemoStage() {
  const [typed, setTyped] = useState(0);
  const [stage, setStage] = useState(0); // 0 typing, 1 sql, 2 rows
  useEffect(() => {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      setTyped(DEMO.question.length);
      setStage(2);
      return;
    }
    let cancelled = false;
    const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));
    (async () => {
      while (!cancelled) {
        setStage(0);
        for (let i = 0; i <= DEMO.question.length && !cancelled; i++) {
          setTyped(i);
          await wait(34);
        }
        await wait(500);
        setStage(1);
        await wait(1300);
        setStage(2);
        await wait(5200);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);
  return { typed, stage };
}

export function Landing({ onEnter }: { onEnter: () => void }) {
  const { typed, stage } = useDemoStage();
  const [way, setWay] = useState(WAYS[0].id);
  const active = WAYS.find((w) => w.id === way) ?? WAYS[0];

  return (
    <div className="lp">
      <div className="lp__glow" aria-hidden />

      <nav className="lp-nav">
        <a className="lp-brand" href="#top">
          <span className="header__mark" />
          Aperture
        </a>
        <div className="lp-nav__links">
          <a href="#features">What it does</a>
          <a href="#how">How it works</a>
          <a href="#use">Use it</a>
          <a href={REPO} target="_blank" rel="noreferrer">
            GitHub
          </a>
        </div>
        <div className="lp-nav__end">
          <ThemeToggle />
          <button className="lp-btn lp-btn--small" onClick={onEnter}>
            Sign in
          </button>
        </div>
      </nav>

      <header className="lp-hero" id="top">
        <p className="lp-eyebrow">Ask your database anything</p>
        <h1>
          Your data, <em>answered</em> in plain English.
        </h1>
        <p className="lp-lede">
          Aperture turns a question into a safe SQL query, runs it, and shows you the answer and
          the query behind it. No SQL to write. Nothing to trust blindly.
        </p>
        <div className="lp-cta">
          <button className="lp-btn" onClick={onEnter}>
            Request access
          </button>
          <a className="lp-btn lp-btn--ghost" href={REPO} target="_blank" rel="noreferrer">
            View on GitHub
          </a>
        </div>

        <div className="lp-demo" aria-label="An example question and its answer">
          <div className="lp-demo__bar">
            <i /> <i /> <i />
            <span>Aperture</span>
          </div>
          <div className="lp-demo__body">
            <div className="lp-q">
              {DEMO.question.slice(0, typed)}
              {stage === 0 && <span className="lp-caret" />}
            </div>
            <div className={`lp-sql ${stage >= 1 ? 'is-on' : ''}`}>
              <span className="lp-tag">SQL</span>
              {DEMO.sql.map((line, i) => (
                <div key={i}>
                  {line.map((part, j) =>
                    j % 2 === 0 ? (
                      <b key={j}>{part}</b>
                    ) : (
                      <span key={j}>{part}</span>
                    ),
                  )}
                </div>
              ))}
            </div>
            <div className={`lp-rows ${stage >= 2 ? 'is-on' : ''}`}>
              <p>
                Meera Kapoor led the month with <strong>₹48,210</strong>. Here are the top five.
              </p>
              <table>
                <tbody>
                  {DEMO.rows.map(([name, spent], i) => (
                    <tr key={name} style={{ animationDelay: `${i * 90}ms` }}>
                      <td>{name}</td>
                      <td>{spent}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </header>

      <section className="lp-section" id="features">
        <h2>
          Everything you need to <em>trust</em> an answer
        </h2>
        <div className="lp-grid">
          {FEATURES.map((f) => (
            <article className="lp-card" key={f.title}>
              <svg viewBox="0 0 24 24" aria-hidden>
                <path d={f.icon} />
              </svg>
              <h3>{f.title}</h3>
              <p>{f.body}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="lp-section" id="how">
        <h2>
          How it <em>works</em>
        </h2>
        <ol className="lp-steps">
          {STEPS.map(([title, body], i) => (
            <li key={title}>
              <span>{i + 1}</span>
              <h3>{title}</h3>
              <p>{body}</p>
            </li>
          ))}
        </ol>
        <p className="lp-note">
          Two model calls for a normal question. For numbers someone will act on, switch on the
          extra check: a second look at whether the rows really answer what you asked.
        </p>
      </section>

      <section className="lp-section" id="use">
        <h2>
          Put it in <em>your</em> project
        </h2>
        <p className="lp-sub">
          Aperture is self-hosted and open source. Run your own server, then use it from anywhere,
          with an API key.
        </p>
        <div className="lp-code">
          <div className="lp-code__tabs" role="tablist">
            {WAYS.map((w) => (
              <button
                key={w.id}
                role="tab"
                aria-selected={w.id === way}
                className={w.id === way ? 'on' : ''}
                onClick={() => setWay(w.id)}
              >
                {w.label}
              </button>
            ))}
            <span>{active.file}</span>
          </div>
          <pre>
            <code>{active.code}</code>
          </pre>
        </div>
        <ul className="lp-ways">
          <li>
            <strong>Web app</strong>
            Chat in the browser, upload files, re-run any query.
          </li>
          <li>
            <strong>REST API</strong>
            Plain HTTP and JSON, with streaming.
          </li>
          <li>
            <strong>MCP server</strong>
            Let Claude or any agent ask your data.
          </li>
          <li>
            <strong>SDKs</strong>
            Python and TypeScript, typed.
          </li>
          <li>
            <strong>CLI</strong>
            Ask from the terminal.
          </li>
        </ul>
      </section>

      <section className="lp-final">
        <h2>
          Ready to ask <em>something</em>?
        </h2>
        <p>Access is by approval. Request it and the owner will let you in.</p>
        <button className="lp-btn" onClick={onEnter}>
          Request access
        </button>
      </section>

      <footer className="lp-foot">
        <span>
          <span className="header__mark" /> Aperture · MIT licensed
        </span>
        <a href={REPO} target="_blank" rel="noreferrer">
          Source and docs
        </a>
      </footer>
    </div>
  );
}
