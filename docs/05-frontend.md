# 5. The web interface

A React + TypeScript app served by Vite. Roughly 700 lines total, no UI
framework, no state library.

---

## What it looks like and why

The reference points are editorial product sites — Anthropic's, OpenAI's,
Perplexity's — rather than analytics dashboards. A dashboard aesthetic
(dense chrome, cards inside cards, saturated blues) signals "configure me". This
tool is a place to ask a question and read a sentence, so it is built like
something you read.

Concretely, that means:

**Paper, not white.** The background is `#fbfaf7`. The warmth is slight and
deliberate — pure `#ffffff` under a page of text reads as clinical, and pure
black text on it is harsher than it needs to be. Ink is `#1a1917`, a warm
near-black.

**A serif for display type.** Instrument Serif for the headline and question
headings, Inter for interface text, JetBrains Mono for SQL. Three faces with
clearly separated jobs.

**One accent.** A clay red (`#b8543a`), used for the primary action, the active
state and the focus ring — and nowhere else. A single accent used sparingly
reads as considered; five accents read as unfinished.

**Borders over shadows.** Structure comes from hairline borders in a warm grey.
Shadows exist but are barely visible, used only to lift the schema drawer above
the page.

**Space.** The content column is capped at 820px and the hero has 72px of
padding above it. Whitespace is the cheapest way to make an interface feel
calm.

### The design tokens

Everything is a CSS custom property in [`index.css`](../frontend/src/index.css),
so the entire surface can be retuned from one block:

```css
:root {
  --paper: #fbfaf7;
  --ink: #1a1917;
  --ink-soft: #55514a;
  --line: #e6e1d6;
  --clay: #b8543a;

  --font-display: 'Instrument Serif', Georgia, serif;
  --font-ui: 'Inter', -apple-system, sans-serif;
  --font-mono: 'JetBrains Mono', Menlo, monospace;
}
```

---

## Structure

```
frontend/src/
├── main.tsx                     entry point
├── index.css                    design tokens, resets, base type
├── App.css                      layout and component styles
├── App.tsx                      shell, state, orchestration
├── api.ts                       typed client: fetch + EventSource
└── components/
    ├── Progress.tsx             live stage indicator
    ├── ResultPanels.tsx         SQL disclosure + results table
    └── SchemaDrawer.tsx         slide-over schema browser
```

### State

Four `useState` values in `App.tsx`: the question, the progress events, the
result, and the schema. Plus `busy` and `error`.

No Redux, no Zustand, no context. Nothing here is shared across distant parts
of the tree, and there are no cross-cutting updates. A state library would add
indirection without removing any.

---

## Streaming

The interesting part. A question takes a couple of seconds and involves two or
three model calls, so the UI reports each stage as it happens.

```ts
export function askStreaming(question, handlers): () => void {
  const source = new EventSource(`${BASE}/api/ask/stream?question=${encodeURIComponent(question)}`);

  for (const stage of stages) {
    source.addEventListener(stage, (event) => {
      handlers.onProgress({ stage, detail: JSON.parse(event.data) });
    });
  }

  source.addEventListener('result', (event) => {
    handlers.onResult(JSON.parse(event.data));
    source.close();
  });

  return () => source.close();     // the cancel function
}
```

Three details that matter:

**It returns a cancel function.** Asking a second question while the first is
in flight must abandon the first, or a late answer overwrites the new one. `App`
holds it in a `useRef` and calls it before starting anything new, and again on
unmount.

**Error events are ambiguous.** `EventSource` fires `error` both for a real
server error *and* for a normal connection close after the result arrived. The
client tracks whether it has settled, and only surfaces an error if no result
was received — otherwise every successful request would end with a spurious
error banner.

**Only started stages are shown.** `Progress` renders the stages that have
actually occurred, not a pre-drawn checklist. Listing all seven up front would
promise steps that may never run: an empty result skips the answering call, and
a repair can end a request early.

```tsx
const visible = SEQUENCE.filter((stage) => seen.has(stage));
```

---

## Components

### `Progress`

A checklist that fills in. Completed stages get a tick, the current one gets a
spinner. When a repair happens it is called out explicitly — *"That failed —
trying again"* — with the attempt number, because silently retrying looks like
a hang.

Below the list, the current detail is shown in monospace: the tables chosen,
the SQL being validated, the error being repaired. This is the difference
between "it is thinking" and "it is doing this specific thing".

### `Transcript`

The conversation, drawn as a list of turns.

The earlier UI replaced the answer on every question. That is the right shape
for a search box and the wrong one for an analyst: the second question is
almost always about the first, and you cannot read a comparison whose other
half has been erased. Keeping the turns on screen is also what makes the
follow-up *legible* — the user can see exactly what context the model is
answering against.

The question is right-aligned in a filled bubble, the answer left-aligned and
full width. Alignment alone distinguishes asker from answer; there are no
avatars or role labels, because with exactly two participants they carry no
information.

**Restored turns are not identical to live ones.** Reloading replays the thread
from the server, which stores the question, the answer and the SQL — but
deliberately not the result rows, because a query like "list every customer"
would turn the history into a shadow copy of the database. A restored turn
therefore shows the answer and the SQL, and says plainly that the table is not
kept rather than rendering an empty one, which would read as "the query
returned nothing".

The composer sticks to the bottom of the viewport once a thread exists, over an
opaque backdrop — translucent chrome above a data table is unreadable. After an
answer it offers generic follow-up chips ("Break that down by month", "Show me
the top 10"). Those exist to teach the mechanism: a user who has only used a
search box does not know a fragment will work, and one click demonstrates it
better than any hint text.

### `ResultPanels`

Two collapsible panels: the SQL, and the result rows.

Both are collapsed by default and sit *below* the answer, because the answer is
what was asked for. The query and the rows are evidence, available on demand.

The SQL panel highlights keywords with a small regex. A real tokeniser would be
more correct, but this is a read-only display of a query we generated
ourselves, and React escapes the text before it reaches the DOM, so the
highlighting cannot inject markup. Pulling in a syntax-highlighting dependency
for this would be poor value.

The results table:

- Sticky header, so column names survive scrolling.
- Numbers right-aligned with `font-variant-numeric: tabular-nums`, so digits
  line up.
- `null` rendered explicitly in italic grey — a blank cell is indistinguishable
  from an empty string, and the difference matters.
- A truncation notice when the server reports the result was capped.

### `SchemaDrawer`

A slide-over listing every table, expandable to show columns, types, primary
keys and outgoing references.

Its real job is orientation. Someone who does not know the schema cannot phrase
a good question, and "I don't know what to ask" is the most common reason a
tool like this goes unused.

Escape closes it. That is the kind of small thing whose absence makes an
interface feel unfinished.

---

### `Toggles`

The quality settings, on the page rather than in an environment variable. Every
one trades latency and tokens for a better chance of being right, and which
side of that trade you want depends on the *question* — "check this one
carefully, it is going in a board pack" is a decision made while asking.

**The list comes from the server.** `GET /api/options` returns each toggle's
label, help text, cost and choices. Hardcoding it here would mean a toggle
added server-side is invisible until someone remembers to update the client,
with an explanation that drifts out of date and no test to catch it.

**Numeric-ness comes from the server too.** The component used to decide which
choices were numbers from a hardcoded list of names, so a new numeric toggle
sent `"4"` where the schema wanted `4` and the user got a 422 they could not
act on. `ToggleInfo.numeric` is now declared alongside the choices, and a test
asserts it matches the request schema's own type.

**One decision, one control.** `Care` (fast / medium / detailed) is the tier,
and it decides three things: whether the query is written three times, whether
a second model reviews it, and whether a multi-part question is split. Those
three used to have their own switches next to it.

That was not merely redundant, it was **wrong on screen**. With Care on
*detailed*, the panel rendered "Review the query" as off, "Write it N times" as
1 and "Split multi-part questions" as off — while the engine ran all three,
because `critic_enabled()`, `vote_samples()` and `decompose_enabled()` all
resolve the tier. A control displaying the opposite of what is happening is
worse than no control.

They are gone from the page and remain in `OVERRIDABLE`, because isolating one
of them is exactly what a benchmark needs — it is how the critic was measured
at net zero. `test_no_toggle_contradicts_the_tier` now walks every tier and
fails if any shown control disagrees with what the engine would actually do.

**Unset and off are different.** Clearing a toggle removes the key rather than
sending `false`, so a user who resets goes back to following the server rather
than being pinned to whatever the default happened to be the day they first
opened the panel.

Each row is two rows, not two columns: label and control on the first, the
explanation full-width beneath. Beside the control the explanation had roughly
half the panel and wrapped a three-sentence paragraph over nine lines — and the
explanation is the part that makes a setting's cost understandable, which is
the whole reason it is there.

### `Drift`

Opened from the header, never shown unprompted.

The wording is load-bearing. Nothing in the history says whether an answer was
*right* — there is no label — so the panel claims only that one set of
questions was answered measurably differently from another, with a p-value, and
says outright that a shift is a prompt to go and look rather than a verdict.

Three states that a simpler panel would collapse into one:

- **Not enough history.** Distinct from "nothing moved". A fresh install
  rendering a calm panel is a claim nobody made.
- **Nothing moved by enough to report.**
- **Shifts**, each with its p-value on screen. Without it the panel is a row of
  percentages that move between any two samples, which produces a false alarm
  on a slow Tuesday and trains everyone to ignore it.

Every metric is a rate of something undesirable, so a *fall* is good news and
still drift. `drift__shift--better` is green; colouring by movement rather than
by direction would make the panel unreadable at a glance.

Underneath the shifts, every metric that *held* is listed too. "Slower but no
more failures" is a different situation from "both moved", and only one of them
is a correctness concern.

The panel reuses the toggles' overlay and scrim. Same kind of thing — a
deliberately opened layer over the conversation — and a second visual language
would be two things to maintain and one more layout to get wrong on a narrow
screen.

### The sidebar's counters

`X% answered` is over the turns where an answer was *attempted*. A clarifying
question is stored with `ok = 0` because no answer was produced, and counting
it as a failure had the sidebar reporting **78% answered** on a system where
almost nothing had failed and a third of turns were the agent declining to
guess. Clarifications now appear as their own `asked back` counter.

---

## Accessibility

Not an afterthought, and cheap when done as you go:

- A single visible focus style (`:focus-visible`) with a clay outline. Removing
  outlines without replacing them is the most common accessibility failure in
  designs like this.
- The progress region is `role="status"` with `aria-live="polite"`, so a screen
  reader announces stage changes without interrupting.
- Disclosure buttons carry `aria-expanded`; the drawer is `role="dialog"` with
  a label.
- `prefers-reduced-motion` collapses every animation to near-zero duration.
- Enter sends, Shift+Enter makes a newline — the convention every chat
  interface uses, so it needs no explanation.

---

## Running it

```bash
make web-install      # once
make api              # terminal 1 — http://localhost:8000
make web              # terminal 2 — http://localhost:5173
```

The dev server proxies nothing; the client calls `http://localhost:8000`
directly, which is why the API enables CORS for `localhost:5173` specifically
rather than `*`. A wildcard would let any site a user visits call the API from
their browser.

For production:

```bash
make web-build        # → frontend/dist/
```

Static files, servable from anywhere. Point `VITE_API_URL` at the API host at
build time.

---

Next: [6. Running it](06-operations.md)
