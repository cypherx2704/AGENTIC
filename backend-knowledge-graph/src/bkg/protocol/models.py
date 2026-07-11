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
    format: str | None = None
    ref: str | None = None
    enum: tuple[str, ...] | None = None
    # validation-lib | static-type | destructuring | runtime | ai | unknown
    source: str = "static-type"
    confidence: Confidence = Confidence.INFERRED


class Auth(_Frozen):
    required: bool = False
    policies: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()


class SymbolRef(_Frozen):
    """An unresolved cross-file reference. Resolution happens at query time."""

    name: str
    from_file: str
    resolved: str | None = None


class RouterMount(_Frozen):
    """A router mount point (Express `app.use`, Nest `@Module`, FastAPI
    `include_router`). Cross-file route assembly stitches these at query time —
    they are NEVER turned into eager cross-file edges."""

    mounting_file: str
    router_local: str
    prefix: str
    target_symbol_ref: str
    middleware: tuple[str, ...] = ()


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
    method: HttpMethod
    path: str  # file-local literal path (NOT yet the resolved absolute path)
    file: str
    line: int
    router_local: str


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


class SchemaRefNode(_NodeBase):
    kind: NodeKind = NodeKind.SCHEMA_REF
    name: str
    fields: tuple[FieldIR, ...] = ()
    open: bool = False
    partial: bool = False
    confidence: Confidence = Confidence.INFERRED


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
    FileNode | RouteNode | HandlerNode | MiddlewareNode | SchemaRefNode | EndpointNode
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
    cross-file is resolved here."""

    nodes: tuple[AnyNode, ...] = ()
    edges: tuple[Edge, ...] = ()
    symbol_refs: tuple[SymbolRef, ...] = ()
    router_mounts: tuple[RouterMount, ...] = ()
