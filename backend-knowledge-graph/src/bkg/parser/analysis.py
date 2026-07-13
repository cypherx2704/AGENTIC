"""The framework-independent fact vocabulary emitted by the language resolver + the
framework adapters, before the linker turns them into a ``PartialGraph``.

Everything here is a frozen dataclass of canonical-friendly scalars (``str``/``int``/
``bool``/``None`` + tuples), pure and deterministic. These types are the
FRAMEWORK-INDEPENDENT vocabulary: a FastAPI router, a Flask blueprint, and (later) an
Express router all normalize onto ``RouteFact`` / ``MountFact``. Nothing here is
FastAPI- or Python-specific.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParamFact:
    """A handler parameter, after framework interpretation (``Depends`` resolved to
    the dependency's source in ``depends``)."""

    name: str
    annotation: str | None
    has_default: bool
    depends: str | None


@dataclass(frozen=True)
class RouteFact:
    """A single declared route (framework-independent). ``router`` is the local name
    of the router/app/blueprint the route is declared on; cross-file mounting is
    resolved later at query time, never here."""

    router: str
    method: str
    path: str
    response_model: str | None
    # ``None`` means the framework has no tags concept (Flask routes carry no ``tags``,
    # unlike FastAPI); the linker maps it to an empty tuple on the RouteNode.
    tags: tuple[str, ...] | None
    handler: str
    line: int
    params: tuple[ParamFact, ...]
    return_annotation: str | None


@dataclass(frozen=True)
class MountFact:
    """A router-into-router mount (FastAPI ``include_router``, Flask
    ``register_blueprint``). ``target_expr`` is the unresolved symbol expression;
    resolution to a ``{file}:{symbol}`` id happens at query time."""

    router_local: str
    prefix: str
    target_expr: str
    tags: tuple[str, ...] | None  # ``None`` -> omit key (Flask mounts carry no tags)


@dataclass(frozen=True)
class ImportFact:
    """One import binding: local name -> (module, imported name, relative level).
    ``name`` is ``None`` for ``import module`` bindings."""

    module: str
    name: str | None
    level: int


@dataclass(frozen=True)
class FieldFact:
    """One DTO/model field."""

    name: str
    type: str
    required: bool
    default: str | None


@dataclass(frozen=True)
class SchemaFact:
    """A DTO/model definition (module-level class with annotated fields)."""

    name: str
    bases: tuple[str, ...]
    fields: tuple[FieldFact, ...]


@dataclass(frozen=True)
class MiddlewareFact:
    """An app-level middleware attachment."""

    router_local: str
    name: str
    line: int


@dataclass(frozen=True)
class ConfigFact:
    """A configuration surface: an env-var read (``kind="env"``) or a settings-class
    field (``kind="setting"``). ``cls`` is the owning settings class (``None`` for env)."""

    kind: str
    name: str
    type: str | None
    default: str | None
    cls: str | None
    line: int
