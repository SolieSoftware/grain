# grain — handoff

**For a fresh Claude Code session picking this up.** Written 2026-08-24.
Read this, then `README.md`, then start at *"Do this first"*.

---

## Where we got to

**All 16 planned build tasks are implemented.** 33 commits, **170 tests passing**,
pushed to `github.com/SolieSoftware/grain` at `0fd29cb`. Both working trees clean.

**But it is not sound enough to rely on yet.** A whole-branch review found **five
critical defects, none fixed**. They are listed below and they are the next job.

### What works, verified against the live database

| | |
|---|---|
| `revenue` at line grain | **2328.60** |
| `invoice_total` at invoice grain | **2328.60** |
| coarse metric over a fanned path | **2328.60** (naive SQL: 20848.62) |
| `revenue` by country | 24 groups, **zero per-group mismatches** vs a hand-written query |
| `revenue` by playlist | correct per group, flagged `additive: false` |
| statement timeout | `pg_sleep(3)` vs 200ms cancels at 0.23s |
| row cap | trips on a no-`LIMIT` statement |

The core grain algorithm withstood direct attack by a reviewer trying to break it.
Every wrong number found came in through a **different door**.

### What is not done, by design

- **No second domain pack.** Milestone 8, decision R6, deferred and now overdue —
  every task built before it weakened the architecture test. **Defect C5 predicts it
  fails.**
- **No golden set, no ablation.** Milestones 9–10. The project's actual claim —
  *"beats raw text-to-SQL on grain correctness and deep traversal"* — is **entirely
  unmeasured**. This belongs to a separate plan.
- Read-only. No writeback, no inference, no entity resolution, no caching.

---

## Do this first

### 1. Set up

```bash
cd ~/projects/grain
uv venv && uv pip install -e ".[dev,mcp]"
cp .env.example .env          # then set GRAIN_DATABASE_URL
set -a && . ./.env && set +a
uv run pytest -q              # expect 170 passed
```

The database is Chinook v1.4.5 in a **local Postgres database named `chinook`**,
loaded from `Chinook_PostgreSql_SerialPKs.sql` — the **snake_case** variant. The
default port uses quoted CamelCase and will not work with this ontology.

Integration tests skip without `GRAIN_DATABASE_URL`. **Two CLI tests currently
*fail* rather than skip** without it (defect I5) — a bare `pytest` gives
`2 failed, 140 passed, 28 skipped`. Fix that early; a red baseline trains you to
ignore failures.

### 2. Know the environment quirks

- **SSH port 22 is blocked on this machine.** Push over GitHub's 443 endpoint:
  ```bash
  export SSH_AUTH_SOCK=~/.ssh/agent.sock
  export GIT_SSH_COMMAND="ssh -p 443 -o HostName=ssh.github.com"
  git push
  ```
  The agent socket holds the key but `SSH_AUTH_SOCK` is not exported by default.
- `ruff` is **not installed**. Line length was checked with `awk`. Install it, and
  remember generated `models.py` is **exempt** from the 100-char rule by an explicit
  ruling.
- Stale `__pycache__` has silently reverted a fix once in this project. If behaviour
  is inexplicable: `find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +`

### 3. The fix wave — this is the work

**One fix dispatch covering C1–C5 and I1–I5, then one scoped re-review.** Do not
split it into five rounds.

**The single highest-leverage line: add `-W error::SAWarning` to pytest config.**
Defect C3 emits a SQLAlchemy cartesian-product warning on four shipped specs and was
invisible for the entire build because nothing promotes warnings to failures.

---

## The five critical defects

All demonstrated with measured numbers. The pattern matters more than any one:

> **The engine defends the *grain* axis rigorously and the *population* axis — which
> rows, which groups, which entity — not at all.**

### C1 — a non-additive metric with no `group_by` returns the over-counted total

`grain.py` sets `additive=False`; **nothing refuses**. `compile.py` refuses only
*rewritten* metrics with no keys, and this one is `inline`, so it passes.

```python
QuerySpec(object="Playlist", group_by=[], metrics=["revenue"],
          traverse=[Hop(link="Playlist_Tracks"), Hop(link="Track_InvoiceLines")])
# returns 5738.28 as one unlabelled figure; truth is 2328.60
```

The flag's own message says *"each group is correct"* — with no grouping there is one
group, it is **not** correct, and the engine did the summing itself. A caller who
obeys the flag literally still gets a wrong number.

**Fix:** in `analyse`, a metric that is non-additive *and* has no group-by key has no
correct rendering — refuse, naming `group_by` or a root-grain metric. Aggregate-then-join
does not rescue it: the many-to-many is in the prefix, so the subquery over-counts
identically.

### C2 — per-group correctness assumes one group = one root row

Chinook ships **duplicate playlist names** (`Music` ×2, `Movies` ×2, `TV Shows` ×2,
`Audiobooks` ×2) and playlist 1 holds nearly the whole catalogue. So grouping
`revenue` by `Playlist.name` double-counts the colliding groups.

Worse: `describe.NON_ADDITIVITY_RULE` publishes *"Each group is still correct on its
own"* to the agent **as a domain fact**. That is conditional on the group key being a
key of the root object, and nothing checks it — `ObjectType` has `primary` and
`title_property`, no key/unique declaration at all.

**Fix:** either restrict non-additive queries to group keys the ontology declares
unique (requires adding that declaration), or state the caveat in the rule *and* in
`non_additive_reason`.

### C3 — a dotted filter through an *object join* emits a cartesian product

`compile.py` takes `target` from the filtered property's table — the **joined** table
(e.g. `genre`) — so the link's own join columns (on `track`) land in a subquery whose
FROM lacks `track`. SQLAlchemy adds it implicitly.

```sql
WHERE EXISTS (SELECT 1 FROM genre, track
              WHERE album.album_id = track.album_id AND genre.name = 'Jazz')
-- no track.genre_id = genre.genre_id
```

Degenerates to *"this album has any track, and some genre called Jazz exists
anywhere"*. A Rock-only album is returned by a Jazz filter. **Four specs in the
shipped ontology hit this:** `Album_Tracks.{genre,media_type}`,
`Playlist_Tracks.{genre,media_type}`. Contradicts the function's own docstring.

**Fix:** build the EXISTS from the link's target **object** (`rf.property.object.primary`),
apply `_apply_object_joins` *inside* the subquery, bind the predicate to the joined
column. Add `-W error::SAWarning` so a cartesian can never pass again.

### C4 — the metric-expression guard is case-sensitive; SQL identifiers are not

`METRIC_COLUMN_TOKEN` and `BARE_WORD` both start `[a-z_]`. An all-uppercase
identifier matches **neither**, so it is neither grain-checked nor existence-checked,
and renders verbatim. Postgres folds it and it runs.

A metric at grain `invoice_line` with `expr: "sum(INVOICE.TOTAL)"` loads without
complaint and reintroduces the **8.95× over-count** — through the one door the loader
exists to guard. Mixed case fails incoherently (`sum(Invoice.Total)` reports
*"references 'nvoice' unqualified"* — the regex matches from the second character).

**Fix:** case-fold before matching, and **reject any token neither regex classified**.
An unrecognised token must be an error, not a pass. Tighten `SQL_WORDS` in the same
pass — it omits window-frame keywords (`rows`, `range`, `unbounded`, `preceding`,
`following`, `current`), so a legitimate windowed metric is rejected at load.

### C5 — `ObjectType.joins` declares no cardinality (deepest finding)

`TableJoin` has `to`, `kind`, `on` — **no cardinality**. And `path_to_table` returns
`[]` for any table in `rq.root.tables`, treating every joined table as root grain.
Every *link* declares cardinality; every *object join* is silently assumed
many-to-one, and nothing in the loader, schema, plan or architecture doc says so.

So the claim **"every verdict is decided from declared cardinality alone" is false**
for one whole class of join. Two verified consequences: an object declaring a fanning
join replicates its *own* rows (truth 3, returned 4), and a metric whose grain is a
*joined* table is treated as root-grain (truth 3, returned 5).

**Fix:** add `cardinality` to `TableJoin` — or minimally validate at load that every
object join's `on` targets a PK/unique on the joined side — and make `path_to_table`
account for it. Until then, document the invariant loudly in the ontology schema.

---

## The important ones

| | |
|---|---|
| **I1** | `order_by` is accepted and **never read**. With `limit` defaulting to 100, *"top 10 countries by revenue"* returns **10 arbitrary countries**, correctly computed, with no truncation flag. Implement it or reject a spec that sets it; and flag a result that hit the limit |
| **I2** | A dotted filter changes the **population**, not the rows — every dotted filter becomes `EXISTS`, even when the link is traversed. *"invoices over 10"* returns every invoice of every customer who has *any* invoice over 10 (35 vs a true 30). Publish this rule in `describe()`, or allow a plain WHERE when the link is traversed |
| **I3** | Recursive traversal silently **drops orphans, cycles and anyone below `max_depth`** (the CTE seeds at `child_col IS NULL` and inner-joins), **and returns the wrong entity** — `to_object` is `Employee`, so properties resolve to the same employee, not the manager. No integration test exercises the compiler's recursive path at all; the existing one runs hand-written SQL |
| **I4** | The AST boundary test misses `from X import Y` — it records `ImportFrom.module` but never `.names`, and skips `ImportFrom` when `module is None`. Misses `from grain import domains`, `from .. import domains`, `from . import cli`, `from grain.engine import server`. Also `glob` not `rglob`, so `engine/sub/` is unchecked |
| **I5** | Two CLI tests **fail** rather than skip without `GRAIN_DATABASE_URL` — they shell out with `check=True` and request no `db_url` fixture |
| **I6** | `explain()` and `query()` each re-resolve, re-analyse and re-compile. Nothing binds the SQL you inspected to the SQL that ran, which undercuts the provenance claim |

---

## Deferred minors, triaged by the final review

**Must fix (blocking or blocks detecting a Critical):**
- Recursive cycle guard has no test exercising an actual cycle — the guard is fine,
  but the *base case* silently drops orphans (I3). Needs a data test, not a trace.
- `SQL_WORDS` window-frame omission — fix with C4; the tokenizer is not safe to leave
  half-rewritten.
- `GuardTripped` takes no `alternatives` — the one place the stated invariant
  *"every error names a legal alternative"* is knowingly false.

**Cheap, no reason to carry:**
- Unused `pytest` import (`test_errors.py`), unused `Filter` import
  (`test_resolve.py`) — both trip ruff F401. Install ruff so the constraint is
  enforced rather than asserted.
- `test_metrics_declare_three_distinct_grains` asserts **four** grains.
- `explain()`/`query()` double-planning (I6).

**Can wait:**
- `suggest()`'s fallback path never exercised.
- Filter ops `ne`/`gt`/`gte`/`lt`/`lte`/`like` unasserted (`gt` and `eq` verified
  live and correct).
- Removing `coalesce` untested — the subset reasoning holds under scrutiny.
- One-hop `describe()` bounds depth not fan-out — fine at this ontology size.
- **Retracted:** `_hops_reaching` *does* terminate (`seen` updates before the
  frontier extends). An earlier note calling it unguarded was wrong.

---

## Design decisions already taken — do not relitigate without cause

| | Decision |
|---|---|
| **R1** | Join conditions are **structured** `on: [{from, to}]` pairs, not expression strings. No parser, fails at load, composite keys natural, matches Apache Ossie's column-pair shape |
| **R2** | **Rewrite and surface it.** Refusal only where no correct rewrite exists; a `rewrites[]` record rides on every result |
| **R3** | Metric selection is a **permanent limit, not a gap**. grain guarantees the answer is *computed* correctly, never that the *right question was asked*. Mitigated by `ai_context`; selection errors are scored in their own column |
| **R4** | Five pipeline stages kept — the separation earns its keep in error attribution |
| **R5** | `explain` kept — auditability during bring-up beats the turn it costs |
| **R6** | Second domain **deferred**. Now overdue; C5 predicts it fails |
| **S1** | Non-additivity surfaces **twice, asymmetrically** — an authoritative per-query flag on the result, plus one *general rule* in `describe_ontology`. **Never a per-pair enumeration**, which grows multiplicatively |
| **S2** | Progressive disclosure in `describe_ontology` — **closed as not-a-problem** until an ontology stops fitting in context |
| — | **No ontology-format standard.** Apache Ossie was checked directly and rejected for v1: its metrics carry no grain and its relationships no cardinality, so it cannot express this design's core. Ossie's *nouns* are used, its `ai_context` adopted, and an `osi_export` deferred |
| — | **MCP for the agent surface** — the one boundary not under our control |
| — | `QuerySpec.limit` has **no upper bound** (`int \| None`, default 100). Owner decision: the database is small and known. This is what makes `GuardConfig.row_cap` a reachable backstop rather than dead code |

---

## Considerations for improvement

Recorded in full at
`~/projects/sol-obsidian-notes/obsidian-vault/Projects/Ontology Engine/grain/Improvements V2.md`.
The headline:

### Symmetric aggregates as a fallback where aggregate-then-join must refuse

Aggregate-then-join can only answer when **the group-by keys are reachable from the
metric's grain**, because the subquery must carry them to rejoin. A whole class of
refusal is therefore *"our mechanism cannot express this"* rather than *"the question
is unanswerable"*.

**Looker's symmetric aggregates** drop that constraint: keep the single flat join and
make the aggregate itself immune to duplicated rows by attaching a unique
key-derived number to each value so duplicates collapse under `SUM(DISTINCT …)`. No
subquery means **no rejoin, therefore none of the rejoin bug class** — which is where
C2 and the nullability Critical both came from — filters apply once, and **any
dimension can group any measure**.

Costs: unreadable SQL (which attacks the provenance thesis — `compiled_sql` is meant
to be *checkable*), fixed-scale precision limits, a required primary key,
decomposable aggregates only (no median/percentiles), and dialect specificity —
in tension with the architecture test.

**Proposal: a fallback, not a replacement.** Keep aggregate-then-join as the default,
reach for symmetric aggregates only where we would otherwise refuse, and mark the
result as machine-verified rather than human-readable. Same pattern as
`additive: false`.

**Trigger — this is an empirical decision.** Count golden-set questions refused for
*reachability* rather than genuine grain errors. **1 in 25 → document the limit
instead. 8 in 25 → build it.** Do **not** start before the ablation; this is exactly
the kind of feature that feels obviously worthwhile and may serve almost nothing.

### Other candidates

- **`order_by`** — implement or reject (I1).
- **Cross-grain metric expressions** — *"revenue where the invoice exceeds that
  customer's average"*. Unrepresentable by construction today; needs a different
  mechanism.
- **`osi_export`** — the vocabulary was deliberately aligned to Apache Ossie's nouns
  so an exporter stays mechanical. Cheapest interop story available.
- **Writeback / Actions** — Foundry's fourth construct. Would need the disjointness
  and cardinality checks read-only does not.
- **A loader rule requiring `ai_context`** wherever metric synonyms collide. This
  prose is the *entire* defence on R3, and nothing currently enforces its presence.

---

## Process notes — what actually caught things

Worth keeping, because it is the reason 170 tests exist and five Criticals were found
rather than shipped:

- **Give implementers the *why*, not just the spec.** The implementer that found the
  inverted grain rule was the one told what the numbers meant and what failure looked
  like. It reasoned from purpose to a hole in the algorithm.
- **Make reviewers prove regression tests by mutation.** Several turned *"there is a
  test"* into *"the test catches the thing"* by breaking the code and watching it
  fail. One found that **every other test stayed green** under a mutation — a whole
  blind spot nobody had noticed.
- **Reallocate reviewers from confirmation to judgement.** Once the mechanical claims
  were verified directly, asking reviewers for design judgement produced findings no
  compliance check would have.
- **Six or seven tests in this build passed for the wrong reason.** Watch for a test
  asserting a substring, a two-value Literal, or a total where a per-group error can
  cancel out.
- **A wrong fixture tempts you to weaken the rule it tests.** It happened once and was
  reverted; the fixture is almost always what is wrong.
- **Per-task review cannot see cross-cutting defects.** All five Criticals survived
  task review because each task was judged against its own brief in isolation. The
  final whole-branch gate is not optional.

---

## Where everything lives

| | |
|---|---|
| Code | `~/projects/grain`, `github.com/SolieSoftware/grain` |
| Design spec | vault: `Projects/Ontology Engine/grain/Design.md` — carries dated corrections for the grain rule (§7 step 3), chasm safety (§7 step 5), the YAML `on:` trap (§5) |
| Improvements | vault: `Projects/Ontology Engine/grain/Improvements V2.md` |
| Postmortem | vault: `Projects/Ontology Engine/grain/Build Mistakes.md` |
| Architecture diagrams | `docs/architecture.html` (published artifact, six figures + per-figure specs) |
| Build plan | `docs/plans/2026-08-17-grain-engine.md` — 16 tasks, TDD, with corrections applied |
| **Full build ledger** | `.superpowers/sdd/2026-08-17-grain-engine/progress.md` — **1004 lines**, every ruling with its cost-if-wrong, every review verdict, gitignored so it exists only on this machine |

The ledger is the richest artifact. If you need to know *why* something is the way it
is, search there before changing it.
