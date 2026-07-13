"""The replaceable, language-independent parser.

Pipeline:  LanguageParser (tree-sitter) -> SemanticResolver -> FrameworkAdapter(s)
           -> Linker -> PartialGraph

``analyze(source, path)`` returns a ``PartialGraph`` (the frozen protocol boundary): the
file's local facts as nodes + ``router_mounts``, with every cross-file reference carrying
the language-resolved ``{file}:{symbol}`` candidate ids. The graph engine consumes this
directly (via ``PartialGraph.to_dict()``) and stitches files together generically — no
tree-sitter, no ``ast``, no import semantics leak into the engine. Adding a language is a
new plugin here; adding a framework is a new ``FrameworkAdapter``.
"""

from __future__ import annotations

from ..protocol.models import PartialGraph
from .base import FrameworkFacts, SemanticModel
from .frameworks.fastapi import FastApiAdapter
from .frameworks.flask import FlaskAdapter
from .languages.python.linker import PythonLinker
from .languages.python.parser import TreeSitterPythonParser
from .languages.python.resolver import PythonSemanticResolver
from .registry import LanguagePlugin, frameworks, languages

# --- wire the default plugins (Python language + FastAPI/Flask frameworks) onto the
# registries. Adding a language/framework later is one more register() call here.
languages.register(
    LanguagePlugin(
        name="python",
        extensions=(".py",),
        parser=TreeSitterPythonParser(),
        resolver=PythonSemanticResolver(),
        linker=PythonLinker(),
    )
)
frameworks.register(FastApiAdapter())
frameworks.register(FlaskAdapter())

_FALLBACK_FRAMEWORK = "fastapi"  # a file importing no known framework is analyzed as FastAPI


def _merge(model: SemanticModel) -> FrameworkFacts:
    """Merge every applicable framework adapter's facts: framework detection by imports, a
    FastAPI-only fallback, canonical route/mount sort, and cross-adapter route dedup ONLY
    when more than one framework applies (an ambiguous file)."""
    applicable = list(frameworks.applicable(model))
    if not applicable:
        fallback = frameworks.get(_FALLBACK_FRAMEWORK)
        applicable = [fallback] if fallback is not None else []
    applicable.sort(key=lambda a: a.name)  # deterministic adapter order

    facts = [a.extract(model) for a in applicable]
    routes = sorted((r for f in facts for r in f.routes), key=lambda r: (r.router, r.method, r.path))
    mounts = sorted(
        (m for f in facts for m in f.mounts), key=lambda m: (m.router_local, m.target_expr, m.prefix)
    )
    middlewares = tuple(mw for f in facts for mw in f.middlewares)
    security: dict[str, str] = {}
    for f in facts:
        security.update(f.security)

    if len(applicable) > 1:  # ambiguous multi-framework file: dedup routes by id (post-sort)
        seen: set[tuple[str, str, str]] = set()
        deduped = []
        for r in routes:
            rid = (r.router, r.method, r.path)
            if rid not in seen:
                seen.add(rid)
                deduped.append(r)
        routes = deduped

    return FrameworkFacts(
        routes=tuple(routes), mounts=tuple(mounts), middlewares=middlewares, security=security
    )


def analyze(source: str, path: str = "__main__.py") -> PartialGraph:
    """Parse one file into a ``PartialGraph`` (the parser->engine boundary). Language is
    chosen by the file extension (defaulting to Python)."""
    plugin = languages.for_path(path) or languages.for_extension(".py")
    if plugin is None:
        raise LookupError(f"no language plugin for {path!r}")
    model = plugin.resolver.resolve(plugin.parser.parse(source), path)
    if model.partial:
        return PartialGraph(partial=True)
    return plugin.linker.build(model, _merge(model))


__all__ = ["PartialGraph", "analyze"]
