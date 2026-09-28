"""Application settings, loaded from environment variables or a ``.env`` file.

If you are coming from Node
---------------------------
This is the equivalent of reading ``process.env`` through a schema validator
like zod. ``pydantic-settings`` reads each field from the environment, coerces
it to the declared type, and fails loudly at startup if something is malformed
— rather than handing you the string ``"30000"`` where you expected a number,
or ``undefined`` three layers deep into a request.

Every variable is prefixed ``SQLAGENT_``. The field ``row_limit`` is read from
``SQLAGENT_ROW_LIMIT``.

Why settings are a single object passed around
----------------------------------------------
Reading ``os.environ`` scattered through the codebase makes behaviour
impossible to test: you cannot construct "the same agent but with a shorter
timeout" without mutating global state. A settings object can be built,
overridden and injected per test.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Everything configurable, in one validated object."""

    model_config = SettingsConfigDict(
        env_prefix="SQLAGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ----------------------------------------------------------------
    # Database under analysis
    # ----------------------------------------------------------------

    database_url: str = "postgresql+psycopg://testuser:testpass@localhost:5434/testdb"
    """Connection string for the database the agent answers questions about.

    This must point at a **read-only role**. The agent refuses write statements
    in its own validation layer as well, but that check is a fast-fail
    convenience: the database role is the boundary that actually cannot be
    talked around by a cleverly-worded question.
    """

    # ----------------------------------------------------------------
    # Amazon Bedrock Mantle
    # ----------------------------------------------------------------

    mantle_region: str = "us-east-1"
    """AWS region. Mantle is not available in every region.

    Supported at time of writing: us-east-1, us-east-2, us-west-2, eu-west-1,
    eu-west-2, eu-central-1, eu-south-1, eu-north-1, ap-northeast-1, ap-south-1,
    ap-southeast-2, ap-southeast-3, sa-east-1, us-gov-west-1.
    """

    mantle_signing_service: str = "bedrock-mantle"
    """SigV4 signing name. Matches the IAM action prefix ``bedrock-mantle:``."""

    light_model: str = "qwen.qwen3-coder-480b-a35b-instruct"
    """Model for mechanical work: table selection, simple queries, phrasing answers.

    Both tiers currently point at the same model, which looks odd until you see
    the measurements. On BIRD mini-dev, 150 questions each:

        qwen.qwen3-coder-480b-a35b-instruct   56.7%   2.5s per question
        qwen.qwen3-coder-next                 52.0%   4.1s per question
        deepseek.v3.2                         49.3%   4.0s per question

    The largest model was also the *fastest*, so there was no trade to make.
    Splitting the tiers would mean choosing a model that is worse on both
    axes for the sake of a tiering scheme.

    The split is kept in the configuration because it costs nothing and
    becomes useful the moment a cheaper model is worth its accuracy loss —
    on a different provider, at a different scale, or under a cost ceiling.

    Only models this AWS account can actually invoke are candidates. Claude and
    GPT-5 are listed by Mantle's ``/v1/models`` but return ``not available for
    this account``. Verify with ``python -m sqlagent.cli --models``.
    """

    strong_model: str = "qwen.qwen3-coder-480b-a35b-instruct"
    """Model for multi-table joins, report decomposition, anything harder.

    Chosen by measurement, not intuition — the benchmark harness exists for
    exactly this decision. See the note on ``light_model`` above.
    """

    max_tokens: int = 1500
    temperature: float = 0.0
    """Zero temperature: the same question must produce the same SQL.

    Determinism matters more than variety here — it makes caching meaningful
    and benchmark runs reproducible.
    """

    request_timeout_seconds: int = 120
    max_retries: int = 4
    """Retries use exponential backoff. Bedrock throttles under sustained load,
    and an unattended benchmark run will certainly hit that ceiling."""

    # ----------------------------------------------------------------
    # Schema retrieval
    # ----------------------------------------------------------------

    full_schema_threshold: int = 15
    """Below this many tables, skip retrieval and show the model everything.

    Graph retrieval exists because a large schema does not fit in a prompt. On a
    small database it is pure overhead, and worse: if seed selection picks the
    wrong starting table, the query fails for lack of a table that would
    comfortably have fitted anyway.

    Measured on BIRD mini-dev, whose databases hold 3–13 tables: sending the
    whole schema removed roughly a quarter of all failures and dropped one model
    call per question, because seed selection is no longer needed.

    Set to 0 to always use retrieval.
    """

    initial_hops: int = 1
    """How far to walk the foreign-key graph on the first attempt."""

    max_hops: int = 3
    """Ceiling when widening after a failure."""

    sample_rows: int = 2
    """Rows sampled per table to show the model real value formats.

    Small on purpose: two rows reveal whether ``status`` holds ``'active'`` or
    ``1`` without flooding the prompt or exporting much data.
    """

    max_cell_chars: int = 100
    """Long text values are truncated before entering a prompt or a log."""

    column_docs_path: str = ""
    """Directory of BIRD-style ``<table>.csv`` column descriptions, or "".

    Both systems above us on the corrected leaderboard feed the model a written
    description per column; this project fed it none. BIRD ships the
    documentation for all 11 of its databases and 77% of columns carry either a
    meaning or an enumerated value list.

    Empty means fall back to native column comments — PostgreSQL's
    ``COMMENT ON COLUMN``, which is where a real deployment already keeps this.
    SQLite has no such facility, which is the only reason the CSV path exists.
    """

    value_profiling: bool = False
    """Show each column's distinct values instead of two whole sample rows.

    **Off by default because it was measured and it did not help.**

    The idea was sound on paper: two sample rows convey format but not
    vocabulary, so a question about carbon becomes a guess between `'c'`, `'C'`
    and `'carbon'`. Showing the real value set should fix that.

    Measured on BIRD mini-dev, 150 questions, paired against the same run
    without it:

        row sampling (default)   57.0%   346k tokens
        value profiling          55.7%   498k tokens

    Slightly worse (p=0.754, so indistinguishable from no change) and 44% more
    tokens. The failures it was built to fix turned out not to be
    vocabulary problems.

    A first version inferred distinct values from `SELECT * ... LIMIT 200`,
    which was worse still (54.4%) because `LIMIT` without `ORDER BY` returns
    physically-clustered rows: 77% of its "this is the complete value list"
    claims were false. The current implementation asks the database directly
    and that error rate is zero — see `sqlagent.db.profile`.

    Kept, tested and switchable because a schema of genuinely opaque codes may
    still benefit. Turn it on, run the benchmark, and let the number decide.
    """

    profile_rows: int = 200
    """Rows read per table when profiling. Enough to tell a status column from
    a name column; cardinality is judged from this sample, not a full scan."""

    max_distinct_values: int = 12
    """A column with at most this many distinct values in the sample is treated
    as categorical, and every value is shown."""

    # ----------------------------------------------------------------
    # Safety limits
    # ----------------------------------------------------------------

    row_limit: int = 1000
    """Hard cap on rows returned by a generated query."""

    statement_timeout_ms: int = 30_000
    """Runtime backstop. The cost estimate can be wrong; a timeout cannot."""

    max_repair_attempts: int = 3
    """How many times a failed query may be regenerated before giving up."""

    max_plan_cost: float = 50_000_000.0
    """Reject a query whose planner cost estimate exceeds this. 0 disables.

    Deliberately far above a normal analytical query over a few million rows:
    the gate exists to catch a cross join or an unfiltered scan of everything,
    not to second-guess ordinary work. The statement timeout remains the
    backstop, because planner estimates are wrong in both directions.
    """

    max_plan_rows: int = 50_000_000
    """Reject a query the planner expects to produce more rows than this. 0
    disables."""

    check_inflated_aggregates: bool = True
    """Warn when a SUM or AVG may be multiplied by a join in the same query.

    The failure it catches produces no error at all: joining orders to
    order_items and summing the order total counts each total once per line
    item. Advisory, not a rejection — the same query shape is sometimes exactly
    what was wanted.
    """

    # ----------------------------------------------------------------
    # Quality toggles
    #
    # Each buys accuracy with latency and tokens, each is measurable against the
    # benchmark, and each is off by default until a measurement says otherwise.
    # The point of a toggle is that the number decides, not the design document.
    # ----------------------------------------------------------------

    quality_tier: Literal["fast", "medium", "thorough"] = "fast"
    """How much work to spend per question.

    ``fast`` — one generation, mechanical checks only. The measured default,
    and it answers most questions correctly: 58.7% on BIRD mini-dev.

    ``medium`` — the query is written three times and the statement that
    recurs is kept. Self-consistency has a real mechanism behind it: where the
    model is confident the samples agree and nothing changes, and where it is
    guessing, the version that repeats is more often the right one. Roughly 5
    model calls against 3.

    ``thorough`` — voting, a second model reviewing the query, and splitting a
    multi-part question into parts that are answered separately. 8-10 calls and
    several times the latency.

    A tier rather than a row of flags because "be more careful" is the decision
    a user actually has, and asking them to reason about self-consistency
    sampling is asking the wrong question.

    **What `thorough` is honestly sold on.** The critic was measured over 150
    BIRD questions and produced identical accuracy — 88/150 either way — for
    1.9x the tokens, rescuing four questions and breaking four (McNemar
    p = 1.000). So the tier is not sold on the critic raising the score. It is
    sold on decomposition, which answers questions one query cannot, and on the
    extra scrutiny being there for someone who has decided they want it.
    """

    use_critic: bool = False
    """Ask a second model whether the SQL answers the question.

    The three existing checks are mechanical: is it read-only, is it cheap, is
    the aggregate inflated. None of them ask whether the query is *about the
    right thing*, which is semantic and needs a model.

    Off by default: it adds a model call to every question, and its value is
    exactly what the benchmark exists to measure. Forced on by
    ``quality_tier="thorough"``.
    """

    judge_model: str = ""
    """Model that grades finished answers offline. Empty means ``strong_model``.

    Separate from the critic's model and from both answering tiers. A reviewer
    and an author failing the same way is the failure mode of any self-review
    scheme, and it matters more here than anywhere else: this model's output is
    the *measurement*, so a shared blind spot does not produce a bad answer, it
    produces a good score for a bad answer.

    Used by the benchmark harness only. Nothing in the request path calls it —
    grading an answer after it has been written can only delay it.
    """

    critic_model: str = ""
    """Model for the critic. Empty means use ``strong_model``.

    Separable because a reviewer and an author failing the same way is the
    failure mode of any self-review scheme, and pointing them at different
    models is the cheapest partial defence.
    """

    check_result_intent: bool = False
    """After the query runs, ask a model whether the *rows* answer the question.

    Measured over 150 BIRD questions, 61 of 62 failures were a valid query that
    returned the wrong rows — no error, nothing to repair against. Every
    existing check is blind to that, because of what each one is given:
    ``clarify`` and the critic see the question and the SQL but never a row,
    and the faithfulness check sees the rows but never the question.

    This runs after execution with all three, plus whatever
    :mod:`sqlagent.guards.evidence` could establish against the database. It
    can accept, send the query back with a named defect, or stop and ask the
    user.

    Off by default and deliberately not in ``OVERRIDABLE``: it adds a model
    call to every successful question, and whether it earns that call is
    exactly what the A/B exists to settle. If it does, it replaces the critic
    rather than joining it — same cost, strictly more information.
    """

    intent_model: str = ""
    """Model for the intent check. Empty means use ``strong_model``.

    Separate from ``critic_model`` for the same reason that one is separate
    from the answering model, and separate from ``judge_model`` for a stronger
    one: the judge produces the *measurement*, so sharing a model with the
    thing being measured would let one blind spot score itself.
    """

    intent_repair_attempts: int = 1
    """How many times a ``mismatch`` may send a query back to be rewritten.

    One, not ``max_repair_attempts``. A repair loop driven by a model's opinion
    of the rows has no ground truth to converge on — a database error either
    stops recurring or does not, whereas "this does not answer the question"
    can be said about every rewrite forever. One rewrite is the part with a
    mechanism behind it: the second attempt has a named defect the first did
    not.
    """

    vote_samples: int = 1
    """Generate the SQL this many times and keep the statement that recurs.

    1 disables voting. 3 is the usual setting when it is on — beyond that the
    marginal agreement gained is small and the cost is linear.

    Requires a non-zero ``vote_temperature`` to mean anything: three samples at
    temperature 0 are three identical strings. Forced to 3 by
    ``quality_tier="thorough"``.
    """

    vote_temperature: float = 0.3
    """Temperature for voting samples only.

    The default ``temperature`` stays at 0 so ordinary questions remain
    reproducible. This is the trade stated explicitly: reproducibility is given
    up for the questions voting is switched on for, and kept everywhere else.
    """

    decompose_questions: bool = False
    """Split a multi-part question into sub-questions, answer each, synthesise.

    The failure it addresses: asked for "most ordered for 2+ 3+ 4+ 5+ 6+ 7+ 8+",
    the agent answered the 2+ case and said nothing about the other six.

    Off by default because it costs a model call on *every* question to discover
    that most questions do not need it, and because a multi-part answer is
    harder to check than a single query with its SQL beside it. Worth turning on
    for exploratory work, where the questions are broad by nature.
    """

    ambiguity_handling: Literal["best_effort", "ask_human"] = "ask_human"
    """What to do with a question that has more than one defensible answer.

    ``ask_human`` stops and asks. ``best_effort`` takes the most plausible
    reading and answers.

    **``ask_human`` is the default, and that is a deliberate reversal.** It was
    ``best_effort`` because an automated benchmark cannot answer a clarifying
    question, so every ambiguous BIRD question would stall and the score would
    describe a system nobody runs.

    That reasoning optimised the measurement rather than the tool. Observed on a
    real database: asked "who are our best customers and how are they doing
    lately", the agent silently decided "best" meant revenue, silently dropped
    "lately" entirely, and returned a confident three-table answer with nothing
    to indicate it had guessed twice. A wrong assumption presented as an answer
    is worse than a question.

    The benchmark now sets ``best_effort`` explicitly, which is where that
    setting belongs: the harness declares that it cannot be asked anything,
    rather than every human user inheriting a default chosen for the harness's
    convenience.
    """

    prescreen_input: bool = False
    """Screen the question before acting on it.

    Defends the conversation, not the database — the read-only role, the
    validator and the read-only transaction already defend the database, and
    none of them can be talked out of it. What this catches is exfiltration
    through a legitimate SELECT, instruction override that now persists across
    conversation turns, and probing for the prompt or credentials.

    Off by default: it adds a model call to every question, and it is worth the
    cost when the person asking is not the person who owns the data. This is a
    filter, not a boundary — the outermost and weakest layer, which is why it is
    the only optional one.
    """

    max_clarifying_questions: int = 7
    """Ceiling on how many things the agent asks about at once.

    A ceiling, not a target: the model decides how many ambiguities a question
    actually has, and this only stops a runaway — "do a detailed study of the
    business" must not come back as a questionnaire.

    It was 3, and 3 was too low for the questions people ask. "Who are our best
    customers lately and how are they doing compared to last year" has four
    genuine ambiguities, and truncating to three left the fourth to be silently
    invented — the exact failure the ambiguity check exists to prevent, arrived
    at through the cap meant to make it usable.

    Overridable because how much back-and-forth is tolerable depends on who is
    asking, not on the deployment. Set it to 1 for a terse exchange; the agent
    will ask about the single worst ambiguity and guess the rest, which is a
    trade the person asking should get to make.
    """

    conversation_window: int = 4
    """Turns of history rendered to the model in full, most recent last.

    Four covers the realistic depth of a drill-down — ask, narrow, group,
    narrow again. Raising it does not make the agent remember more so much as
    it makes the schema compete with the transcript for room in the prompt,
    which trades a correct query for a well-recalled one.

    Turns beyond this are not discarded: they are folded into a standing
    summary when ``summarise_conversation`` is on, and kept in the store and
    the UI either way.
    """

    summarise_conversation: bool = True
    """Fold turns that fall out of the window into a standing-context note.

    Without it, a constraint stated in turn 1 ("delivered orders only, no test
    accounts") is simply gone by turn 9, and the query silently widens — two
    numbers that are not comparable, with nothing saying so.

    On by default, unlike the other quality toggles, because it costs nothing
    until a conversation is longer than the window. A first question, a
    benchmark run and a CLI invocation all pass no history at all, so their
    behaviour and their measured numbers are unchanged by this being on.
    """

    cache_sql: bool = True
    """Reuse the SQL an identical earlier question produced.

    The *statement* is cached, never the rows: the query re-runs against live
    data on every hit, so the answer stays current while the two or three model
    calls that wrote it are skipped. Every guard still applies — a cached
    statement is validated, cost-gated and executed read-only exactly like a
    freshly generated one.

    Follow-ups are never cached. "And for April?" means whatever the previous
    turns made it mean.
    """

    cache_max_entries: int = 512
    """Bound on the SQL cache. A few hundred questions is a generous working set
    for one database, and each entry is a few hundred bytes."""

    stored_result_rows: int = 50
    """Result rows kept per turn, so reopening a conversation still shows its tables.

    **This is a deliberate narrowing of an earlier stance.** Results were not
    stored at all: a query like "list every customer" returns thousands of rows
    of personal data, and keeping them turns a question log into a shadow copy
    of the database. That reasoning still holds — which is why this is a bound
    and not a switch.

    What is kept is a *display preview*: the first N rows, cells truncated to
    ``max_cell_chars``. A thousand-row answer stores fifty and says so. The
    model still receives far less — see ``conversation.carryable_result``, which
    carries at most three rows and only when the whole result is that small.

    The store is a local SQLite file next to the application, not a service. Set
    to 0 to keep nothing, which restores the original behaviour.
    """

    glossary_path: str = ""
    """Path to a JSON file of domain facts the schema cannot carry.

    Units, ambiguous terms and named metrics. Empty by default, and an empty
    glossary renders to nothing — a database with no declarations produces
    exactly the prompts it did before glossaries existed.

    This exists because of a measured failure: ``order_items.price`` stores
    paise, nothing in the schema says so, and revenue was reported 100× too
    large. See :mod:`sqlagent.glossary`.
    """

    check_answer_faithfulness: bool = True
    """Verify the answer sentence against the rows it claims to describe.

    On by default because the failure it catches is the worst kind this system
    produces: a query that ran perfectly, described by a sentence that is false.
    Observed on a real database — a one-row result naming one item, reported in
    the answer as a different item entirely.

    Costs one extra model call only when the check fires, which on correct
    answers is never. Switchable so the benchmark can measure the cost and so a
    false-positive-prone workload can turn it off.
    """

    # ----------------------------------------------------------------
    # Uploads and history
    # ----------------------------------------------------------------

    data_dir: str = "./data"
    """Where uploaded datasets and the history store live.

    Kept separate from the database under analysis, which is read-only and
    belongs to someone else — writing application state into it would be both
    impolite and, given the read-only role, impossible.
    """

    postgres_admin_url: str = ""
    """A PostgreSQL URL with permission to CREATE DATABASE.

    Needed only to restore uploaded pg dumps, each of which gets its own
    database. Empty means dump upload is unavailable; CSV and Excel still work,
    since those become SQLite files.
    """

    history_limit: int = 100
    """How many past questions the history endpoint returns by default."""

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    mantle_base_url: str = Field(default="")
    """Override for tests. Empty means "derive it from the region"."""

    llm_base_url: str = ""
    """An OpenAI-compatible endpoint to use instead of Bedrock Mantle.

    Set this to run against a local server — llama.cpp, vLLM, Ollama — or any
    other provider speaking the same protocol. When set, SigV4 signing is
    skipped entirely: a local process has no AWS credentials to sign with, and
    signing a request nobody verifies just adds latency and a failure mode.

    Example: ``SQLAGENT_LLM_BASE_URL=http://127.0.0.1:8080/v1``
    """

    llm_auth: Literal["sigv4", "bedrock_token", "bearer"] = "sigv4"
    """How to authenticate against ``llm_base_url``.

    * ``sigv4`` — sign with AWS credentials. Mantle's ``/v1`` route.
    * ``bedrock_token`` — mint a short-lived bearer from the same AWS credential
      chain (``aws configure`` / ``AWS_PROFILE``). Mantle's ``/openai/v1`` route
      needs this; SigV4 is rejected there.
    * ``bearer`` — send ``llm_api_key`` verbatim. Local servers, other providers.

    The route matters as much as the credential. ``google.gemma-4-31b`` answers
    on ``/openai/v1`` and returns "isn't supported on this route" on ``/v1`` —
    which reads exactly like an entitlement error and is not one.
    """

    # ----------------------------------------------------------------
    # Multi-tenancy
    #
    # Off by default. A single-tenant deployment — the CLI, the benchmark, a
    # team running this against their own warehouse — should not have to think
    # about accounts, and turning authentication on is a deliberate act.
    # ----------------------------------------------------------------

    require_auth: bool = False
    """Whether every request must resolve to a tenant.

    When False the API behaves exactly as it always has: one configured
    database, no accounts. When True there is no unauthenticated path at all.

    The default is False and that is a deployment decision, not a safety
    opinion. A hosted deployment sets it True, and `/api/health` reports which
    mode it is in so the answer is never a guess.
    """

    control_database_url: str = ""
    """Where accounts, credentials and entitlements live. Empty means
    ``<data_dir>/control.db``.

    Deliberately a *different* database from the history store: one holds
    tenant data, the other holds the credentials that decide who may reach it.
    Sharing them means the credential that reads question history is the same
    one that can grant an enterprise plan.
    """

    secret_key: str = ""
    """Fernet key for tenant database credentials.

    Required when ``require_auth`` is on; a missing key is a hard failure
    rather than a silent fallback to storing connection strings in clear.
    Generate one with ``Cipher.generate_key()``; in production it comes from
    KMS or Secrets Manager via the task role, never from an environment
    variable baked into an image.
    """

    session_cookie: str = "aperture_session"
    secure_cookies: bool = False
    """Set True behind TLS, which is everywhere except a developer's laptop.

    False locally because a Secure cookie is silently dropped over plain HTTP,
    and the resulting "sign-in does nothing" is a genuinely hard thing to
    diagnose.
    """

    llm_api_key: str = ""
    """Bearer token for ``llm_base_url``. Many local servers accept anything.

    Sent as ``Authorization: Bearer <key>`` only when non-empty, because some
    servers reject an Authorization header they were not expecting.
    """

    @property
    def uses_local_llm(self) -> bool:
        """Whether the completion endpoint is somewhere AWS credentials mean nothing.

        Decided from the host, not from "was a URL set at all". The previous
        version returned True for *any* explicit ``llm_base_url``, which made
        ``llm_auth="sigv4"`` unreachable in combination with one: the client
        took the local branch, attached no credential, and Mantle answered 401
        "Missing 'authorization' or 'x-api-key' header". A setting whose value
        is silently ignored is worse than one that does not exist.
        """
        if not self.llm_base_url:
            return False
        host = urlparse(self.llm_base_url).hostname or ""
        return not (host.endswith(".amazonaws.com") or host.endswith(".api.aws"))

    @property
    def resolved_llm_auth(self) -> str:
        """The authentication actually used, after one deliberate inference.

        ``llm_auth`` defaults to ``sigv4``, which is right for Mantle and wrong
        for a llama.cpp on localhost — signing a request nobody verifies buys
        nothing but latency and another way to fail. So a non-AWS host left at
        the default resolves to ``bearer``, which sends ``llm_api_key`` when
        there is one and nothing when there is not.

        Anything set explicitly is obeyed, including ``sigv4`` against a
        non-AWS host: that is a strange thing to ask for, and not something
        this should quietly override.
        """
        if self.llm_auth != "sigv4":
            return self.llm_auth
        return "bearer" if self.uses_local_llm else "sigv4"

    @property
    def base_url(self) -> str:
        """Where completion requests go."""
        if self.llm_base_url:
            return self.llm_base_url.rstrip("/")
        if self.mantle_base_url:
            return self.mantle_base_url.rstrip("/")
        return f"https://bedrock-mantle.{self.mantle_region}.api.aws/v1"


@lru_cache(maxsize=1)
def settings() -> Settings:
    """The process-wide settings object.

    Cached so the ``.env`` file is parsed once. Tests that need different values
    construct ``Settings(...)`` directly rather than going through here.
    """
    return Settings()


# --------------------------------------------------------------------------
# Per-request overrides
#
# The quality toggles are the only settings a *user* has any business changing,
# and they are the ones whose right value depends on the question rather than on
# the deployment. "Be more careful with this one" is a decision made while
# asking, not while configuring a server, so they are overridable per request
# and surfaced in the interface.
#
# Everything else — the database URL, the models, the row cap, the timeouts —
# stays server-side. A client that could set its own row limit or point the
# agent at another database would not be configuring a feature, it would be
# removing a control.
# --------------------------------------------------------------------------

OVERRIDABLE = frozenset(
    {
        "initial_hops",
        "max_hops",
        "quality_tier",
        "use_critic",
        "vote_samples",
        "prescreen_input",
        "ambiguity_handling",
        "decompose_questions",
        "cache_sql",
        "conversation_window",
        "summarise_conversation",
        "max_clarifying_questions",
    }
)
"""Settings a request may override. Deliberately an allow-list, not a denylist.

An allow-list fails closed: a new setting added to ``Settings`` is *not*
remotely settable until someone deliberately adds it here. A denylist would make
every new setting client-controllable by default, which is how a row cap or a
database URL ends up being something the browser can change.
"""


def apply_overrides(config: Settings, overrides: dict | None) -> Settings:
    """Return settings for one request, with permitted overrides applied.

    Returns the original object when nothing is overridden, so the common path
    allocates nothing and identity comparisons still hold.

    Values outside ``OVERRIDABLE`` are dropped silently rather than raising: the
    caller is a web request, and rejecting a whole question because a client
    sent one unrecognised field would be a worse outcome than ignoring it.
    """
    if not overrides:
        return config

    permitted = {
        key: value
        for key, value in overrides.items()
        if key in OVERRIDABLE and value is not None
    }
    if not permitted:
        return config

    # `model_copy` validates nothing, so the values are re-validated by
    # constructing through the model — a client sending vote_samples="lots"
    # should produce a 422, not a crash three nodes into the graph.
    return Settings(**{**config.model_dump(), **permitted})


def critic_enabled(config: Settings) -> bool:
    """Whether Loop C runs, resolving the quality tier.

    ``medium`` deliberately does not turn it on. That tier is self-consistency
    and nothing else: the critic measured net zero on accuracy for 1.9x the
    tokens, so putting it in the middle tier would be charging for latency.
    """
    return config.use_critic or config.quality_tier == "thorough"


def vote_samples(config: Settings) -> int:
    """How many candidates to generate. 1 means no voting.

    The tier is resolved here, in one place, rather than at each call site —
    "does thorough imply voting?" answered in two places is a decision that
    drifts apart.

    ``medium`` and ``thorough`` both imply three samples: voting *is* the
    middle tier, and the tier above adds the critic and decomposition on top
    rather than more samples. Beyond three, the marginal agreement gained is
    small and the cost is linear.
    """
    if config.quality_tier in ("medium", "thorough"):
        return max(3, config.vote_samples)
    return max(1, config.vote_samples)


def decompose_enabled(config: Settings) -> bool:
    """Whether a multi-part question is split.

    Forced on by ``thorough``. This is the part of that tier with a mechanism
    nothing else provides: a question that genuinely needs three queries cannot
    be answered by one, however carefully the one is written, and no amount of
    reviewing a single statement finds the two questions it never addressed.
    """
    return config.decompose_questions or config.quality_tier == "thorough"


def options_of(config: Settings) -> dict:
    """The overridable settings of a resolved config, as an overrides dict.

    Used when one request spawns another — a multi-part question answering its
    own sub-questions. Without this the parts would silently drop back to the
    server defaults, so a user who asked for ``thorough`` would get a thorough
    *decomposition* and four hasty answers.
    """
    return {name: getattr(config, name) for name in sorted(OVERRIDABLE)}
