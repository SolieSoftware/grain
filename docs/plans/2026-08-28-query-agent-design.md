# Query Agent — Design

**Status:** design 2026-08-28.

**Goal:** A conversational agent the user chats to in natural language, which
answers questions about the domain by emitting a **validated `QuerySpec`** and
running it through grain. It replaces the human hand-writing specs; it does not
replace the engine.

**Stack:** Anthropic Messages API via the official `anthropic` Python SDK,
`ANTHROPIC_API_KEY` from the environment, `claude-opus-5`, Pydantic as the
enforcement boundary.

---

## 1. The one invariant

**The model never writes SQL, and never gets to.** Its only output channel into
the engine is a `QuerySpec` — a Pydantic model whose every string must name
something the ontology declares. Everything downstream (grain analysis, the
fan-out rewrite, the guard) runs exactly as it does for a hand-written spec.

This is not a safety add-on; it is the reason the project exists. A text-to-SQL
agent's failure mode is a plausible query returning a wrong number. Here the
worst a bad generation can do is name something that does not exist, which is a
typed error at the door.

So the agent is deliberately **not** given: raw SQL, the database connection,
the schema DDL, or any way to influence compilation. It gets `describe()` and a
tool that takes a `QuerySpec`.

## 2. How the constraint is enforced

Three layers, in order, and each is load-bearing:

1. **Strict tool use.** The agent is given one tool, `run_query`, whose
   `input_schema` is `QuerySpec.model_json_schema()` verbatim, with
   `strict: true`. `QuerySpec` already sets `extra="forbid"`, so Pydantic emits
   `additionalProperties: false` and the schema is directly usable — the
   constraint the API needs is the constraint the model already declares.
2. **Pydantic validation.** `QuerySpec.model_validate(block.input)` on the way
   in. Strict tool use makes malformed input unlikely; it does not make it
   impossible, and this is the boundary that decides. A `ValidationError` is
   fed back as a repair (§4), never coerced.
3. **The engine's own resolution.** Even a schema-valid spec can name a metric
   or link that does not exist, or ask a question with no correct answer. That
   is `resolve`/`analyse`'s job and needs no help from the agent layer.

Tool use rather than `output_config.format`, because a chat agent must be able
to do something other than query — ask a clarifying question, explain a refusal,
say a question is not answerable from this domain. A forced output format makes
every turn a query; a tool makes querying one option among several.

## 3. What the agent knows

The system prompt is `Grain.describe()` — the same JSON an MCP client gets. Not
the DDL, and not a hand-written summary that could drift from it.

`describe()` already publishes the rules an agent needs and would otherwise
guess at: which links exist and their cardinality, what each metric measures and
at what grain, the `ai_context` synonyms and instructions that disambiguate two
metrics that both sound like "revenue", and the non-additivity rule.

Because it is large and identical on every turn, it takes a `cache_control`
breakpoint and sits first, before anything volatile. The conversation follows.

## 4. The repair loop

grain's errors were built for exactly this. Every `GrainError` carries
`.alternatives` — a list of legal next moves, each of which is required to
itself resolve. That contract turns an error into a machine-readable repair
instruction rather than a dead end.

So a failed query is returned to the model as a `tool_result` with
`is_error: true`, carrying the message and the alternatives, and the loop
continues. `NonAdditiveRefused` naming the unique keys to group by, or
`UnknownName` naming the near-misses, is usually enough for a correct second
attempt.

Bounded by `max_repairs` (default 3), because a model that cannot fix a spec in
three tries is not converging and should say so to the user instead of burning
tokens.

## 5. Reporting the answer honestly

The agent is told the rows, and **also** told the caveats — but the caveats are
appended by code, not left to the prompt:

- `additive: false` is prepended to the tool result as an explicit warning
  naming the reason. A model that summed a non-additive column into a "total"
  would undo the entire point of the engine, and a prompt instruction is not a
  strong enough guarantee for that.
- `limit_reached` is stated, so "the top 5" is not reported as "all of them".
- `rewrites` and `engine` are included, so the agent can say how the number was
  computed if asked.

The prompt then tells it to pass those on. Belt and braces: the information is
in the tool result whether or not the model was going to mention it.

## 6. Shape

```
grain/agent/
  __init__.py
  session.py     # AgentSession: the conversation loop
  tools.py       # the run_query tool definition + execution
  prompt.py      # system prompt assembly from describe()
  cli.py         # `grain chat`
```

`agent/` may import `grain.engine.api` — it is an adapter, like the CLI and MCP
layers, and the standing rule is only that `engine/` must not import *upward*.

The `anthropic` SDK is an **optional dependency** under a `[agent]` extra. The
library, CLI and MCP paths must keep working with it uninstalled, so the import
happens inside the agent module and its absence is a clear error naming the
install command.

## 7. Model and request settings

- **`claude-opus-5`.** Overridable with `--model`.
- **Adaptive thinking** (`{"type": "adaptive"}`). Choosing a grain-correct spec
  over a domain with overlapping metric names is exactly the kind of task it
  helps. `budget_tokens` is rejected on this model.
- **`max_tokens: 16000`**, non-streaming. Responses here are short — a spec and
  a paragraph — and the low ceiling that would tempt is the one that truncates.
- **Typed error handling** per exception class, not one broad catch:
  `AuthenticationError` is a wrong key, `RateLimitError` is retryable,
  `APIConnectionError` is the network. Each gets its own message.

## 8. Non-goals

- **No SQL generation, ever.** Not as a fallback, not behind a flag.
- **No writeback.** The engine is read-only and so is the agent.
- **No multi-question planning.** One question, one spec, one answer. Chaining
  is a later question and should not be smuggled in now.
- **No conversation persistence.** In-memory for the session's lifetime.
- **No streaming** in v1. It is a small change if the latency proves annoying.

## 9. Risks

1. **Metric selection is the real failure mode, and it is not new.** The engine
   guarantees the number is computed correctly; nothing guarantees the right
   metric was chosen. If two metrics both sound like "revenue", only
   `ai_context` prose distinguishes them, and the agent can pick wrong while
   every layer below it behaves perfectly. This is the honest weak point of the
   whole design and it should be measured, not asserted.
2. **No evaluation set.** There is no fixed question set proving the agent picks
   correct specs. Until there is, its accuracy is anecdote. This is the same gap
   the README records for the project as a whole.
3. **Cost is unbounded per question** if the repair loop thrashes. Capped at
   three, but a pathological ontology could still cost several calls per turn.
