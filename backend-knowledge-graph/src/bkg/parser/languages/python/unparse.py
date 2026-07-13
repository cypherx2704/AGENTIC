"""Canonical expression unparser over the tree-sitter Python CST.

The graph stores ``ast.unparse``-style text for almost every string it emits
(annotations, defaults, ``include_router`` targets, base classes, ``response_model``,
…). ``ast.unparse`` NORMALIZES its input — single-quoted strings, spaces around ``|``,
decimal-normalized numeric literals — so tree-sitter's raw ``node.text`` will NOT match
byte-for-byte. To keep the graph digest stable across machines, this module reproduces
``ast.unparse``'s formatting for the expression node types that reach stored facts.

It is validated directly against ``ast.unparse`` by ``tests/test_python_unparse.py``
(static corpus + hypothesis fuzz). Node types it does not canonicalize fall back to raw
text — a deliberate, test-surfaced boundary, since exotic expressions do not appear in
the stored facts. This is the ONLY place the shape of the tree-sitter grammar leaks in.
"""

from __future__ import annotations

from tree_sitter import Node

_BINOP_SPACED = True  # ast.unparse writes binary operators as ``a OP b`` (spaced)


def _text(node: Node) -> str:
    return node.text.decode("utf-8") if node.text is not None else ""


def _field(node: Node, name: str) -> Node | None:
    return node.child_by_field_name(name)


def string_value(node: Node) -> str | None:
    """The decoded VALUE of a text (non-bytes) string literal, or None for forms that
    need real escape decoding (f-strings, escaped non-raw strings, bytes) which do not
    appear in stored facts. Handles plain and raw (``r"..."``) literals."""
    prefix = ""
    inner: list[str] = []
    has_escape = has_interp = False
    for child in node.children:
        t = child.type
        if t == "string_start":
            prefix = _text(child).rstrip("\"'").lower()
        elif t in ("string_content", "escape_sequence"):
            inner.append(_text(child))
            has_escape = has_escape or t == "escape_sequence"
        elif t == "interpolation":
            has_interp = True
    if "f" in prefix or "b" in prefix or has_interp:
        return None
    text = "".join(inner)
    if "r" in prefix:  # raw: the literal characters ARE the value
        return text
    if has_escape:  # non-raw with escapes needs true decoding — rare, bail to raw
        return None
    return text


def _string_repr(node: Node) -> str | None:
    """``ast.unparse`` output for a string OR bytes literal (``repr(value)``), else None
    to fall back to raw text."""
    value = string_value(node)
    if value is not None:
        return repr(value)
    # bytes literal without escapes: repr the utf-8 bytes (matches ast for ascii bodies)
    prefix = ""
    inner: list[str] = []
    has_escape = has_interp = False
    for child in node.children:
        t = child.type
        if t == "string_start":
            prefix = _text(child).rstrip("\"'").lower()
        elif t in ("string_content", "escape_sequence"):
            inner.append(_text(child))
            has_escape = has_escape or t == "escape_sequence"
        elif t == "interpolation":
            has_interp = True
    if "b" in prefix and "r" not in prefix and "f" not in prefix and not has_escape and not has_interp:
        return repr("".join(inner).encode("utf-8"))
    return None


def unparse(node: Node) -> str:
    """Canonical ``ast.unparse``-equivalent text for a tree-sitter expression node."""
    t = node.type

    if t == "identifier":
        return _text(node)
    if t == "type":
        # annotation-context wrapper around a single expression — unwrap and recurse so
        # spacing/quotes normalize (raw text would not match ast.unparse).
        kids = node.named_children
        return unparse(kids[0]) if len(kids) == 1 else _text(node)
    if t == "generic_type":
        # ``ClassVar[str]`` / ``List[int]`` in TYPE position (subscript is used in expr
        # position) -> base + a type_parameter child holding the ``[...]`` args.
        base = node.named_children[0]
        tp = next((c for c in node.named_children if c.type == "type_parameter"), None)
        inner = ", ".join(unparse(c) for c in tp.named_children) if tp else ""
        return f"{unparse(base)}[{inner}]"
    if t == "type_parameter":
        return ", ".join(unparse(c) for c in node.named_children)
    if t == "dotted_name":
        return ".".join(_text(c) for c in node.named_children)
    if t == "attribute":
        obj = _field(node, "object")
        attr = _field(node, "attribute")
        return f"{unparse(obj)}.{_text(attr)}" if obj and attr else _text(node)
    if t == "call":
        func = _field(node, "function")
        args = _field(node, "arguments")
        return f"{unparse(func) if func else ''}({_arglist(args)})"
    if t == "keyword_argument":
        name = _field(node, "name")
        value = _field(node, "value")
        return f"{_text(name)}={unparse(value)}" if name and value else _text(node)
    if t == "subscript":
        value = _field(node, "value")
        indices = node.children_by_field_name("subscript")
        inner = ", ".join(unparse(i) for i in indices)
        return f"{unparse(value) if value else ''}[{inner}]"
    if t == "binary_operator":
        left, op, right = _field(node, "left"), _field(node, "operator"), _field(node, "right")
        if left and op and right:
            sep = f" {_text(op)} " if _BINOP_SPACED else _text(op)
            return f"{unparse(left)}{sep}{unparse(right)}"
        return _text(node)
    if t == "boolean_operator":
        left, op, right = _field(node, "left"), _field(node, "operator"), _field(node, "right")
        if left and op and right:
            return f"{unparse(left)} {_text(op)} {unparse(right)}"
        return _text(node)
    if t == "unary_operator":
        op = _field(node, "operator")
        operand = _field(node, "argument")
        if op and operand:
            return f"{_text(op)}{unparse(operand)}"
        return _text(node)
    if t == "not_operator":
        arg = _field(node, "argument")
        return f"not {unparse(arg)}" if arg else _text(node)
    if t == "string":
        r = _string_repr(node)
        return r if r is not None else _text(node)
    if t == "concatenated_string":
        values = [string_value(c) for c in node.named_children if c.type == "string"]
        if values and all(v is not None for v in values):  # implicit concat folds to one literal
            return repr("".join(v for v in values if v is not None))
        return _text(node)
    if t == "integer":
        try:
            return str(int(_text(node), 0))
        except ValueError:
            return _text(node)
    if t == "float":
        try:
            return repr(float(_text(node)))
        except ValueError:
            return _text(node)
    if t == "true":
        return "True"
    if t == "false":
        return "False"
    if t == "none":
        return "None"
    if t == "ellipsis":
        return "..."
    if t == "list":
        return "[" + ", ".join(unparse(c) for c in node.named_children) + "]"
    if t == "set":
        return "{" + ", ".join(unparse(c) for c in node.named_children) + "}"
    if t == "tuple":
        elts = node.named_children
        if len(elts) == 0:
            return "()"
        if len(elts) == 1:
            return f"({unparse(elts[0])},)"
        return "(" + ", ".join(unparse(c) for c in elts) + ")"
    if t == "dictionary":
        return "{" + ", ".join(_pair(c) for c in node.named_children) + "}"
    if t == "parenthesized_expression":
        kids = node.named_children
        return unparse(kids[0]) if len(kids) == 1 else _text(node)
    return _text(node)


def _arglist(args: Node | None) -> str:
    """Render a call's arguments the way ``ast.unparse`` does: positional args (incl.
    ``*args``) in source order first, then keyword args (incl. ``**kw``). ast groups by
    ``Call.args`` vs ``Call.keywords``, so a keyword written before a ``*args`` still
    prints after it."""
    if args is None:
        return ""
    positional: list[str] = []
    keyword: list[str] = []
    for child in args.named_children:
        if child.type == "list_splat":
            positional.append("*" + unparse(child.named_children[0]))
        elif child.type == "dictionary_splat":
            keyword.append("**" + unparse(child.named_children[0]))
        elif child.type == "keyword_argument":
            keyword.append(unparse(child))
        else:
            positional.append(unparse(child))
    return ", ".join(positional + keyword)


def _pair(node: Node) -> str:
    if node.type == "dictionary_splat":
        return "**" + unparse(node.named_children[0])
    key = _field(node, "key")
    value = _field(node, "value")
    return f"{unparse(key)}: {unparse(value)}" if key and value else _text(node)
