"""How many decimal places a column can hold exactly.

The one place where the DATABASE overrides the ontology. `ValueType` has no
float member, so a `double precision` column can be -- and would be -- declared
`decimal` by an ontology author. Trusting that declaration would let an inexact
column into an encoding that depends on exactness, so eligibility is decided
from the reflected column instead. The database is authoritative here exactly as
it already is for cardinality, uniqueness and nullability.

This is also the difference between grain's order-statistic encoding and
Looker's. Looker FLOOR-scales to a GUESSED precision -- 6 by default, and its
own docs advise dropping to 5 for large values, which loses more. A guessed
scale must truncate. A scale read from the schema cannot.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import Column, Float, Integer, Numeric


def column_scale(column: Column[Any]) -> int | None:
    """The column's exact decimal scale, or None if it has none.

    `None` means "cannot be encoded exactly" and is the caller's cue to refuse.
    It covers three cases needing no separate handling: floating point, which
    has no exact decimal scale at all; `numeric` declared without precision,
    which holds arbitrary scale so no fixed power of ten clears the fraction;
    and every non-numeric type.
    """
    # Float BEFORE Numeric: `Float` subclasses `Numeric` in SQLAlchemy, so the
    # order matters. A float that fell through to the Numeric branch would be
    # judged on a `scale` attribute it does not meaningfully have.
    if isinstance(column.type, Float):
        return None
    if isinstance(column.type, Integer):
        return 0
    if isinstance(column.type, Numeric):
        # `asdecimal` is deliberately not consulted: it describes how values are
        # handed back to Python, not what the column can hold.
        return getattr(column.type, "scale", None)
    return None
