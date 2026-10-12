"""CLI workflows: validate -> collect -> process -> export."""

import argparse
import json
import sys
from pathlib import Path

from .config import load_settings
from .exporter import export_batch
from .pipeline import collect
from .plugins import load_entry_point_plugins
from .processors import process
from .review import generate_review_token, serve_review


def _progress_printer(event: dict) -> None:
    if event.get("phase") == "asset":
        processed = event.get("processed", 0)
        used = event.get("collected_bytes", 0)
        byte_limit = event.get("byte_limit", 0)
        message = (
            f"[{event.get('source_id')}] {processed} candidates "
            f"{used}/{byte_limit} bytes; last={event.get('status')}"
        )
        end = "\r" if sys.stderr.isatty() else "\n"
        print(message, file=sys.stderr, end=end, flush=True)
    elif event.get("phase") == "source":
        current = event.get("source_index", 0)
        total = event.get("sources_total", 0)
        percent = int(100 * current / total) if total else 0
        print(
            f"Sources: {current}/{total} ({percent}%); "
            f"{event.get('source_id')}={event.get('status')}",
            file=sys.stderr,
            flush=True,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dsforge", description="Dataset candidate collection")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="Validate a TOML source registry")
    validate.add_argument("--config", required=True, type=Path)
    collection = sub.add_parser("collect", help="Collect a new immutable raw run")
    collection.add_argument("--config", required=True, type=Path)
    collection.add_argument("--run", required=True, type=Path)
    collection.add_argument(
        "--resume",
        action="store_true",
        help="Resume an interrupted run after verifying its config and checkpoints",
    )
    collection.add_argument(
        "--no-progress", action="store_true", help="Disable collection progress on stderr"
    )
    collection.add_argument(
        "--hash-index",
        type=Path,
        help="Override the workspace-level SQLite exact-hash index path",
    )
    processing = sub.add_parser(
        "process", help="Add generic quality metadata and optional video frames"
    )
    processing.add_argument("--run", required=True, type=Path)
    processing.add_argument(
        "--video-interval", type=float, default=0, help="Seconds per video frame; 0 disables"
    )
    processing.add_argument("--max-frames-per-video", type=int, default=40)
    processing.add_argument(
        "--processor",
        action="append",
        dest="processors",
        help="Processor registry name; repeat to select multiple stages",
    )
    exp = sub.add_parser("export", help="Export candidate ZIP for project-side audit")
    exp.add_argument("--run", required=True, type=Path)
    exp.add_argument("--output", required=True, type=Path)
    exp.add_argument(
        "--review-filter",
        choices=["accepted", "rejected", "pending"],
        help="Export only assets matching this review decision (e.g. accepted)",
    )
    review = sub.add_parser("review", help="Open a token-protected local review UI")
    review.add_argument("--run", required=True, type=Path)
    review.add_argument("--host", default="127.0.0.1")
    review.add_argument("--port", default=8765, type=int)
    review.add_argument("--token", help="Session token; generated when omitted")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    load_entry_point_plugins()
    if args.command == "validate":
        cfg = load_settings(args.config)
        result = {
            "project": cfg.name,
            "sources": [
                {
                    "id": s.id,
                    "adapter": s.adapter,
                    "enabled": s.enabled,
                    "collectible": s.enabled and s.rights_confirmed,
                }
                for s in cfg.sources
            ],
        }
    elif args.command == "collect":
        result = collect(
            load_settings(args.config),
            args.run,
            resume=args.resume,
            progress=None if args.no_progress else _progress_printer,
            hash_index_path=args.hash_index,
        )
    elif args.command == "process":
        result = process(
            args.run,
            video_interval=args.video_interval,
            max_frames_per_video=args.max_frames_per_video,
            processors=args.processors,
        )
    elif args.command == "export":
        result = export_batch(args.run, args.output, review_filter=args.review_filter)
    elif args.command == "review":
        token = args.token or generate_review_token()
        print(
            f"Review UI: http://{args.host}:{args.port}/\nReview token: {token}",
            file=sys.stderr,
            flush=True,
        )
        serve_review(args.run, token, host=args.host, port=args.port)
        return
    else:
        raise RuntimeError("Unknown command")
    print(json.dumps(result, ensure_ascii=False, indent=2))
