"""Python ``SemanticResolver`` — tree-sitter tree -> language-neutral ``SemanticModel``.

Walks the tree-sitter CST to extract imports / classes-as-schemas / config surface /
functions / calls / assignments, using REPOSITORY CONTENTS ONLY (no installed packages,
no execution, no environment resolution). Every emitted string goes through the canonical
:mod:`~bkg.parser.languages.python.unparse`, giving stable ``ast.unparse``-equivalent text.

Two ordering fidelities matter: (1) file traversal is BFS (so the env-var dedup keeps a
deterministic first-seen read); (2) all list outputs (schemas, config) are sorted.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from dataclasses import replace
from types import MappingProxyType

from tree_sitter import Node, Tree

from ...analysis import ConfigFact, FieldFact, ImportFact, SchemaFact
from ...base import AssignInfo, Expr, FunctionInfo, Keyword, ParamInfo, SemanticModel

_SETTINGS_BASES = frozenset({"BaseSettings"})
_KIND = {
    "identifier": "name",
    "attribute": "attribute",
    "call": "call",
    "string": "string",
    "list": "collection",
    "tuple": "collection",
    "set": "collection",
    "none": "none",
    "ellipsis": "ellipsis",
}


def _text(node: Node | None) -> str:
    return node.text.decode("utf-8") if node is not None and node.text is not None else ""


def _unwrap_type(node: Node) -> Node:
    """Annotation position wraps the expression in a ``type`` node; unwrap to the real
    expression so it decodes/pattern-matches like an ordinary expr."""
    if node.type == "type" and node.named_children:
        return node.named_children[0]
    return node


def _walk(node: Node) -> Iterator[Node]:
    """BFS over named children — mirrors ``ast.walk`` traversal order."""
    todo: deque[Node] = deque([node])
    while todo:
        n = todo.popleft()
        todo.extend(n.named_children)
        yield n


def _attr_name(node: Node) -> str:
    return _text(node.child_by_field_name("attribute"))


def _callee_name(call: Node) -> str | None:
    func = call.child_by_field_name("function")
    if func is None:
        return None
    if func.type == "identifier":
        return _text(func)
    if func.type == "attribute":
        return _attr_name(func)
    return None


def _positional_args(arglist: Node | None) -> list[Node]:
    if arglist is None:
        return []
    return [
        c
        for c in arglist.named_children
        if c.type not in ("keyword_argument", "list_splat", "dictionary_splat")
    ]


# ------------------------------------------------------------------- neutral Expr
def _expr(node: Node) -> Expr:
    """Build the language-neutral :class:`Expr` for a tree-sitter expression node."""
    from .unparse import string_value, unparse

    node = _unwrap_type(node)
    t = node.type
    src = unparse(node)
    line = node.start_point[0] + 1
    name = dotted = receiver = str_value = None
    str_items: tuple[str, ...] | None = None
    args: tuple[Expr, ...] = ()
    keywords: tuple[Keyword, ...] = ()

    if t == "identifier":
        name = _text(node)
    elif t == "attribute":
        dotted = src
        name = _attr_name(node)
        obj = node.child_by_field_name("object")
        if obj is not None and obj.type == "identifier":
            receiver = _text(obj)
    elif t == "call":
        func = node.child_by_field_name("function")
        dotted = unparse(func) if func is not None else None
        name = _callee_name(node)
        if func is not None and func.type == "attribute":
            obj = func.child_by_field_name("object")
            if obj is not None and obj.type == "identifier":
                receiver = _text(obj)
        arglist = node.child_by_field_name("arguments")
        pos: list[Expr] = []
        kws: list[Keyword] = []
        if arglist is not None:
            for c in arglist.named_children:
                if c.type == "keyword_argument":
                    kn = c.child_by_field_name("name")
                    kv = c.child_by_field_name("value")
                    kws.append(Keyword(_text(kn) if kn else None, _expr(kv) if kv else _expr(c)))
                elif c.type in ("list_splat", "dictionary_splat"):
                    continue
                else:
                    pos.append(_expr(c))
        args, keywords = tuple(pos), tuple(kws)
    elif t == "string":
        str_value = string_value(node)
    elif t in ("list", "tuple", "set"):
        items = [string_value(c) for c in node.named_children if c.type == "string"]
        str_items = tuple(v for v in items if v is not None)

    return Expr(
        source=src,
        kind=_KIND.get(t, "other"),
        line=line,
        name=name,
        dotted=dotted,
        receiver=receiver,
        str_value=str_value,
        str_items=str_items,
        is_none=t == "none",
        is_ellipsis=t == "ellipsis",
        args=args,
        keywords=keywords,
    )


# ----------------------------------------------------------------------- imports
def _dotted(node: Node | None) -> str:
    return _text(node)


def _module_and_level(mod: Node | None) -> tuple[str, int]:
    if mod is None:
        return "", 0
    if mod.type == "relative_import":
        prefix = next((c for c in mod.children if c.type == "import_prefix"), None)
        dotted = next((c for c in mod.named_children if c.type == "dotted_name"), None)
        return _dotted(dotted), len(_text(prefix))
    return _dotted(mod), 0


def _imports(nodes: list[Node]) -> dict[str, ImportFact]:
    imports: dict[str, ImportFact] = {}
    for n in nodes:
        if n.type == "import_statement":
            for name_node in n.children_by_field_name("name"):
                if name_node.type == "aliased_import":
                    nm = name_node.child_by_field_name("name")
                    alias = name_node.child_by_field_name("alias")
                    module = _dotted(nm)
                    imports[_text(alias) if alias else module] = ImportFact(module, None, 0)
                elif name_node.type == "dotted_name":
                    module = _dotted(name_node)
                    imports[module] = ImportFact(module, None, 0)
        elif n.type == "import_from_statement":
            module, level = _module_and_level(n.child_by_field_name("module_name"))
            for name_node in n.children_by_field_name("name"):
                if name_node.type == "aliased_import":
                    nm = name_node.child_by_field_name("name")
                    alias = name_node.child_by_field_name("alias")
                    imported = _dotted(nm)
                    imports[_text(alias) if alias else imported] = ImportFact(module, imported, level)
                elif name_node.type == "dotted_name":
                    imported = _dotted(name_node)
                    imports[imported] = ImportFact(module, imported, level)
                elif name_node.type == "wildcard_import":
                    imports["*"] = ImportFact(module, "*", level)
    return imports


# --------------------------------------------------------------------- functions
def _decorators(fn: Node) -> tuple[Expr, ...]:
    parent = fn.parent
    if parent is None or parent.type != "decorated_definition":
        return ()
    out = [
        _expr(c.named_children[0])
        for c in parent.children
        if c.type == "decorator" and c.named_children
    ]
    return tuple(out)


def _peel_annotated(type_node: Node) -> tuple[Node, list[Node]]:
    """``Annotated[T, m1, m2]`` -> ``(T, [m1, m2])``; anything else -> ``(node, [])``.
    Handles both the expr-form ``subscript`` and the annotation-form ``generic_type``."""
    actual = _unwrap_type(type_node)
    if actual.type == "subscript":
        value = actual.child_by_field_name("value")
        indices = actual.children_by_field_name("subscript")
        if value is not None and _text(value) == "Annotated" and indices:
            return indices[0], list(indices[1:])
    elif actual.type == "generic_type":
        base = actual.named_children[0]
        tp = next((c for c in actual.named_children if c.type == "type_parameter"), None)
        if _text(base) == "Annotated" and tp is not None and tp.named_children:
            elts = tp.named_children
            return elts[0], list(elts[1:])
    return actual, []


def _param_name(node: Node) -> str:
    named = node.child_by_field_name("name")
    if named is not None:
        return _text(named)
    ident = next((c for c in node.named_children if c.type == "identifier"), None)
    return _text(ident)


def _make_param(name: str, type_node: Node | None, value_node: Node | None) -> ParamInfo | None:
    if name in ("self", "cls"):
        return None
    annotation: Expr | None = None
    metadata: tuple[Expr, ...] = ()
    if type_node is not None:
        inner, meta = _peel_annotated(type_node)
        annotation = _expr(inner)
        metadata = tuple(_expr(m) for m in meta)
    default = _expr(value_node) if value_node is not None else None
    return ParamInfo(name=name, annotation=annotation, annotation_metadata=metadata, default=default)


def _params(params_node: Node | None) -> tuple[ParamInfo, ...]:
    if params_node is None:
        return ()
    out: list[ParamInfo] = []
    for c in params_node.named_children:
        t = c.type
        info: ParamInfo | None = None
        if t == "identifier":
            info = _make_param(_text(c), None, None)
        elif t == "typed_parameter":
            info = _make_param(_param_name(c), c.child_by_field_name("type"), None)
        elif t == "default_parameter":
            info = _make_param(_param_name(c), None, c.child_by_field_name("value"))
        elif t == "typed_default_parameter":
            info = _make_param(
                _param_name(c), c.child_by_field_name("type"), c.child_by_field_name("value")
            )
        # list_splat_pattern (*args) / dictionary_splat_pattern (**kw) / separators: excluded
        if info is not None:
            out.append(info)
    return tuple(out)


def _function(fn: Node) -> FunctionInfo:
    ret = fn.child_by_field_name("return_type")
    return FunctionInfo(
        name=_text(fn.child_by_field_name("name")),
        params=_params(fn.child_by_field_name("parameters")),
        return_annotation=_expr(ret) if ret is not None else None,
        decorators=_decorators(fn),
        line=fn.start_point[0] + 1,
    )


# ------------------------------------------------------------ classes / schemas
def _module_classes(root: Node) -> list[Node]:
    out: list[Node] = []
    for c in root.named_children:
        if c.type == "class_definition":
            out.append(c)
        elif c.type == "decorated_definition":
            inner = c.child_by_field_name("definition")
            if inner is not None and inner.type == "class_definition":
                out.append(inner)
    return out


def _class_bases(cls: Node) -> list[str]:
    from .unparse import unparse

    sup = cls.child_by_field_name("superclasses")
    if sup is None:
        return []
    return [unparse(c) for c in sup.named_children if c.type != "keyword_argument"]


def _field_required_default(value_node: Node | None) -> tuple[bool, str | None]:
    from .unparse import unparse

    if value_node is None:
        return True, None
    v = _unwrap_type(value_node)
    if v.type == "call" and _callee_name(v) == "Field":
        arglist = v.child_by_field_name("arguments")
        kwargs: dict[str, Node] = {}
        pos: list[Node] = []
        if arglist is not None:
            for c in arglist.named_children:
                if c.type == "keyword_argument":
                    kn = c.child_by_field_name("name")
                    kv = c.child_by_field_name("value")
                    if kn is not None and kv is not None:
                        kwargs[_text(kn)] = kv
                elif c.type in ("list_splat", "dictionary_splat"):
                    continue
                else:
                    pos.append(c)
        if "default_factory" in kwargs:
            return False, f"{unparse(kwargs['default_factory'])}()"
        if "default" in kwargs:
            return False, unparse(kwargs["default"])
        if pos:
            if pos[0].type == "ellipsis":
                return True, None
            return False, unparse(pos[0])
        return True, None
    return False, unparse(value_node)


def _model_fields(cls: Node) -> list[FieldFact]:
    from .unparse import unparse

    body = cls.child_by_field_name("body")
    if body is None:
        return []
    fields: list[FieldFact] = []
    for stmt in body.named_children:
        if stmt.type != "expression_statement" or not stmt.named_children:
            continue
        assign = stmt.named_children[0]
        if assign.type != "assignment":
            continue
        type_node = assign.child_by_field_name("type")
        left = assign.child_by_field_name("left")
        if type_node is None or left is None or left.type != "identifier":
            continue  # not an annotated field
        name = _text(left)
        annotation = unparse(type_node)
        if (
            name.startswith("_")
            or name == "model_config"
            or annotation == "ClassVar"
            or annotation.startswith("ClassVar[")
        ):
            continue
        required, default = _field_required_default(assign.child_by_field_name("right"))
        fields.append(FieldFact(name=name, type=annotation, required=required, default=default))
    return fields


def _schemas(root: Node) -> tuple[SchemaFact, ...]:
    schemas: list[SchemaFact] = []
    for cls in _module_classes(root):
        fields = _model_fields(cls)
        if fields:
            schemas.append(
                SchemaFact(
                    name=_text(cls.child_by_field_name("name")),
                    bases=tuple(_class_bases(cls)),
                    fields=tuple(fields),
                )
            )
    schemas.sort(key=lambda s: s.name)
    return tuple(schemas)


# ------------------------------------------------------------------------ config
def _is_environ(node: Node | None) -> bool:
    if node is None:
        return False
    return (node.type == "attribute" and _attr_name(node) == "environ") or (
        node.type == "identifier" and _text(node) == "environ"
    )


def _env_read(node: Node) -> ConfigFact | None:
    from .unparse import string_value, unparse

    line = node.start_point[0] + 1
    if node.type == "call":
        func = node.child_by_field_name("function")
        if func is None:
            return None
        is_getenv = (func.type == "attribute" and _attr_name(func) == "getenv") or (
            func.type == "identifier" and _text(func) == "getenv"
        )
        is_environ_get = (
            func.type == "attribute"
            and _attr_name(func) == "get"
            and _is_environ(func.child_by_field_name("object"))
        )
        if not (is_getenv or is_environ_get):
            return None
        pos = _positional_args(node.child_by_field_name("arguments"))
        if not pos or pos[0].type != "string":
            return None
        name = string_value(pos[0])
        if name is None:
            return None
        default = unparse(pos[1]) if len(pos) > 1 else None
        return ConfigFact("env", name, None, default, None, line)
    if node.type == "subscript":
        if not _is_environ(node.child_by_field_name("value")):
            return None
        indices = node.children_by_field_name("subscript")
        if not indices or indices[0].type != "string":
            return None
        name = string_value(indices[0])
        if name is None:
            return None
        return ConfigFact("env", name, None, None, None, line)
    return None


def _config(root: Node, nodes: list[Node]) -> tuple[ConfigFact, ...]:
    envs: dict[str, ConfigFact] = {}
    for n in nodes:  # BFS order == ast.walk: first read wins, later non-None default upgrades
        entry = _env_read(n)
        if entry is None:
            continue
        if entry.name not in envs:
            envs[entry.name] = entry
        elif envs[entry.name].default is None and entry.default is not None:
            envs[entry.name] = replace(envs[entry.name], default=entry.default)
    settings: list[ConfigFact] = []
    for cls in _module_classes(root):
        if {b.split(".")[-1] for b in _class_bases(cls)} & _SETTINGS_BASES:
            cls_name = _text(cls.child_by_field_name("name"))
            cls_line = cls.start_point[0] + 1
            for f in _model_fields(cls):
                settings.append(ConfigFact("setting", f.name, f.type, f.default, cls_name, cls_line))
    out = [*envs.values(), *settings]
    out.sort(key=lambda c: (c.kind, c.name, c.cls or "", c.line))
    return tuple(out)


# ------------------------------------------------------------ module assignments
def _assign_targets(left: Node | None) -> list[str]:
    if left is not None and left.type == "identifier":
        return [_text(left)]
    return []  # only simple Name targets are security-scheme definitions


def _module_assignments(root: Node) -> tuple[AssignInfo, ...]:
    out: list[AssignInfo] = []
    for c in root.named_children:
        if c.type != "expression_statement" or not c.named_children:
            continue
        node = c.named_children[0]
        if node.type != "assignment" or node.child_by_field_name("type") is not None:
            continue  # a typed assignment (AnnAssign) is not scanned for security schemes
        right = node.child_by_field_name("right")
        targets = _assign_targets(node.child_by_field_name("left"))
        if right is None or not targets:
            continue
        out.append(AssignInfo(targets=tuple(targets), value=_expr(right), line=c.start_point[0] + 1))
    return tuple(out)


class PythonSemanticResolver:
    def resolve(self, tree: Tree, path: str) -> SemanticModel:
        root = tree.root_node
        if root.has_error:
            return SemanticModel(path=path, partial=True)
        nodes = list(_walk(root))
        return SemanticModel(
            path=path,
            partial=False,
            imports=MappingProxyType(_imports(nodes)),
            schemas=_schemas(root),
            config=_config(root, nodes),
            functions=tuple(_function(n) for n in nodes if n.type == "function_definition"),
            calls=tuple(_expr(n) for n in nodes if n.type == "call"),
            assignments=_module_assignments(root),
        )
