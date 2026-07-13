"""FastAPI ``FrameworkAdapter`` — neutral ``SemanticModel`` -> ``FrameworkFacts``.

Extracts FastAPI facts (routes, ``include_router`` mounts, middleware, security schemes)
from the neutral model ONLY — it never touches a tree-sitter tree or a Python ``ast``.
Routes/mounts are left UNSORTED (the registry applies the canonical sort); middlewares
are sorted here so a file's middleware order is source-order and deterministic.
"""

from __future__ import annotations

from ..analysis import MiddlewareFact, MountFact, ParamFact, RouteFact
from ..base import Expr, FrameworkFacts, ParamInfo, SemanticModel

_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})

# fastapi.security constructors -> normalized scheme type (mirrors the legacy map).
_SECURITY_SCHEMES = {
    "OAuth2PasswordBearer": "oauth2",
    "OAuth2AuthorizationCodeBearer": "oauth2",
    "OAuth2": "oauth2",
    "HTTPBearer": "bearer",
    "HTTPBasic": "basic",
    "HTTPDigest": "digest",
    "APIKeyHeader": "api-key",
    "APIKeyQuery": "api-key",
    "APIKeyCookie": "api-key",
}


def _is_depends(expr: Expr | None) -> bool:
    return expr is not None and expr.kind == "call" and expr.name in ("Depends", "Security")


def _depends_target(expr: Expr) -> str:
    return expr.args[0].source if expr.args else (expr.name or "")


def _param_fact(p: ParamInfo) -> ParamFact:
    depends: str | None = None
    if _is_depends(p.default):
        assert p.default is not None
        depends = _depends_target(p.default)
    if depends is None:
        for meta in p.annotation_metadata:
            if _is_depends(meta):
                depends = _depends_target(meta)
                break
    return ParamFact(
        name=p.name,
        annotation=p.annotation.source if p.annotation is not None else None,
        has_default=p.default is not None,
        depends=depends,
    )


def _route_bases(dec: Expr) -> list[tuple[str, str, str | None, tuple[str, ...]]]:
    """(method, path, response_model, tags) tuples for one decorator; empty if not a
    route. A method decorator yields one; ``api_route(methods=[...])`` yields one each."""
    if dec.kind != "call" or dec.receiver is None:
        return []
    attr = (dec.name or "").lower()
    path = dec.args[0].str_value if dec.args else None
    response_model: str | None = None
    methods: tuple[str, ...] | None = None
    tags: tuple[str, ...] = ()
    for kw in dec.keywords:
        if kw.arg == "path" and path is None:
            path = kw.value.str_value
        elif kw.arg == "response_model":
            response_model = kw.value.source
        elif kw.arg == "methods":
            methods = kw.value.str_items or ()
        elif kw.arg == "tags":
            tags = tuple(sorted(set(kw.value.str_items or ())))
    if path is None:
        return []
    if attr in _HTTP_METHODS:
        return [(attr.upper(), path, response_model, tags)]
    if attr == "api_route" and methods is not None:
        return [(m.upper(), path, response_model, tags) for m in methods]
    if attr == "websocket":
        return [("WEBSOCKET", path, None, tags)]
    return []


def _routes(model: SemanticModel) -> list[RouteFact]:
    out: list[RouteFact] = []
    for fn in model.functions:
        params = tuple(_param_fact(p) for p in fn.params)
        ret = fn.return_annotation.source if fn.return_annotation is not None else None
        for dec in fn.decorators:
            for method, path, response_model, tags in _route_bases(dec):
                out.append(
                    RouteFact(
                        router=dec.receiver or "",
                        method=method,
                        path=path,
                        response_model=response_model,
                        tags=tags,
                        handler=fn.name,
                        line=fn.line,
                        params=params,
                        return_annotation=ret,
                    )
                )
    return out


def _mounts(model: SemanticModel) -> list[MountFact]:
    out: list[MountFact] = []
    for c in model.calls:
        if c.kind != "call" or c.name != "include_router" or c.receiver is None or not c.args:
            continue
        prefix = ""
        tags: tuple[str, ...] = ()
        for kw in c.keywords:
            if kw.arg == "prefix" and kw.value.str_value is not None:
                prefix = kw.value.str_value
            elif kw.arg == "tags":
                tags = tuple(sorted(set(kw.value.str_items or ())))
        out.append(MountFact(router_local=c.receiver, prefix=prefix, target_expr=c.args[0].source, tags=tags))
    return out


def _middlewares(model: SemanticModel) -> tuple[MiddlewareFact, ...]:
    out: list[MiddlewareFact] = []
    for c in model.calls:
        if c.kind == "call" and c.name == "add_middleware" and c.receiver is not None and c.args:
            out.append(MiddlewareFact(router_local=c.receiver, name=c.args[0].source, line=c.line))
    for fn in model.functions:
        for dec in fn.decorators:
            if dec.kind == "call" and dec.name == "middleware" and dec.receiver is not None:
                out.append(MiddlewareFact(router_local=dec.receiver, name=fn.name, line=fn.line))
    return tuple(sorted(out, key=lambda m: (m.router_local, m.line, m.name)))


def _security(model: SemanticModel) -> dict[str, str]:
    out: dict[str, str] = {}
    for a in model.assignments:
        if a.value.kind == "call" and a.value.name in _SECURITY_SCHEMES:
            scheme = _SECURITY_SCHEMES[a.value.name]
            for target in a.targets:
                out[target] = scheme
    return out


class FastApiAdapter:
    name = "fastapi"

    def applies(self, model: SemanticModel) -> bool:
        return any((imp.module or "").split(".")[0] == "fastapi" for imp in model.imports.values())

    def extract(self, model: SemanticModel) -> FrameworkFacts:
        return FrameworkFacts(
            routes=tuple(_routes(model)),
            mounts=tuple(_mounts(model)),
            middlewares=_middlewares(model),
            security=_security(model),
        )
