"""Plugin contracts for the replaceable parser architecture + the language-neutral IR.

The parser is a per-file plugin pipeline:

    LanguageParser (tree-sitter)  ->  SemanticResolver  ->  FrameworkAdapter  ->  FactExtractor
         syntax tree                    SemanticModel        FrameworkFacts        PartialGraph

The load-bearing decoupling: a ``FrameworkAdapter`` reads a **language-neutral**
``SemanticModel`` (normalized imports / classes / functions / calls / assignments),
NOT a tree-sitter tree or a Python ``ast``. So the FastAPI adapter's route/mount logic
never mentions a concrete parser, and the graph engine never imports one either.
Adding a language = a new ``LanguageParser`` + ``SemanticResolver`` that populate the
same ``SemanticModel``. Adding a framework = a new ``FrameworkAdapter``.

Everything here is pure/deterministic and uses repository contents only — no installed
packages, no environment resolution, no code execution.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from ..protocol.models import PartialGraph
from .analysis import (
    ConfigFact,
    ImportFact,
    MiddlewareFact,
    MountFact,
    RouteFact,
    SchemaFact,
)

# An opaque, language-specific syntax tree (e.g. a tree-sitter ``Tree``). The neutral
# layers never inspect it; only a language's own SemanticResolver does.
SyntaxTree = Any


# --------------------------------------------------------------- neutral expression IR
@dataclass(frozen=True)
class Keyword:
    """A call keyword argument: ``arg=value`` (``arg`` is ``None`` for ``**kwargs``)."""

    arg: str | None
    value: Expr


@dataclass(frozen=True)
class Expr:
    """A normalized, language-neutral expression node — enough decoded structure for a
    framework adapter to pattern-match without touching the raw syntax tree.

    ``source`` is the canonical unparsed text (the ``ast.unparse`` equivalent), which is
    what the legacy facts store for annotations / target expressions. The remaining
    fields are pre-decoded conveniences populated when applicable.
    """

    source: str
    kind: str  # "name"|"attribute"|"call"|"string"|"collection"|"subscript"|"binop"|"none"|"ellipsis"|"other"
    line: int = 0
    name: str | None = None  # identifier (name) / last attribute / call's callee last name
    dotted: str | None = None  # full dotted callee or attribute path (e.g. "fastapi.Depends", "os.environ")
    receiver: str | None = None  # base Name of an attribute/method call (e.g. "router" in router.get)
    str_value: str | None = None  # decoded value if a string literal
    str_items: tuple[str, ...] | None = None  # decoded values if a list/tuple of string literals
    is_none: bool = False
    is_ellipsis: bool = False
    args: tuple[Expr, ...] = ()  # call positional args / subscript elements
    keywords: tuple[Keyword, ...] = ()  # call keyword args


# ------------------------------------------------------------ neutral structural IR
@dataclass(frozen=True)
class ParamInfo:
    """A function parameter. ``annotation`` is already peeled of ``Annotated[...]``;
    the ``Annotated`` metadata (which can carry a framework marker like ``Depends``) is
    preserved separately so the framework adapter can interpret it."""

    name: str
    annotation: Expr | None
    annotation_metadata: tuple[Expr, ...]
    default: Expr | None


@dataclass(frozen=True)
class FunctionInfo:
    name: str
    params: tuple[ParamInfo, ...]
    return_annotation: Expr | None
    decorators: tuple[Expr, ...]
    line: int


@dataclass(frozen=True)
class AssignInfo:
    """A module-level assignment ``targets = value`` (used e.g. to spot
    ``oauth2 = OAuth2PasswordBearer(...)`` security-scheme definitions)."""

    targets: tuple[str, ...]
    value: Expr
    line: int


_EMPTY_IMPORTS: Mapping[str, ImportFact] = MappingProxyType({})


@dataclass(frozen=True)
class SemanticModel:
    """The language-neutral, repository-only semantic view of ONE file.

    Language-level facts already resolved by the language's ``SemanticResolver``
    (``imports`` / ``schemas`` / ``config`` — each ecosystem-specific: Pydantic models,
    ``os.getenv`` reads, …) plus normalized structural access (``functions`` / ``calls`` /
    ``assignments``) that framework adapters pattern-match against.

    ``partial=True`` marks an unparseable file (mirrors the legacy degraded contract).
    """

    path: str
    partial: bool = False
    imports: Mapping[str, ImportFact] = field(default_factory=lambda: _EMPTY_IMPORTS)
    schemas: tuple[SchemaFact, ...] = ()
    config: tuple[ConfigFact, ...] = ()
    functions: tuple[FunctionInfo, ...] = ()
    calls: tuple[Expr, ...] = ()  # all call expressions (any depth), for include_router / add_middleware
    assignments: tuple[AssignInfo, ...] = ()  # module-level assignments, for security-scheme detection


# ----------------------------------------------------------------- framework output
@dataclass(frozen=True)
class FrameworkFacts:
    """The framework-specific facts one ``FrameworkAdapter`` contributes."""

    routes: tuple[RouteFact, ...] = ()
    mounts: tuple[MountFact, ...] = ()
    middlewares: tuple[MiddlewareFact, ...] = ()
    security: Mapping[str, str] = field(default_factory=dict)


# ------------------------------------------------------------------------- protocols
@runtime_checkable
class LanguageParser(Protocol):
    """Turns source text into an opaque, language-specific syntax tree. Pure &
    deterministic; the grammar VERSION is part of the determinism contract."""

    def parse(self, source: str) -> SyntaxTree: ...


@runtime_checkable
class SemanticResolver(Protocol):
    """Turns a syntax tree into a language-neutral ``SemanticModel`` using repository
    contents ONLY (no installed packages, no execution, no environment resolution)."""

    def resolve(self, tree: SyntaxTree, path: str) -> SemanticModel: ...


@runtime_checkable
class FrameworkAdapter(Protocol):
    """Turns framework-specific structures in a ``SemanticModel`` into
    framework-INDEPENDENT facts. Reads only the neutral model, never a raw tree."""

    name: str

    def applies(self, model: SemanticModel) -> bool: ...

    def extract(self, model: SemanticModel) -> FrameworkFacts: ...


@runtime_checkable
class FactExtractor(Protocol):
    """Assembles the language facts (imports/schemas/config) + the merged framework
    facts into a ``PartialGraph`` — the parser boundary. Language-specific, because it
    pre-computes each cross-file reference's resolution candidates with that language's
    import/typing rules."""

    def build(self, model: SemanticModel, framework: FrameworkFacts) -> PartialGraph: ...
