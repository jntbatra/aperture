/**
 * Render the app in a real browser and capture what a user would see.
 *
 * Type-checking and unit tests prove the code compiles and the API behaves.
 * Neither proves the interface renders, that the stream arrives, or that an
 * answer appears — so this drives the real thing and screenshots the result.
 *
 * Run with:  node verify.mjs
 * Requires the API on :8000 and the dev server on :5173.
 */

import { chromium } from 'playwright';
import { mkdirSync } from 'node:fs';

const WEB = 'http://localhost:5173';
const OUT = 'shots';

const problems = [];

function check(name, condition, detail = '') {
  const mark = condition ? '  ok  ' : ' FAIL ';
  console.log(`[${mark}] ${name}${detail ? ` — ${detail}` : ''}`);
  if (!condition) problems.push(name);
}

mkdirSync(OUT, { recursive: true });

// The headless-shell build is absent on this machine but the full Chromium
// build is present, so ask for that channel explicitly.
const browser = await chromium.launch({ channel: 'chromium' });
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

// Surface anything the page logs as an error; a React crash shows up here
// long before it shows up in a screenshot.
const consoleErrors = [];
page.on('console', (m) => m.type() === 'error' && consoleErrors.push(m.text()));
page.on('pageerror', (e) => consoleErrors.push(String(e)));

await page.goto(WEB, { waitUntil: 'networkidle' });
await page.screenshot({ path: `${OUT}/1-landing.png` });

check('page title set', (await page.title()).includes('Aperture'));
check('hero renders', await page.locator('.hero__title').isVisible());
check('ask box renders', await page.locator('.ask__input').isVisible());
check('example chips render', (await page.locator('.chip').count()) >= 3);

// The starter chips are the one moment a new user decides whether this
// understands their database. Schema-derived ones spent it on row counts.
const starters = await page.locator('.examples .chip').allInnerTexts();
check(
  'starter questions are about the business, not the schema',
  starters.every((q) => !/how many rows|joined with|most common values/i.test(q)),
  starters[0],
);

// Health comes from the API, so a green dot proves the frontend reached it.
const headerText = await page.locator('.header__meta').innerText();
check('backend reachable from the browser', headerText.includes('tables'), headerText.replace(/\n/g, ' '));

// --- answer tuning, on the homepage ---
//
// These trade latency for a better chance of being right, and which side of
// that trade you want depends on the question — so they belong on the page, not
// in an environment variable.
await page.getByRole('button', { name: /^Tuning/ }).click();
await page.waitForSelector('.toggles__panel', { state: 'visible' });
const toggleCount = await page.locator('.toggle').count();
check('tuning panel lists the toggles', toggleCount >= 5, `${toggleCount} toggles`);
check('switches render', (await page.locator('.switch').count()) > 0);
check('choice controls render', (await page.locator('.seg').count()) > 0);

// Every toggle must state what it costs. One offered without a price gets
// switched on by everyone, who then conclude the tool is slow.
const costs = await page.locator('.toggle__cost').allInnerTexts();
check('every toggle states its cost', costs.length === toggleCount && costs.every((c) => c.trim()));
const helps = await page.locator('.toggle__help').allInnerTexts();
check('every toggle explains itself', helps.every((h) => h.trim().length > 40));
await page.screenshot({ path: `${OUT}/7-tuning.png` });

// Flipping one marks the button, so a setting made yesterday is not silently
// still changing what every question costs.
await page.locator('.switch').first().click();
await page.waitForTimeout(150);
const tuningLabel = await page.getByRole('button', { name: /^Tuning/ }).innerText();
check('changed settings are counted on the button', /Tuning · \d/.test(tuningLabel), tuningLabel);

// And it survives a reload: a preference, not a per-question choice.
await page.reload({ waitUntil: 'networkidle' });
await page.waitForTimeout(400);
check(
  'tuning survives a reload',
  /Tuning · \d/.test(await page.getByRole('button', { name: /^Tuning/ }).innerText()),
);

await page.getByRole('button', { name: /^Tuning/ }).click();
await page.waitForSelector('.toggles__panel', { state: 'visible' });
await page.getByRole('button', { name: 'Reset to defaults' }).click();
await page.waitForTimeout(150);
check(
  'reset clears every override',
  (await page.getByRole('button', { name: /^Tuning/ }).innerText()).trim() === 'Tuning',
);
await page.keyboard.press('Escape');
await page.waitForTimeout(200);
check('Escape closes the tuning panel', (await page.locator('.toggles__panel').count()) === 0);

// --- sidebar: graph ---
await page.getByRole('button', { name: 'Explore' }).click();
await page.waitForSelector('.drawer', { state: 'visible' });
// Chats is the default tab now, so the graph has to be selected explicitly.
await page.getByRole('button', { name: 'Graph' }).click();
await page.waitForSelector('.gnode', { timeout: 15_000 });
const nodeCount = await page.locator('.gnode').count();
check('schema graph renders table nodes', nodeCount > 0, `${nodeCount} nodes`);
// Wait for an edge rather than counting immediately: React Flow renders edges
// only after it has measured the nodes, so a bare count races the first paint.
await page.waitForSelector('.react-flow__edge', { timeout: 10_000 });
check('graph draws relationship edges', (await page.locator('.react-flow__edge').count()) > 0);
check('graph legend shows counts', (await page.locator('.graph__legend').innerText()).includes('tables'));
await page.waitForTimeout(600);
await page.screenshot({ path: `${OUT}/2-graph.png` });

// --- sidebar: tables ---
await page.getByRole('button', { name: 'Tables' }).click();
await page.waitForSelector('.tablecard');
const tableCount = await page.locator('.tablecard').count();
check('tables tab lists tables', tableCount > 0, `${tableCount} tables`);
await page.locator('.tablecard__head').first().click();
await page.waitForTimeout(250);
check('table expands to show columns', (await page.locator('.column').count()) > 0);

// --- sidebar: data (upload) ---
await page.getByRole('button', { name: 'Data' }).click();
await page.waitForSelector('.drop');
check('upload target is offered', (await page.locator('.drop').innerText()).includes('CSV'));
check('configured database is listed', (await page.locator('.dataset').count()) > 0);
await page.screenshot({ path: `${OUT}/3-data.png` });

// --- sidebar: chats (search lives here now; the flat History tab is gone) ---
await page.getByRole('button', { name: 'Chats' }).click();
await page.waitForTimeout(400);
check('chat search is offered', await page.locator('.search').isVisible());
check('History tab is gone', (await page.getByRole('button', { name: 'History' }).count()) === 0);

await page.keyboard.press('Escape');
await page.waitForTimeout(300);
check('Escape closes the sidebar', (await page.locator('.drawer').count()) === 0);

// --- ask a real question, end to end ---
//
// The question comes from the app's own suggestions, which are derived from
// whichever schema is connected. Hardcoding one tied this script to a single
// fixture database: pointed at anything else it failed on questions that were
// simply irrelevant, not wrong.
const suggested = await page.locator('.chip').first().innerText();
await page.locator('.ask__input').fill(suggested);
await page.locator('.ask__submit').click();

// Progress should appear while the model works.
await page.waitForSelector('.progress', { timeout: 10_000 });
check('progress stages stream in', await page.locator('.progress__row').first().isVisible());
await page.screenshot({ path: `${OUT}/4-thinking.png` });

// Then the answer.
await page.waitForSelector('.answer__body', { timeout: 90_000 });
const answer = await page.locator('.answer__body').innerText();
check('answer rendered', answer.length > 10, answer.slice(0, 70));
check('answer quotes a figure', /\d/.test(answer), 'contains a number');
check('answer is not an error', !(await page.locator('.answer__body--error').count()));
check('metrics shown', (await page.locator('.metric').count()) > 0);

// SQL panel
await page.getByRole('button', { name: /^SQL/ }).click();
await page.waitForSelector('.sql', { state: 'visible' });
const sql = await page.locator('.sql').innerText();
check('SQL is shown to the user', sql.toUpperCase().includes('SELECT'));
check('SQL keywords highlighted', (await page.locator('.sql__keyword').count()) > 0);

// Results table
await page.getByRole('button', { name: /^Results/ }).click();
await page.waitForSelector('table', { state: 'visible' });
const rows = await page.locator('tbody tr').count();
check('results table populated', rows > 0, `${rows} rows`);
await page.screenshot({ path: `${OUT}/5-answer.png`, fullPage: true });

// The chat id is shown in the header, not buried in a drawer: it is the one
// identifier that makes "this conversation went wrong" actionable.
check('chat id is visible in the header', await page.locator('.pill--id').isVisible());
const idBadge = await page.locator('.pill--id').innerText();
check('chat id looks like an id', /^chat [0-9a-f]{8,}$/.test(idBadge), idBadge);

// --- the question is findable by search, and names its conversation ---
await page.getByRole('button', { name: 'Explore' }).click();
await page.getByRole('button', { name: 'Chats' }).click();
await page.waitForSelector('.search', { timeout: 10_000 });
await page.locator('.search').fill(suggested.split(' ').slice(-1)[0].replace('?', ''));
await page.waitForSelector('.hit', { timeout: 10_000 });
const hitText = await page.locator('.hit').first().innerText();
check('search finds the question just asked', hitText.length > 0, hitText.split('\n')[0].slice(0, 50));
check('search result names its conversation', hitText.includes('in "'));
await page.screenshot({ path: `${OUT}/6-search.png` });
await page.locator('.search').fill('');
await page.keyboard.press('Escape');

// --- clarification: it asks rather than supposing ---
//
// A vague question has more than one defensible answer, and picking one
// silently produces a figure that is correct for a question nobody asked.
await page.getByRole('button', { name: 'New chat' }).click();
await page.waitForTimeout(300);
await page.locator('.ask__input').fill('Who are our best customers and how are they doing lately?');
await page.locator('.ask__submit').click();
await page.waitForSelector('.askform', { timeout: 90_000 });

const askRows = await page.locator('.askform__row').count();
check('a vague question is asked back, not guessed', askRows > 0, `${askRows} ambiguities`);
check('the clarification is not styled as an error', (await page.locator('.answer__body--asking').count()) > 0);
check('no SQL was run for it', (await page.getByRole('button', { name: /^SQL/ }).count()) === 0);

// Each ambiguity gets its own options. Merged into one list they are
// unanswerable: the options are not alternatives to each other.
let everyRowHasOptions = true;
for (let i = 0; i < askRows; i++) {
  const opts = await page.locator('.askform__row').nth(i).locator('.chip').count();
  if (opts < 2) everyRowHasOptions = false;
}
check('each ambiguity offers its own alternatives', everyRowHasOptions);
await page.screenshot({ path: `${OUT}/12-askform.png`, fullPage: true });

for (let i = 0; i < askRows; i++) {
  await page.locator('.askform__row').nth(i).locator('.chip').first().click();
  await page.waitForTimeout(120);
}
await page.waitForSelector('.turn:nth-child(2) .answer__body', { timeout: 90_000 });
const clarified = await page.locator('.answer__body').nth(1).innerText();
check('answering the clarification produces a real answer', /\d/.test(clarified), clarified.slice(0, 60));

await page.getByRole('button', { name: 'New chat' }).click();
await page.waitForTimeout(300);

// --- the conversational part ---
//
// The thing being proved here is that the previous turn stays on screen and
// that a *fragment* — a phrase that is not a question about a database on its
// own — gets answered. A follow-up chip is used rather than a typed question
// so the script does not have to know anything about the connected schema.
await page.waitForTimeout(400);
const followUp = await page.locator('.chip').first().innerText();
await page.locator('.chip').first().click();

await page.waitForSelector('.turn:nth-child(2) .answer__body', { timeout: 90_000 });
const turns = await page.locator('.turn').count();
check('follow-up starts a second turn', turns === 2, `${turns} turns on screen`);
check(
  'the first question is still on screen',
  (await page.locator('.bubble').first().innerText()).includes(suggested.slice(0, 20)),
);
check('the follow-up was asked verbatim', (await page.locator('.bubble').nth(1).innerText()) === followUp);

const followUpAnswer = await page.locator('.answer__body').nth(1).innerText();
check('the fragment was answered', followUpAnswer.length > 10, followUpAnswer.slice(0, 70));
check(
  'the fragment did not error',
  (await page.locator('.answer__body--error').count()) === 0,
  'no error body rendered',
);
await page.screenshot({ path: `${OUT}/7-conversation.png`, fullPage: true });

// --- the thread survives a reload ---
//
// A conversation that evaporates on refresh is not a conversation. This is
// also what proves the turns are stored server-side rather than held in
// browser memory.
await page.reload({ waitUntil: 'networkidle' });
await page.waitForSelector('.turn', { timeout: 15_000 });
const restored = await page.locator('.turn').count();
check('thread is restored after a reload', restored === 2, `${restored} turns restored`);
check(
  'restored turns explain that results are not stored',
  (await page.locator('.turn__note').count()) > 0,
);

// --- the thread is listed and can be reopened ---
await page.getByRole('button', { name: 'Explore' }).click();
await page.waitForSelector('.drawer', { state: 'visible' });
await page.getByRole('button', { name: 'Chats' }).click();
await page.waitForSelector('.dataset__name', { timeout: 10_000 });
const chatTitle = await page.locator('.dataset__name').first().innerText();
check('conversation is listed under Chats', chatTitle.includes(suggested.slice(0, 20)), chatTitle);
const chatId = await page.locator('.dataset__id').first().innerText();
check('conversation shows its id in the list', /^[0-9a-f]{8,}$/.test(chatId), chatId);
check(
  'conversation reports its turn count',
  (await page.locator('.dataset__meta').first().innerText()).includes('2 questions'),
);
await page.screenshot({ path: `${OUT}/8-chats.png` });
await page.keyboard.press('Escape');

// --- New chat clears the board ---
await page.getByRole('button', { name: 'New chat' }).click();
await page.waitForTimeout(300);
check('New chat empties the transcript', (await page.locator('.turn').count()) === 0);
check('New chat brings the hero back', await page.locator('.hero__title').isVisible());
check('chat id badge is cleared with the thread', (await page.locator('.pill--id').count()) === 0);

check('no console errors', consoleErrors.length === 0, consoleErrors.slice(0, 2).join(' | '));

await browser.close();

console.log(
  problems.length
    ? `\n${problems.length} check(s) failed: ${problems.join(', ')}`
    : '\nAll checks passed. Screenshots in frontend/shots/',
);
process.exit(problems.length ? 1 : 0);
