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
