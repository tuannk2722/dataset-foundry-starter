"""Bounded streaming copy, local zip ingest, and official dataset API ingest."""
import hashlib
import shutil
import tempfile
from pathlib import Path
from zipfile import ZipFile

import requests

from .models import Candidate, Limits
from .policy import BLOCKED_STATUSES, USER_AGENT, SkipAsset, WebPolicy, checked_url


class AssetTooLarge(SkipAsset):
    pass


def _copy_bounded(stream, output: Path, max_bytes: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with output.open("wb") as target:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise AssetTooLarge(f"asset exceeds maximum {max_bytes} bytes")
            target.write(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def fetch(candidate: Candidate, destination: Path, limits: Limits, policy: WebPolicy | None) -> tuple[int, str]:
    """Write destination only on success; never store partial output."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as folder:
        pending = Path(folder) / "pending"
        if candidate.kind == "local":
            assert candidate.path is not None
            with candidate.path.open("rb") as src:
                result = _copy_bounded(src, pending, limits.max_bytes)
        elif candidate.kind == "zip_member":
            assert candidate.path is not None
            with ZipFile(candidate.path) as zf, zf.open(candidate.member) as src:
                result = _copy_bounded(src, pending, limits.max_bytes)
        elif candidate.kind == "http":
            if policy is None:
                raise RuntimeError("HTTP candidate requires WebPolicy")
            policy.check(candidate.origin)
            host, _ = checked_url(candidate.origin)
            policy.pace(host)
            try:
                response = policy.session.get(
                    candidate.origin, stream=True, timeout=(8, 30), allow_redirects=False,
                    headers={"User-Agent": USER_AGENT},
                )
                try:
                    if response.status_code in BLOCKED_STATUSES:
                        policy.record_block(host, response.status_code)
                        raise SkipAsset(f"remote source blocked or limited: HTTP {response.status_code}")
                    if response.is_redirect:
                        raise SkipAsset("redirect not followed; add approved final HTTPS URL explicitly")
                    if response.status_code != 200:
                        raise SkipAsset(f"download failed: HTTP {response.status_code}")
                    content_length = response.headers.get("Content-Length")
                    if content_length and int(content_length) > limits.max_bytes:
                        raise AssetTooLarge("Content-Length exceeds configured limit")
                    class ChunkReader:
                        def __init__(self, chunks):
                            self.iterator = iter(chunks)
                        def read(self, _size):
                            for chunk in self.iterator:
                                if chunk:
                                    return chunk
                            return b""
                    result = _copy_bounded(ChunkReader(response.iter_content(64 * 1024)), pending, limits.max_bytes)
                finally:
                    response.close()
            except requests.RequestException as exc:
                raise SkipAsset(f"HTTP request failed: {exc}") from exc
        elif candidate.kind == "huggingface":
            try:
                from huggingface_hub import hf_hub_download
            except ImportError as exc:
                raise RuntimeError("Install pip install -e '.[huggingface]'") from exc
            # Official API only. Gated/private repos are not bypassed. No token by default.
            cached_path = hf_hub_download(
                candidate.repo_id, filename=candidate.member, revision=candidate.revision,
                repo_type="dataset", token=False, cache_dir=str(destination.parents[2] / "hf-cache"),
            )
            with Path(cached_path).open("rb") as src:
                result = _copy_bounded(src, pending, limits.max_bytes)
        else:
            raise ValueError(f"Unknown candidate kind {candidate.kind}")
        shutil.move(str(pending), str(destination))
        return result
