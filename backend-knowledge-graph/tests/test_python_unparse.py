"""Oracle for the tree-sitter canonical unparser: it must equal ``ast.unparse`` for the
expression forms that reach stored facts (annotations / defaults / call args / bases).

``ast.unparse`` is the ground truth the legacy adapter used, so byte-parity of the whole
migration rests on this. A static corpus pins the known-important forms; a hypothesis
strategy fuzzes nested type/call/literal expressions to catch formatting drift.
"""

from __future__ import annotations

import ast

import pytest
import tree_sitter_python as tsp
from hypothesis import given
from hypothesis import strategies as st
from tree_sitter import Language, Parser

from bkg.parser.languages.python.unparse import unparse

_LANG = Language(tsp.language())
_PARSER = Parser(_LANG)


def _ts_expr(src: str):
    tree = _PARSER.parse(bytes(src, "utf-8"))
    stmt = tree.root_node.named_children[0]
    return stmt.named_children[0]


def _both(src: str) -> tuple[str, str]:
    want = ast.unparse(ast.parse(src, mode="eval").body)
    got = unparse(_ts_expr(src))
    return want, got


CASES = [
    "UserOut", "users.router", "fastapi.security.OAuth2PasswordBearer",
    "list[UserOut]", "Optional[User]", "List[int]", "Dict[str, int]",
    "dict[str, list[int]]", "Callable[[int], str]", "Annotated[User, Depends(f)]",
    "x | None", "User | None", "int | str | None",
    "Field(default_factory=list)", "Field(...)", "Field(default='user')", "Field('anon')",
    "Depends(get_db)", "Security(scopes)", "func(a, b, key=1, *args, **kw)", "func(key=1, *args)",
    '"anon"', "'anon'", "0", "10", "0x10", "1_000", "1.0", "1e3", "-1", "+5", "~3",
    "True", "False", "None", "...", "b'bytes'", 'b"bytes"', 'r"raw"', r'r"\d+"',
    "[1, 2, 3]", '["a", "b"]', "(1, 2)", "(1,)", "()", "{'a': 1}", "{1, 2}",
    "not x", "a and b", "a or b", "x == 1", '"a" "b"', "datetime", "uuid.UUID",
]


@pytest.mark.parametrize("src", CASES)
def test_unparse_matches_ast(src: str) -> None:
    want, got = _both(src)
    assert got == want, f"{src!r}: ast={want!r} ts={got!r}"


# annotation position uses ``type``/``generic_type`` nodes (not bare expr) — verify those
ANNOTATIONS = [
    "int", "str", "list", "List[int]", "Optional[User]", "ClassVar[str]", "ClassVar",
    "dict[str, int]", "User | None", "Annotated[User, Depends(f)]", "Callable[[int], str]",
]


@pytest.mark.parametrize("ann", ANNOTATIONS)
def test_unparse_annotation_matches_ast(ann: str) -> None:
    want = ast.unparse(ast.parse(f"x: {ann}").body[0].annotation)
    tree = _PARSER.parse(bytes(f"x: {ann}", "utf-8"))
    type_node = tree.root_node.named_children[0].named_children[0].child_by_field_name("type")
    assert unparse(type_node) == want, f"{ann!r}: ast={want!r} ts={unparse(type_node)!r}"


# --- fuzz: build nested type/call/literal expressions and compare to ast.unparse ---
_ATOMS = st.sampled_from(["User", "int", "str", "x", "None", "0", "1_000", "'s'", '"d"', "True", "..."])


@st.composite
def _expr(draw, depth: int = 3):
    if depth <= 0:
        return draw(_ATOMS)
    kind = draw(st.integers(min_value=0, max_value=6))
    if kind == 0:
        return draw(_ATOMS)
    if kind == 1:  # subscript
        base = draw(st.sampled_from(["list", "Optional", "dict", "List", "Annotated"]))
        n = draw(st.integers(min_value=1, max_value=2))
        return f"{base}[{', '.join(draw(_expr(depth - 1)) for _ in range(n))}]"
    if kind == 2:  # union
        return f"{draw(_expr(depth - 1))} | {draw(_expr(depth - 1))}"
    if kind == 3:  # call
        callee = draw(st.sampled_from(["Depends", "Field", "f", "g.h"]))
        n = draw(st.integers(min_value=0, max_value=2))
        return f"{callee}({', '.join(draw(_expr(depth - 1)) for _ in range(n))})"
    if kind == 4:  # attribute
        return f"{draw(st.sampled_from(['a', 'pkg', 'mod']))}.{draw(st.sampled_from(['x', 'Y', 'router']))}"
    if kind == 5:  # list
        n = draw(st.integers(min_value=0, max_value=3))
        return f"[{', '.join(draw(_expr(depth - 1)) for _ in range(n))}]"
    return f"kw({draw(st.sampled_from(['a', 'b']))}={draw(_expr(depth - 1))})"  # keyword call


@given(_expr())
def test_unparse_fuzz_matches_ast(src: str) -> None:
    try:
        want = ast.unparse(ast.parse(src, mode="eval").body)
    except SyntaxError:
        return  # generator produced an invalid expr; skip
    assert unparse(_ts_expr(src)) == want, f"{src!r}"
