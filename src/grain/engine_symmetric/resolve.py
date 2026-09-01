"""Turn names into ontology elements and a walked path. Nothing here touches a
database; every failure is decidable from the ontology alone.
    NOTE: this is a DELIBERATE COPY of `engine/resolve.py`. The two engines
    share only the loaded ontology, so that a bug in resolution shows up as
    disagreement between them rather than being inherited by both and agreed
    upon -- a shared resolver would make the differential harness blind to
    exactly the class of bug it exists to catch.

    The cost is real: a fix here is not a fix there. `test_resolver_parity`
    asserts the two modules expose the same public names, so drift is at least
    visible rather than silent.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any

from ..engine.errors import AmbiguousGroupKey, GroupKeyNotOnPath, NoPath, UnknownName
from ..engine.ontology import LinkType, Metric, ObjectType, Ontology, Property
from ..engine.spec import FilterOp, OrderBy, QuerySpec


def _unique(names: list[str]) -> list[str]:
    """Duplicates removed, first occurrence kept, order otherwise untouched."""
    return list(dict.fromkeys(names))


def suggest(name: str, candidates: list[str], limit: int = 3) -> list[str]:
    close = difflib.get_close_matches(name, candidates, n=limit, cutoff=0.5)
    return close or sorted(candidates)[:limit]


@dataclass(frozen=True)
class Edge:
    link: LinkType
    from_object: ObjectType
    to_object: ObjectType


@dataclass(frozen=True)
class ResolvedProperty:
    object: ObjectType
    name: str
    prop: Property
    # Which position on the walked path this property is read AT. `None` is the
    # root; `i` is the object hop `i` lands on. The same physical column can be
    # in scope at more than one position -- a self-referential link traversed
    # once puts `employee` in scope as both the root and the ancestor -- so the
    # column alone does not say which row is meant. `compile` keeps one FROM
    # element per position and reads the column off the element for THIS one.
    edge_index: int | None = None

    @property
    def qualified(self) -> bool:
        """True when this property belongs to a traversed object rather than the
        root. Such a key is labelled by its full dotted name, so a result column
        can never silently mean the root's property of the same name."""
        return self.edge_index is not None


@dataclass
class ResolvedFilter:
    property: ResolvedProperty
    op: FilterOp
    value: Any
    hops: list[LinkType] = field(default_factory=list)


@dataclass
class ResolvedQuery:
    ontology: Ontology
    root: ObjectType
    path: list[Edge] = field(default_factory=list)
    filters: list[ResolvedFilter] = field(default_factory=list)
    group_by: list[ResolvedProperty] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)
    order_by: list[OrderBy] = field(default_factory=list)
    limit: int | None = 100

    @property
    def tables_in_scope(self) -> list[str]:
        tables = list(self.root.tables)
        for edge in self.path:
            for table in edge.to_object.tables:
                if table not in tables:
                    tables.append(table)
            if edge.link.via and edge.link.via not in tables:
                tables.append(edge.link.via)
        return tables

    @property
    def fanning_edges(self) -> list[Edge]:
        return [e for e in self.path if e.link.fans_out]


def _object(onto: Ontology, name: str) -> ObjectType:
    if name not in onto.objects:
        raise UnknownName("object type", name, suggest(name, list(onto.objects)))
    return onto.objects[name]


def _property(obj: ObjectType, name: str) -> ResolvedProperty:
    if name not in obj.properties:
        raise UnknownName(
            f"property of {obj.name}", name, suggest(name, list(obj.properties))
        )
    return ResolvedProperty(object=obj, name=name, prop=obj.properties[name])


def _resolve_filter(
    spec_filter: Any, root: ObjectType, onto: Ontology
) -> ResolvedFilter:
    """A filter's property is either bare (`country`, on the root) or dotted
    (`Customer_Invoices.total`, one declared hop off the root). The dotted form
    never traverses more than one hop — it names a link, not a path — so the
    grain/EXISTS machinery downstream only ever sees a single-element `hops`."""
    name = spec_filter.property
    if "." not in name:
        return ResolvedFilter(_property(root, name), spec_filter.op, spec_filter.value, [])
    link_name, _, prop_name = name.partition(".")
    if link_name not in onto.links:
        raise UnknownName("link", link_name, suggest(link_name, list(onto.links)))
    link = onto.links[link_name]
    if link.from_ != root.name:
        raise NoPath(root.name, link.to, [l.name for l in onto.links_from(root.name)])
    target = _object(onto, link.to)
    return ResolvedFilter(
        _property(target, prop_name), spec_filter.op, spec_filter.value, [link]
    )


def _group_key(key: str, root: ObjectType, path: list[Edge]) -> ResolvedProperty:
    """A group key is either bare (a root property) or qualified by a TRAVERSED
    link (`Employee_Manager.last_name` — a property of the object that hop lands
    on).

    Qualification names a link rather than an object because a link is what the
    caller wrote in `traverse`, and because two different links can land on the
    same object type. It is also why the link must actually be traversed: the
    column has to be in FROM to be grouped by, and only a hop puts it there.

    NOTE the deliberate asymmetry with a dotted FILTER, which does not require
    the link to be traversed and does not read a column of the joined row — it
    compiles to `EXISTS` and selects which root objects the query is about. A
    filter changes the population; a group key reads a value. Both are published
    as rules in `describe()`, because the shared dotted spelling is otherwise an
    invitation to assume they work the same way.
    """
    if "." not in key:
        return _property(root, key)

    link_name, _, prop_name = key.partition(".")
    positions = [i for i, edge in enumerate(path) if edge.link.name == link_name]
    traversed = _unique([edge.link.name for edge in path])
    if not positions:
        raise GroupKeyNotOnPath(key, link_name, traversed)
    if len(positions) > 1:
        raise AmbiguousGroupKey(key, link_name, positions)

    index = positions[0]
    target = path[index].to_object
    resolved = _property(target, prop_name)
    # `name` carries the FULL key, because it is the label the caller gets back
    # and the name `order_by` matches against. A qualified key labelled by its
    # bare property name would collide with the root's property of that name in
    # the emitted SELECT, which is exactly the ambiguity qualification exists to
    # remove.
    return ResolvedProperty(
        object=target, name=key, prop=resolved.prop, edge_index=index
    )


def resolve(spec: QuerySpec, onto: Ontology) -> ResolvedQuery:
    root = _object(onto, spec.object)

    path: list[Edge] = []
    current = root
    for hop in spec.traverse:
        if hop.link not in onto.links:
            raise UnknownName("link", hop.link, suggest(hop.link, list(onto.links)))
        link = onto.links[hop.link]
        if link.from_ != current.name:
            raise NoPath(
                current.name,
                link.to,
                [l.name for l in onto.links_from(current.name)],
            )
        target = _object(onto, link.to)
        if hop.max_depth is not None:
            link = link.model_copy(update={"max_depth": hop.max_depth})
        path.append(Edge(link=link, from_object=current, to_object=target))
        current = target

    filters = [_resolve_filter(f, root, onto) for f in spec.filters]

    # Asking for one key, or one metric, twice means what asking once means,
    # and a caller assembling a spec programmatically does it often. Left in,
    # the duplicate becomes two identically-labelled SELECT columns and two
    # identically-aliased subqueries, which SQLAlchemy rejects mid-compile — a
    # crash on a request whose meaning was never in doubt. First occurrence
    # wins, so the caller's own column order survives.
    group_by = [_group_key(key, root, path) for key in _unique(spec.group_by)]

    metrics: list[Metric] = []
    for name in _unique(spec.metrics):
        if name not in onto.metrics:
            raise UnknownName("metric", name, suggest(name, list(onto.metrics)))
        metrics.append(onto.metrics[name])

    # An order_by key must name a column the query actually emits. It used to be
    # accepted and never read: combined with `limit` defaulting to 100, "top 10
    # countries by revenue" returned 10 ARBITRARY countries, correctly computed,
    # with nothing to say they were not the top 10 (defect I1). Validating here
    # means a key that could never be honoured is a typed error at the door
    # rather than a silently ignored field.
    emitted = [rp.name for rp in group_by] + [m.name for m in metrics]
    for ob in spec.order_by:
        if ob.key not in emitted:
            raise UnknownName("order_by key", ob.key, suggest(ob.key, emitted))

    return ResolvedQuery(
        ontology=onto,
        root=root,
        path=path,
        filters=filters,
        group_by=group_by,
        metrics=metrics,
        order_by=spec.order_by,
        limit=spec.limit,
    )
