"""YAML in, validated Ontology out. Everything checkable is checked here, at
startup -- a typo must fail at boot, not halfway through a query."""
from __future__ import annotations

import re
from pathlib import Path

import yaml
from sqlalchemy import Engine, MetaData, UniqueConstraint, text

from .errors import OntologyError
from .ontology import (
    ACCUMULATES,
    ColumnRef,
    Metric,
    ObjectType,
    Ontology,
    Property,
    TableJoin,
)

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"

# Both halves deliberately exclude a leading digit, so a decimal literal like
# `0.5` can never match as a `table.column` token.
#
# The character class spans BOTH cases. It used to be `[a-z_]` only, which made
# the guard case-sensitive while SQL identifiers are not: `sum(INVOICE.TOTAL)`
# matched neither this regex nor BARE_WORD, so it was neither grain-checked nor
# existence-checked, rendered verbatim, and Postgres folded it and ran it --
# reintroducing the 8.95x over-count through the one door this loader exists to
# guard (defect C4). Case is folded for COMPARISON below rather than at match
# time, so a database with genuinely quoted mixed-case identifiers still
# validates against its real column names.
METRIC_COLUMN_TOKEN = re.compile(rf"\b({_IDENT})\.({_IDENT})\b")

# A bare word in a metric expression, once numbers and qualified tokens are
# masked out. The lookbehind stops a fragment of something already classified
# (the tail of a qualified token, the exponent of a float) reading as an
# identifier of its own.
BARE_WORD = re.compile(rf"(?<![A-Za-z0-9_.]){_IDENT}")

# Masked FIRST, so that neither classifier below ever sees a numeric literal and
# the residue check cannot mistake the `e` of `1.5e3` for an identifier.
NUMBER = re.compile(r"\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b")

# Whatever is left once the classifiers above have masked their own matches is a
# token NEITHER of them recognised. An unrecognised token must be an error, not
# a pass: passing is what let an all-uppercase column reference through.
IDENT_CHAR = re.compile(r"[A-Za-z_]")

SQL_WORDS: frozenset[str] = frozenset(
    {
        "all", "and", "as", "asc", "between", "by", "case", "cast", "desc",
        "distinct", "else", "end", "false", "filter", "in", "interval", "is",
        "like", "not", "null", "or", "order", "over", "partition", "then",
        "true", "when", "where",
        # Window frames. Omitting these rejected a perfectly legal windowed
        # metric at load -- `over (rows between unbounded preceding and current
        # row)` -- which is friction, not safety.
        "current", "exclude", "following", "groups", "no", "others",
        "preceding", "range", "row", "rows", "ties", "unbounded",
        # Aggregate ORDER BY and ordered-set aggregates.
        "first", "group", "last", "nulls", "within",
    }
)
"""Words that may legally stand unqualified in an aggregate expression. A
function name is recognised structurally (it is followed by `(`), so this holds
only the operators, literals and clause keywords — never a column.

Every word added here is a word an unqualified COLUMN of that name could hide
behind, so the set stays as small as real SQL allows. Matching against it is
case-insensitive, since SQL keywords are."""


# Exists solely so the `on:` join-condition key survives parsing as a string,
# not a boolean -- see the class docstring. Do not swap this back to
# `yaml.safe_load`/`yaml.SafeLoader`: `test_bare_on_key_is_not_parsed_as_a_boolean`
# in tests/unit/test_loader.py is the regression guard for exactly that.
class _OntologyLoader(yaml.SafeLoader):
    """SafeLoader, but scoped to the YAML 1.2 core-schema notion of bool.

    PyYAML's default resolver follows YAML 1.1, which also folds the bare
    words `on`/`off`/`yes`/`no` into booleans. `on` is exactly the field name
    every join pair in this schema uses, so a plain ``on: [...]`` in an
    ontology file silently becomes the key ``True`` and the join data is lost
    -- with no error, just an ontology that mysteriously fails a later,
    unrelated validation rule. Restricting bool resolution to `true`/`false`
    (still parsed natively) avoids that trap.
    """


_OntologyLoader.yaml_implicit_resolvers = {
    first_char: [
        resolver
        for resolver in resolvers
        if resolver[0] != "tag:yaml.org,2002:bool"
    ]
    for first_char, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_OntologyLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def load_ontology_from_string(
    text: str, metadata: MetaData, engine: Engine | None = None
) -> Ontology:
    """`engine` is optional and only enables the checks that must READ DATA
    (see `_check_symmetric_headroom`). Every other check is structural, so an
    ontology still loads and validates with no database connection -- which is
    what keeps the unit tests hermetic."""
    raw = yaml.load(text, Loader=_OntologyLoader)
    if not isinstance(raw, dict):
        raise OntologyError("Ontology file must be a YAML mapping")
    for key in ("objects", "links", "metrics"):
        for name, body in (raw.get(key) or {}).items():
            if not isinstance(body, dict):
                raise OntologyError(
                    f"{key} entry '{name}' must be a mapping, got {body!r}"
                )
            body.setdefault("name", name)
    onto = Ontology.model_validate(raw)
    validate(onto, metadata, engine)
    return onto


def load_ontology(
    path: Path, metadata: MetaData, engine: Engine | None = None
) -> Ontology:
    return load_ontology_from_string(
        path.read_text(encoding="utf-8"), metadata, engine
    )


def _require_table(metadata: MetaData, table: str, context: str) -> None:
    if table not in metadata.tables:
        raise OntologyError(f"{context}: table '{table}' does not exist in the database")


def _require_column(metadata: MetaData, ref: ColumnRef, context: str) -> None:
    _require_table(metadata, ref.table, context)
    if ref.column not in metadata.tables[ref.table].columns:
        raise OntologyError(
            f"{context}: column '{ref.qualified}' does not exist in the database"
        )


def _unique_column_sets(metadata: MetaData, table: str) -> set[frozenset[str]]:
    """Every column set the DATABASE itself guarantees is unique.

    Read from the reflected primary key, unique constraints and unique indexes,
    so a declaration of uniqueness anywhere in the ontology is checked against
    the only authority on the matter rather than taken on trust.
    """
    t = metadata.tables[table]
    sets: set[frozenset[str]] = set()
    if t.primary_key is not None and len(t.primary_key.columns):
        sets.add(frozenset(c.name for c in t.primary_key.columns))
    for constraint in t.constraints:
        if isinstance(constraint, UniqueConstraint):
            sets.add(frozenset(c.name for c in constraint.columns))
    for index in t.indexes:
        if index.unique:
            sets.add(frozenset(c.name for c in index.columns))
    return sets


def _check_join_cardinality(
    obj: ObjectType, join_name: str, join: TableJoin, metadata: MetaData
) -> None:
    """An object join must not fan out, and its non-fanning claim must be true.

    `TableJoin.cardinality` used to not exist: every object join was silently
    assumed many_to_one, so an object spanning a fanning table replicated its own
    rows in every query with no rewrite and no flag (defect C5). Declaring the
    cardinality is only half a fix — a declaration nothing checks is the same
    silent assumption with a field name attached — so the `to` side must be
    backed by a key the database actually enforces.

    A FANNING object join is refused outright rather than supported. `joins` says
    'these tables are all one row of this object'; a fanning table is by
    definition a different grain, which is what `links` are for. Refusing keeps
    the engine's claim true (every verdict from declared cardinality alone) and
    names the legal alternative, instead of half-teaching the grain machinery
    about a second kind of edge.
    """
    ctx = f"object '{obj.name}' join '{join_name}'"
    if join.fans_out:
        raise OntologyError(
            f"{ctx} declares cardinality '{join.cardinality}', which fans out. An "
            f"object join may not fan out — it would replicate '{obj.name}' rows in "
            f"every query that touches this object, at no declared grain. Model it "
            f"as a link from '{obj.name}' to the object that owns '{join.to}' "
            f"instead; links carry cardinality into the grain analysis, object "
            f"joins do not."
        )
    joined = frozenset(p.to.column for p in join.on if p.to.table == join.to)
    if not joined:
        raise OntologyError(
            f"{ctx}: no join pair targets '{join.to}', so nothing connects the "
            f"spanned table to '{obj.primary}'."
        )
    if joined not in _unique_column_sets(metadata, join.to):
        raise OntologyError(
            f"{ctx} declares cardinality '{join.cardinality}', but "
            f"({', '.join(sorted(joined))}) is not a primary key, unique constraint "
            f"or unique index on '{join.to}'. The database therefore does not "
            f"guarantee at most one '{join.to}' row per '{obj.primary}' row, and the "
            f"join would fan out while claiming not to. Add the constraint, or model "
            f"this as a link."
        )


def _check_uniqueness(
    obj: ObjectType, prop_name: str, prop: Property, metadata: MetaData
) -> None:
    """`unique: true` must identify one row of THIS object, provably.

    `grain.analyse` refuses a non-additive query that groups by no unique key, so
    this flag decides whether a question is answerable. A wrong one silently
    re-authorises the double-count it exists to prevent (defect C2), which is why
    it is checked against the database and confined to the object's own primary
    table.
    """
    if not prop.unique:
        return
    ctx = f"object '{obj.name}' property '{prop_name}'"
    if prop.via is not None:
        raise OntologyError(
            f"{ctx} declares 'unique: true' but is reached through the join "
            f"'{prop.via}'. Uniqueness must be a property of '{obj.primary}' "
            f"itself — a spanned table's key identifies its own row, not this one."
        )
    if prop.column.table != obj.primary:
        raise OntologyError(
            f"{ctx} declares 'unique: true' but its column lives on "
            f"'{prop.column.table}', not on this object's primary table "
            f"'{obj.primary}'."
        )
    if frozenset({prop.column.column}) not in _unique_column_sets(
        metadata, prop.column.table
    ):
        raise OntologyError(
            f"{ctx} declares 'unique: true', but '{prop.column.qualified}' is not a "
            f"single-column primary key, unique constraint or unique index in the "
            f"database. A group key that is not really unique merges two rows into "
            f"one group and double-counts everything they share."
        )


def _check_nullability(
    obj: ObjectType, prop_name: str, prop: Property, metadata: MetaData
) -> None:
    """A declaration may ADD nullability; it may never take it away.

    `compile.py` reads this flag to choose between `=` and `IS NOT DISTINCT
    FROM` when it rejoins a pre-aggregated metric onto its group keys. Under a
    plain `=`, a NULL key silently fails to match its own group — the wrong
    number, at the right magnitude, with no error. A hand-written `nullable:
    false` over a column the database says is nullable is therefore not a
    documentation slip; it is a live wrong answer waiting for one NULL row. It
    fails here, at load, which is what this loader is for.

    A property reached through a `kind: left` join is nullable whatever it
    declares and whatever its column says: the outer join manufactures NULLs
    for unmatched rows all on its own.
    """
    if prop.nullable:
        return
    ctx = f"object '{obj.name}' property '{prop_name}'"
    if metadata.tables[prop.column.table].columns[prop.column.column].nullable:
        raise OntologyError(
            f"{ctx} declares 'nullable: false', but column "
            f"'{prop.column.qualified}' is nullable in the database. A "
            f"declaration may add nullability, never remove it."
        )
    if prop.via is not None and obj.joins[prop.via].kind == "left":
        raise OntologyError(
            f"{ctx} declares 'nullable: false', but it is reached through the "
            f"left join '{prop.via}', which yields NULL for every unmatched row."
        )


def _folded_column(metadata: MetaData, table: str, column: str) -> str | None:
    """The real name of `column` on `table`, matched without regard to case.

    Postgres folds an unquoted identifier to lower case before resolving it, so
    `TOTAL` and `total` are the same column to the database and must be the same
    column to this validator. Returning the REAL name (rather than a bool) keeps
    the error messages naming what the database actually calls the column.
    """
    columns = metadata.tables[table].columns
    if column in columns:
        return column
    folded = column.lower()
    return next((c.name for c in columns if c.name.lower() == folded), None)


def _validate_metric_expr(metric: Metric, metadata: MetaData) -> None:
    """Every table.column token must belong to the metric's own grain table,
    every column reference must be qualified, and every token must be one of
    those two things.

    This is what makes relocating the expression into a subquery grouped at its
    grain provably safe, without parsing the arithmetic — and it is also what
    makes rendering the expression as raw SQL safe at all.

    Unqualified is not merely untidy: a bare `sum(unit_price)` binds to a
    DIFFERENT column depending on the strategy chosen for it. Inline, the FROM
    holds the whole walked path and the name resolves against whichever table
    happens to own it; in a subquery the FROM holds only the prefix reaching
    the grain. Same declared metric, two numbers, no error either way.

    The three passes are exhaustive BY CONSTRUCTION, which is the point. Every
    classifier masks what it accepted, and whatever survives all three is a
    token nothing recognised — an error, never a pass. Silently passing an
    unclassified token is exactly how `sum(INVOICE.TOTAL)` used to reach the
    database unchecked (defect C4): it is enough for one token to match no
    regex for the whole guard to become decorative.
    """
    ctx = f"metric '{metric.name}'"
    _require_table(metadata, metric.grain, ctx)

    # Numbers first: masked here, they can never be mistaken for identifiers by
    # either classifier below, nor left behind as residue.
    # `sql_expr`, not `expr`: a structurally-declared metric puts its columns in
    # `value`, and every guarantee below must cover both forms or the structured
    # form is an unchecked hole. Validating the RENDERED aggregate rather than
    # `value` alone also means the `distinct` keyword `count_distinct` introduces
    # is checked as the SQL keyword it is, by the same keyword list, instead of
    # needing a second code path.
    masked = NUMBER.sub(" ", metric.sql_expr)

    for match in METRIC_COLUMN_TOKEN.finditer(masked):
        table, column = match.group(1), match.group(2)
        if table.lower() != metric.grain.lower():
            raise OntologyError(
                f"{ctx} has grain '{metric.grain}' but its expression "
                f"references '{table}.{column}'. A metric may only reference "
                f"columns of its own grain table."
            )
        real = _folded_column(metadata, metric.grain, column)
        if real is None:
            raise OntologyError(
                f"{ctx}: column '{metric.grain}.{column}' does not exist in "
                f"the database"
            )
    masked = METRIC_COLUMN_TOKEN.sub(" ", masked)

    for match in BARE_WORD.finditer(masked):
        word = match.group(0)
        if masked[match.end():].lstrip().startswith("("):
            continue  # a function name, not a column
        if word.lower() in SQL_WORDS:
            continue
        raise OntologyError(
            f"{ctx} references '{word}' unqualified. Every column in a metric "
            f"expression must be written as '{metric.grain}.{word}' — an "
            f"unqualified name binds to a different column depending on how "
            f"the metric is compiled."
        )
    masked = BARE_WORD.sub(" ", masked)

    residue = IDENT_CHAR.search(masked)
    if residue is not None:
        raise OntologyError(
            f"{ctx} contains '{masked[residue.start():].strip().split()[0]}', "
            f"which is neither a qualified column of '{metric.grain}', a "
            f"function name, nor a SQL keyword. Every token in a metric "
            f"expression must be one of those three — an unrecognised token "
            f"would render into SQL unchecked."
        )


def validate(
    onto: Ontology, metadata: MetaData, engine: Engine | None = None
) -> None:
    for obj in onto.objects.values():
        ctx = f"object '{obj.name}'"
        _require_table(metadata, obj.primary, ctx)
        for join_name, join in obj.joins.items():
            _require_table(metadata, join.to, f"{ctx} join '{join_name}'")
            for pair in join.on:
                _require_column(metadata, pair.from_, f"{ctx} join '{join_name}'")
                _require_column(metadata, pair.to, f"{ctx} join '{join_name}'")
            _check_join_cardinality(obj, join_name, join, metadata)
        for prop_name, prop in obj.properties.items():
            _require_column(metadata, prop.column, f"{ctx} property '{prop_name}'")
            if prop.via is not None and prop.via not in obj.joins:
                raise OntologyError(
                    f"{ctx} property '{prop_name}': via '{prop.via}' is not a "
                    f"declared join. Declared joins: {sorted(obj.joins)}"
                )
            if prop_name in onto.metrics:
                # A group key and a metric are labelled by their own names in
                # the same SELECT, and the metric subquery exposes both. Equal
                # names collide there and SQLAlchemy raises mid-compile, on a
                # query the caller had every reason to think was legal.
                raise OntologyError(
                    f"{ctx} property '{prop_name}' has the same name as a "
                    f"declared metric. A group key and a metric share one "
                    f"namespace in the emitted SELECT — rename one of them."
                )
            _check_nullability(obj, prop_name, prop, metadata)
            _check_uniqueness(obj, prop_name, prop, metadata)

    for link in onto.links.values():
        ctx = f"link '{link.name}'"
        for side in (link.from_, link.to):
            if side not in onto.objects:
                raise OntologyError(
                    f"{ctx}: '{side}' is not a declared object. "
                    f"Declared objects: {sorted(onto.objects)}"
                )
        if link.via is not None:
            _require_table(metadata, link.via, ctx)
        for pair in [*link.on, *link.on_from, *link.on_to]:
            _require_column(metadata, pair.from_, ctx)
            _require_column(metadata, pair.to, ctx)
        if link.inverse_of is not None and link.inverse_of not in onto.links:
            raise OntologyError(f"{ctx}: inverse_of '{link.inverse_of}' is not a declared link")

    for metric in onto.metrics.values():
        _validate_metric_expr(metric, metadata)

    _check_quantity_kinds(onto)

    if engine is not None:
        _check_symmetric_headroom(onto, metadata, engine)


def _check_symmetric_headroom(
    onto: Ontology, metadata: MetaData, engine: Engine
) -> None:
    """Verify every symmetric-eligible metric's values fit the encoding's bound.

    `engine_symmetric.symmetric` needs |v| < K/2 so that distinct keys give
    distinct encoded terms -- its condition (b). At K = 1e30 the bound is 5e29,
    which is unreachable for monetary and count data. But "unreachable" was
    reasoning about money, not a measurement, and this codebase spent a whole
    branch replacing exactly that kind of reasoning with a check.

    It is a CHECK, NOT A GUARANTEE. It sees the data present at load; rows
    written afterwards can cross the bound with no error, because condition (b)
    fails silently rather than loudly. That residual risk is the design's
    weakest point and the reason a self-enforcing SQL guard is kept in reserve
    rather than discarded.

    Deliberately NOT a refusal of ineligible metrics. Opaque, inexactly-typed
    and composite-key metrics are refused by the symmetric PLANNER, not here,
    because refusing them at load would make an ontology containing one opaque
    metric fail to load for the subquery engine too -- which serves them
    correctly today. Eligibility is per-engine; this bound is a fact about data.
    """
    from ..engine_symmetric.symmetric import BOUND, EXACT_TYPES

    eligible = [
        m for m in onto.metrics.values()
        if m.is_structured and m.agg in ("sum", "avg") and m.type in EXACT_TYPES
    ]
    if not eligible:
        return
    with engine.connect() as conn:
        for metric in eligible:
            observed = conn.execute(
                text(f"select max(abs({metric.value})) from {metric.grain}")
            ).scalar()
            if observed is not None and abs(observed) >= BOUND:
                raise OntologyError(
                    f"metric '{metric.name}' has an observed maximum absolute "
                    f"value of {observed}, which leaves no headroom under the "
                    f"symmetric encoding's bound of {BOUND}. Use only the "
                    f"'subquery' engine for it, or reduce its magnitude."
                )

# A value that is nothing but one `table.column` reference. The quantity check
# applies ONLY to this shape -- see `_check_quantity_kinds`.
BARE_COLUMN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*$")


def _property_for_column(onto: Ontology, table: str, column: str):
    """The declared property reading this column, if any object declares one."""
    for obj in onto.objects.values():
        for name, prop in obj.properties.items():
            if (prop.column.table.lower() == table.lower()
                    and prop.column.column.lower() == column.lower()):
                return f"{obj.name}.{name}", prop
    return None, None


def _check_quantity_kinds(onto: Ontology) -> None:
    """A quantity that does not accumulate may not be summed.

    grain validates a metric's GRAIN -- that its rows are not replicated -- and
    had no concept of whether the QUANTITY was additive by nature.
    `sum(track.unit_price)` was arithmetically perfect, reported
    `additive: true`, and answered no useful question.

    NARROW ON PURPOSE: only a summed value that is a BARE column reference is
    inspected. `sum(a * b)` is left alone, because a rate multiplied by a count
    genuinely IS extensive -- `revenue` is `sum(unit_price * quantity)`, exactly
    that shape, and a cruder rule would refuse grain's flagship metric. Doing
    the algebra properly would mean an expression evaluator; declaring the
    composite's kind is the author's job, and writing the product is them doing
    it.

    Opaque `expr` metrics are skipped. They are not decomposed, so nothing here
    can tell whether they sum, and sniffing for `sum(` with a regex is the kind
    of fragility this file exists to avoid. Recorded as a known limit rather
    than papered over.
    """
    for metric in onto.metrics.values():
        if metric.agg != "sum" or not metric.value:
            continue
        match = BARE_COLUMN.match(metric.value)
        if match is None:
            continue
        table, column = match.group(1), match.group(2)
        ctx = f"metric '{metric.name}' sums '{table}.{column}'"

        name, prop = _property_for_column(onto, table, column)
        if prop is None:
            raise OntologyError(
                f"{ctx}, which has no declared property, so there is nowhere to "
                f"say whether that quantity accumulates. Declare a property for "
                f"it with an explicit 'quantity'."
            )
        if prop.quantity is None:
            raise OntologyError(
                f"{ctx}, but '{name}' does not declare a 'quantity'. Summing is "
                f"only meaningful for a quantity that accumulates, so say which "
                f"it is: extensive (money, counts, durations), rate (a price, a "
                f"speed) or ratio (a percentage, a share)."
            )
        if prop.quantity not in ACCUMULATES:
            raise OntologyError(
                f"{ctx}, which '{name}' declares a {prop.quantity}. A "
                f"{prop.quantity} does not accumulate -- summing it produces a "
                f"number with no referent, however correct the arithmetic. "
                f"Alternatives: use agg avg, min or max; or measure an extensive "
                f"quantity instead."
            )
