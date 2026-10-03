// Runs against the compiled dist/ with a fake fetch: no server, no model calls.
const test = require("node:test");
const assert = require("node:assert/strict");
const { SqlAgentClient, SqlAgentError, TIER_OPTIONS } = require("../dist/index.js");

const RAW = {
  question: "How many orders?", answer: "There are 3 orders.", conversation_id: "c1",
  sql: "SELECT count(*) FROM orders", columns: ["count"], rows: [[3]], row_count: 1,
  truncated: false, ok: true, error: null, repairs: 0, model_calls: 2,
  input_tokens: 2900, output_tokens: 110, seconds: 3.1, tables_considered: ["orders"],
  clarification_asks: [], warnings: [],
};

function fakeFetch(status = 200, payload = RAW) {
  const calls = [];
  const f = async (url, init) => {
    calls.push({ url, init, body: init.body ? JSON.parse(init.body) : undefined });
    return new Response(JSON.stringify(payload), { status });
  };
  return { f, calls };
}

test("cheap is the default and does not turn on the intent check", async () => {
  const { f, calls } = fakeFetch();
  const client = new SqlAgentClient({ baseUrl: "http://agent:8000/", fetch: f });
  await client.ask("How many orders?");
  assert.equal(calls[0].url, "http://agent:8000/api/ask");
  assert.equal(calls[0].body.options.check_result_intent, false);
  assert.equal(calls[0].body.options.quality_tier, "fast");
});

test("expensive turns on the intent check and nothing else", async () => {
  const { f, calls } = fakeFetch();
  const client = new SqlAgentClient({ baseUrl: "http://agent:8000", fetch: f });
  const a = await client.ask("How many orders?", { tier: "expensive" });
  assert.deepEqual(
    { ...calls[0].body.options, ambiguity_handling: undefined },
    { ...TIER_OPTIONS.expensive, ambiguity_handling: undefined },
  );
  assert.equal(a.usage.tier, "expensive");
});

test("the response is mapped to camelCase with usage", async () => {
  const { f } = fakeFetch();
  const a = await new SqlAgentClient({ baseUrl: "http://x", fetch: f }).ask("q");
  assert.equal(a.conversationId, "c1");
  assert.equal(a.rowCount, 1);
  assert.deepEqual(a.rows, [[3]]);
  assert.equal(a.usage.modelCalls, 2);
  assert.equal(a.usage.inputTokens, 2900);
});

test("conversation id, ambiguity and api key are sent", async () => {
  const { f, calls } = fakeFetch();
  const client = new SqlAgentClient({ baseUrl: "http://x", apiKey: "ak_live_1", fetch: f });
  await client.ask("and for April?", { conversationId: "c1", ambiguity: "guess" });
  assert.equal(calls[0].body.conversation_id, "c1");
  assert.equal(calls[0].body.options.ambiguity_handling, "best_effort");
  assert.equal(calls[0].init.headers.Authorization, "Bearer ak_live_1");
});

test("an HTTP error rejects with the status and the server's detail", async () => {
  const { f } = fakeFetch(422, { detail: [{ msg: "field required" }] });
  const client = new SqlAgentClient({ baseUrl: "http://x", fetch: f });
  await assert.rejects(client.ask("q"), (err) => {
    assert.ok(err instanceof SqlAgentError);
    assert.equal(err.status, 422);
    assert.match(err.message, /field required/);
    return true;
  });
});

test("a timeout rejects with status 0", async () => {
  const slow = (url, init) => new Promise((_, reject) => {
    init.signal.addEventListener("abort", () => reject(new Error("aborted")));
  });
  const client = new SqlAgentClient({ baseUrl: "http://x", fetch: slow, timeoutMs: 50 });
  await assert.rejects(client.ask("q"), (err) => err.status === 0 && /timed out/.test(err.message));
});

test("an unknown tier is refused before any request", async () => {
  const { f, calls } = fakeFetch();
  const client = new SqlAgentClient({ baseUrl: "http://x", fetch: f });
  await assert.rejects(client.ask("q", { tier: "premium" }));
  assert.equal(calls.length, 0);
});
