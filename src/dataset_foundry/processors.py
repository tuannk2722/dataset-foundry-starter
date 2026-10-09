"""Optional generic processors. No task-specific bounding boxes or labels."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from .manifest import read_jsonl, write_json, write_jsonl


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
            ahash = "".join("1" if p > avg else "0" for p in pixels)
            return {
                "width": width, "height": height,
                "ahash64": f"{int(ahash, 2):016x}",
                "quality_flags": ["small_image"] if min(width, height) < 96 else [],
            }
    except (OSError, ValueError, SyntaxError, EOFError, UnidentifiedImageError):
        return {"quality_flags": ["invalid_image"]}


def process(run_dir: Path, *, video_interval: float = 0, max_frames_per_video: int = 40) -> dict:
    if video_interval < 0 or max_frames_per_video < 1:
        raise ValueError("Invalid video processing limits")
    run_dir = run_dir.resolve()
    originals = read_jsonl(run_dir / "manifest.jsonl")
    records = [dict(row) for row in originals]
    derived = run_dir / "derived"
    if derived.exists():
        shutil.rmtree(derived)
    flags = 0
    for record_index, record in enumerate(originals):
        if record["status"] != "downloaded":
            continue
        path = run_dir / record["file_path"]
        if record["media_kind"] == "image":
            extras = _image_metadata(path)
            records[record_index].update(extras)
            flags += len(extras.get("quality_flags", []))
        if record["media_kind"] == "video" and video_interval:
            if shutil.which("ffmpeg") is None:
                records[record_index].setdefault("quality_flags", []).append("ffmpeg_not_installed")
                flags += 1
                continue
            frame_dir = derived / record["asset_id"]
            frame_dir.mkdir(parents=True, exist_ok=True)
            command = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-i", str(path), "-vf", f"fps=1/{video_interval}",
                "-frames:v", str(max_frames_per_video), str(frame_dir / "frame_%05d.jpg"),
            ]
            try:
                subprocess.run(command, check=True, timeout=300, capture_output=True)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                records[record_index].setdefault("quality_flags", []).append("frame_extract_failed")
                flags += 1
                continue
            for frame in sorted(frame_dir.glob("*.jpg")):
                digest_builder = hashlib.sha256()
                with frame.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(65536), b""):
                        digest_builder.update(chunk)
                digest = digest_builder.hexdigest()
                frame_id = hashlib.sha256(f"{record['asset_id']}:{frame.name}".encode()).hexdigest()[:20]
                child = {**record,
                    "asset_id": frame_id, "parent_id": record["asset_id"],
                    "status": "derived", "origin": record["origin"] + "#" + frame.name,
                    "file_path": frame.relative_to(run_dir).as_posix(),
                    "sha256": digest, "bytes": frame.stat().st_size, "media_kind": "image",
                    "mime_guess": "image/jpeg", "review_status": "unreviewed",
                    "label": None, "split": None,
                }
                child.update(_image_metadata(frame))
                records.append(child)
                flags += len(child.get("quality_flags", []))
    write_jsonl(run_dir / "processed.jsonl", records)
    summary = {
        "processed_assets": sum(r["status"] == "downloaded" for r in records),
        "derived_frames": sum(r["status"] == "derived" for r in records),
        "quality_flags": flags,
        "note": "Image aHash is advisory only; review perceptual duplicates across sources/runs.",
    }
    write_json(run_dir / "processing_summary.json", summary)
    return summary
