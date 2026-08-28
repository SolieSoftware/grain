# Chat Agent — Design

**Status:** design 2026-08-26, not yet implemented.

**Goal:** A conversational agent the user talks to instead of talking to Claude
Code, which turns natural-language questions into validated `QuerySpec`s, runs
them through the engine, and narrates the results. The agent authors no SQL and
cannot author any: its only channel into the database is a `QuerySpec`.

**Placement:** a new adapter tier, `src/grain/agent/`. The existing
architectural constraint holds and extends — **`engine/` never imports from
`agent/`**, and an architecture test asserts it, exactly as it already does for
`domains/` and the other adapters.

---

## 1. Why tool use rather than structured outputs

The Messages API offers two ways to constrain output, and only one of them
survives being a chat interface.

`output_config: {format: {...}}` constrains *every* assistant reply to match a
schema. Applied here it would make every turn a `QuerySpec` and leave the agent
unable to say "which of the two revenue metrics do you mean?" — it eliminates
the conversation the feature exists to provide.

**Tool use** is the correct shape. The agent talks in ordinary prose and calls a
tool when it has enough to query. `QuerySpec` becomes the tool's
`input_schema`, so the constraint applies exactly where it should — to the
request crossing into the engine — and nowhere else.

This also means `QuerySpec` needs no redesign. It is already
`extra="forbid"` (`STRICT` in `spec.py`), which is what
`additionalProperties: false` wants, and it was already documented as "the
agent's only input. Every string here must name something the ontology
declares, so an invalid request is unrepresentable rather than merely
rejected." That comment was written for an agent that did not exist yet. This
is that agent.

**PydanticAI is not used.** Claude is called through the official `anthropic`
SDK, and Pydantic is used directly for validation. An agent framework in
between would obscure the three-layer validation below, which is the design's
whole point, and would put a second opinion about tool schemas between
`QuerySpec` and the API.

## 2. Validation is three layers

It is tempting to assume `strict: true` does the whole job. It does not, and
the gaps are where this design earns its keep.

### Layer 1 — `strict: true` on the tool definition

Guarantees the `tool_use.input` conforms to the schema: correct types, required
keys present, no extra keys. Set as a **top-level field on the tool
definition**, beside `name`/`description`/`input_schema` — not on `tool_choice`.
Requires `additionalProperties: false` and a `required` list in the schema.

What it cannot do: anything cross-field. JSON Schema has no way to say "`value`
is forbidden when `op` is `is_null`".

### Layer 2 — Pydantic on receipt

`QuerySpec` already carries a `model_validator` that Layer 1 structurally
cannot express:

```python
if self.op == "in" and not isinstance(self.value, (list, tuple)):
    raise ValueError("op 'in' requires a list or tuple of values")
if self.op == "is_null" and self.value is not None:
    raise ValueError("op 'is_null' takes no value")
```

Every tool call is therefore re-validated by `QuerySpec.model_validate()` after
the API's guarantee, not instead of it. A `ValidationError` here is a tool
result, not a crash (§5).

### Layer 3 — `resolve()` and `analyse()`

Ontology semantics: does `object` name a declared object, do `metrics` name
declared metrics, is the group key reachable, is the query grain-sound. This
layer was already built for this purpose — the engine's global constraint reads
*"every error names a legal alternative, and that alternative must itself
resolve."* `UnknownName` carries `difflib` suggestions; `NonAdditiveRefused`
enumerates the legal group keys; `KeyBeyondGrain` names what to drop.

Those errors are the agent's repair instructions (§5). Nothing new needs
writing to make them so.

### The wire model

`QuerySpec` cannot be handed to `strict: true` as-is, for two reasons:

1. **`Filter.value: Any`** produces an unconstrained `{}` schema. Strict mode
   needs a concrete type. The wire model narrows it to
   `str | float | bool | list[str | float | bool] | None`, which covers every
   `FilterOp` the engine supports.
2. **Strict requires every property in `required`**, which conflicts with
   `QuerySpec`'s defaults (`filters=[]`, `traverse=[]`, `limit=100`). On the
   wire the agent states all of them explicitly, including empty lists.

So `agent/wire.py` defines `QuerySpecWire` — same field names, everything
required, `value` concretely typed — and one function mapping it to a real
`QuerySpec`. That mapping is where Layer 2 runs.

**This duplication is a liability and is named as one.** A field added to
`QuerySpec` and not to `QuerySpecWire` is silently unreachable by the agent.
Mitigation: a test asserts the two models' field names are identical, so
divergence fails the suite rather than quietly shrinking what the agent can
ask.

## 3. Tool surface

Three tools, mirroring `Grain`'s public methods:

| Tool | Wraps | Why the agent needs it |
|---|---|---|
| `describe_ontology` | `Grain.describe(object=None)` | The domain briefing. Also callable per-object to drill in without re-reading everything. |
| `explain_query` | `Grain.explain(spec)` | Compiles and returns SQL, rewrites and the `additive` verdict **without executing**. Lets the agent check a draft before spending a query, and lets it see *why* a spec was refused. |
| `run_query` | `Grain.query(spec)` | Executes. Returns rows plus `compiled_sql`, `rewrites[]` and `additive`. |

`explain_query` earns its place: it is the only way the agent can iterate on a
spec without touching the database, and its output includes the grain verdicts
the agent must relay.

**`additive: false` must reach the user.** A non-additive result whose caveat
the agent drops in narration is precisely the failure this project exists to
prevent, one layer up. The system prompt requires the caveat be surfaced
verbatim whenever `additive` is false, and §7 tests it.

## 4. The conversation loop

**A manual loop, not the SDK's Tool Runner.** The Tool Runner would remove
boilerplate, but it is a beta surface (`client.beta.messages.tool_runner`) and
the loop here needs custom control that is the design's substance, not its
scaffolding: a repair *budget* spanning turns, typed-error-to-tool-result
mapping, and a distinction between errors the agent may retry and errors it may
not. That is ~40 lines written directly, fully testable, with no beta
dependency in a codebase that has none.

Request shape, per the current API:

```python
with client.messages.stream(
    model="claude-opus-5",
    max_tokens=64000,
    thinking={"type": "adaptive"},
    output_config={"effort": "high"},
    system=[{"type": "text", "text": briefing,
             "cache_control": {"type": "ephemeral"}}],
    tools=TOOLS,
    messages=history,
) as stream:
    response = stream.get_final_message()
```

- **`claude-opus-5`**, adaptive thinking, effort via `output_config`. No
  `budget_tokens` — removed on this model, and a 400 if sent.
- **No assistant prefill.** Returns 400 on Opus 5. Response shaping is done
  with the system prompt.
- **Streaming**, as above, so a long turn does not read as a hang.
  `max_tokens` is set for the streaming path (64000); the 16000 default exists
  to keep *non-streaming* calls under the SDK's HTTP timeout, which streaming
  removes as a concern.
- **Check `stop_reason` before reading `content`.** A `"refusal"` carries
  `stop_details`; every other stop reason leaves it `None`, so it must be
  guarded before access.
- **Parallel tool calls:** one assistant message may hold several `tool_use`
  blocks. All their `tool_result` blocks go back in a **single** user message —
  splitting them across messages trains the model out of parallel calls.

### Prompt caching

Render order is `tools` → `system` → `messages`. The briefing from
`describe()` is large and byte-stable, so it sits in `system` behind a
`cache_control` breakpoint and is read from cache on every subsequent turn.

Two invalidators to avoid, both easy to introduce by accident: **no timestamp
or session id in the briefing**, and **`describe()`'s dict must serialise
deterministically** (sorted keys) — an unordered `json.dumps` changes bytes
between processes and silently costs the cache. A test asserts
`usage.cache_read_input_tokens > 0` on the second turn of the live smoke test,
because a silently-broken cache is otherwise invisible.

## 5. The repair loop

A refusal from Layer 2 or Layer 3 goes back as a `tool_result` with
`is_error: true` carrying the engine's own error text — which already names a
legal alternative. The agent then retries.

**Bounded at 3 attempts per user turn.** On exhaustion the loop stops and shows
the user the final error plus the specs that were tried. Unbounded repair turns
a genuinely inexpressible question into an expensive infinite loop; surfacing
immediately would waste errors that were purpose-built to be machine-repairable.

The budget is per user turn, not per conversation — a new question starts fresh.

**What is never auto-repaired:** `GuardTripped`. A tripped row cap or timeout is
a fact about the query's size, not a malformed spec, and retrying it is either
identical or a way to hammer the database. It surfaces immediately.

## 6. Errors and configuration

Anthropic SDK failures are caught most-specific-first —
`AuthenticationError` → `RateLimitError` → `APIStatusError` →
`APIConnectionError` — because one broad `except` collapses the retryable and
the non-retryable into the same message. The SDK already retries 429 and 5xx
twice with backoff; the loop does not re-implement that.

**Credentials.** The client is constructed zero-arg, which resolves
`ANTHROPIC_API_KEY` first and an `ant auth login` profile after it — so an
unset env var does not mean no credentials, and the agent must not claim it
does. No key is ever hardcoded or logged. A missing credential is reported as
the engine reports everything else: what is wrong, and the one action that
fixes it.

**Dependency.** `anthropic` goes in an optional extra, `[agent]`, matching the
existing `[mcp]` pattern. The engine and its tests must remain installable and
green without it.

**CLI.** `grain chat` starts the loop, added as an `argparse` subcommand
alongside the existing ones (the CLI is `argparse`, not Click). `--engine` from the symmetric-engine
design composes with it, so a conversation can be run against either engine.

## 7. Testing

The suite must stay hermetic and free. The agent takes its client by
injection, so:

- **Unit, no network.** A stub client returns canned `tool_use` blocks. This
  covers the loop, the wire→`QuerySpec` mapping, Layer 2 rejection, the repair
  budget, and `GuardTripped` bypassing repair.
- **Schema conformance.** `QuerySpecWire.model_json_schema()` is asserted to
  satisfy what `strict: true` requires — `additionalProperties: false`, every
  property in `required`, no unconstrained `{}` anywhere. This catches the
  `Any` regression class at build time rather than as a 400.
- **Field parity.** `QuerySpecWire` and `QuerySpec` have identical field names
  (§2).
- **Caveat propagation.** Given a tool result with `additive: false`, the
  narration must contain the caveat. Asserted against a stubbed response, so it
  tests the prompt contract rather than the model's mood.
- **Live smoke test**, marked `integration` and skipped without a credential —
  the same convention `GRAIN_DATABASE_URL` already uses. One real
  question end-to-end, plus the cache-hit assertion from §4.

## 8. The risk that no layer closes

`describe.py` names it already: *"a grain-compatible but semantically wrong
pick passes silently."*

An agent asked for revenue can choose `invoice_total` instead of `revenue`.
Both resolve. Both compile. Both return a figure that is correct for the
question it answers, and the two agree only at invoice grain. **No validation
layer catches this**, because it is not a grain error — Layer 1 sees a
conforming schema, Layer 2 a valid model, Layer 3 a sound query.

Putting an LLM in front of this engine therefore reintroduces the class of
plausible-wrong-answer the engine was built to eliminate, one level up. The
existing mitigation is `ai_context.instructions`, which for these two metrics
already says *"Only for questions about invoices as documents. Not the default
revenue metric."* That is prose, and prose is not enforcement.

What this design does about it, honestly: nothing that closes it. What it does
instead is make it **visible and measurable** —

- Every result carries `compiled_sql` and the ontology elements used, and the
  agent is required to state which metric it chose and why before reporting a
  number. A wrong pick becomes a wrong pick the user can *see*.
- A golden set of question→expected-metric pairs is the only real defence, and
  it is named as follow-on work rather than pretended at here. The existing
  plan already defers a golden set and an ablation; this is the point at which
  they stop being optional.

Nobody should read this document and conclude the agent is as trustworthy as
the engine. It is not, and the gap is structural.

## 9. Non-goals

- **Writing SQL.** The agent's only database channel is a `QuerySpec`.
- **Mutating anything.** The engine is read-only; the agent inherits that.
- **Choosing the metric for the user when the question is ambiguous.** It asks.
  This is the mitigation for §8 and must not be optimised away for fewer turns.
- **Managed Agents / server-side sessions.** Conversation state is local, as
  the CLI already is. Revisit only if hosting is wanted.
- **Multi-ontology conversations.** One ontology per session.
- **Replacing the MCP adapter.** MCP exposes the engine to *other* agents;
  this is a first-party chat loop. They coexist.

## 10. Phasing

1. **Wire model and schema conformance.** `QuerySpecWire`, the mapping, and the
   §7 schema tests. No network, no loop — provable on its own.
2. **The three tools** over `Grain`, with the briefing assembled from
   `describe()` and deterministic serialisation.
3. **The loop**, streaming, with the repair budget and typed-error mapping.
4. **`grain chat`** and the live smoke test.
5. **The golden set** (§8). Not optional, despite being last.
