/**
 * Client for the SQL agent API.
 *
 * Two ways to ask, mirroring the two server endpoints:
 *
 *  - `ask()` posts a question and waits for the whole answer. Simple.
 *  - `askStreaming()` opens a Server-Sent Events connection and reports each
 *    stage as it happens, so the UI can say what it is doing during the several
 *    seconds of model calls rather than showing an unexplained spinner.
 *
 * SSE is a plain HTTP response that stays open; the browser exposes it through
 * `EventSource`. It only supports GET, which is why the streaming endpoint
 * takes the question as a query parameter rather than a body.
 */

const BASE = import.meta.env.VITE_API_URL ?? 'http://localhost:8000';

export interface AskResponse {
  question: string;
  answer: string;
  /** The thread this turn belongs to. The server creates one if none was sent,
   *  so the client learns the id from the first answer rather than having to
   *  make a separate call before it can ask anything. */
  conversation_id: string | null;
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
  /** Set when the agent asked instead of answering: one entry per ambiguity,
   *  each with its own alternatives. Two ambiguities merged into one list are
   *  unanswerable — the options are not alternatives to each other, so picking
   *  one resolves neither. */
  clarification_asks: { question: string; options: string[] }[];
  /** Things that ran successfully but may still be wrong — an aggregate a join
   *  may have multiplied, an answer the rows do not support. The query worked;
   *  the number may not be the one anyone wanted. */
  warnings: string[];
}

export interface TableInfo {
  name: string;
  columns: string[];
  primary_key: string[];
  references: string[];
}

export interface SchemaResponse {
  version: string;
  table_count: number;
  tables: TableInfo[];
}

export interface HealthResponse {
  status: string;
  tables: number;
  schema_version: string;
  light_model: string;
  strong_model: string;
}

export interface GraphNode {
  id: string;
  label: string;
  columns: number;
  x: number;
  y: number;
  degree: number;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  label: string;
}

export interface SchemaGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface Dataset {
  id: string;
  name: string;
  kind: string;
  tables: string[];
  row_counts: Record<string, number>;
  created_at: string;
  note: string;
}

export interface ConversationSummary {
  id: string;
  started_at: string;
  updated_at: string;
  title: string;
  dataset_id: string | null;
  turns: number;
}

export interface ConversationDetail {
  conversation: ConversationSummary;
  entries: HistoryEntry[];
}

export interface HistoryEntry {
  id: number;
  /** A bounded slice of the result, kept so reopening a conversation still
   *  shows its table. `truncated` means the query returned more than was
   *  stored. Null when nothing was kept. */
  result_preview: {
    columns: string[];
    rows: unknown[][];
    row_count: number;
    truncated: boolean;
  } | null;
  asked_at: string;
  conversation_id: string | null;
  question: string;
  answer: string | null;
  sql: string | null;
  dataset_id: string | null;
  ok: boolean;
  error: string | null;
  row_count: number | null;
  seconds: number | null;
  model_calls: number | null;
  tokens: number | null;
  repairs: number | null;
}

export interface SearchHit {
  id: number;
  asked_at: string;
  question: string;
  sql: string | null;
  ok: boolean;
  row_count: number | null;
  conversation_id: string | null;
  conversation_title: string | null;
}

/**
 * Per-question overrides for the quality toggles.
 *
 * Every field is optional, and omitting one means "use the server's setting".
 * That is not the same as sending `false`: omitted is "I have no opinion",
 * `false` is "turn this off for this question".
 */
export interface AskOptions {
  initial_hops?: number;
  max_hops?: number;
  quality_tier?: 'fast' | 'thorough';
  use_critic?: boolean;
  vote_samples?: number;
  prescreen_input?: boolean;
  ambiguity_handling?: 'best_effort' | 'ask_human';
  decompose_questions?: boolean;
  cache_sql?: boolean;
  summarise_conversation?: boolean;
  conversation_window?: number;
}

export interface ToggleInfo {
  name: keyof AskOptions;
  label: string;
  help: string;
  /** What switching it on costs, in the user's terms. Shown next to the
   *  control — a toggle offered without one invites switching everything on
   *  and concluding the tool is slow. */
  cost: string;
  kind: 'switch' | 'choice';
  choices: string[];
  /** Whether the choices are numbers. Declared by the server rather than
   *  inferred from a hard-coded list of names here, which is how a new numeric
   *  toggle ends up sending "4" where the schema wants 4. */
  numeric?: boolean;
}

export interface OptionsResponse {
  defaults: Record<string, unknown>;
  toggles: ToggleInfo[];
}

export interface Stats {
  total: number;
  successful: number;
  clarified: number;
  success_rate: number;
  mean_seconds: number;
  total_tokens: number;
}

/** The stages the server reports, in the order they normally occur. */
export type Stage =
  | 'seeds'
  | 'schema'
  | 'generating'
  | 'validating'
  | 'executing'
  | 'repairing'
  | 'answering';

export interface ProgressEvent {
  stage: Stage;
  detail: Record<string, unknown>;
}

/** Human-readable label for each stage. Kept here, next to the type, so adding
 *  a stage server-side surfaces as a TypeScript error rather than a blank UI. */
export const STAGE_LABELS: Record<Stage, string> = {
  seeds: 'Finding the relevant tables',
  schema: 'Reading the schema',
  generating: 'Writing SQL',
  validating: 'Checking the query is safe',
  executing: 'Running the query',
  repairing: 'That failed — trying again',
  answering: 'Writing the answer',
};

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });

  if (!response.ok) {
    // FastAPI puts validation problems in `detail`; surface that rather than a
    // bare status code, which tells the user nothing actionable.
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (body?.detail) message = typeof body.detail === 'string' ? body.detail : message;
    } catch {
      /* response had no JSON body; the status-based message stands */
    }
    throw new Error(message);
  }

  return response.json() as Promise<T>;
}

export const getHealth = () => json<HealthResponse>('/api/health');

export const getSchema = () => json<SchemaResponse>('/api/schema');

export const ask = (
  question: string,
  datasetId?: string | null,
  conversationId?: string | null,
  options?: AskOptions,
) =>
  json<AskResponse>('/api/ask', {
    method: 'POST',
    body: JSON.stringify({
      question,
      dataset_id: datasetId ?? null,
      conversation_id: conversationId ?? null,
      options: options ?? null,
    }),
  });

export const listConversations = () => json<ConversationSummary[]>('/api/conversations');

export const createConversation = (datasetId?: string | null) =>
  json<ConversationSummary>('/api/conversations', {
    method: 'POST',
    body: JSON.stringify({ dataset_id: datasetId ?? null }),
  });

export const getConversation = (id: string) =>
  json<ConversationDetail>(`/api/conversations/${id}`);

export const deleteConversation = (id: string) =>
  json<{ deleted: string }>(`/api/conversations/${id}`, { method: 'DELETE' });

export const getGraph = (datasetId?: string | null) =>
  json<SchemaGraph>(`/api/schema/graph${datasetId ? `?dataset_id=${datasetId}` : ''}`);

export const listDatasets = () => json<Dataset[]>('/api/datasets');

export const deleteDataset = (id: string) =>
  json<{ deleted: string }>(`/api/datasets/${id}`, { method: 'DELETE' });

/**
 * Find past questions across every conversation.
 *
 * What is left of the removed flat history view, and the only part worth
 * keeping. A bare list of every question ever asked was mostly unreadable —
 * half its entries were fragments like "and for April?" with no subject outside
 * their thread. A search result that names its conversation can be opened in
 * context.
 */
export const searchTurns = (term: string, limit = 50) =>
  json<SearchHit[]>(
    `/api/search?q=${encodeURIComponent(term)}&limit=${limit}`,
  );

export const getStats = () => json<Stats>('/api/stats');

/** Opening questions proposed for this database.
 *
 *  Server-side and model-written, because the schema-derived ones were
 *  answerable and useless — "How many rows are in kitchen_profiles?" is a fact
 *  about storage, not a question anybody has. Returns an empty list if the
 *  model call failed, and the caller falls back. */
export const getSuggestions = () =>
  json<{ questions: string[] }>('/api/suggestions');

/** The toggles, their server defaults and what each costs.
 *
 *  Fetched rather than hardcoded: the defaults are a server decision a
 *  deployment may change, and the explanations belong next to the code that
 *  implements them. */
export const getOptions = () => json<OptionsResponse>('/api/options');

/**
 * Upload a CSV, Excel workbook or PostgreSQL dump.
 *
 * Uses FormData rather than JSON, and deliberately does *not* set a
 * Content-Type header: the browser must set it itself so it can append the
 * multipart boundary. Setting it by hand is the classic way to make a file
 * upload fail with an unhelpful 422.
 */
export async function uploadDataset(file: File): Promise<Dataset> {
  const body = new FormData();
  body.append('file', file);

  const response = await fetch(`${BASE}/api/datasets`, { method: 'POST', body });

  if (!response.ok) {
    let message = `Upload failed (${response.status})`;
    try {
      const payload = await response.json();
      if (typeof payload?.detail === 'string') message = payload.detail;
    } catch {
      /* no JSON body; keep the status-based message */
    }
    throw new Error(message);
  }

  return response.json() as Promise<Dataset>;
}

/**
 * Ask a question, reporting progress as it happens.
 *
 * Returns a cancel function. Calling it closes the connection — important when
 * the user navigates away or asks something else mid-flight, otherwise the
 * browser holds the socket open and a late answer overwrites the new one.
 */
export function askStreaming(
  question: string,
  handlers: {
    onProgress: (event: ProgressEvent) => void;
    onResult: (result: AskResponse) => void;
    onError: (message: string) => void;
    datasetId?: string | null;
    conversationId?: string | null;
    options?: AskOptions;
  },
): () => void {
  const url =
    `${BASE}/api/ask/stream?question=${encodeURIComponent(question)}` +
    (handlers.datasetId ? `&dataset_id=${encodeURIComponent(handlers.datasetId)}` : '') +
    (handlers.conversationId
      ? `&conversation_id=${encodeURIComponent(handlers.conversationId)}`
      : '') +
    // EventSource can only issue a GET, so the options travel as a JSON string
    // in the query. Only sent when something is actually set, so the common
    // request is unchanged.
    (handlers.options && Object.keys(handlers.options).length > 0
      ? `&options=${encodeURIComponent(JSON.stringify(handlers.options))}`
      : '');
  const source = new EventSource(url);
  let settled = false;

  const stages: Stage[] = [
    'seeds',
    'schema',
    'generating',
    'validating',
    'executing',
    'repairing',
    'answering',
  ];

  for (const stage of stages) {
    source.addEventListener(stage, (event) => {
      try {
        handlers.onProgress({ stage, detail: JSON.parse((event as MessageEvent).data) });
      } catch {
        handlers.onProgress({ stage, detail: {} });
      }
    });
  }

  source.addEventListener('result', (event) => {
    settled = true;
    try {
      handlers.onResult(JSON.parse((event as MessageEvent).data) as AskResponse);
    } catch {
      handlers.onError('The server sent a malformed response.');
    }
    source.close();
  });

  source.addEventListener('error', (event) => {
    const data = (event as MessageEvent).data;
    if (data) {
      settled = true;
      try {
        handlers.onError(JSON.parse(data).message ?? 'Something went wrong.');
      } catch {
        handlers.onError('Something went wrong.');
      }
      source.close();
      return;
    }

    // No payload means a transport-level failure. EventSource retries on its
    // own, so only report it if the request never produced a result — otherwise
    // a normal post-result close would surface as a spurious error.
    if (!settled) {
      handlers.onError('Lost connection to the server.');
      source.close();
    }
  });

  return () => {
    settled = true;
    source.close();
  };
}
