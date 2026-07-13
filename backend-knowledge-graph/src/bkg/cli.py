"""The ``bkg`` CLI — a thin transport over GraphService (no LLM on the query path)."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from .service import GraphService, _match_method, _match_tag


def _print_endpoint_table(endpoints: list[dict[str, Any]]) -> None:
    for ep in endpoints:
        location = f"{ep['handler_file']}:{ep['handler_line']}"
        flag = "!" if ep.get("partial") else ("=" if ep["confidence"] == "static-certain" else "~")
        tags = f"  [{','.join(ep['tags'])}]" if ep.get("tags") else ""
        print(f"{flag} {ep['method']:7} {ep['resolved_path']:40} -> {ep['handler']}{tags}  ({location})")
    print(f"\n{len(endpoints)} endpoint(s)  (= static-certain  ~ inferred  ! partial)", file=sys.stderr)


def _cmd_endpoints(args: argparse.Namespace) -> int:
    endpoints = GraphService.from_directory(args.directory).list_endpoints()
    if args.method:
        endpoints = [ep for ep in endpoints if _match_method(ep, args.method.strip().upper())]
    if args.tag:
        endpoints = [ep for ep in endpoints if _match_tag(ep, args.tag.strip().lower())]
    if args.json:
        print(json.dumps(endpoints, indent=2))
    else:
        _print_endpoint_table(endpoints)
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    endpoints = GraphService.from_directory(args.directory).search_endpoints(args.query)
    if args.json:
        print(json.dumps(endpoints, indent=2))
    else:
        _print_endpoint_table(endpoints)
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:  # pragma: no cover - blocking server
    from .api import serve

    serve(
        args.directory,
        host=args.host,
        port=args.port,
        repo_id=args.repo_id,
        cors_origins=args.cors_origin,
    )
    return 0


def _cmd_trust(args: argparse.Namespace) -> int:
    print(json.dumps(GraphService.from_directory(args.directory).trust_summary(), indent=2))
    return 0


def _cmd_propose(args: argparse.Namespace) -> int:
    endpoints = GraphService.from_directory(args.directory).propose_gaps()
    proposed = [e for e in endpoints if "ai_proposals" in e]
    print(json.dumps(proposed, indent=2))
    print(
        f"\n{len(proposed)} endpoint(s) with AI proposals (ai-inferred; leads to verify, not facts)",
        file=sys.stderr,
    )
    return 0


def _cmd_runtime(args: argparse.Namespace) -> int:
    with open(args.observations, encoding="utf-8") as handle:
        records = json.load(handle)
    result = GraphService.from_directory(args.directory).reconcile_runtime(records)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for ep in result["endpoints"]:
            if ep["verification_status"] == "runtime-confirmed":
                print(f"confirmed     {ep['method']:9} {ep['resolved_path']}")
        for ro in result["runtime_only"]:
            print(f"runtime-only  {ro['method']:9} {ro['path']}")
        print(
            f"\n{result['confirmed']} confirmed, {len(result['runtime_only'])} runtime-only",
            file=sys.stderr,
        )
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:  # pragma: no cover - blocking loop
    from .daemon import Daemon

    daemon = Daemon(args.directory)

    def report(service: GraphService) -> None:
        print(f"[bkg] {len(service.list_endpoints())} endpoint(s)", file=sys.stderr)

    report(daemon.service)
    print(f"[bkg] watching {args.directory} (Ctrl-C to stop)", file=sys.stderr)
    daemon.watch(report)
    return 0


def _cmd_endpoint(args: argparse.Namespace) -> int:
    ep = GraphService.from_directory(args.directory).get_endpoint(args.method, args.path)
    if ep is None:
        print(f"no endpoint {args.method.upper()} {args.path}", file=sys.stderr)
        return 1
    print(json.dumps(ep, indent=2))
    return 0


def _cmd_schemas(args: argparse.Namespace) -> int:
    schemas = GraphService.from_directory(args.directory).list_schemas()
    if args.json:
        print(json.dumps(schemas, indent=2))
    else:
        for s in schemas:
            fields = ", ".join(
                f"{f['name']}: {f['type']}" + ("" if f["required"] else " = ...") for f in s["fields"]
            )
            print(f"{s['id']}\n    {fields}")
        print(f"\n{len(schemas)} schema(s)", file=sys.stderr)
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    config = GraphService.from_directory(args.directory).list_config()
    if args.json:
        print(json.dumps(config, indent=2))
    else:
        for c in config:
            default = f" = {c['default']}" if c.get("default") is not None else ""
            typ = f": {c['type']}" if c.get("type") else ""
            print(f"{c['kind']:7} {c['name']}{typ}{default}  ({c['file']}:{c['line']})")
        print(f"\n{len(config)} config item(s)", file=sys.stderr)
    return 0


def _cmd_blast(args: argparse.Namespace) -> int:
    endpoints = GraphService.from_directory(args.directory).blast_radius(args.schema)
    if not endpoints:
        print(f"no endpoints reference {args.schema}", file=sys.stderr)
        return 1
    for endpoint_id in endpoints:
        print(endpoint_id)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bkg", description="Backend Knowledge Graph")
    sub = parser.add_subparsers(dest="command", required=True)

    p_eps = sub.add_parser("endpoints", help="list all backend endpoints in a project")
    p_eps.add_argument("directory")
    p_eps.add_argument("--method", help="filter by HTTP method (e.g. GET)")
    p_eps.add_argument("--tag", help="filter by tag")
    p_eps.add_argument("--json", action="store_true", help="emit JSON")
    p_eps.set_defaults(func=_cmd_endpoints)

    p_srch = sub.add_parser("search", help="search endpoints by method/path/handler/tag substring")
    p_srch.add_argument("directory")
    p_srch.add_argument("query")
    p_srch.add_argument("--json", action="store_true", help="emit JSON")
    p_srch.set_defaults(func=_cmd_search)

    p_ep = sub.add_parser("endpoint", help="show one endpoint by method + resolved path")
    p_ep.add_argument("directory")
    p_ep.add_argument("method")
    p_ep.add_argument("path")
    p_ep.set_defaults(func=_cmd_endpoint)

    p_sch = sub.add_parser("schemas", help="list DTO/schema definitions")
    p_sch.add_argument("directory")
    p_sch.add_argument("--json", action="store_true", help="emit JSON")
    p_sch.set_defaults(func=_cmd_schemas)

    p_cfg = sub.add_parser("config", help="list configuration (env vars + BaseSettings)")
    p_cfg.add_argument("directory")
    p_cfg.add_argument("--json", action="store_true", help="emit JSON")
    p_cfg.set_defaults(func=_cmd_config)

    p_bl = sub.add_parser("blast", help="endpoints affected if a DTO changes (arg: file:Model)")
    p_bl.add_argument("directory")
    p_bl.add_argument("schema")
    p_bl.set_defaults(func=_cmd_blast)

    p_tr = sub.add_parser("trust", help="confidence/partial summary of the graph")
    p_tr.add_argument("directory")
    p_tr.set_defaults(func=_cmd_trust)

    p_pr = sub.add_parser("propose", help="AI proposals (ai-inferred) for endpoints with gaps")
    p_pr.add_argument("directory")
    p_pr.set_defaults(func=_cmd_propose)

    p_rt = sub.add_parser("runtime", help="reconcile observed traffic with the graph (raises confidence)")
    p_rt.add_argument("directory")
    p_rt.add_argument("observations", help="path to a JSON array of {method, path, status}")
    p_rt.add_argument("--json", action="store_true", help="emit JSON")
    p_rt.set_defaults(func=_cmd_runtime)

    p_w = sub.add_parser("watch", help="watch a project and keep the graph live (incremental)")
    p_w.add_argument("directory")
    p_w.set_defaults(func=_cmd_watch)

    p_srv = sub.add_parser("serve", help="serve the Backend Intelligence HTTP API (needs the 'api' extra)")
    p_srv.add_argument("directory")
    p_srv.add_argument("--host", default=None, help="bind host (default 127.0.0.1)")
    p_srv.add_argument("--port", type=int, default=None, help="bind port (default 8765)")
    p_srv.add_argument("--repo-id", default=None, help="repo id for composite addressing (default: dir name)")
    p_srv.add_argument(
        "--cors-origin", action="append", default=None, help="extra allowed CORS origin (repeatable)"
    )
    p_srv.set_defaults(func=_cmd_serve)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result
