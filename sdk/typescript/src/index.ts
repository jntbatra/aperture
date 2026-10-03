/**
 * Typed client for the sql-agent HTTP service.
 *
 * Two tiers, both measured on all 500 BIRD mini-dev questions against corrected
 * gold (docs/10-the-ablation.md in the sql-agent repo):
 *
 *   cheap      72.4%   ~2.0 model calls   ~3,000 tokens   ~$0.00045 / question
 *   expensive  76.2%   ~3.1 model calls   ~8,100 tokens   ~$0.00117 / question
 *
 * `expensive` adds the result-intent check: after the query runs, a model looks
 * at the rows and asks whether they answer the question, and rewrites the query
 * once if not. Column documentation (PostgreSQL `COMMENT ON COLUMN`) is read by
 * the server in both tiers.
 *
 * Figures are from BIRD, not from your database. Larger schemas send larger
 * prompts.
 */

export type Tier = "cheap" | "expensive";

export interface AskOptions {
  /** Defaults to the client's `defaultTier`, which defaults to `cheap`. */
  tier?: Tier;
  /**
   * Continue a thread so the question can refer back ("and for April?").
   * Take it from the previous response's `conversationId`.
   */
  conversationId?: string;
  /**
   * `ask` (default) lets the agent return a clarifying question instead of
   * guessing when the question has two defensible readings. `guess` always
   * answers — right for batch jobs where nobody can reply.
   */
  ambiguity?: "ask" | "guess";
  /** Aborts the request. */
  signal?: AbortSignal;
}

export interface Clarification {
  [key: string]: unknown;
}

export interface Answer {
  question: string;
  /** One or two sentences describing the rows. */
  answer: string;
  /** Send this back as `conversationId` to continue the thread. */
  conversationId: string | null;
  /** The query that produced the rows. Show it to people who need to check. */
  sql: string | null;
  columns: string[];
  rows: unknown[][];
  rowCount: number;
  /** True when the server's row cap stopped the result short. */
  truncated: boolean;
  /** False when no query succeeded. `error` says why. */
  ok: boolean;
  error: string | null;
  /**
   * Set when the agent asked instead of answering. Show the options to the
   * user and send their choice as the next question in the same thread.
   */
  clarifications: Clarification[];
  /** Ran successfully but may still be wrong — surface these, do not drop them. */
  warnings: string[];
  usage: {
    tier: Tier;
    modelCalls: number;
    inputTokens: number;
    outputTokens: number;
    seconds: number;
    repairs: number;
  };
  tablesConsidered: string[];
}

export interface SqlAgentClientOptions {
  /** e.g. `http://127.0.0.1:8000`. No trailing `/api`. */
  baseUrl: string;
  /** Only when the service runs with `SQLAGENT_REQUIRE_AUTH=true`. */
  apiKey?: string;
  defaultTier?: Tier;
  /** Per-request timeout. The expensive tier can take 10s+ on a large schema. */
  timeoutMs?: number;
  /** Inject a fetch for runtimes without a global one (Node < 18). */
  fetch?: typeof fetch;
}

export class SqlAgentError extends Error {
  constructor(
    message: string,
    /** HTTP status, or 0 when the request never got a response. */
    readonly status: number,
    readonly body?: unknown,
  ) {
    super(message);
    this.name = "SqlAgentError";
  }
}

/**
 * What each tier sends. Kept as data, in one place, so the mapping cannot
 * drift between call sites. `quality_tier` stays `fast` in both: the server's
 * voting / critic tiers measured no better on BIRD, so neither tier uses them.
 */
export const TIER_OPTIONS: Record<Tier, Record<string, unknown>> = {
  cheap: { quality_tier: "fast", check_result_intent: false },
  expensive: { quality_tier: "fast", check_result_intent: true },
};

export class SqlAgentClient {
  private readonly baseUrl: string;
  private readonly apiKey?: string;
  private readonly defaultTier: Tier;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;

  constructor(options: SqlAgentClientOptions) {
    if (!options.baseUrl) throw new Error("baseUrl is required");
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.apiKey = options.apiKey;
    this.defaultTier = options.defaultTier ?? "cheap";
    this.timeoutMs = options.timeoutMs ?? 60_000;
    const f = options.fetch ?? (globalThis as { fetch?: typeof fetch }).fetch;
    if (!f) throw new Error("no fetch available: use Node 18+ or pass options.fetch");
    this.fetchImpl = f;
  }

  /** Ask a question in English. Resolves even when `ok` is false; rejects on transport/HTTP errors. */
  async ask(question: string, options: AskOptions = {}): Promise<Answer> {
    const tier = options.tier ?? this.defaultTier;
    if (!(tier in TIER_OPTIONS)) throw new Error(`unknown tier: ${tier}`);

    const body = {
      question,
      conversation_id: options.conversationId ?? null,
      options: {
        ...TIER_OPTIONS[tier],
        ambiguity_handling: options.ambiguity === "guess" ? "best_effort" : "ask_human",
      },
    };
    const raw = await this.request("POST", "/api/ask", body, options.signal);
    return toAnswer(raw as RawAnswer, tier);
  }

  /** Liveness and which database/mode the service is pointed at. */
  async health(signal?: AbortSignal): Promise<Record<string, unknown>> {
    return (await this.request("GET", "/api/health", undefined, signal)) as Record<string, unknown>;
  }

  private async request(
    method: string,
    path: string,
    body: unknown,
    signal?: AbortSignal,
  ): Promise<unknown> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    const onAbort = () => controller.abort();
    signal?.addEventListener("abort", onAbort);

    try {
      const headers: Record<string, string> = { Accept: "application/json" };
      if (body !== undefined) headers["Content-Type"] = "application/json";
      if (this.apiKey) headers.Authorization = `Bearer ${this.apiKey}`;

      let response: Response;
      try {
        response = await this.fetchImpl(this.baseUrl + path, {
          method,
          headers,
          body: body === undefined ? undefined : JSON.stringify(body),
          signal: controller.signal,
        });
      } catch (err) {
        const reason = controller.signal.aborted ? "timed out or aborted" : String(err);
        throw new SqlAgentError(`sql-agent request failed: ${reason}`, 0);
      }

      const text = await response.text();
      let parsed: unknown = text;
      try {
        parsed = text ? JSON.parse(text) : null;
      } catch {
        // Non-JSON error page; keep the text.
      }
      if (!response.ok) {
        const detail =
          parsed && typeof parsed === "object" && "detail" in parsed
            ? JSON.stringify((parsed as { detail: unknown }).detail)
            : String(text).slice(0, 300);
        throw new SqlAgentError(`sql-agent ${response.status}: ${detail}`, response.status, parsed);
      }
      return parsed;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
    }
  }
}

interface RawAnswer {
  question: string;
  answer: string;
  conversation_id?: string | null;
  sql: string | null;
  columns: string[];
  rows: unknown[][];
  row_count: number;
  truncated: boolean;
  ok: boolean;
  error: string | null;
  repairs: number;
  model_calls: number;
  input_tokens: number;
  output_tokens: number;
  seconds: number;
  tables_considered: string[];
  clarification_asks?: Clarification[];
  warnings?: string[];
}

function toAnswer(raw: RawAnswer, tier: Tier): Answer {
  return {
    question: raw.question,
    answer: raw.answer,
    conversationId: raw.conversation_id ?? null,
    sql: raw.sql,
    columns: raw.columns,
    rows: raw.rows,
    rowCount: raw.row_count,
    truncated: raw.truncated,
    ok: raw.ok,
    error: raw.error,
    clarifications: raw.clarification_asks ?? [],
    warnings: raw.warnings ?? [],
    usage: {
      tier,
      modelCalls: raw.model_calls,
      inputTokens: raw.input_tokens,
      outputTokens: raw.output_tokens,
      seconds: raw.seconds,
      repairs: raw.repairs,
    },
    tablesConsidered: raw.tables_considered,
  };
}
