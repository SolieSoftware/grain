# grain — handoff

**For a fresh Claude Code session picking this up.** Written 2026-08-24,
updated 2026-08-24 after the fix wave. Read this, then `README.md`, then start at
*"Do this first"*.

---

## Where we got to

**All 16 planned build tasks are implemented**, and **every defect from the
whole-branch review is fixed** — the five criticals and I1–I6, including I3, which
needed a feature built rather than a repair. **241 tests passing** (was 170), ruff
clean, `SAWarning` promoted to an error.

Each Critical has a measured regression test at the FACADE, in
`tests/integration/test_defect_anchors.py` — chosen deliberately, because four of
the five produced correct-looking SQL and a test that inspects a statement cannot
tell whether the number it returns is right. Every new test was mutation-checked:
the fix was reverted and the test watched to fail.

I3 was the one that needed a decision. Option B was taken — qualified group keys
plus an ancestor CTE plus a per-position alias environment in `compile` — so a
traversed object's properties are nameable and a hierarchy question is answerable.
See *"I3, and what it turned into"*.

Unchanged: no second domain pack, no golden set, no ablation. The project's
actual claim — *"beats raw text-to-SQL on grain correctness and deep
traversal"* — remains **entirely unmeasured**, and that is the next real job.

### What works, verified against the live database

| | |
|---|---|
| `revenue` at line grain | **2328.60** |
| `invoice_total` at invoice grain | **2328.60** |
| coarse metric over a fanned path | **2328.60** (naive SQL: 20848.62) |
| `revenue` by country | 24 groups, **zero per-group mismatches** vs a hand-written query |
| `revenue` by playlist identity | 12 groups, correct per group, flagged `additive: false` |
| statement timeout | `pg_sleep(3)` vs 200ms cancels at 0.23s |
| row cap | trips on a no-`LIMIT` statement |

The core grain algorithm withstood direct attack by a reviewer trying to break it.
Every wrong number found came in through a **different door**.

### What is not done, by design

- **No second domain pack.** Milestone 8, decision R6, deferred and now overdue —
  every task built before it weakened the architecture test. C5, which predicted it
  would fail, is fixed, so this is now a fair test of the reuse claim.
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
uv run pytest -q              # expect 241 passed
uv run ruff check src tests   # expect clean
```

The database is Chinook v1.4.5 in a **local Postgres database named `chinook`**,
loaded from `Chinook_PostgreSql_SerialPKs.sql` — the **snake_case** variant. The
default port uses quoted CamelCase and will not work with this ontology. From
nothing, on macOS:

```bash
brew install postgresql@17 && brew services start postgresql@17
# `brew services` may report "started" without starting under some launchd
# setups — check with pg_isready, and fall back to:
#   pg_ctl -D /opt/homebrew/var/postgresql@17 -l /opt/homebrew/var/log/postgresql@17.log start
curl -sSLO https://github.com/lerocha/chinook-database/releases/download/v1.4.5/Chinook_PostgreSql_SerialPKs.sql
createdb chinook
# the script creates and connects to `chinook_serial`; strip those three lines
grep -v -E '^(DROP DATABASE|CREATE DATABASE|\\c chinook_serial)' \
  Chinook_PostgreSql_SerialPKs.sql | psql -q -v ON_ERROR_STOP=1 -d chinook
```

Integration tests skip without `GRAIN_DATABASE_URL`; the no-database baseline is
`190 passed, 51 skipped` with **no failures** (I5, the two CLI tests that used to
fail rather than skip, is fixed — they request the `db_url` fixture purely to
skip).

### 2. Know the environment quirks

- **SSH port 22 is blocked on this machine.** Push over GitHub's 443 endpoint:
  ```bash
  export SSH_AUTH_SOCK=~/.ssh/agent.sock
  export GIT_SSH_COMMAND="ssh -p 443 -o HostName=ssh.github.com"
  git push
  ```
  The agent socket holds the key but `SSH_AUTH_SOCK` is not exported by default.
- `ruff` **is now installed** and configured in `pyproject.toml` (`F` + `E501`,
  line-length 100), with generated `models.py` exempt from `E501` by
  per-file-ignore — the explicit ruling, now encoded rather than remembered.
- Stale `__pycache__` has silently reverted a fix once in this project. If behaviour
  is inexplicable: `find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +`

### 3. The work now

The fix wave is done. What is left, in order of value:

1. **The golden set and the ablation** (milestones 9–10). The project's claim is
   unmeasured. Nothing else is worth building first. Count `KeyBeyondGrain`
   refusals while you do it — they are the reachability class the
   symmetric-aggregates decision is gated on (1 in 25 → document the limit;
   8 in 25 → build it).
2. **A second domain pack** (R6, overdue). C5 predicted it would fail; C5 is now
   fixed, so this is the honest test of the reuse claim.

**The single highest-leverage line, applied:** `filterwarnings = ["error::sqlalchemy.exc.SAWarning"]`
in `pyproject.toml`. C3 emitted a cartesian-product warning on four shipped specs
and stayed invisible for the whole build because nothing promoted warnings to
failures. Note that 170 tests still passed the moment it was switched on — which
proved the other half of the problem: **no test touched those four specs at all.**
`tests/integration/test_defect_anchors.py` now runs all four.

---

## I3, and what it turned into

**Decision taken: option B — build the mechanism.** Refusing the hop (option A)
would have removed the silent row-dropping but thrown away a tested recursive CTE
and left the hierarchy question unanswerable.

### What was wrong

`traverse=[Hop(link="Employee_Manager")]` joined each employee to **its own** row
in a top-down hierarchy CTE (`ON employee.employee_id = cte.employee_id`). Two
consequences, measured: it returned the same 8 employees as no traversal at all,
and the group key resolved to the same employee rather than the manager. Its only
real effect was to drop every row not reachable from a root within `max_depth` —
invisible on chinook, whose 8 employees form one connected tree.

The wrong-entity half could not be repaired in place: `resolve()` built `group_by`
from the root object only, so a traversed object's property was **unnameable by
construction**. There was no spec that asked for the manager's name.

### What was built

| Part | Where |
|---|---|
| Qualified group keys — `Employee_Manager.last_name` | `resolve._group_key`, `ResolvedProperty.edge_index` |
| Ancestor-shaped CTE — one row per (row, ancestor), with `__grain_start` | `compile._ancestor_cte` |
| Per-position alias environment — one FROM element per hop | `compile.Scope` |
| Closure cardinality — a recursive traversal is `many_to_many` | `LinkType.effective_cardinality` |
| A fanning hop pinned by a unique key needs no rewrite | `grain._pinned_by_a_unique_key` |
| `KeyBeyondGrain` — a pre-aggregate cannot carry a key past a fanning edge | `errors.py`, `grain.analyse` |

Measured, against hand-written recursive SQL: headcount under each manager at any
depth is Adams 7, Edwards 3, Mitchell 2, `additive: false`, no rewrites.
`Hop(max_depth=1)` gives direct reports and is additive again, summing to 7.

### Three things to know before changing it

- **`Scope` aliases a table only when its name is already in scope.** That is what
  keeps every pre-existing query compiling to byte-identical SQL, which is what
  the provenance guarantee (`explain()` SQL == `query()` SQL) rests on. Aliasing
  everything would be simpler and would break metric expressions, which are raw
  SQL naming real tables.
- **The unique key must sit at the fanning edge's OWN position.** A key further
  along determines the row at that edge only when every edge in between is
  functional in reverse. The counterexample is in `_pinned_by_a_unique_key`'s
  docstring: `Playlist → Playlist_Tracks (m2m) → Track_Album (m2o)` grouped by
  album double-counts a playlist holding two tracks from one album. The wider rule
  is a deliberate non-goal.
- **A recursive edge now applies the target's object joins**, against the CTE. The
  earlier rule said it must not — correct then, because the target was the source
  row; wrong now, because reusing the employee's `department` would report it
  under the manager's name.

### Still deliberately absent

- **Qualified FILTER keys.** A dotted filter remains one hop and remains an
  `EXISTS` over the population. Both dotted forms are published as rules in
  `describe()`, because the shared spelling invites the assumption that they work
  the same way.
- **Metrics at a traversed object's grain.** A metric expression is raw SQL naming
  a real table, so it binds to the first, un-aliased occurrence. A metric whose
  grain is only in scope under an alias is not expressible — correct, since its
  rows would not be the row set the name suggests.

---

## The five critical defects — all fixed 2026-08-24

Kept in full because the *pattern* is the transferable part. Each heading now
carries where the fix and its regression test live.

> **The engine defended the *grain* axis rigorously and the *population* axis —
> which rows, which groups, which entity — not at all.** Every wrong number found
> came in through a different door from the last.

### C1 — a non-additive metric with no `group_by` returns the over-counted total

> **FIXED.** `grain._require_identifying_keys` raises `NonAdditiveRefused`.
> Tests: `test_grain_additivity.py::test_non_additive_with_no_group_by_is_refused`,
> `test_defect_anchors.py::test_a_non_additive_metric_with_no_group_by_is_refused`.

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

> **FIXED**, by the stronger of the two options: `Property.unique` was added and
> is verified against the database's own keys, a non-additive query must group by
> one, and `NON_ADDITIVITY_RULE` now states its own condition. Behaviour change:
> `revenue` by `playlist.name` is now refused; by `["id", "name"]` it returns 12
> groups, playlists 1 and 8 at 2107.71 each. `Playlist.id` was added to the pack
> so the refusal has a legal alternative to name.

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

> **FIXED.** `_exists_clause` builds from `rf.property.object.primary` and applies
> the `via` join inside the subquery. 13 albums, not 347. Tests:
> `test_compile_spanned_filters.py`, and all four shipped specs execute in
> `test_defect_anchors.py` under `-W error::SAWarning`.

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

> **FIXED.** Both regexes span both cases, comparison is case-folded, numbers are
> masked first, and **any token no classifier recognised is now an error rather
> than a pass** — that last part is the general form of the defect. Window-frame
> keywords added to `SQL_WORDS`. Tests: `test_loader_declarations.py`.

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

> **FIXED**, and more strictly than the note below proposed. `TableJoin.cardinality`
> is **required with no default** (a default would restore the same silence), it is
> checked against the database's keys, and a *fanning* object join is **refused at
> load** naming links as the alternative. `path_to_table` returning `[]` for a
> spanned table is therefore now sound by enforcement rather than by assumption.
> Measured before the fix on a synthetic two-join schema: `sum(thing.weight)` = 6
> for a truth of 3, `additive: true`, no rewrites.

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

## The important ones — all six fixed

| | |
|---|---|
| **I1** | **FIXED.** `order_by` was accepted and never read; with `limit` defaulting to 100, *"top 10 countries by revenue"* returned 10 arbitrary countries — omitting USA, the largest at 523.06. Now compiled (after the rewrite joins, so a rewritten metric is orderable), a key the query does not emit is a typed `UnknownName`, and `Result.limit_reached` flags a result that hit the limit |
| **I2** | **FIXED as documentation**, which is what it was: the EXISTS semantics are intended. `describe()` now publishes `DOTTED_FILTER_RULE` — a dotted filter selects objects, not rows — and names the repair (query that object directly with a bare filter). Measured: 2328.60 over 412 invoices under the population reading, 942.32 over 64 under the row reading |
| **I3** | **FIXED by building option B.** A recursive `traverse` hop joined each row to its own CTE row: it expressed nothing and silently dropped rows. Now walks the ancestor chain, and a group key may be qualified by a traversed link. See *"I3, and what it turned into"* above |
| **I4** | **FIXED.** `_imports` now records `ImportFrom.names` and handles `module is None`, and both tests use `rglob`. Mutation-checked against all four previously-invisible forms: `from grain import domains`, `from .. import domains`, `from . import cli`, `from grain.engine import server` — each now fails the test |
| **I5** | **FIXED.** Both CLI tests request the `db_url` fixture purely to skip; the no-database baseline has no failures |
| **I6** | **FIXED as a guarantee rather than a refactor.** Planning reads only the spec and the ontology — never data, never the clock — so the same spec compiles to the same bytes. `test_defect_anchors.py::test_the_sql_explain_shows_is_the_sql_query_runs` pins `explain()["compiled_sql"] == query().compiled_sql`, which is the content of the provenance claim; the double planning is a cost, not a correctness gap |

---

## Deferred minors, triaged by the final review

**Must fix (blocking or blocks detecting a Critical):**
- Recursive cycle guard — **done**, with I3. `test_hierarchy_anchors.py::
  test_a_cycle_in_the_data_terminates_and_never_repeats_an_ancestor` creates a
  3-cycle inside a transaction, runs the compiled statement, asserts no (row,
  ancestor) pair repeats, and rolls back. Chinook has no cycle of its own, which is
  why the guard was previously only ever proven by reading the SQL.
- `SQL_WORDS` window-frame omission — **done**, with C4, as prescribed.
- `GuardTripped` takes no `alternatives` — **done**. It now carries three, so the
  invariant *"every error names a legal alternative"* is no longer knowingly false.

**Cheap, no reason to carry — all done:**
- ruff installed and configured; the four F401s removed. The 100-char limit is now
  enforced rather than checked by hand with `awk`.
- `test_metrics_declare_three_distinct_grains` renamed to `..._four_...`, which is
  what it asserts.
- I6 double-planning: closed as a cost, with the guarantee pinned by a test — see
  the table above.

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

- ~~**`order_by`** — implement or reject (I1).~~ **Implemented**, with a
  `limit_reached` flag alongside it.
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
