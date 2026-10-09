"""Registry of optional generic processors. No task-specific labels."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .manifest import read_jsonl, write_json, write_jsonl


@dataclass(frozen=True)
class ProcessingContext:
    run_dir: Path
    derived_dir: Path
    video_interval: float
    max_frames_per_video: int


class Processor(Protocol):
    """A stage may enrich its record in place and return derived records."""

    def __call__(self, record: dict, context: ProcessingContext) -> Iterable[dict]: ...


PROCESSORS: dict[str, Processor] = {}
PROCESSOR_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
DEFAULT_PROCESSORS = ("image_metadata", "video_frames")
PROTECTED_ORIGINAL_FIELDS = (
    "asset_id",
    "parent_id",
    "source_id",
    "source_adapter",
    "origin",
    "original_name",
    "license",
    "rights_note",
    "collected_at",
    "mime_guess",
    "media_kind",
    "review_status",
    "label",
    "split",
    "status",
    "file_path",
    "sha256",
    "bytes",
)


def register_processor(name: str, processor: Processor, *, replace: bool = False) -> None:
    if not PROCESSOR_NAME_RE.fullmatch(name):
        raise ValueError(f"Invalid processor name: {name!r}")
    if name in PROCESSORS and not replace:
        raise ValueError(f"Processor already registered: {name}")
    if not callable(processor):
        raise TypeError("processor must be callable")
    PROCESSORS[name] = processor


def unregister_processor(name: str) -> None:
    try:
        del PROCESSORS[name]
    except KeyError as exc:
        raise ValueError(f"Processor is not registered: {name}") from exc


def processor_names() -> tuple[str, ...]:
    return tuple(sorted(PROCESSORS))


def _image_metadata(path: Path) -> dict:
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        return {"quality_flags": ["pillow_not_installed"]}
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            image = image.convert("L").resize((8, 8))
            pixels = list(image.getdata())
            avg = sum(pixels) / len(pixels)
            ahash = "".join("1" if pixel > avg else "0" for pixel in pixels)
            return {
                "width": width,
                "height": height,
                "ahash64": f"{int(ahash, 2):016x}",
                "quality_flags": ["small_image"] if min(width, height) < 96 else [],
            }
    except (OSError, ValueError, SyntaxError, EOFError, UnidentifiedImageError):
        return {"quality_flags": ["invalid_image"]}


def _image_metadata_processor(record: dict, context: ProcessingContext) -> Iterable[dict]:
    if record["media_kind"] == "image":
        record.update(_image_metadata(context.run_dir / record["file_path"]))
    return ()


def _video_frames_processor(record: dict, context: ProcessingContext) -> Iterable[dict]:
    if record["media_kind"] != "video" or not context.video_interval:
        return ()
    if shutil.which("ffmpeg") is None:
        record.setdefault("quality_flags", []).append("ffmpeg_not_installed")
        return ()
    path = context.run_dir / record["file_path"]
    frame_dir = context.derived_dir / record["asset_id"]
    frame_dir.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(path),
        "-vf",
        f"fps=1/{context.video_interval}",
        "-frames:v",
        str(context.max_frames_per_video),
        str(frame_dir / "frame_%05d.jpg"),
    ]
    try:
        subprocess.run(command, check=True, timeout=300, capture_output=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        record.setdefault("quality_flags", []).append("frame_extract_failed")
        return ()

    children = []
    for frame in sorted(frame_dir.glob("*.jpg")):
        digest_builder = hashlib.sha256()
        with frame.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest_builder.update(chunk)
        frame_id = hashlib.sha256(f"{record['asset_id']}:{frame.name}".encode()).hexdigest()[:20]
        child = {
            **record,
            "asset_id": frame_id,
            "parent_id": record["asset_id"],
            "status": "derived",
            "origin": record["origin"] + "#" + frame.name,
            "original_name": frame.name,
            "file_path": frame.relative_to(context.run_dir).as_posix(),
            "sha256": digest_builder.hexdigest(),
            "bytes": frame.stat().st_size,
            "media_kind": "image",
            "mime_guess": "image/jpeg",
            "review_status": "unreviewed",
            "label": None,
            "split": None,
        }
        child.update(_image_metadata(frame))
        children.append(child)
    return children


register_processor("image_metadata", _image_metadata_processor)
register_processor("video_frames", _video_frames_processor)


def _file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _validate_processor_output(
    originals: list[dict], records: list[dict], context: ProcessingContext
) -> None:
    for index, original in enumerate(originals):
        processed = records[index]
        for field in PROTECTED_ORIGINAL_FIELDS:
            if processed.get(field) != original.get(field):
                raise ValueError(
                    f"processor changed protected field {field!r} "
                    f"for asset {original.get('asset_id')}"
                )

    original_ids = {record["asset_id"] for record in originals}
    seen_ids: set[str] = set()
    for record in records:
        asset_id = record.get("asset_id")
        if not isinstance(asset_id, str) or asset_id in seen_ids:
            raise ValueError(f"processor emitted invalid or duplicate asset ID: {asset_id!r}")
        seen_ids.add(asset_id)
        if (
            record.get("review_status") != "unreviewed"
            or record.get("label") is not None
            or record.get("split") is not None
        ):
            raise ValueError(f"processor attempted review, label, or split promotion: {asset_id}")
        if asset_id in original_ids:
            continue
        if record.get("status") != "derived" or record.get("parent_id") not in original_ids:
            raise ValueError(f"processor emitted invalid derived record: {asset_id}")
        relative = record.get("file_path")
        if not isinstance(relative, str):
            raise TypeError(f"derived record has no file path: {asset_id}")
        asset = (context.run_dir / relative).resolve()
        if not asset.is_relative_to(context.derived_dir.resolve()) or not asset.is_file():
            raise ValueError(f"derived record has missing or unsafe file path: {relative}")
        size, digest = _file_digest(asset)
        if record.get("bytes") != size or record.get("sha256") != digest:
            raise ValueError(f"derived record checksum mismatch: {asset_id}")


def process(
    run_dir: Path,
    *,
    video_interval: float = 0,
    max_frames_per_video: int = 40,
    processors: Sequence[str] | None = None,
) -> dict:
    if video_interval < 0 or max_frames_per_video < 1:
        raise ValueError("Invalid video processing limits")
    selected = tuple(processors) if processors is not None else DEFAULT_PROCESSORS
    unknown = [name for name in selected if name not in PROCESSORS]
    if unknown:
        raise ValueError(f"Unknown processors: {unknown}")

    run_dir = run_dir.resolve()
    originals = read_jsonl(run_dir / "manifest.jsonl")
    records = [dict(row) for row in originals]
    derived = run_dir / "derived"
    if derived.exists():
        shutil.rmtree(derived)
    context = ProcessingContext(
        run_dir=run_dir,
        derived_dir=derived,
        video_interval=video_interval,
        max_frames_per_video=max_frames_per_video,
    )
    for record_index, original in enumerate(originals):
        if original["status"] != "downloaded":
            continue
        record = records[record_index]
        for processor_name in selected:
            children = [dict(child) for child in PROCESSORS[processor_name](record, context)]
            if any(child.get("parent_id") != record["asset_id"] for child in children):
                raise ValueError(
                    f"processor {processor_name!r} emitted a child for the wrong parent"
                )
            records.extend(children)

    _validate_processor_output(originals, records, context)
    flags = sum(len(record.get("quality_flags", [])) for record in records)
    write_jsonl(run_dir / "processed.jsonl", records)
    summary = {
        "processed_assets": sum(record["status"] == "downloaded" for record in records),
        "derived_frames": sum(record["status"] == "derived" for record in records),
        "quality_flags": flags,
        "processors": list(selected),
        "note": "Image aHash is advisory only; review perceptual duplicates across sources/runs.",
    }
    write_json(run_dir / "processing_summary.json", summary)
    return summary
