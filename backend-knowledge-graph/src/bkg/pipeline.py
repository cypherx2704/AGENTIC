"""The backend-graph pipeline: memoized queries that turn a project's source into
assembled Endpoints, feeding the incremental engine.

The parser boundary is a ``PartialGraph`` (``bkg.parser.analyze``): each file's local
facts as nodes (Route / SchemaRef / Middleware / SecurityScheme / Config) + router
mounts, with every cross-file reference carrying language-resolved ``{file}:{symbol}``
CANDIDATE ids. This pipeline is LANGUAGE-NEUTRAL: it never imports tree-sitter, ``ast``,
or any import/typing logic — it only stitches candidates by picking the first whose file
exists in the project, then assembles endpoints.

Firewall structure (each per-fact node depends on a PROJECTION, never raw text):

    fileText:{p}   (input: source)
      -> fileFacts:{p}   (the file's PartialGraph — re-parse absorbs comment edits)
        -> routeDeclList / mountDeclList / schemaDeclList / middlewareDeclList / securityMap / configDeclList
          -> allMounts -> mountChain:{routerId}   (cross-file mounting, candidate-resolved)
          -> routeFact -> endpoint -> graph:all   (root)

Inputs (kinds with no registered query) are ``fileText:{path}`` and ``files:all``.
Cross-file resolution is at QUERY time (never eager edges), so a change to a mount point
or a DTO re-resolves exactly the affected facts.
"""

from __future__ import annotations

import re
from typing import Any

from bkg.parser import analyze

from .engine import Cx, Engine

ROOT = "graph:all"


def _route_id(file: str, route: dict[str, Any]) -> str:
    return f"{file}:{route['router_local']}:{route['method']}:{route['path']}"


def _join_prefix(parent: str, child: str) -> str:
    c = child.strip("/")
    return f"{parent.rstrip('/')}/{c}" if c else parent.rstrip("/")


def _join_path(prefix: str, literal: str) -> str:
    p = prefix.rstrip("/")
    if literal in ("", "/"):
        return f"{p}/" if p else "/"
    return f"{p}/{literal.lstrip('/')}"


_PATH_PARAM = re.compile(r"\{([^}:]+)")


def _path_param_names(path: str) -> set[str]:
    return set(_PATH_PARAM.findall(path))


def _first_existing(candidates: list[str], files: set[str]) -> str | None:
    """The first ``{file}:{symbol}`` candidate whose file is in the project — the ONLY
    cross-file reconciliation the (language-neutral) engine does. The parser already
    ordered the candidates by the language's import rules (naive path, then package
    ``__init__`` variant, …)."""
    for candidate in candidates:
        if candidate.rsplit(":", 1)[0] in files:
            return candidate
    return None


def install(engine: Engine) -> None:
    def file_facts(key: str, cx: Cx) -> Any:
        path = key.split(":", 1)[1]
        return analyze(cx.read(f"fileText:{path}"), path).to_dict()

    def _nodes(cx: Cx, path: str, kind: str) -> list[dict[str, Any]]:
        return [n for n in cx.read(f"fileFacts:{path}")["nodes"] if n["kind"] == kind]

    def route_decl_list(key: str, cx: Cx) -> Any:
        return _nodes(cx, key.split(":", 1)[1], "Route")

    def mount_decl_list(key: str, cx: Cx) -> Any:
        return cx.read(f"fileFacts:{key.split(':', 1)[1]}")["router_mounts"]

    def middleware_decl_list(key: str, cx: Cx) -> Any:
        return _nodes(cx, key.split(":", 1)[1], "Middleware")

    def security_map(key: str, cx: Cx) -> Any:
        return {n["var"]: n["scheme"] for n in _nodes(cx, key.split(":", 1)[1], "SecurityScheme")}

    def schema_decl_list(key: str, cx: Cx) -> Any:
        return _nodes(cx, key.split(":", 1)[1], "SchemaRef")

    def config_decl_list(key: str, cx: Cx) -> Any:
        out: list[dict[str, Any]] = []
        for n in _nodes(cx, key.split(":", 1)[1], "Config"):
            entry: dict[str, Any] = {
                "kind": n["config_kind"],
                "name": n["name"],
                "type": n["type"],
                "default": n["default"],
                "line": n["line"],
            }
            if n["cls"] is not None:  # settings carry an owning class; env reads omit the key
                entry["class"] = n["cls"]
            out.append(entry)
        return out

    def _resolved_mounts(cx: Cx) -> list[dict[str, Any]]:
        """Every mount whose target router resolves to a real project file, sorted."""
        file_list = cx.read("files:all")
        files = set(file_list)
        out: list[dict[str, Any]] = []
        for path in file_list:
            for m in cx.read(f"mountDeclList:{path}"):
                target = _first_existing(m["target_candidates"], files)
                if target is None:
                    continue
                out.append(
                    {
                        "owner": path,
                        "router_local": m["router_local"],
                        "prefix": m["prefix"],
                        "target": target,
                        "middleware": [],  # route-level dependencies are on the endpoint's auth
                        "tags": m["tags"],
                    }
                )
        out.sort(key=lambda m: (m["target"], m["owner"], m["router_local"], m["prefix"]))
        return out

    def mount_cycles(key: str, cx: Cx) -> Any:
        """Routers transitively mounted under themselves (``a.include_router(b)`` +
        ``b.include_router(a)``) — broken source that nonetheless PARSES. ``mountChain``
        walks parents recursively, so leaving these in would re-enter the engine and take
        the whole graph down. Resolves the mounts itself rather than reading ``allMounts``
        (which filters on THIS result — reading it would be a query cycle)."""
        parent: dict[str, str] = {}
        for m in _resolved_mounts(cx):  # sorted, so a target's first mount wins (as mountChain does)
            parent.setdefault(m["target"], f"{m['owner']}:{m['router_local']}")
        cyclic: set[str] = set()
        state: dict[str, int] = {}  # 1 = on the current walk, 2 = settled
        for start in sorted(parent):
            path: list[str] = []
            node: str | None = start
            while node is not None and node not in state:
                state[node] = 1
                path.append(node)
                node = parent.get(node)
            if node is not None and state.get(node) == 1:  # looped back onto this walk
                cyclic.update(path[path.index(node) :])
            for seen in path:
                state[seen] = 2
        return sorted(cyclic)

    def all_mounts(key: str, cx: Cx) -> Any:
        cyclic = set(cx.read("mountCycles:all"))
        return [m for m in _resolved_mounts(cx) if m["target"] not in cyclic]

    def mount_chain(key: str, cx: Cx) -> Any:
        router_id = key.split(":", 1)[1]  # "{file}:{router_local}"
        for m in cx.read("allMounts"):
            if m["target"] == router_id:
                parent = cx.read(f"mountChain:{m['owner']}:{m['router_local']}")
                return {
                    "prefix": _join_prefix(parent["prefix"], m["prefix"]),
                    "middleware": [*parent["middleware"], *m["middleware"]],
                    "tags": sorted(set([*parent["tags"], *m["tags"]])),  # nested chains union tags
                    "cyclic": parent["cyclic"],  # a truncated ancestor taints the whole chain
                }
        # base case: an unmounted top-level app — its own add_middleware applies to every
        # route it (transitively) mounts, so seed the chain with this router's middleware.
        # A router on a mount CYCLE also lands here (its mount was pruned) — flag it, so the
        # endpoint reports the truncated prefix as `partial` rather than serving a path it
        # could not actually resolve as `static-certain`.
        file, router_local = router_id.rsplit(":", 1)
        decls = cx.read(f"middlewareDeclList:{file}")
        own_mw = [m["name"] for m in decls if m["router_local"] == router_local]
        return {
            "prefix": "",
            "middleware": own_mw,
            "tags": [],
            "cyclic": router_id in set(cx.read("mountCycles:all")),
        }

    def route_fact(key: str, cx: Cx) -> Any:
        route_id = key.split(":", 1)[1]
        path = route_id.split(":", 1)[0]
        for r in cx.read(f"routeDeclList:{path}"):
            if _route_id(path, r) == route_id:
                return r
        return None

    def schema_decl(key: str, cx: Cx) -> Any:
        """One model's RAW (unmerged) declaration, or None if the file doesn't declare it.

        Reads only its own file's decl list, so it can NEVER recurse into another schema.
        That makes it three things at once:
        - the **existence oracle** a schema uses to check a nested field's DTO WITHOUT
          forcing that DTO's assembly — which is what lets mutually-referencing models
          (``User.posts: list[Post]`` + ``Post.author: User``) resolve instead of tripping
          the engine's cycle detector;
        - the **blast anchor**: ``blast_radius`` keys on it, so everything that reads a
          model (its own schemaRef, its subclasses, and any schema NESTING it) is in its
          reverse-dependency closure;
        - a per-model **firewall**: editing a sibling model in the same file recomputes
          this node to byte-identical bytes, so it backdates and nothing downstream moves.
        """
        file, model = key.split(":", 1)[1].rsplit(":", 1)
        return next((s for s in cx.read(f"schemaDeclList:{file}") if s["name"] == model), None)

    def schema_deps(key: str, cx: Cx) -> Any:
        """The project's schema REFERENCE graph: ``{schema id -> sorted referenced ids}``
        (base classes + nested field DTOs), resolved against the real file set.

        Built in ONE query — it never recurses into another schema — so it stays
        well-defined when models reference each other CYCLICALLY. That matters because the
        engine's dependency graph is a DAG and therefore cannot represent a reference
        cycle; ``blast_radius`` walks this map in reverse (with a visited set) to answer
        "what breaks if this DTO changes" transitively and cycle-safely.
        """
        file_list = cx.read("files:all")
        files = set(file_list)
        graph: dict[str, list[str]] = {}
        for path in file_list:
            for s in cx.read(f"schemaDeclList:{path}"):
                sid = f"{path}:{s['name']}"
                if sid in graph:
                    continue  # a duplicate class name: the FIRST declaration wins, exactly
                    # as schemaDecl's ``next(...)`` does. Disagreeing here would analyze a
                    # DIFFERENT graph than schema_ref actually resolves against.
                refs: set[str] = set()
                for base_ref in s["base_refs"]:
                    target = _first_existing(base_ref["candidates"], files)
                    if target is not None:
                        refs.add(target)
                for field in s["fields"]:
                    target = _first_existing(field["ref_candidates"], files)
                    if target is not None:
                        refs.add(target)
                refs.discard(sid)  # a self-reference adds nothing to the closure
                graph[sid] = sorted(refs)
        # drop dangling refs (a file exists but doesn't declare that model)
        return {sid: [r for r in refs if r in graph] for sid, refs in graph.items()}

    def schema_base_cycles(key: str, cx: Cx) -> Any:
        """Schema ids caught in a base-class cycle (``class A(B)`` + ``class B(A)``).

        Inheritance is a DAG in *valid* Python, so ``schema_ref`` merges base fields by
        recursing through ``schemaRef``. But tree-sitter happily parses INVALID Python —
        the normal state of a file mid-edit — and an unguarded recursion there re-enters
        the engine and takes the WHOLE graph down. So base cycles are detected up front
        (one query, no recursion into schemaRef) and those schemas degrade to ``partial``.
        """
        file_list = cx.read("files:all")
        files = set(file_list)
        bases: dict[str, list[str]] = {}
        for path in file_list:
            for s in cx.read(f"schemaDeclList:{path}"):
                sid = f"{path}:{s['name']}"
                if sid in bases:
                    continue  # duplicate class name: FIRST declaration wins (as schemaDecl does),
                    # otherwise this guard would analyze a base graph schema_ref never walks
                resolved: list[str] = []
                for base_ref in s["base_refs"]:
                    target = _first_existing(base_ref["candidates"], files)
                    if target is not None and target != sid:  # direct self-base: skipped below
                        resolved.append(target)
                bases[sid] = resolved

        cyclic: set[str] = set()
        state: dict[str, int] = {}  # 1 = on the current DFS path, 2 = settled

        def visit(node: str, path: list[str]) -> None:
            state[node] = 1
            path.append(node)
            for nxt in bases.get(node, ()):
                if nxt not in bases:
                    continue  # dangling base (the file exists but declares no such model)
                if state.get(nxt, 0) == 1:  # back edge -> the whole loop is cyclic
                    cyclic.update(path[path.index(nxt) :])
                elif state.get(nxt, 0) == 0:
                    visit(nxt, path)
            path.pop()
            state[node] = 2

        for sid in sorted(bases):
            if state.get(sid, 0) == 0:
                visit(sid, [])
        return sorted(cyclic)

    def schema_ref(key: str, cx: Cx) -> Any:
        # key = schemaRef:{file}:{model}; file has no ':' so rsplit peels the model
        file, model = key.split(":", 1)[1].rsplit(":", 1)
        own_id = f"{file}:{model}"
        match = cx.read(f"schemaDecl:{own_id}")
        if match is None:
            return None
        files = set(cx.read("files:all"))
        # merge inherited base-class fields (child overrides); reading each base's
        # schemaRef also records the edge, so editing a base blasts its subclasses.
        # Recursing through bases is safe ONLY because base cycles are excluded up front
        # (see schema_base_cycles); FIELD references, which legitimately cycle, go through
        # the non-recursive schemaDecl below.
        merged: dict[str, Any] = {}
        partial = own_id in set(cx.read("schemaBaseCycles:all"))
        for base_ref in [] if partial else match["base_refs"]:
            base_id = _first_existing(base_ref["candidates"], files)
            if base_id is None or base_id == own_id:
                continue  # external base (e.g. BaseModel) — expected, not partial
            base = cx.read(f"schemaRef:{base_id}")
            if base is None:
                partial = True  # an in-project base file exists but the model is missing
                continue
            for field in base["fields"]:
                merged[field["name"]] = field
            partial = partial or base.get("partial", False)
        for field in match["fields"]:
            merged[field["name"]] = {
                "name": field["name"],
                "type": field["type"],
                "required": field["required"],
                "default": field["default"],
            }
            # nested field-referenced DTO: depend on each in-project model used as a field
            # type, so editing THAT model blasts this one (inherited fields are covered
            # transitively via the base dependency). We read its schemaDECL, not its
            # schemaRef: we only need to know it EXISTS, and not forcing its assembly is
            # what makes cyclic/bidirectional models work. Sets partial when it looks like
            # a project model but can't be resolved to a schema.
            nested_id = _first_existing(field["ref_candidates"], files)
            if nested_id is None or nested_id == own_id:
                continue
            if cx.read(f"schemaDecl:{nested_id}") is None:
                partial = True
        return {
            "name": model,
            "file": file,
            "fields": list(merged.values()),
            "bases": match["bases"],
            "source": "static",
            "confidence": "inferred" if partial else "static-certain",
            "verification_status": "unverified",
            "partial": partial,
        }

    def endpoint(key: str, cx: Cx) -> Any:
        route_id = key.split(":", 1)[1]
        parts = route_id.split(":")
        path, router = parts[0], parts[1]
        rf = cx.read(f"routeFact:{route_id}")
        if rf is None:
            return None
        chain = cx.read(f"mountChain:{path}:{router}")
        files = set(cx.read("files:all"))
        path_params = _path_param_names(rf["path"])
        # a mount cycle truncated this route's prefix — the resolved_path below is
        # incomplete, so say so instead of serving it as certain
        partial = bool(chain["cyclic"])

        def resolve_dto(candidates: list[str]) -> str | None:
            # returns an in-project DTO id, or None. Sets `partial` when a reference LOOKS
            # like a model (non-empty candidates) but can't be resolved to a project schema
            # (external / unloaded / ambiguous) — so the served answer flags incompleteness.
            nonlocal partial
            if not candidates:
                return None  # a scalar/builtin — not a model
            model_id = _first_existing(candidates, files)
            if model_id is not None and cx.read(f"schemaRef:{model_id}") is not None:
                return model_id
            partial = True
            return None

        params_out: list[dict[str, Any]] = []
        dependencies: list[str] = []
        schemes: set[str] = set()
        body: str | None = None
        for p in rf["params"]:
            if p["depends"]:
                dependencies.append(p["depends"])
                scheme = _classify_scheme(cx, p, files)
                if scheme is not None:
                    schemes.add(scheme)
                continue
            name = p["name"]
            annotation = p["annotation"]
            if name in path_params:
                params_out.append({"name": name, "location": "path", "type": annotation, "required": True})
                continue
            # resolve EVERY non-path param, so a schemaRef edge exists for every
            # model-typed param (not just the first) -> blast_radius stays sound
            schema_id = resolve_dto(p["dto_candidates"])
            if schema_id is not None:
                if body is None:
                    body = schema_id  # the first model-typed param is the primary body
                continue
            params_out.append(
                {
                    "name": name,
                    "location": "query",
                    "type": annotation,
                    "required": not p["has_default"],
                }
            )

        response = resolve_dto(rf["response_candidates"])
        # the assembled endpoint is 'inferred' when it required cross-file resolution
        # (a mount prefix or a DTO ref) or dropped a DTO ref; else it's 'static-certain'
        certain = not (chain["prefix"] or body or response or partial)
        return {
            "method": rf["method"],
            "resolved_path": _join_path(chain["prefix"], rf["path"]),
            "params": params_out,
            "body": body,
            "response": response,
            "auth": {
                "required": len(dependencies) > 0,
                "dependencies": sorted(dependencies),
                "schemes": sorted(schemes),
            },
            "middleware_chain": chain["middleware"],
            "tags": sorted(set([*rf["tags"], *chain["tags"]])),  # route ∪ router-chain tags
            "handler": rf["handler"],
            "handler_file": path,
            "handler_line": rf["line"],
            "source": "static",
            "confidence": "static-certain" if certain else "inferred",
            "verification_status": "unverified",
            "partial": partial,
        }

    def graph_all(key: str, cx: Cx) -> Any:
        keys: list[str] = []
        for path in cx.read("files:all"):
            for r in cx.read(f"routeDeclList:{path}"):
                ekey = f"endpoint:{_route_id(path, r)}"
                cx.read(ekey)  # force assembly
                keys.append(ekey)
        return sorted(keys)

    engine.define_query("fileFacts", file_facts)
    engine.define_query("routeDeclList", route_decl_list)
    engine.define_query("mountDeclList", mount_decl_list)
    engine.define_query("middlewareDeclList", middleware_decl_list)
    engine.define_query("securityMap", security_map)
    engine.define_query("schemaDeclList", schema_decl_list)
    engine.define_query("schemaDecl", schema_decl)
    engine.define_query("schemaDeps", schema_deps)
    engine.define_query("schemaBaseCycles", schema_base_cycles)
    engine.define_query("configDeclList", config_decl_list)
    engine.define_query("mountCycles", mount_cycles)
    engine.define_query("allMounts", all_mounts)
    engine.define_query("mountChain", mount_chain)
    engine.define_query("routeFact", route_fact)
    engine.define_query("schemaRef", schema_ref)
    engine.define_query("endpoint", endpoint)
    engine.define_query("graph", graph_all)


def _classify_scheme(cx: Cx, param: dict[str, Any], files: set[str]) -> str | None:
    """Classify a dependency param's auth scheme from parser-computed hints: an inline
    constructor (``Depends(HTTPBearer())``) wins; otherwise the first ``{file}:{var}``
    candidate whose file is in the project and defines that security-scheme variable."""
    if param["scheme_inline"] is not None:
        return str(param["scheme_inline"])
    for candidate in param["scheme_candidates"]:
        file, var = candidate.rsplit(":", 1)
        if file not in files:
            continue
        scheme = cx.read(f"securityMap:{file}").get(var)
        if scheme is not None:
            return str(scheme)
    return None


def apply_sources(engine: Engine, sources: dict[str, str]) -> None:
    """Feed a whole project (``{repo-relative path: source text}``) as inputs."""
    engine.set_input("files:all", sorted(sources))
    for path in sorted(sources):
        engine.set_input(f"fileText:{path}", sources[path])
