"""YAML in, validated Ontology out. Everything checkable is checked here, at
startup -- a typo must fail at boot, not halfway through a query."""
from __future__ import annotations

import re
from pathlib import Path

import yaml
from sqlalchemy import MetaData

from .errors import OntologyError
from .ontology import ColumnRef, Metric, ObjectType, Ontology, Property

# `[a-z_]` as the first character of each half deliberately excludes digits, so a
# decimal literal like `0.5` can never match as a `table.column` token.
METRIC_COLUMN_TOKEN = re.compile(r"\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b")

# A bare word in a metric expression, once the qualified tokens are masked out.
# The lookbehind keeps the exponent of a float literal (`1.5e3`) from reading as
# an identifier.
BARE_WORD = re.compile(r"(?<![a-z0-9_.])[a-z_][a-z0-9_]*")

SQL_WORDS: frozenset[str] = frozenset(
    {
        "all", "and", "as", "asc", "between", "by", "case", "cast", "desc",
        "distinct", "else", "end", "false", "filter", "in", "interval", "is",
        "like", "not", "null", "or", "order", "over", "partition", "then",
        "true", "when", "where",
    }
)
"""Words that may legally stand unqualified in an aggregate expression. A
function name is recognised structurally (it is followed by `(`), so this holds
only the operators and literals — never a column."""


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


def load_ontology_from_string(text: str, metadata: MetaData) -> Ontology:
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
    validate(onto, metadata)
    return onto


def load_ontology(path: Path, metadata: MetaData) -> Ontology:
    return load_ontology_from_string(path.read_text(encoding="utf-8"), metadata)


def _require_table(metadata: MetaData, table: str, context: str) -> None:
    if table not in metadata.tables:
        raise OntologyError(f"{context}: table '{table}' does not exist in the database")


def _require_column(metadata: MetaData, ref: ColumnRef, context: str) -> None:
    _require_table(metadata, ref.table, context)
    if ref.column not in metadata.tables[ref.table].columns:
        raise OntologyError(
            f"{context}: column '{ref.qualified}' does not exist in the database"
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


def _validate_metric_expr(metric: Metric, metadata: MetaData) -> None:
    """Every table.column token must belong to the metric's own grain table,
    and every column reference must be qualified.

    This is what makes relocating the expression into a subquery grouped at its
    grain provably safe, without parsing the arithmetic — and it is also what
    makes rendering the expression as raw SQL safe at all.

    Unqualified is not merely untidy: a bare `sum(unit_price)` binds to a
    DIFFERENT column depending on the strategy chosen for it. Inline, the FROM
    holds the whole walked path and the name resolves against whichever table
    happens to own it; in a subquery the FROM holds only the prefix reaching
    the grain. Same declared metric, two numbers, no error either way.
    """
    _require_table(metadata, metric.grain, f"metric '{metric.name}'")
    masked = METRIC_COLUMN_TOKEN.sub(" ", metric.expr)
    for match in BARE_WORD.finditer(masked):
        word = match.group(0)
        if masked[match.end():].lstrip().startswith("("):
            continue  # a function name, not a column
        if word in SQL_WORDS:
            continue
        raise OntologyError(
            f"metric '{metric.name}' references '{word}' unqualified. Every "
            f"column in a metric expression must be written as "
            f"'{metric.grain}.{word}' — an unqualified name binds to a "
            f"different column depending on how the metric is compiled."
        )
    for table, column in METRIC_COLUMN_TOKEN.findall(metric.expr):
        if table != metric.grain:
            raise OntologyError(
                f"metric '{metric.name}' has grain '{metric.grain}' but its "
                f"expression references '{table}.{column}'. A metric may only "
                f"reference columns of its own grain table."
            )
        _require_column(
            metadata, ColumnRef(table=table, column=column), f"metric '{metric.name}'"
        )


def validate(onto: Ontology, metadata: MetaData) -> None:
    for obj in onto.objects.values():
        ctx = f"object '{obj.name}'"
        _require_table(metadata, obj.primary, ctx)
        for join_name, join in obj.joins.items():
            _require_table(metadata, join.to, f"{ctx} join '{join_name}'")
            for pair in join.on:
                _require_column(metadata, pair.from_, f"{ctx} join '{join_name}'")
                _require_column(metadata, pair.to, f"{ctx} join '{join_name}'")
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
