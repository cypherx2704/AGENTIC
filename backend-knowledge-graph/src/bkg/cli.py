"""The ``bkg`` CLI — a thin transport over GraphService (no LLM on the query path)."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from .service import GraphService


def _cmd_endpoints(args: argparse.Namespace) -> int:
    endpoints = GraphService.from_directory(args.directory).list_endpoints()
    if args.json:
        print(json.dumps(endpoints, indent=2))
    else:
        for ep in endpoints:
            location = f"{ep['handler_file']}:{ep['handler_line']}"
            print(f"{ep['method']:7} {ep['resolved_path']:40} -> {ep['handler']}  ({location})")
        print(f"\n{len(endpoints)} endpoint(s)", file=sys.stderr)
    return 0


def _cmd_endpoint(args: argparse.Namespace) -> int:
    ep = GraphService.from_directory(args.directory).get_endpoint(args.method, args.path)
    if ep is None:
        print(f"no endpoint {args.method.upper()} {args.path}", file=sys.stderr)
        return 1
    print(json.dumps(ep, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bkg", description="Backend Knowledge Graph")
    sub = parser.add_subparsers(dest="command", required=True)

    p_eps = sub.add_parser("endpoints", help="list all backend endpoints in a project")
    p_eps.add_argument("directory")
    p_eps.add_argument("--json", action="store_true", help="emit JSON")
    p_eps.set_defaults(func=_cmd_endpoints)

    p_ep = sub.add_parser("endpoint", help="show one endpoint by method + resolved path")
    p_ep.add_argument("directory")
    p_ep.add_argument("method")
    p_ep.add_argument("path")
    p_ep.set_defaults(func=_cmd_endpoint)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result
