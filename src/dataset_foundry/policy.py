"""Conservative HTTPS policy: no anti-bot bypass or private-network fetching."""
import ipaddress
import socket
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests

USER_AGENT = "DatasetFoundryBot/0.1 (research collector; respects robots.txt)"
BLOCKED_STATUSES = {401, 403, 429, 451}


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
        self.min_delay = min_delay
        self.last_request: dict[str, float] = {}
        self.robots: dict[str, RobotFileParser] = {}
        self.blocked_hosts: set[str] = set()
        self.delays: dict[str, float] = {}

    def pace(self, host: str) -> None:
        delay = max(self.min_delay, self.delays.get(host, 0.0))
        until = self.last_request.get(host, 0) + delay
        if until > time.monotonic():
            time.sleep(until - time.monotonic())
        self.last_request[host] = time.monotonic()

    def check(self, url: str) -> None:
        host, _ = checked_url(url)
        if host in self.blocked_hosts:
            raise SkipAsset("source blocked after access-denied or throttling response")
        if host not in self.robots:
            robots_url = f"https://{host}/robots.txt"
            self.pace(host)
            try:
                response = self.session.get(
                    robots_url, timeout=(8, 20), stream=True, allow_redirects=False,
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
                                self.blocked_hosts.add(host)
                                raise SkipAsset("robots.txt too large")
                            chunks.append(chunk)
                        parser = RobotFileParser()
                        parser.parse(b"".join(chunks).decode("utf-8", "replace").splitlines())
                    else:
                        self.blocked_hosts.add(host)
                        raise SkipAsset(f"robots.txt unavailable, status {response.status_code}")
                finally:
                    response.close()
            except requests.RequestException as exc:
                self.blocked_hosts.add(host)
                raise SkipAsset(f"robots.txt inaccessible: {exc}") from exc
            self.robots[host] = parser
            crawl_delay = parser.crawl_delay(USER_AGENT)
            if crawl_delay is not None:
                self.delays[host] = max(self.min_delay, float(crawl_delay))
        if not self.robots[host].can_fetch(USER_AGENT, url):
            raise SkipAsset("robots.txt disallows this URL")

    def record_block(self, host: str, status: int) -> None:
        if status in BLOCKED_STATUSES:
            self.blocked_hosts.add(host)
