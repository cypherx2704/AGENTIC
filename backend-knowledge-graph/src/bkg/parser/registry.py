"""Plugin registries — the extension points that make the parser replaceable.

- ``LanguageRegistry`` maps a file extension to its ``(LanguageParser, SemanticResolver)``
  pair. Adding a language = registering one pair.
- ``FrameworkRegistry`` holds the ordered list of ``FrameworkAdapter``s. Adding a
  framework = registering one adapter.

Registration is deterministic: frameworks keep a stable insertion order, and a file's
applicable adapters are always visited in that order so merged output is reproducible.
Nothing here imports tree-sitter or a concrete language — plugins self-register.
"""

from __future__ import annotations

from dataclasses import dataclass

from .base import FactExtractor, FrameworkAdapter, LanguageParser, SemanticResolver


@dataclass(frozen=True)
class LanguagePlugin:
    """A language's parser + semantic resolver + fact-builder (linker), registered under
    one or more file extensions (e.g. ``.py``; later ``.ts`` / ``.tsx`` / ``.java`` /
    ``.go``). The linker is language-specific: it resolves cross-file reference
    candidates with that language's import/typing rules."""

    name: str
    extensions: tuple[str, ...]
    parser: LanguageParser
    resolver: SemanticResolver
    linker: FactExtractor


class LanguageRegistry:
    def __init__(self) -> None:
        self._by_ext: dict[str, LanguagePlugin] = {}

    def register(self, plugin: LanguagePlugin) -> None:
        for ext in plugin.extensions:
            self._by_ext[ext] = plugin

    def for_extension(self, ext: str) -> LanguagePlugin | None:
        return self._by_ext.get(ext)

    def for_path(self, path: str) -> LanguagePlugin | None:
        dot = path.rfind(".")
        return self._by_ext.get(path[dot:]) if dot != -1 else None


class FrameworkRegistry:
    def __init__(self) -> None:
        self._adapters: list[FrameworkAdapter] = []

    def register(self, adapter: FrameworkAdapter) -> None:
        self._adapters.append(adapter)

    def all(self) -> tuple[FrameworkAdapter, ...]:
        return tuple(self._adapters)

    def get(self, name: str) -> FrameworkAdapter | None:
        return next((a for a in self._adapters if a.name == name), None)

    def applicable(self, model: object) -> tuple[FrameworkAdapter, ...]:
        # narrow import avoided; ``model`` is a SemanticModel — kept loose to dodge a cycle
        from .base import SemanticModel

        assert isinstance(model, SemanticModel)
        return tuple(a for a in self._adapters if a.applies(model))


# Process-wide default registries. Language/framework plugins register onto these at
# import time (wired in ``parser/__init__.py``); ``analyze`` reads them.
languages = LanguageRegistry()
frameworks = FrameworkRegistry()
