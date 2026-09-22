/**
 * The quality toggles, on the page rather than in a config file.
 *
 * Why these are user-facing at all
 * --------------------------------
 * Every one of them trades latency and tokens for a better chance of being
 * right, and which side of that trade you want depends on the *question*, not
 * on the deployment. "Check this one carefully, it is going in a board pack" is
 * a decision made while asking. Burying it in an environment variable means it
 * is made once, by whoever deployed the server, for every question anyone ever
 * asks.
 *
 * Why each states its cost
 * ------------------------
 * A toggle offered without one invites the obvious move: switch everything on,
 * watch the tool take twenty seconds, conclude it is slow. The cost line is
 * what makes "thorough" an informed choice rather than a free upgrade nobody
 * would decline.
 *
 * Why the list comes from the server
 * ----------------------------------
 * The defaults are a server decision a deployment may change, and the
 * explanations belong next to the code implementing them. Hardcoding the list
 * here would mean a toggle added on the server is invisible until someone
 * remembers to update the client, and an explanation that drifts out of date
 * with no test to catch it.
 *
 * What is deliberately not here
 * -----------------------------
 * The row cap, the timeouts, the models, the database. Those are controls, not
 * preferences — a client able to raise its own row limit is not configuring a
 * feature, it is removing a safeguard. The server enforces that with an
 * allow-list; this component simply has nothing to show for them.
 */

import { useEffect, useState } from 'react';
import { getOptions, type AskOptions, type ToggleInfo } from '../api';

/** Where the user's choices persist between sessions.
 *
 *  Chosen once and kept: someone who wants every question reviewed should not
 *  have to say so every morning. Stored per browser rather than per server,
 *  because this is a preference, not a deployment setting. */
const STORE_KEY = 'aperture.options';

export function loadOptions(): AskOptions {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    return raw ? (JSON.parse(raw) as AskOptions) : {};
  } catch {
    // A corrupt entry should cost the user their preferences, not the page.
    return {};
  }
}

function saveOptions(options: AskOptions) {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(options));
  } catch {
    /* private browsing, quota — the options still apply for this session */
  }
}

interface Props {
  options: AskOptions;
  onChange: (options: AskOptions) => void;
  /** Disabled while a question is in flight: changing the rules mid-answer
   *  would show settings that do not match what is running. */
  disabled?: boolean;
}

export function Toggles({ options, onChange, disabled }: Props) {
  const [open, setOpen] = useState(false);
  const [toggles, setToggles] = useState<ToggleInfo[]>([]);
  const [defaults, setDefaults] = useState<Record<string, unknown>>({});

  useEffect(() => {
    getOptions()
      .then((payload) => {
        setToggles(payload.toggles);
        setDefaults(payload.defaults);
      })
      .catch(() => setToggles([]));
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => event.key === 'Escape' && setOpen(false);
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  /** Set one toggle, or clear it back to the server default.
   *
   *  Clearing matters: "unset" and "off" are different states. A user who
   *  switches something off has overridden the server; a user who resets has
   *  gone back to following it, and would otherwise be pinned to whatever the
   *  default happened to be the day they first opened the panel. */
  const set = (name: keyof AskOptions, value: unknown) => {
    const next: AskOptions = { ...options };
    if (value === undefined || value === defaults[name]) {
      delete next[name];
    } else {
      // The shape is validated by the server; the union here is wider than any
      // single field, which TypeScript cannot narrow from a dynamic key.
      (next as Record<string, unknown>)[name] = value;
    }
    saveOptions(next);
    onChange(next);
  };

  const changed = Object.keys(options).length;

  if (toggles.length === 0) return null;

  return (
    <div className="toggles">
      <button
        className={`ghost-button ${changed ? 'ghost-button--on' : ''}`}
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        {/* The count is the whole point of showing anything when closed: a
            user who set something yesterday needs to know it is still set,
            because it is silently changing what every question costs. */}
        Tuning{changed ? ` · ${changed}` : ''}
      </button>

      {open && (
        <>
          <div className="toggles__scrim" onClick={() => setOpen(false)} />
          <div className="toggles__panel" role="dialog" aria-label="Answer tuning">
            <div className="toggles__head">
              <strong>How hard to try</strong>
              <span>Each costs time. None of them change what the data says.</span>
            </div>

            {toggles.map((toggle) => (
              <div className="toggle" key={toggle.name}>
                <div className="toggle__text">
                  <div className="toggle__label">{toggle.label}</div>
                  <div className="toggle__help">{toggle.help}</div>
                  <div className="toggle__cost">{toggle.cost}</div>
                </div>

                <div className="toggle__control">
                  {toggle.kind === 'switch' ? (
                    <Switch
                      on={Boolean(options[toggle.name] ?? defaults[toggle.name])}
                      disabled={disabled}
                      onToggle={(value) => set(toggle.name, value)}
                    />
                  ) : (
                    <Choice
                      choices={toggle.choices}
                      value={String(options[toggle.name] ?? defaults[toggle.name] ?? '')}
                      disabled={disabled}
                      onPick={(value) =>
                        // Numeric-ness comes from the server. Deciding it here
                        // from a list of names meant every new numeric toggle
                        // sent a string and got a 422 nobody could act on.
                        set(toggle.name, toggle.numeric ? Number(value) : value)
                      }
                    />
                  )}
                </div>
              </div>
            ))}

            <div className="toggles__foot">
              <button
                className="ghost-button"
                disabled={!changed}
                onClick={() => {
                  saveOptions({});
                  onChange({});
                }}
              >
                Reset to defaults
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function Switch({
  on,
  disabled,
  onToggle,
}: {
  on: boolean;
  disabled?: boolean;
  onToggle: (value: boolean) => void;
}) {
  return (
    <button
      className={`switch ${on ? 'switch--on' : ''}`}
      role="switch"
      aria-checked={on}
      disabled={disabled}
      onClick={() => onToggle(!on)}
    >
      <span className="switch__knob" />
    </button>
  );
}

function Choice({
  choices,
  value,
  disabled,
  onPick,
}: {
  choices: string[];
  value: string;
  disabled?: boolean;
  onPick: (value: string) => void;
}) {
  return (
    <div className="seg">
      {choices.map((choice) => (
        <button
          key={choice}
          className={`seg__item ${choice === value ? 'seg__item--on' : ''}`}
          disabled={disabled}
          onClick={() => onPick(choice)}
        >
          {LABELS[choice] ?? choice}
        </button>
      ))}
    </div>
  );
}

/** Readable names for values whose server-side spelling is not user-facing. */
const LABELS: Record<string, string> = {
  best_effort: 'answer',
  ask_human: 'ask me',
  fast: 'fast',
  thorough: 'thorough',
};
