"""Turn names into ontology elements and a walked path. Nothing here touches a
database; every failure is decidable from the ontology alone."""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any

from .errors import NoPath, UnknownName
from .ontology import LinkType, Metric, ObjectType, Ontology, Property
from .spec import FilterOp, OrderBy, QuerySpec


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


@dataclass(frozen=True)
class ResolvedFilter:
    property: ResolvedProperty
    op: FilterOp
    value: Any


@dataclass
class ResolvedQuery:
    ontology: Ontology
    root: ObjectType
    path: list[Edge] = field(default_factory=list)
    filters: list[ResolvedFilter] = field(default_factory=list)
    group_by: list[ResolvedProperty] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)
    order_by: list[OrderBy] = field(default_factory=list)
    limit: int = 100

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

    filters = [
        ResolvedFilter(_property(root, f.property), f.op, f.value) for f in spec.filters
    ]
    group_by = [_property(root, key) for key in spec.group_by]

    metrics: list[Metric] = []
    for name in spec.metrics:
        if name not in onto.metrics:
            raise UnknownName("metric", name, suggest(name, list(onto.metrics)))
        metrics.append(onto.metrics[name])

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
