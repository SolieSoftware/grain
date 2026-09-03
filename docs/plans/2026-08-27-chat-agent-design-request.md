# Design request: a pydantic-ai chat agent over grain

**Status:** request for design. Nothing is decided; no code is implied by this note.
**Raised:** 2026-08-27
**Wanted:** a design document (not an implementation) for an agent that lets a
non-technical user ask questions in English and get grain-correct answers back.

---

## The gap this closes

`grain` guarantees that a `QuerySpec` is answered correctly or refused. It does not
produce a `QuerySpec`. Nothing in this repository converts free text into one — there is
no model call, no prompt and no parser anywhere in `src/`, and the dependencies are
SQLAlchemy, psycopg, pydantic and PyYAML.

Today that translation happens in whichever agent holds the MCP tools, which means:

- the quality of the whole system rests on a step this repo neither owns nor tests;
- the MCP tools declare `spec: dict`, so the published input schema is
  `{"type": "object", "additionalProperties": true}` — the model is not schema-bound
  even on field names, let alone on which objects and metrics exist;
- `describe()` is the only thing steering that step, and its effectiveness is unmeasured.

An owned agent turns that step into something with a version number, a test suite and a
failure rate.

## What to design

A **pydantic-ai agent** that is the user-facing surface for a grain domain. The design
should cover, at minimum:

1. **Tool surface.** Which grain calls the agent gets, and in what shape. Candidates:
   `describe_ontology`, `explain`, `query`. Whether `explain` is offered at all, or
   whether the agent should always run and then justify.
2. **Typed tool signatures.** Whether the tools should take `QuerySpec` directly rather
   than `dict`, so pydantic-ai publishes the real schema — closing the field set and the
   nine filter ops at generation time. And whether to go further: generating a
   per-domain schema whose `object`, `metrics` and `link` fields are `enum`s built from
   the loaded ontology, making an unknown name unrepresentable rather than merely
   refused.
3. **The refusal loop.** Every `GrainError` carries `alternatives` that are designed to
   be actionable (`NonAdditiveRefused` names `group_by … id`; `FanOutRefused` names the
   hop to add). The design must say how many repair attempts are allowed, what the agent
   does when the alternatives run out, and how a refusal is reported to the user without
   leaking engine vocabulary at them.
4. **Caveat propagation.** `additive: false` with `non_additive_reason` is the engine
   telling the caller a column must not be summed. The design must state how that reaches
   the user — verbatim, paraphrased, or as a visual treatment — and must not let a
   friendly summary quietly drop it. Same for `limit_reached`.
5. **Provenance.** Every `Result` carries `compiled_sql`, `rewrites` and
   `ontology_elements_used`. Decide what the user sees and what only the operator sees.
6. **Metric selection.** The known permanent limit: the engine guarantees the number is
   *computed* right, never that the right metric was *chosen*. Only `ai_context`
   synonyms and instructions distinguish `revenue` from `invoice_total`. The design should
   say how the agent disambiguates — asking the user, stating its choice, or both.
7. **Conversation state.** Whether a follow-up ("now split that by year") re-specifies
   from scratch or edits the previous `QuerySpec`, and where that state lives.
8. **Model and cost.** Which Claude model, and whether `describe()` output is cached per
   session — note that `_grain()` in `server.py` currently re-loads and re-reflects on
   *every* MCP tool call, so a describe-then-query pair is two full loads.

## It must work for people who do not have Claude Code

The MCP path assumes the user already runs an MCP-capable host. That is the wrong
assumption for the people this is for. The agent must therefore be a **self-contained
adapter that owns the model call**, authenticating with an `ANTHROPIC_API_KEY` the
operator supplies — so a user with a browser or a terminal and no Claude Code at all can
ask questions.

That gives two surfaces over one agent, and the design should say how they share code:

| Surface | For | Model call |
|---|---|---|
| Owned agent (`grain chat`, or a small HTTP endpoint) | end users with no MCP host | ours, via `ANTHROPIC_API_KEY` |
| MCP server (`server.py`, exists) | agent hosts — Claude Code, Claude Desktop | the host's |

Both must sit on `api.py` and neither may hold logic the other lacks, exactly as the CLI
and MCP adapters do today. If the two paths disagree about anything — how a refusal is
phrased, whether a caveat is shown — that difference is a defect.

Points the design has to settle:

- **Key handling.** `ANTHROPIC_API_KEY` from the environment, never from the domain pack
  and never committed; added to `.env.example` beside `GRAIN_DATABASE_URL`. Whose key
  pays — one operator key for all users, or a key per user — decides whether this can be
  hosted for anyone but the operator, so answer it explicitly.
- **Model.** Default `claude-opus-5` ($5 / $25 per MTok). `claude-sonnet-5` ($2 / $10) is
  the obvious step down if measurement shows the spec-writing task does not need Opus —
  that is a decision for the evaluation to make, not an assumption to start from. Use
  adaptive thinking (`thinking: {"type": "adaptive"}`) and set depth with
  `output_config: {"effort": ...}`; `budget_tokens` is removed on these models and returns
  a 400.
- **Caching.** `describe()` output is a large, stable prefix repeated on every turn —
  exactly what prompt caching is for. Cache it and verify with
  `usage.cache_read_input_tokens`, and note that `_grain()` re-loading per call
  (`server.py:22`) makes the describe payload cheap to *produce* but not cheap to *send*.
- **Streaming.** A chat surface should stream, both for perceived latency and to avoid
  HTTP timeouts on long turns.
- **Library binding.** pydantic-ai's Anthropic provider reads `ANTHROPIC_API_KEY` from the
  environment and takes a model string of the form `anthropic:claude-opus-5`. Pin that
  against current pydantic-ai documentation while designing rather than trusting this
  sentence — the surface has moved before.
- **Cost visibility.** The operator should be able to see what a conversation cost. Decide
  whether that is a log line per turn or an aggregate, and whether the user ever sees it.

## Constraints inherited from this repo

These are not negotiable in the design:

- **The engine boundary holds.** `engine/` imports no domain and no adapter, enforced by
  `test_boundary`. The agent is an adapter: it lives beside `cli.py` and `server.py` and
  adds no logic the library does not already have.
- **The agent never authors SQL** and never authors Python. Its only output is a spec.
- **Every refusal stays typed.** The agent may rephrase an error for a human; it may not
  swallow one or turn it into a guess.
- **No writeback.** Read-only, as the engine is.
- **A malformed spec is currently the one unstructured failure.** `QuerySpec.model_validate`
  sits inside the `except GrainError` blocks in `server.py`, and pydantic's
  `ValidationError` is not a `GrainError`, so it escapes without `alternatives`. The
  design should say whether the agent handles that or whether the adapter should be fixed.

## What the design must also answer

**Why this rather than an existing semantic layer.** Looker's LookML has declared join
cardinality, symmetric aggregates that compute correctly through a fanout without
refusing, a conversational surface, and an MCP server. dbt's Semantic Layer, Cube,
Malloy, Snowflake semantic views and Databricks metric views are all in the same space.
The design should be explicit that grain's differentiators are narrow — declarations
verified against the database's own keys, and refusals typed for a machine rather than
computed through silently — and should not restate the general case for semantic layers
as though it were novel.

**How it gets evaluated.** This is the real prize. An owned agent makes the project's
central claim measurable for the first time: a fixed question set with known answers,
scored on whether the returned number is right, whether a refusal was correct, and how
many turns it took. The right baseline is *not* raw text-to-SQL — that is the easy
one to beat — it is an agent given the same questions over the same database with a
schema dump, and ideally an agent over an existing semantic layer. The design should
specify the question set's shape and the scoring rules, because those decide whether the
result means anything.

## Explicitly out of scope for this design

- A second domain pack. Still deferred, and this agent must not assume Chinook.
- Charting, exports, scheduling, or anything resembling a BI front end.
- Writeback, inference, entity resolution, caching.
