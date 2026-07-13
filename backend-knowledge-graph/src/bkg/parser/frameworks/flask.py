"""Flask ``FrameworkAdapter`` — neutral ``SemanticModel`` -> ``FrameworkFacts``.

Extracts Flask facts: ``@app.route``/method shortcuts + ``register_blueprint`` mounts,
with ``<int:x>`` -> ``{x}`` (and ``<path:name>`` -> ``{name:path}``) normalization. A
blueprint is modeled as a router, so the pipeline's cross-file resolution treats it like
a FastAPI router. Flask routes/mounts carry NO ``tags`` (``tags=None``), and Flask
contributes no middleware/security.
"""

from __future__ import annotations

import re

from ..analysis import MountFact, ParamFact, RouteFact
from ..base import Expr, FrameworkFacts, ParamInfo, SemanticModel

_METHOD_SHORTCUTS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
_CONVERTER = re.compile(r"<(?:([^:<>]+):)?([^<>]+)>")


def _normalize_path(path: str) -> str:
    """``<int:user_id>`` -> ``{user_id}``; ``<path:name>`` -> ``{name:path}`` (keeps the
    multi-segment converter so runtime matching spans ``/``)."""

    def repl(m: re.Match[str]) -> str:
        converter, name = m.group(1), m.group(2)
        return f"{{{name}:path}}" if converter == "path" else f"{{{name}}}"

    return _CONVERTER.sub(repl, path)


def _param_fact(p: ParamInfo) -> ParamFact:
    # Flask handlers have no Depends injection; reuse the same shape (depends always None).
    return ParamFact(
        name=p.name,
        annotation=p.annotation.source if p.annotation is not None else None,
        has_default=p.default is not None,
        depends=None,
    )


def _route_of(dec: Expr) -> tuple[str, list[str]] | None:
    """(path, methods) for a Flask route decorator, else None."""
    if dec.kind != "call" or dec.receiver is None:
        return None
    attr = (dec.name or "").lower()
    path = dec.args[0].str_value if dec.args else None
    if path is None:
        for kw in dec.keywords:  # @bp.route(rule="/x")
            if kw.arg == "rule":
                path = kw.value.str_value
    if path is None:
        return None
    if attr in _METHOD_SHORTCUTS:  # @app.get("/x") (Flask 2.0+)
        return path, [attr.upper()]
    if attr == "route":  # @app.route("/x", methods=[...]) — defaults to GET
        methods_kw = next((kw for kw in dec.keywords if kw.arg == "methods"), None)
        if methods_kw is not None:
            methods = sorted({m.upper() for m in (methods_kw.value.str_items or ())})
        else:
            methods = []
        return path, methods or ["GET"]
    return None


def _routes(model: SemanticModel) -> list[RouteFact]:
    out: list[RouteFact] = []
    for fn in model.functions:
        params = tuple(_param_fact(p) for p in fn.params)
        ret = fn.return_annotation.source if fn.return_annotation is not None else None
        for dec in fn.decorators:
            parsed = _route_of(dec)
            if parsed is None:
                continue
            path, methods = parsed
            for method in methods:
                out.append(
                    RouteFact(
                        router=dec.receiver or "",
                        method=method,
                        path=_normalize_path(path),
                        response_model=None,
                        tags=None,  # Flask routes have no tags concept
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
        if c.kind != "call" or c.name != "register_blueprint" or c.receiver is None or not c.args:
            continue
        prefix = ""
        for kw in c.keywords:
            if kw.arg == "url_prefix" and kw.value.str_value is not None:
                prefix = _normalize_path(kw.value.str_value)
        out.append(
            MountFact(router_local=c.receiver, prefix=prefix, target_expr=c.args[0].source, tags=None)
        )
    return out


class FlaskAdapter:
    name = "flask"

    def applies(self, model: SemanticModel) -> bool:
        return any((imp.module or "").split(".")[0] == "flask" for imp in model.imports.values())

    def extract(self, model: SemanticModel) -> FrameworkFacts:
        return FrameworkFacts(routes=tuple(_routes(model)), mounts=tuple(_mounts(model)))
