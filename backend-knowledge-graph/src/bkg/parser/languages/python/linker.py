"""Python fact-builder: (SemanticModel + FrameworkFacts) -> PartialGraph.

This is where the parser pre-computes every cross-file reference as ordered
``{file}:{symbol}`` CANDIDATE ids (using Python import + typing semantics from
:mod:`resolve`), so the graph engine can stitch the file-local PartialGraphs together
without any language knowledge. It is the language-specific counterpart to the neutral
graph engine — a TypeScript/Java plugin would provide its own builder emitting the same
``PartialGraph`` shape with candidates resolved by that language's rules.
"""

from __future__ import annotations

from collections.abc import Mapping

from ....protocol.models import (
    AnyNode,
    BaseRef,
    ConfigNode,
    FieldIR,
    MiddlewareNode,
    PartialGraph,
    RouteNode,
    RouteParam,
    RouterMount,
    SchemaRefNode,
    SecuritySchemeNode,
)
from ...analysis import ConfigFact, ImportFact, MiddlewareFact, MountFact, ParamFact, RouteFact, SchemaFact
from ...base import FrameworkFacts, SemanticModel
from . import resolve

Imports = Mapping[str, ImportFact]


def _route_param(p: ParamFact, imports: Imports, file: str) -> RouteParam:
    if p.depends is not None:
        # an auth dependency: classify inline (Depends(HTTPBearer())), and emit the
        # same-file then cross-file {file}:{var} candidates a security scheme lookup tries
        return RouteParam(
            name=p.name,
            annotation=p.annotation,
            has_default=p.has_default,
            depends=p.depends,
            scheme_inline=resolve.scheme_of(p.depends),
            scheme_candidates=(f"{file}:{p.depends}", *resolve.resolve_symbol(p.depends, imports, file)),
        )
    return RouteParam(
        name=p.name,
        annotation=p.annotation,
        has_default=p.has_default,
        dto_candidates=resolve.resolve_dto(p.annotation, imports, file),
    )


def _route_node(r: RouteFact, imports: Imports, file: str) -> RouteNode:
    response_ann = r.response_model if r.response_model is not None else r.return_annotation
    return RouteNode(
        id=f"route:{file}:{r.router}:{r.method}:{r.path}",
        method=r.method,
        path=r.path,
        file=file,
        line=r.line,
        router_local=r.router,
        handler=r.handler,
        tags=tuple(r.tags) if r.tags is not None else (),
        params=tuple(_route_param(p, imports, file) for p in r.params),
        response_candidates=resolve.resolve_dto(response_ann, imports, file),
    )


def _schema_node(s: SchemaFact, imports: Imports, file: str) -> SchemaRefNode:
    fields = tuple(
        FieldIR(
            name=f.name,
            type=f.type,
            required=f.required,
            default=f.default,
            ref_candidates=resolve.resolve_dto(f.type, imports, file),
        )
        for f in s.fields
    )
    base_refs = tuple(BaseRef(name=b, candidates=resolve.resolve_dto(b, imports, file)) for b in s.bases)
    return SchemaRefNode(
        id=f"schemaRef:{file}:{s.name}", name=s.name, fields=fields, bases=tuple(s.bases), base_refs=base_refs
    )


def _mount(m: MountFact, imports: Imports, file: str) -> RouterMount:
    return RouterMount(
        mounting_file=file,
        router_local=m.router_local,
        prefix=m.prefix,
        target_symbol_ref=m.target_expr,
        tags=tuple(m.tags) if m.tags is not None else (),
        target_candidates=resolve.resolve_symbol(m.target_expr, imports, file),
    )


def _middleware_node(m: MiddlewareFact, file: str) -> MiddlewareNode:
    return MiddlewareNode(
        id=f"middleware:{file}:{m.name}:{m.line}",
        name=m.name,
        file=file,
        line=m.line,
        router_local=m.router_local,
    )


def _config_node(c: ConfigFact, file: str) -> ConfigNode:
    return ConfigNode(
        id=f"config:{file}:{c.kind}:{c.name}:{c.line}",
        config_kind=c.kind,
        name=c.name,
        type=c.type,
        default=c.default,
        cls=c.cls,
        line=c.line,
    )


class PythonLinker:
    """Builds the per-file PartialGraph with all cross-file candidates resolved."""

    def build(self, model: SemanticModel, framework: FrameworkFacts) -> PartialGraph:
        if model.partial:
            return PartialGraph(partial=True)
        imports = model.imports
        file = model.path
        nodes: list[AnyNode] = []
        nodes.extend(_route_node(r, imports, file) for r in framework.routes)
        nodes.extend(_schema_node(s, imports, file) for s in model.schemas)
        nodes.extend(_middleware_node(m, file) for m in framework.middlewares)
        nodes.extend(
            SecuritySchemeNode(
                id=f"securityScheme:{file}:{var}", var=var, scheme=framework.security[var], file=file
            )
            for var in sorted(framework.security)
        )
        nodes.extend(_config_node(c, file) for c in model.config)
        mounts = tuple(_mount(m, imports, file) for m in framework.mounts)
        return PartialGraph(nodes=tuple(nodes), router_mounts=mounts)
