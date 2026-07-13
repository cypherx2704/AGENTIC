"""Frozen node / edge / IR vocabulary + the PartialGraph container.

Nodes and edges are frozen pydantic models (validated, immutable). `PartialGraph`
is a plain frozen dataclass container — the facts inside it are already validated,
so the container needs no validation of its own.

Every fact carries `provenance` / `confidence` / `verification_status`. In the
CORE these are always `static`/`static-certain` (literals) or `inferred` (derived);
the `ai*` and `runtime*` values are reserved for later layers.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PField

from .enums import Confidence, EdgeKind, HttpMethod, NodeKind, Provenance, VerificationStatus


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- IR
class Param(_Frozen):
    name: str
    location: str  # "path" | "query" | "header"
    type: str = "string"
    required: bool = True


class FieldIR(_Frozen):
    """A single DTO/schema field. `source` records HOW the type was learned."""

    name: str
    type: str
    required: bool = True
    default: str | None = None
    format: str | None = None
    ref: str | None = None
    enum: tuple[str, ...] | None = None
    # validation-lib | static-type | destructuring | runtime | ai | unknown
    source: str = "static-type"
    confidence: Confidence = Confidence.INFERRED
    # Candidate ``{file}:{Model}`` ids if this field's type is an in-project DTO — the
    # engine picks the first whose file exists (language-neutral cross-file stitching).
    ref_candidates: tuple[str, ...] = ()


class Auth(_Frozen):
    required: bool = False
    policies: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()


class SymbolRef(_Frozen):
    """An unresolved cross-file reference. Resolution happens at query time.

    ``candidates`` are the language-resolved ``{file}:{symbol}`` ids to try in order
    (e.g. ``foo.py:X`` then the package ``foo/__init__.py:X`` variant); the engine wires
    the first whose file exists. This is how per-language import semantics stay OUT of
    the (language-neutral) graph engine."""

    name: str
    from_file: str
    resolved: str | None = None
    candidates: tuple[str, ...] = ()


class BaseRef(_Frozen):
    """A base-class reference on a schema: the raw base name + its resolution
    candidates (empty for an external base like ``BaseModel``)."""

    name: str
    candidates: tuple[str, ...] = ()


class RouteParam(_Frozen):
    """A route handler parameter as the parser emits it (pre-assembly). The engine
    classifies it (path/query/body) and resolves its DTO/auth references generically.

    - ``dto_candidates``: ``{file}:{Model}`` ids if the annotation is model-typed (a body
      candidate); empty for a scalar/builtin.
    - ``depends`` / ``scheme_inline`` / ``scheme_candidates``: for a ``Depends``/``Security``
      dependency — the dependency source, an inline-classified scheme, and the
      ``{file}:{var}`` candidates to look up a cross-file security scheme."""

    name: str
    annotation: str | None = None
    has_default: bool = False
    depends: str | None = None
    dto_candidates: tuple[str, ...] = ()
    scheme_inline: str | None = None
    scheme_candidates: tuple[str, ...] = ()


class RouterMount(_Frozen):
    """A router mount point (Express `app.use`, Nest `@Module`, FastAPI
    `include_router`). Cross-file route assembly stitches these at query time —
    they are NEVER turned into eager cross-file edges. ``target_candidates`` are the
    language-resolved ``{file}:{router}`` ids to try in order."""

    mounting_file: str
    router_local: str
    prefix: str
    target_symbol_ref: str
    middleware: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    target_candidates: tuple[str, ...] = ()


# ------------------------------------------------------------------------ Nodes
class _NodeBase(_Frozen):
    id: str  # stable nominal identity key — never a byte offset / array index
    provenance: Provenance = Provenance.STATIC
    confidence: Confidence = Confidence.STATIC_CERTAIN
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED


class FileNode(_NodeBase):
    kind: NodeKind = NodeKind.FILE
    path: str


class RouteNode(_NodeBase):
    kind: NodeKind = NodeKind.ROUTE
    # A plain str, not HttpMethod: routes can be WEBSOCKET / TRACE / any
    # ``api_route(methods=[...])`` value. Existing HttpMethod values still serialize the same.
    method: str
    path: str  # file-local literal path (NOT yet the resolved absolute path)
    file: str
    line: int
    router_local: str
    handler: str = ""
    tags: tuple[str, ...] = ()
    params: tuple[RouteParam, ...] = ()
    response_candidates: tuple[str, ...] = ()  # DTO ids for response_model/return type


class HandlerNode(_NodeBase):
    kind: NodeKind = NodeKind.HANDLER
    symbol: str
    file: str
    line: int


class MiddlewareNode(_NodeBase):
    kind: NodeKind = NodeKind.MIDDLEWARE
    name: str
    file: str
    line: int
    router_local: str = ""  # the router/app the middleware is attached to


class SchemaRefNode(_NodeBase):
    kind: NodeKind = NodeKind.SCHEMA_REF
    name: str
    fields: tuple[FieldIR, ...] = ()
    open: bool = False
    partial: bool = False
    confidence: Confidence = Confidence.INFERRED
    bases: tuple[str, ...] = ()  # raw base-class names (in declaration order)
    base_refs: tuple[BaseRef, ...] = ()  # each base + its cross-file resolution candidates


class ConfigNode(_NodeBase):
    """A configuration surface: an env-var read (``config_kind='env'``) or a settings-
    class field (``config_kind='setting'``). Framework-agnostic."""

    kind: NodeKind = NodeKind.CONFIG
    config_kind: str  # "env" | "setting"
    name: str
    type: str | None = None
    default: str | None = None
    cls: str | None = None  # owning settings class (None for env)
    line: int = 0


class SecuritySchemeNode(_NodeBase):
    """A ``name = SecurityClass(...)`` definition mapping a variable to a normalized
    scheme type (``oauth2``/``bearer``/``api-key``/…), so a cross-file
    ``Depends(imported_scheme)`` can be classified at assembly time."""

    kind: NodeKind = NodeKind.SECURITY_SCHEME
    var: str
    scheme: str
    file: str


class EndpointNode(_NodeBase):
    """The denormalized "hero" payload the product exists to serve — assembled by
    the core from Route + HANDLES + VALIDATES_WITH/RETURNS + GUARDED_BY + mounts."""

    kind: NodeKind = NodeKind.ENDPOINT
    method: HttpMethod
    resolved_path: str
    params: tuple[Param, ...] = ()
    body: str | None = None  # SchemaRef node id
    response: str | None = None  # SchemaRef node id
    auth: Auth = PField(default_factory=Auth)
    middleware_chain: tuple[str, ...] = ()  # ordered middleware/guard node ids
    handler_file: str
    handler_line: int
    confidence: Confidence = Confidence.INFERRED


AnyNode = (
    FileNode
    | RouteNode
    | HandlerNode
    | MiddlewareNode
    | SchemaRefNode
    | EndpointNode
    | ConfigNode
    | SecuritySchemeNode
)


# ------------------------------------------------------------------------ Edges
class Edge(_Frozen):
    id: str
    kind: EdgeKind
    src: str
    dst: str
    ordinal: int | None = None  # ordering for GUARDED_BY / middleware chains
    provenance: Provenance = Provenance.STATIC
    confidence: Confidence = Confidence.STATIC_CERTAIN


# -------------------------------------------------------------------- Container
@dataclass(frozen=True)
class PartialGraph:
    """The connector-agnostic output of parsing ONE file (or a hand-authored
    fixture): local facts + unresolved symbol refs + router mounts. Nothing
    cross-file is resolved here — references carry candidate ids the engine wires.

    ``partial=True`` marks a file the language parser could not fully parse (mirrors
    the legacy degraded contract); its node/ref tuples are empty."""

    nodes: tuple[AnyNode, ...] = ()
    edges: tuple[Edge, ...] = ()
    symbol_refs: tuple[SymbolRef, ...] = ()
    router_mounts: tuple[RouterMount, ...] = ()
    partial: bool = False

    def to_dict(self) -> dict[str, object]:
        """Canonical JSON dict (lists, string scalars) for the engine's memo store —
        the parser->engine wire form. Never emit tuples or the dataclass itself."""
        return {
            "nodes": [n.model_dump(mode="json") for n in self.nodes],
            "edges": [e.model_dump(mode="json") for e in self.edges],
            "symbol_refs": [r.model_dump(mode="json") for r in self.symbol_refs],
            "router_mounts": [m.model_dump(mode="json") for m in self.router_mounts],
            "partial": self.partial,
        }
