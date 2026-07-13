"""Python-specific cross-file reference resolution (repository contents only).

This is where Python's import + typing semantics live — deliberately INSIDE the parser
plugin, so the graph engine/pipeline stay language-neutral. The parser uses these to
pre-compute the ``{file}:{symbol}`` CANDIDATE ids for every cross-file reference (a
route's DTO, a schema base, a nested field type, an auth scheme); the engine then wires
the first candidate whose file exists — no import semantics leak into the graph.

Ported from the retired stdlib-``ast`` adapter, with one change: annotation peeling now
runs on tree-sitter (the universal syntax layer), so this module never imports ``ast``.
"""

from __future__ import annotations

from collections.abc import Mapping

import tree_sitter_python as tsp
from tree_sitter import Language, Node, Parser

from ...analysis import ImportFact
from .unparse import unparse

_LANGUAGE = Language(tsp.language())
_PARSER = Parser(_LANGUAGE)

# Scalars that are NOT project DTOs — an annotation peeling to one of these is not a body.
_BUILTIN_TYPES = frozenset(
    {
        "int", "str", "bool", "float", "bytes", "complex", "dict", "list", "set",
        "tuple", "frozenset", "None", "Any", "object", "bytearray", "datetime",
        "date", "time", "UUID", "Decimal",
    }
)

# type-annotation wrappers peeled to find a single model identifier
_SEQUENCE_WRAPPERS = frozenset(
    {"List", "list", "Sequence", "Iterable", "Set", "set", "FrozenSet", "frozenset", "Tuple", "tuple"}
)

# fastapi.security constructors -> normalized scheme type.
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


# ------------------------------------------------------------ import -> file:symbol
def _abs_module(owner_file: str, module: str, level: int) -> str:
    """Resolve a (possibly relative) import module to an absolute dotted module, using
    the owner file's package for relative (`from .x import y`) imports."""
    if level == 0:
        return module
    stem = owner_file[:-3] if owner_file.endswith(".py") else owner_file
    package = stem.split("/")[:-1]  # drop the module's own filename
    up = level - 1
    base = package[: len(package) - up] if up <= len(package) else []
    return ".".join([*base, module]) if module else ".".join(base)


def _module_to_file(module: str) -> str:
    return module.replace(".", "/") + ".py" if module else ""


def resolve_target(target_expr: str, imports: Mapping[str, ImportFact], owner_file: str) -> str | None:
    """Resolve a symbol expression (an ``include_router`` argument or a DTO name) to a
    naive ``{file}:{symbol}`` id, using this file's imports. Longest imported prefix
    wins. A name with no matching import is treated as locally defined in ``owner_file``.
    The file part is naive (``module -> path.py``); the caller reconciles it against the
    real file set (incl. package ``__init__.py``) via :func:`candidates`."""
    parts = target_expr.split(".")
    for i in range(len(parts), 0, -1):
        imp = imports.get(".".join(parts[:i]))
        if imp is None:
            continue
        rest = parts[i:]
        module = _abs_module(owner_file, imp.module, imp.level)
        if imp.name:  # `from module import name`
            if rest:
                full = ".".join([module, imp.name, *rest[:-1]])
                return f"{_module_to_file(full)}:{rest[-1]}"
            return f"{_module_to_file(module)}:{imp.name}"
        if rest:  # `import module` (module is the full dotted path)
            full = ".".join([module, *rest[:-1]])
            return f"{_module_to_file(full)}:{rest[-1]}"
        return f"{_module_to_file(module)}:{parts[-1]}"
    return f"{owner_file}:{parts[-1]}"


def candidates(naive_id: str | None) -> tuple[str, ...]:
    """Ordered ``{file}:{symbol}`` candidates for a naive id: the id itself, then the
    package ``__init__.py`` variant (Python package convention). The engine picks the
    first whose file exists — this is the ONLY place ``.py``/``__init__.py`` lives."""
    if naive_id is None:
        return ()
    file, symbol = naive_id.rsplit(":", 1)
    out = [naive_id]
    if file.endswith(".py"):
        out.append(f"{file[:-3]}/__init__.py:{symbol}")
    return tuple(out)


def resolve_symbol(expr: str, imports: Mapping[str, ImportFact], owner_file: str) -> tuple[str, ...]:
    return candidates(resolve_target(expr, imports, owner_file))


def resolve_dto(
    annotation: str | None, imports: Mapping[str, ImportFact], owner_file: str
) -> tuple[str, ...]:
    """Candidate DTO ids for a type annotation, or () if it is not an in-project model
    (a scalar/builtin or unpeelable)."""
    name = peel_model_name(annotation)
    if name is None or name in _BUILTIN_TYPES:
        return ()
    return resolve_symbol(name, imports, owner_file)


# --------------------------------------------------------------- annotation peeling
def _peel(node: Node) -> str | None:
    """Peel ``list[X]`` / ``Optional[X]`` / ``X | None`` / ``Annotated[X, ...]`` down to
    a single model identifier — the tree-sitter analogue of the legacy ast peeler."""
    t = node.type
    if t == "identifier":
        return node.text.decode("utf-8") if node.text else None
    if t == "attribute":
        return unparse(node)  # dotted, e.g. schemas.UserOut — resolve_target handles it
    if t == "subscript":
        value = node.child_by_field_name("value")
        base = value.text.decode("utf-8") if value is not None and value.text else None
        indices = node.children_by_field_name("subscript")
        if base in ("Annotated", "Optional") or base in _SEQUENCE_WRAPPERS:
            return _peel(indices[0]) if indices else None
        return None
    if t == "binary_operator":
        op = node.child_by_field_name("operator")
        if op is None or (op.text or b"").decode() != "|":
            return None
        left_n, right_n = node.child_by_field_name("left"), node.child_by_field_name("right")
        left = _peel(left_n) if left_n is not None else None
        right = _peel(right_n) if right_n is not None else None
        if left in (None, "None"):
            return right if right != "None" else None
        if right in (None, "None"):
            return left
        return left  # X | Y (two models) — best-effort, first wins
    if t == "none":
        return "None"
    return None


def peel_model_name(annotation: str | None) -> str | None:
    """Extract a single model identifier from a type annotation string."""
    if not annotation:
        return None
    if annotation.isidentifier():
        return annotation
    tree = _PARSER.parse(bytes(annotation, "utf-8"))
    stmt = tree.root_node.named_children[0] if tree.root_node.named_children else None
    if stmt is None or not stmt.named_children:
        return None
    return _peel(stmt.named_children[0])


# ------------------------------------------------------------------- security scheme
def scheme_of(expr: str) -> str | None:
    """Classify an inline security-scheme constructor expression (``HTTPBearer()``,
    ``fastapi.security.OAuth2PasswordBearer(...)``) -> scheme type, else None."""
    if "(" not in expr:
        return None
    callee = expr.split("(", 1)[0].strip().split(".")[-1]
    return _SECURITY_SCHEMES.get(callee)
