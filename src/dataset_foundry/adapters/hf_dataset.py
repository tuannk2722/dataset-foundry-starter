"""Hugging Face public dataset adapter; prefer official API."""
from collections.abc import Iterator
from pathlib import PurePosixPath

from ..models import Candidate, Limits, Source
from .base import allowed_extension


def discover_hf_dataset(source: Source, limits: Limits) -> Iterator[Candidate]:
    try:
        from huggingface_hub import HfApi
    except ImportError as exc:
        raise RuntimeError("Install optional adapter: pip install -e '.[huggingface]'") from exc
    api = HfApi()
    count = 0
    # repo tree surfaces file sizes, allowing pre-download size limits.
    for entry in api.list_repo_tree(
        source.repo_id, repo_type="dataset", revision=source.revision,
        recursive=True, expand=True, token=False,
    ):
        filename = getattr(entry, "path", "")
        filesize = getattr(entry, "size", None)
        if filesize is None or filesize > limits.max_bytes:
            continue  # folders, unknown-size files and oversized assets
        if not allowed_extension(filename, source.include_extensions):
            continue
        yield Candidate(
            source.id, "huggingface", f"hf://datasets/{source.repo_id}/{filename}",
            PurePosixPath(filename).name, member=filename,
            repo_id=source.repo_id, revision=source.revision,
        )
        count += 1
        if count >= limits.max_assets_per_source:
            return
