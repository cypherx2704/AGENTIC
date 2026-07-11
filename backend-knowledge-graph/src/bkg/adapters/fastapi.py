"""FastAPI adapter — extract structural facts from Python source via stdlib ``ast``.

Emits FILE-LOCAL facts only: routes (from ``@router.get(...)`` decorators),
``include_router`` mounts, and imports. Cross-file router resolution + prefix /
middleware assembly happen at QUERY time in the pipeline (never eager cross-file
edges), so single-file incrementality holds. ``extract`` is a pure, deterministic
function of the source text; the engine's memoized ``fileFacts`` query gives
incrementality without needing incremental parsing.
"""

from __future__ import annotations

import ast
from typing import Any

_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})

Facts = dict[str, Any]


def _str_const(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _route_from_decorator(dec: ast.expr, func: ast.FunctionDef | ast.AsyncFunctionDef) -> Facts | None:
    # @router.get("/x") -> Call(func=Attribute(value=Name('router'), attr='get'), args=[Constant('/x')])
    if not isinstance(dec, ast.Call):
        return None
    f = dec.func
    if not isinstance(f, ast.Attribute) or not isinstance(f.value, ast.Name):
        return None
    method = f.attr.lower()
    if method not in _HTTP_METHODS:
        return None
    path = _str_const(dec.args[0]) if dec.args else None
    if path is None:  # non-literal path (variable / f-string) -> deferred
        return None
    return {
        "router": f.value.id,
        "method": method.upper(),
        "path": path,
        "handler": func.name,
        "line": func.lineno,
    }


def _mount_from_call(node: ast.Call) -> Facts | None:
    # X.include_router(Y, prefix="...")
    f = node.func
    if not isinstance(f, ast.Attribute) or f.attr != "include_router" or not isinstance(f.value, ast.Name):
        return None
    if not node.args:
        return None
    prefix = ""
    for kw in node.keywords:
        if kw.arg == "prefix":
            p = _str_const(kw.value)
            if p is not None:
                prefix = p
    return {"router_local": f.value.id, "prefix": prefix, "target_expr": ast.unparse(node.args[0])}


def extract(source: str) -> Facts:
    """Parse FastAPI source into deterministic, canonical-friendly file-local facts."""
    if source.startswith("﻿"):  # tolerate a leading UTF-8 BOM
        source = source[1:]
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {"routes": [], "mounts": [], "imports": {}, "partial": True}

    routes: list[Facts] = []
    mounts: list[Facts] = []
    imports: dict[str, Facts] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for dec in node.decorator_list:
                route = _route_from_decorator(dec, node)
                if route is not None:
                    routes.append(route)
        elif isinstance(node, ast.Call):
            mount = _mount_from_call(node)
            if mount is not None:
                mounts.append(mount)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                local = alias.asname or alias.name
                imports[local] = {"module": node.module or "", "name": alias.name, "level": node.level}
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    imports[alias.asname] = {"module": alias.name, "name": None, "level": 0}
                else:
                    top = alias.name.split(".")[0]
                    imports.setdefault(top, {"module": top, "name": None, "level": 0})

    routes.sort(key=lambda r: (r["router"], r["method"], r["path"]))
    mounts.sort(key=lambda m: (m["router_local"], m["target_expr"], m["prefix"]))
    return {"routes": routes, "mounts": mounts, "imports": imports}


# ---------------------------------------------------------------- resolution
def _abs_module(owner_file: str, module: str, level: int) -> str:
    """Resolve a (possibly relative) import module to an absolute dotted module,
    using the owner file's package for relative (`from .x import y`) imports."""
    if level == 0:
        return module
    stem = owner_file[:-3] if owner_file.endswith(".py") else owner_file
    package = stem.split("/")[:-1]  # drop the module's own filename
    up = level - 1
    base = package[: len(package) - up] if up <= len(package) else []
    return ".".join([*base, module]) if module else ".".join(base)


def _module_to_file(module: str) -> str:
    return module.replace(".", "/") + ".py" if module else ""


def resolve_target(target_expr: str, imports: dict[str, Any], owner_file: str) -> str | None:
    """Resolve an ``include_router`` argument expression to a ``{file}:{symbol}``
    router id, using this file's imports. Returns None if it can't be resolved
    (dynamic expression), in which case the mount is dropped."""
    if "." in target_expr:
        base, attr = target_expr.split(".", 1)
        imp = imports.get(base)
        if imp is None:
            return None
        module = _abs_module(owner_file, imp["module"], imp["level"])
        # `from pkg import base` binds `base` to the submodule `pkg.base`
        full_module = f"{module}.{imp['name']}" if imp.get("name") else module
        return f"{_module_to_file(full_module)}:{attr}"

    imp = imports.get(target_expr)
    if imp is None:  # a locally-defined router mounted in the same file
        return f"{owner_file}:{target_expr}"
    module = _abs_module(owner_file, imp["module"], imp["level"])
    symbol = imp.get("name") or target_expr
    return f"{_module_to_file(module)}:{symbol}"
