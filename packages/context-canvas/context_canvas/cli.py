from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from context_canvas.core import CanvasStore  # noqa: E402


def emit(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes Task Canvas local CLI")
    parser.add_argument("--root", default=None, help="Canvas root directory; defaults to ~/.hermes/context-canvas")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="Create a new canvas")
    p.add_argument("--goal", required=True)
    p.add_argument("--session-id", default=None)
    p.add_argument("--title", default=None)

    p = sub.add_parser("add-ref", help="Add evidence ref")
    p.add_argument("session_id")
    p.add_argument("--content", required=True)
    p.add_argument("--label", default="evidence")
    p.add_argument("--source", default="")
    p.add_argument("--kind", default="evidence")

    p = sub.add_parser("upsert-node", help="Add or update a node")
    p.add_argument("session_id")
    p.add_argument("--node-id", default=None)
    p.add_argument("--kind", required=True)
    p.add_argument("--status", required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--ref", action="append", dest="refs", default=[])
    p.add_argument("--depends-on", action="append", default=[])

    p = sub.add_parser("read", help="Read canvas")
    p.add_argument("session_id")
    p.add_argument("--include-refs", action="store_true")

    p = sub.add_parser("search", help="Search canvas nodes/refs")
    p.add_argument("query")
    p.add_argument("--session-id", default=None)
    p.add_argument("--limit", type=int, default=10)

    p = sub.add_parser("closeout", help="Write a MemPalace-ready closeout pack")
    p.add_argument("session_id")
    p.add_argument("--no-write-ref", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = CanvasStore(root=args.root)
    try:
        if args.command == "start":
            emit(store.start(goal=args.goal, session_id=args.session_id, title=args.title))
        elif args.command == "add-ref":
            emit(store.add_ref(args.session_id, content=args.content, label=args.label, source=args.source, kind=args.kind))
        elif args.command == "upsert-node":
            emit(store.upsert_node(args.session_id, node_id=args.node_id, kind=args.kind, status=args.status, summary=args.summary, refs=args.refs, depends_on=args.depends_on))
        elif args.command == "read":
            emit(store.read(args.session_id, include_refs=args.include_refs))
        elif args.command == "search":
            emit(store.search(args.query, session_id=args.session_id, limit=args.limit))
        elif args.command == "closeout":
            emit(store.closeout(args.session_id, write_ref=not args.no_write_ref))
        else:
            raise AssertionError(args.command)
    except Exception as exc:
        emit({"ok": False, "error": type(exc).__name__, "detail": str(exc)})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
