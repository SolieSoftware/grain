"""YAML in, validated Ontology out. Everything checkable is checked here, at
startup -- a typo must fail at boot, not halfway through a query."""
from __future__ import annotations

import re
from pathlib import Path

import yaml
from sqlalchemy import MetaData

from .errors import OntologyError
from .ontology import ColumnRef, Metric, Ontology

# `[a-z_]` as the first character of each half deliberately excludes digits, so a
# decimal literal like `0.5` can never match as a `table.column` token.
METRIC_COLUMN_TOKEN = re.compile(r"\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b")


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


def _validate_metric_expr(metric: Metric, metadata: MetaData) -> None:
    """Every table.column token must belong to the metric's own grain table.

    This is what makes relocating the expression into a subquery grouped at its
    grain provably safe, without parsing the arithmetic.
    """
    _require_table(metadata, metric.grain, f"metric '{metric.name}'")
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
