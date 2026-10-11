import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dataset_foundry.downloader import fetch
from dataset_foundry.models import Candidate, Limits
from dataset_foundry.policy import SkipAsset, WebPolicy, checked_url


class PolicyTests(unittest.TestCase):
    def test_disallows_insecure_or_private_urls(self):
        for url in ("http://example.com/data.jpg", "https://127.0.0.1/test", "https://localhost/a"):
            with self.subTest(url=url), self.assertRaises(SkipAsset):
                checked_url(url, resolve_dns=False)

    @patch("dataset_foundry.policy.checked_url", return_value=("example.org", "/private/data.jpg"))
    def test_robots_disallow(self, _checked):
        response = Mock(status_code=200)
        response.iter_content.return_value = [b"User-agent: *\nDisallow: /private/\n"]
        session = Mock()
        session.get.return_value = response
        policy = WebPolicy(session, min_delay=0.5)
        with self.assertRaisesRegex(SkipAsset, "robots.txt disallows"):
            policy.check("https://example.org/private/data.jpg")
        self.assertEqual(session.get.call_count, 1)

    @patch("dataset_foundry.policy.checked_url", return_value=("example.org", "/asset.jpg"))
    def test_robots_unavailable_fails_closed(self, _checked):
        response = Mock(status_code=403)
        session = Mock()
        session.get.return_value = response
        policy = WebPolicy(session, 0.5)
        with self.assertRaisesRegex(SkipAsset, "robots.txt unavailable"):
            policy.check("https://example.org/asset.jpg")
        with self.assertRaisesRegex(SkipAsset, "source blocked"):
            policy.check("https://example.org/another.jpg")
        self.assertEqual(session.get.call_count, 1)

    @patch("dataset_foundry.downloader.checked_url", return_value=("example.org", "/test.bin"))
    def test_remote_403_stops_host_and_skips_file(self, _checked):
        response = Mock(status_code=403)
        session = Mock()
        session.get.return_value = response
        policy = Mock()
        policy.session = session
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "asset.bin"
            candidate = Candidate("remote", "http", "https://example.org/test.bin", "test.bin")
            with self.assertRaisesRegex(SkipAsset, "blocked or limited"):
                fetch(candidate, dest, Limits(), policy)
            self.assertFalse(dest.exists())
            policy.record_block.assert_called_once_with("example.org", 403)

    @patch("dataset_foundry.downloader.checked_url", return_value=("example.org", "/test.bin"))
    def test_remote_redirect_skipped(self, _checked):
        response = Mock(status_code=302, is_redirect=True)
        session = Mock()
        session.get.return_value = response
        policy = Mock()
        policy.session = session
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "asset.bin"
            with self.assertRaisesRegex(SkipAsset, "redirect not followed"):
                fetch(
                    Candidate("remote", "http", "https://example.org/test.bin", "test.bin"),
                    dest,
                    Limits(),
                    policy,
                )
            self.assertFalse(dest.exists())

    @patch("dataset_foundry.downloader.checked_url", return_value=("example.org", "/test.bin"))
    def test_invalid_content_length_is_skipped(self, _checked):
        response = Mock(status_code=200, is_redirect=False)
        response.headers = {"Content-Length": "not-an-integer"}
        session = Mock()
        session.get.return_value = response
        policy = Mock()
        policy.session = session
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "asset.bin"
            with self.assertRaisesRegex(SkipAsset, "invalid Content-Length"):
                fetch(
                    Candidate("remote", "http", "https://example.org/test.bin", "test.bin"),
                    dest,
                    Limits(),
                    policy,
                )
            self.assertFalse(dest.exists())

    @patch("dataset_foundry.downloader.time.sleep")
    @patch("dataset_foundry.downloader.checked_url", return_value=("example.org", "/test.bin"))
    def test_transient_response_retries_then_succeeds(self, _checked, _sleep):
        unavailable = Mock(status_code=503, is_redirect=False, headers={})
        unavailable.iter_content.return_value = []
        success = Mock(status_code=200, is_redirect=False, headers={})
        success.iter_content.return_value = [b"payload"]
        session = Mock()
        session.get.side_effect = [unavailable, success]
        policy = Mock()
        policy.session = session
        candidate = Candidate("remote", "http", "https://example.org/test.bin", "test.bin")
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "asset.bin"
            size, _digest = fetch(
                candidate,
                dest,
                Limits(retry_attempts=2, retry_backoff_seconds=0),
                policy,
            )
            self.assertEqual(size, 7)
            self.assertEqual(dest.read_bytes(), b"payload")
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(candidate.meta["attempts"], 2)

    @patch("dataset_foundry.downloader.checked_url", return_value=("example.org", "/test.jpg"))
    def test_http_200_captcha_challenge_is_blocked_not_saved(self, _checked):
        response = Mock(status_code=200, is_redirect=False, headers={"Server": "cloudflare"})
        response.iter_content.return_value = [
            b"<html><title>Just a moment</title><body>Verify you are human CAPTCHA</body></html>"
        ]
        session = Mock()
        session.get.return_value = response
        policy = Mock()
        policy.session = session
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "asset.jpg"
            with self.assertRaisesRegex(SkipAsset, "captcha_required.*cloudflare"):
                fetch(
                    Candidate("remote", "http", "https://example.org/test.jpg", "test.jpg"),
                    dest,
                    Limits(),
                    policy,
                )
            self.assertFalse(dest.exists())
        policy.record_block.assert_called_once()

    @patch("dataset_foundry.downloader._fetch_browser")
    @patch("dataset_foundry.downloader._fetch_http", side_effect=SkipAsset("captcha_required"))
    def test_auto_render_never_falls_back_after_access_challenge(self, _direct, browser):
        policy = Mock()
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "page.html"
            candidate = Candidate(
                "page",
                "web_page",
                "https://example.org/app",
                "app.html",
                meta={"render_engine": "auto"},
            )
            with self.assertRaisesRegex(SkipAsset, "captcha_required"):
                fetch(candidate, destination, Limits(), policy)
            self.assertFalse(destination.exists())
        browser.assert_not_called()

    @patch("dataset_foundry.downloader._fetch_browser")
    @patch("dataset_foundry.downloader._fetch_http")
    def test_auto_render_uses_browser_for_javascript_shell(self, direct, browser):
        def direct_fetch(_candidate, pending, _limits, _policy, _quota):
            content = b'<html><body><div id="root"></div><script src="app.js"></script></body></html>'
            pending.write_bytes(content)
            return len(content), "0" * 64

        def browser_fetch(_candidate, pending, _limits, _policy, _quota):
            content = b"<html><body>Rendered content</body></html>"
            pending.write_bytes(content)
            return len(content), "1" * 64

        direct.side_effect = direct_fetch
        browser.side_effect = browser_fetch
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "page.html"
            candidate = Candidate(
                "page",
                "web_page",
                "https://example.org/app",
                "app.html",
                meta={"render_engine": "auto"},
            )
            fetch(candidate, destination, Limits(), Mock())
            self.assertIn(b"Rendered content", destination.read_bytes())
        browser.assert_called_once()
