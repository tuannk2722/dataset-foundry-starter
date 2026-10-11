"""Conservative HTTPS policy: no anti-bot bypass or private-network fetching."""
import ipaddress
import socket
import threading
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests

USER_AGENT = "DatasetFoundryBot/0.3 (research collector; respects robots.txt)"
BLOCKED_STATUSES = {401, 403, 429, 451}
TRANSIENT_STATUSES = {408, 425, 500, 502, 503, 504}


def detect_access_challenge(status: int, headers, prefix: bytes) -> tuple[str, str] | None:
    """Classify access challenges without attempting to bypass them.

    Vendor attribution is deliberately heuristic. It is audit context, not proof that an
    IP address was blocked.
    """
    header_text = " ".join(f"{key}:{value}" for key, value in headers.items()).lower()
    sample = prefix[:64 * 1024].decode("utf-8", "replace").lower()
    html_like = "<html" in sample or "<!doctype html" in sample
    vendor = "unknown"
    markers = (
        ("cloudflare", ("cf-ray", "cloudflare", "cf-chl-")),
        ("datadome", ("datadome", "captcha-delivery.com")),
        ("imperva", ("incapsula", "imperva", "_incap_")),
        ("akamai", ("akamai", "reference #", "akamai bot manager")),
    )
    combined = header_text + " " + sample
    for name, values in markers:
        if any(value in combined for value in values):
            vendor = name
            break
    captcha = html_like and any(
        marker in sample
        for marker in ("captcha", "verify you are human", "human verification")
    )
    challenge = html_like and any(
        marker in sample
        for marker in (
            "checking your browser",
            "just a moment",
            "access denied",
            "bot challenge",
            "unusual traffic",
        )
    )
    if captcha:
        return "captcha_required", vendor
    if status in BLOCKED_STATUSES or challenge:
        category = "rate_limited" if status == 429 else "bot_challenge"
        return category, vendor
    return None


class SkipAsset(Exception):
    """Recorded as skipped (not an attempted bypass)."""


def checked_url(url: str, *, resolve_dns: bool = True) -> tuple[str, str]:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise SkipAsset("URL must be a credential-free HTTPS URL")
    host = parts.hostname.lower()
    try:
        port = parts.port
    except ValueError as exc:
        raise SkipAsset("invalid HTTPS port") from exc
    if port not in (None, 443):
        raise SkipAsset("only standard HTTPS port 443 is supported")
    if host == "localhost" or host.endswith((".local", ".internal")):
        raise SkipAsset("local/private domain disallowed")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise SkipAsset("non-public IP disallowed")
    if resolve_dns:
        try:
            infos = socket.getaddrinfo(host, parts.port or 443, type=socket.SOCK_STREAM)
            addresses = [ipaddress.ip_address(i[4][0]) for i in infos]
        except (OSError, ValueError) as exc:
            raise SkipAsset(f"DNS lookup failed: {exc}") from exc
        if not addresses or any(not ip.is_global for ip in addresses):
            raise SkipAsset("host resolves to non-public IP")
    return host, parts.path or "/"


class WebPolicy:
    def __init__(self, session: requests.Session, min_delay: float):
        self.session = session
        self._owner_thread = threading.get_ident()
        self.min_delay = min_delay
        self.last_request: dict[str, float] = {}
        self.robots: dict[str, RobotFileParser] = {}
        self.blocked_hosts: set[str] = set()
        self.block_reasons: dict[str, str] = {}
        self.delays: dict[str, float] = {}
        self._state_lock = threading.RLock()
        self._robots_lock = threading.Lock()

    def request_session(self) -> tuple[requests.Session, bool]:
        """Return an isolated worker session; the caller closes owned sessions."""
        if threading.get_ident() == self._owner_thread:
            return self.session, False
        session = requests.Session()
        session.trust_env = False
        return session, True

    def pace(self, host: str) -> None:
        with self._state_lock:
            delay = max(self.min_delay, self.delays.get(host, 0.0))
            now = time.monotonic()
            until = max(now, self.last_request.get(host, 0) + delay)
            self.last_request[host] = until
        if until > now:
            time.sleep(until - now)

    def check(self, url: str) -> None:
        host, _ = checked_url(url)
        with self._state_lock:
            if host in self.blocked_hosts:
                reason = self.block_reasons.get(host, "access-denied or throttling response")
                raise SkipAsset(f"source blocked after {reason}")
        with self._robots_lock:
            with self._state_lock:
                cached = host in self.robots
            if not cached:
                robots_url = f"https://{host}/robots.txt"
                self.pace(host)
                try:
                    response = self.session.get(
                        robots_url,
                        timeout=(8, 20),
                        stream=True,
                        allow_redirects=False,
                        headers={"User-Agent": USER_AGENT},
                    )
                    try:
                        if response.status_code == 404:
                            parser = RobotFileParser()
                            parser.parse([])
                        elif response.status_code == 200:
                            chunks = []
                            total = 0
                            for chunk in response.iter_content(8192):
                                total += len(chunk)
                                if total > 256 * 1024:
                                    self.record_block(
                                        host, response.status_code, "robots.txt too large"
                                    )
                                    raise SkipAsset("robots.txt too large")
                                chunks.append(chunk)
                            parser = RobotFileParser()
                            parser.parse(
                                b"".join(chunks).decode("utf-8", "replace").splitlines()
                            )
                        else:
                            self.record_block(
                                host, response.status_code, "robots.txt unavailable"
                            )
                            raise SkipAsset(
                                f"robots.txt unavailable, status {response.status_code}"
                            )
                    finally:
                        response.close()
                except requests.RequestException as exc:
                    self.record_block(host, 0, "robots.txt inaccessible")
                    raise SkipAsset(f"robots.txt inaccessible: {exc}") from exc
                with self._state_lock:
                    self.robots[host] = parser
                crawl_delay = parser.crawl_delay(USER_AGENT)
                if crawl_delay is not None:
                    with self._state_lock:
                        self.delays[host] = max(self.min_delay, float(crawl_delay))
        with self._state_lock:
            parser = self.robots[host]
        if not parser.can_fetch(USER_AGENT, url):
            raise SkipAsset("robots.txt disallows this URL")

    def record_block(self, host: str, status: int, reason: str = "") -> None:
        if status in BLOCKED_STATUSES or reason:
            with self._state_lock:
                self.blocked_hosts.add(host)
                self.block_reasons[host] = reason or f"HTTP {status}"
