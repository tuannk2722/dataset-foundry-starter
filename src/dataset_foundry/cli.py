"""CLI workflows: validate -> collect -> process -> export."""

import argparse
import json
from pathlib import Path

from .config import load_settings
from .exporter import export_batch
from .pipeline import collect
from .processors import process


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
    processing = sub.add_parser(
        "process", help="Add generic quality metadata and optional video frames"
    )
    processing.add_argument("--run", required=True, type=Path)
    processing.add_argument(
        "--video-interval", type=float, default=0, help="Seconds per video frame; 0 disables"
    )
    processing.add_argument("--max-frames-per-video", type=int, default=40)
    exp = sub.add_parser("export", help="Export candidate ZIP for project-side audit")
    exp.add_argument("--run", required=True, type=Path)
    exp.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
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
        result = collect(load_settings(args.config), args.run, resume=args.resume)
    elif args.command == "process":
        result = process(
            args.run,
            video_interval=args.video_interval,
            max_frames_per_video=args.max_frames_per_video,
        )
    elif args.command == "export":
        result = export_batch(args.run, args.output)
    else:
        raise RuntimeError("Unknown command")
    print(json.dumps(result, ensure_ascii=False, indent=2))
