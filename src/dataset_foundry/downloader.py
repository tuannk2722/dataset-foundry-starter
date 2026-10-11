"""Bounded streaming copy, local zip ingest, and official dataset API ingest."""

import hashlib
import io
import shutil
import tempfile
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZipFile

import requests

from .models import Candidate, Limits
from .policy import (
    BLOCKED_STATUSES,
    TRANSIENT_STATUSES,
    USER_AGENT,
    SkipAsset,
    WebPolicy,
    checked_url,
    detect_access_challenge,
)


class AssetTooLarge(SkipAsset):
    pass


class SourceQuotaExceeded(SkipAsset):
    pass


class SourceByteQuota:
    """Thread-safe collected-byte budget shared by workers for one source."""

    def __init__(self, limit: int, used: int = 0):
        if limit < 0 or not 0 <= used <= limit:
            raise ValueError("invalid source byte quota")
        self.limit = limit
        self.used = used
        self._lock = threading.Lock()

    def reserve(self, amount: int) -> None:
        with self._lock:
            if amount < 0 or self.used + amount > self.limit:
                raise SourceQuotaExceeded(f"source exceeds maximum bytes {self.limit}")
            self.used += amount

    def release(self, amount: int) -> None:
        with self._lock:
            if amount < 0 or amount > self.used:
                raise ValueError("invalid source quota release")
            self.used -= amount


def _copy_bounded(
    stream, output: Path, max_bytes: int, quota: SourceByteQuota | None = None
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    reserved = 0
    try:
        with output.open("wb") as target:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise AssetTooLarge(f"asset exceeds maximum {max_bytes} bytes")
                if quota is not None:
                    quota.reserve(len(chunk))
                    reserved += len(chunk)
                target.write(chunk)
                digest.update(chunk)
    except BaseException:
        if quota is not None and reserved:
            quota.release(reserved)
        raise
    return size, digest.hexdigest()


def _copy_known_bounded(
    stream,
    output: Path,
    max_bytes: int,
    declared_size: int,
    quota: SourceByteQuota | None,
) -> tuple[int, str]:
    """Atomically reserve known local/archive sizes before concurrent copying."""
    if declared_size > max_bytes:
        raise AssetTooLarge(f"asset exceeds maximum {max_bytes} bytes")
    if quota is not None:
        quota.reserve(declared_size)
    try:
        result = _copy_bounded(stream, output, max_bytes)
        if result[0] != declared_size:
            raise SkipAsset("source size changed while it was being collected")
        return result
    except BaseException:
        if quota is not None:
            quota.release(declared_size)
        raise


class _ChunkReader:
    def __init__(self, chunks: Iterable[bytes], prefix: bytes = b""):
        self.iterator = iter(chunks)
        self.prefix = prefix

    def read(self, _size):
        if self.prefix:
            value, self.prefix = self.prefix, b""
            return value
        for chunk in self.iterator:
            if chunk:
                return chunk
        return b""


def _first_chunk(chunks: Iterable[bytes]) -> tuple[bytes, Iterable[bytes]]:
    iterator = iter(chunks)
    for chunk in iterator:
        if chunk:
            return chunk, iterator
    return b"", iterator


def _response_headers(response) -> Mapping[str, str]:
    headers = getattr(response, "headers", {})
    return headers if isinstance(headers, Mapping) else {}


def _policy_request_session(policy: WebPolicy):
    if isinstance(policy, WebPolicy):
        return policy.request_session()
    return policy.session, False


def _retry_delay(headers: Mapping[str, str], attempt: int, limits: Limits) -> float:
    retry_after = headers.get("Retry-After")
    if retry_after is not None:
        try:
            return min(60.0, max(0.0, float(retry_after)))
        except (TypeError, ValueError):
            pass
    return min(60.0, limits.retry_backoff_seconds * (2 ** (attempt - 1)))


def _same_approved_url(left: str, right: str) -> bool:
    def normalized(url: str) -> tuple[str, str, int, str, str]:
        parts = urlsplit(url)
        return (
            parts.scheme.lower(),
            (parts.hostname or "").lower(),
            parts.port or 443,
            parts.path or "/",
            parts.query,
        )

    return normalized(left) == normalized(right)


def _fetch_http(
    candidate: Candidate,
    pending: Path,
    limits: Limits,
    policy: WebPolicy,
    quota: SourceByteQuota | None,
) -> tuple[int, str]:
    policy.check(candidate.origin)
    host, _ = checked_url(candidate.origin)
    last_error = "unknown transient failure"
    for attempt in range(1, limits.retry_attempts + 1):
        candidate.meta["attempts"] = attempt
        policy.pace(host)
        policy.check(candidate.origin)
        headers: Mapping[str, str] = {}
        request_session, owns_session = _policy_request_session(policy)
        try:
            response = request_session.get(
                candidate.origin,
                stream=True,
                timeout=(8, 30),
                allow_redirects=False,
                headers={"User-Agent": USER_AGENT},
            )
            try:
                headers = _response_headers(response)
                if response.status_code in BLOCKED_STATUSES:
                    prefix = b""
                    if str(headers.get("Content-Type", "")).lower().startswith("text/html"):
                        prefix, _remaining = _first_chunk(response.iter_content(64 * 1024))
                    challenge = detect_access_challenge(
                        response.status_code, headers, prefix
                    )
                    if challenge is not None and challenge[1] != "unknown":
                        category, vendor = challenge
                        policy.record_block(
                            host, response.status_code, f"{category} ({vendor})"
                        )
                    else:
                        policy.record_block(host, response.status_code)
                    raise SkipAsset(
                        f"remote source blocked or limited: HTTP {response.status_code}"
                    )
                if response.is_redirect:
                    raise SkipAsset(
                        "redirect not followed; add approved final HTTPS URL explicitly"
                    )
                if response.status_code in TRANSIENT_STATUSES:
                    last_error = f"transient HTTP {response.status_code}"
                elif response.status_code != 200:
                    raise SkipAsset(f"download failed: HTTP {response.status_code}")
                else:
                    content_type = headers.get("Content-Type")
                    if content_type:
                        candidate.meta["content_type"] = content_type
                    content_length = headers.get("Content-Length")
                    if content_length:
                        try:
                            declared_size = int(content_length)
                        except ValueError as exc:
                            raise SkipAsset("invalid Content-Length response header") from exc
                        if declared_size < 0:
                            raise SkipAsset("invalid Content-Length response header")
                        if declared_size > limits.max_bytes:
                            raise AssetTooLarge("Content-Length exceeds configured limit")
                    first, remaining = _first_chunk(response.iter_content(64 * 1024))
                    challenge = detect_access_challenge(response.status_code, headers, first)
                    if challenge is not None:
                        category, vendor = challenge
                        reason = f"{category} ({vendor})"
                        policy.record_block(host, response.status_code, reason)
                        raise SkipAsset(
                            f"remote access blocked: {category}; vendor={vendor}; "
                            f"HTTP {response.status_code}"
                        )
                    result = _copy_bounded(
                        _ChunkReader(remaining, first), pending, limits.max_bytes, quota
                    )
                    return result
            finally:
                response.close()
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        finally:
            if owns_session:
                request_session.close()
        if attempt < limits.retry_attempts:
            time.sleep(_retry_delay(headers, attempt, limits))
    raise SkipAsset(
        f"HTTP request failed after {limits.retry_attempts} attempts: {last_error}"
    )


def _needs_browser(path: Path) -> bool:
    """Detect a small HTML JavaScript shell; never treat an access challenge as a shell."""
    prefix = path.read_bytes()[:64 * 1024]
    lowered = prefix.lower()
    if b"<html" not in lowered and b"<!doctype html" not in lowered:
        return False
    visible = lowered
    for start, end in ((b"<script", b"</script>"), (b"<style", b"</style>")):
        while start in visible and end in visible:
            before, _, rest = visible.partition(start)
            _, _, after = rest.partition(end)
            visible = before + after
    markers = (b'id="root"', b'id="app"', b"__next_data__", b"enable javascript")
    return any(marker in lowered for marker in markers) and len(visible.strip()) < 2048


def _fetch_browser(
    candidate: Candidate,
    pending: Path,
    limits: Limits,
    policy: WebPolicy,
    quota: SourceByteQuota | None,
) -> tuple[int, str]:
    """Render one explicitly approved page; no stealth, login, CAPTCHA, or proxy support."""
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Install pip install -e '.[browser]' and playwright chromium") from exc

    policy.check(candidate.origin)
    host, _ = checked_url(candidate.origin)
    policy.pace(host)
    policy.check(candidate.origin)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    accept_downloads=False,
                    service_workers="block",
                )
                page = context.new_page()

                def route_request(route) -> None:
                    request = route.request
                    try:
                        request_host, _ = checked_url(request.url)
                        if request_host != host:
                            raise SkipAsset("cross-origin browser subresource refused")
                        if request.is_navigation_request() and not _same_approved_url(
                            request.url, candidate.origin
                        ):
                            raise SkipAsset("browser redirect refused")
                    except SkipAsset:
                        route.abort()
                    else:
                        route.continue_()

                page.route("**/*", route_request)
                response = page.goto(candidate.origin, wait_until="networkidle", timeout=30_000)
                status = response.status if response is not None else 200
                content = page.content().encode("utf-8")
                challenge = detect_access_challenge(status, {}, content[:64 * 1024])
                if challenge is not None:
                    category, vendor = challenge
                    policy.record_block(host, status, f"{category} ({vendor})")
                    raise SkipAsset(
                        f"browser access blocked: {category}; vendor={vendor}; HTTP {status}"
                    )
                if status != 200:
                    raise SkipAsset(f"browser render failed: HTTP {status}")
                if not _same_approved_url(page.url, candidate.origin):
                    raise SkipAsset("browser redirect refused")
                candidate.meta["attempts"] = 1
                return _copy_bounded(io.BytesIO(content), pending, limits.max_bytes, quota)
            finally:
                browser.close()
    except PlaywrightError as exc:
        raise SkipAsset(f"browser render failed: {exc}") from exc


def fetch(
    candidate: Candidate,
    destination: Path,
    limits: Limits,
    policy: WebPolicy | None,
    quota: SourceByteQuota | None = None,
) -> tuple[int, str]:
    """Write destination only on success; never store partial output."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as folder:
        pending = Path(folder) / "pending"
        if candidate.kind == "local":
            assert candidate.path is not None
            with candidate.path.open("rb") as src:
                result = _copy_known_bounded(
                    src, pending, limits.max_bytes, candidate.path.stat().st_size, quota
                )
        elif candidate.kind == "zip_member":
            assert candidate.path is not None
            with ZipFile(candidate.path) as zf, zf.open(candidate.member) as src:
                result = _copy_known_bounded(
                    src,
                    pending,
                    limits.max_bytes,
                    zf.getinfo(candidate.member).file_size,
                    quota,
                )
        elif candidate.kind == "http":
            if policy is None:
                raise RuntimeError("HTTP candidate requires WebPolicy")
            result = _fetch_http(candidate, pending, limits, policy, quota)
        elif candidate.kind == "web_page":
            if policy is None:
                raise RuntimeError("web page candidate requires WebPolicy")
            engine = str(candidate.meta.get("render_engine", "direct"))
            if engine == "browser":
                result = _fetch_browser(candidate, pending, limits, policy, quota)
            elif engine in {"direct", "auto"}:
                result = _fetch_http(candidate, pending, limits, policy, quota)
                if engine == "auto" and _needs_browser(pending):
                    if quota is not None:
                        quota.release(result[0])
                    pending.unlink(missing_ok=True)
                    result = _fetch_browser(candidate, pending, limits, policy, quota)
            else:
                raise ValueError(f"Unknown render engine {engine!r}")
        elif candidate.kind == "huggingface":
            try:
                from huggingface_hub import hf_hub_download
            except ImportError as exc:
                raise RuntimeError("Install pip install -e '.[huggingface]'") from exc
            # Official API only. Gated/private repos are not bypassed. No token by default.
            cached_path = hf_hub_download(
                candidate.repo_id,
                filename=candidate.member,
                revision=candidate.revision,
                repo_type="dataset",
                token=False,
                cache_dir=str(destination.parents[2] / "hf-cache"),
            )
            cached = Path(cached_path)
            with cached.open("rb") as src:
                result = _copy_known_bounded(
                    src, pending, limits.max_bytes, cached.stat().st_size, quota
                )
        else:
            raise ValueError(f"Unknown candidate kind {candidate.kind}")
        shutil.move(str(pending), str(destination))
        return result
